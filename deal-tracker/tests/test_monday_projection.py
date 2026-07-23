from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from deal_tracker.integrations.monday import (
    BOARD_COLUMNS,
    BOARD_NAMES,
    DealsOnlyProjector,
    _creation_name,
    projection_rows,
)
from deal_tracker.platform.enums import OfferStatus
from deal_tracker.platform.models import (
    Base,
    Contact,
    Domain,
    MigrationSourceRecord,
    MondayMapping,
    Offer,
    PublisherDomain,
    PublisherEntity,
    Reply,
    utcnow,
)


class FakeMonday:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.item_locations: dict[str, str] = {}
        self.item_names: dict[str, str] = {}
        self._boards = []
        for index, (key, name) in enumerate(BOARD_NAMES.items(), 1):
            self._boards.append(
                {
                    "id": f"board-{index}",
                    "name": name,
                    "board_kind": "share",
                    "workspace": {"id": "test-workspace"},
                    "subscribers": [{"id": "owner", "is_guest": False, "enabled": True}],
                    "columns": [
                        {"id": f"{key}-{column_index}", "title": title, "type": kind}
                        for column_index, (title, kind) in enumerate(BOARD_COLUMNS[key], 1)
                    ],
                }
            )

    def boards(self, board_ids=None):
        wanted = {str(board_id) for board_id in (board_ids or [])}
        return [board for board in self._boards if str(board["id"]) in wanted]

    def create_item(self, board_id, name, values):
        self.created.append((board_id, name, values))
        item_id = f"item-{len(self.created)}"
        self.item_locations[item_id] = board_id
        self.item_names[item_id] = name
        return item_id

    def update_item(self, board_id, item_id, values):
        self.updated.append((board_id, item_id, values))

    def item_board_ids(self, item_ids):
        item_ids = [str(item_id) for item_id in item_ids]
        locations = {
            item_id: "board-1"
            for item_id in item_ids
            if item_id == "legacy-item-123"
        }
        locations.update(
            {
                item_id: self.item_locations[item_id]
                for item_id in item_ids
                if item_id in self.item_locations
            }
        )
        return locations

    def item_ids_by_name(self, board_id, name):
        return [
            item_id
            for item_id, item_name in self.item_names.items()
            if item_name == name and self.item_locations.get(item_id) == board_id
        ]


@pytest.fixture(autouse=True)
def pinned_monday_boards(monkeypatch):
    monkeypatch.setenv("MONDAY_WORKSPACE_ID", "test-workspace")
    for index, key in enumerate(BOARD_NAMES, 1):
        monkeypatch.setenv(f"MONDAY_{key.upper()}_BOARD_ID", f"board-{index}")


def _status_column(title: str, *labels: str) -> dict:
    return {
        "id": title,
        "title": title,
        "type": "status",
        "settings_str": json.dumps(
            {"labels": {str(index): label for index, label in enumerate(labels)}}
        ),
    }


class LegacySchemaMonday(FakeMonday):
    """Safe facsimile of the live pre-cutover board schemas."""

    def __init__(self, *, include_guest_post: bool = True) -> None:
        super().__init__()
        placement_labels = [
            "advertorial",
            "niche_edit",
            "sponsored_post",
            "media_package",
            "other",
        ]
        if include_guest_post:
            placement_labels.append("guest_post")
        columns = {
            "inventory": [
                {"id": "Root Domain", "title": "Root Domain", "type": "text"},
                {"id": "Contact Email", "title": "Contact Email", "type": "email"},
                _status_column("Deal Status", "confirmed", "needs_review"),
                _status_column("Placement Type", *placement_labels),
                {"id": "Publisher Cost", "title": "Publisher Cost", "type": "numbers"},
                _status_column("Publisher Currency", "AUD", "USD", "GBP"),
                {"id": "Reseller Price", "title": "Reseller Price", "type": "numbers"},
                _status_column("Reseller Currency", "AUD"),
                {"id": "Link Insert Cost", "title": "Link Insert Cost", "type": "numbers"},
                _status_column("Link Insert Currency", "AUD", "USD", "GBP"),
                {"id": "Link Insert Reseller", "title": "Link Insert Reseller", "type": "numbers"},
                _status_column("Review Status", "approved", "needs_review"),
                {"id": "Local Deal ID", "title": "Local Deal ID", "type": "numbers"},
                {"id": "Last Seen", "title": "Last Seen", "type": "date"},
            ],
            "entities": [
                {"id": "Entity Key", "title": "Entity Key", "type": "text"},
                {"id": "Primary Email", "title": "Primary Email", "type": "email"},
                {"id": "Primary Domain", "title": "Primary Domain", "type": "text"},
                {"id": "Domain Count", "title": "Domain Count", "type": "numbers"},
                {"id": "Local Entity ID", "title": "Local Entity ID", "type": "numbers"},
                {"id": "Last Synced", "title": "Last Synced", "type": "date"},
            ],
            "replies": [
                {"id": "Email ID", "title": "Email ID", "type": "text"},
                {"id": "Campaign ID", "title": "Campaign ID", "type": "text"},
                {"id": "Lead Email", "title": "Lead Email", "type": "email"},
                {"id": "Thread ID", "title": "Thread ID", "type": "text"},
                {"id": "Subject", "title": "Subject", "type": "text"},
                _status_column("Classification", "deal_terms", "rejected", "auto_reply", "ignore"),
                _status_column("Review Action", "Working on it", "Deal Created/Updated", "Rejected", "Ignored"),
                {"id": "Received At", "title": "Received At", "type": "date"},
                {"id": "Local Reply ID", "title": "Local Reply ID", "type": "numbers"},
                {"id": "Body Text", "title": "Body Text", "type": "long_text"},
            ],
            "sync_log": [
                _status_column("Trigger Type", "startup", "cli", "manual"),
                _status_column("Sync Status", "success", "failed", "running"),
                {"id": "Started At", "title": "Started At", "type": "date"},
                {"id": "Finished At", "title": "Finished At", "type": "date"},
                {"id": "Summary JSON", "title": "Summary JSON", "type": "long_text"},
            ],
        }
        for board in self._boards:
            key = next(key for key, name in BOARD_NAMES.items() if name == board["name"])
            board["columns"] = columns[key]


def _seed_approved_projection(session) -> tuple[Offer, PublisherEntity]:
    domain = Domain(normalized_domain="publisher.com", first_input_value="publisher.com", tld="com")
    contact = Contact(
        normalized_email="editor@publisher.com",
        original_email="editor@publisher.com",
        email_domain="publisher.com",
    )
    session.add_all([domain, contact])
    session.flush()
    reply = Reply(
        external_email_id="email-legacy-schema",
        external_thread_id="thread-legacy-schema",
        domain_id=domain.id,
        contact_id=contact.id,
        from_address=contact.normalized_email,
        subject="Rates",
        body_text="AUD 200",
        received_at=utcnow(),
    )
    publisher = PublisherEntity(
        canonical_name="Publisher",
        normalized_key="email:editor@publisher.com",
        primary_contact_id=contact.id,
    )
    session.add_all([reply, publisher])
    session.flush()
    session.add(PublisherDomain(publisher_id=publisher.id, domain_id=domain.id))
    offer = Offer(
        reply_id=reply.id,
        domain_id=domain.id,
        publisher_id=publisher.id,
        status=OfferStatus.APPROVED,
        placement_type="guest_post",
        original_amount=Decimal("200"),
        original_currency="AUD",
        reseller_price_aud=Decimal("300"),
    )
    session.add(offer)
    session.commit()
    return offer, publisher


def test_projection_reuses_legacy_mapping_and_writes_sync_log() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    fake = FakeMonday()

    with Session() as session:
        domain = Domain(
            normalized_domain="publisher.com",
            first_input_value="publisher.com",
            tld="com",
        )
        contact = Contact(
            normalized_email="editor@publisher.com",
            original_email="editor@publisher.com",
            email_domain="publisher.com",
        )
        session.add_all([domain, contact])
        session.flush()
        reply = Reply(
            external_email_id="email-1",
            external_thread_id="thread-1",
            domain_id=domain.id,
            contact_id=contact.id,
            from_address=contact.normalized_email,
            subject="Rates",
            body_text="AUD 200",
            received_at=utcnow(),
        )
        publisher = PublisherEntity(
            canonical_name="Publisher",
            normalized_key="email:editor@publisher.com",
            primary_contact_id=contact.id,
        )
        session.add_all([reply, publisher])
        session.flush()
        session.add(PublisherDomain(publisher_id=publisher.id, domain_id=domain.id))
        offer = Offer(
            reply_id=reply.id,
            domain_id=domain.id,
            publisher_id=publisher.id,
            status=OfferStatus.APPROVED,
            placement_type="guest_post",
            original_amount=Decimal("200"),
            original_currency="AUD",
            reseller_price_aud=Decimal("300"),
        )
        session.add(offer)
        session.flush()
        session.add(
            MigrationSourceRecord(
                migration_id="migration-1",
                category="offers",
                natural_key="legacy-offer-123",
                source_label="standalone",
                source_precedence=400,
                source_table="link_deals",
                source_payload={"id": 123},
                target_type="offer",
                target_id=offer.id,
            )
        )
        session.add(
            MondayMapping(
                stable_key="inventory|deal|123|publisher.com",
                entity_type="deal",
                entity_id="123",
                board_key="inventory",
                external_board_id="board-1",
                external_item_id="legacy-item-123",
            )
        )
        session.commit()

        result = DealsOnlyProjector(fake).reconcile(session, execute=True)

        assert result["legacy_mappings_reused"] == 1
        assert result["updated"] == 1
        assert result["sync_log_created"] == 1
        assert any(item_id == "legacy-item-123" for _board, item_id, _values in fake.updated)
        mapping = session.scalar(
            select(MondayMapping).where(MondayMapping.external_item_id == "legacy-item-123")
        )
        assert mapping is not None
        assert mapping.stable_key == f"inventory:offer:{offer.id}"
        assert session.scalar(
            select(MondayMapping).where(MondayMapping.board_key == "sync_log")
        ) is not None


def test_live_legacy_schema_is_preflighted_and_safely_translated() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    fake = LegacySchemaMonday()

    with Session() as session:
        _seed_approved_projection(session)
        preview = DealsOnlyProjector(fake).reconcile(session, execute=False)
        assert preview["schema_compatible"] is True
        assert preview["projection_ready"] is True
        assert preview["column_aliases"] == {
            "inventory.Last Synced": "Last Seen",
            "replies.Evidence Excerpt": "Body Text",
        }
        assert set(preview["skipped_optional_columns"]) == {
            "inventory.Local Offer ID",
            "entities.Local Entity ID",
            "replies.Local Reply ID",
        }

        result = DealsOnlyProjector(fake).reconcile(session, execute=True)
        assert result["schema_compatible"] is True
        inventory = next(values for board, _name, values in fake.created if board == "board-1")
        assert inventory["Deal Status"] == {"label": "confirmed"}
        assert inventory["Placement Type"] == {"label": "guest_post"}
        assert inventory["Publisher Currency"] == {"label": "AUD"}
        assert inventory["Reseller Currency"] == {"label": "AUD"}
        assert inventory["Link Insert Cost"] == ""
        assert inventory["Link Insert Currency"] == ""
        assert inventory["Link Insert Reseller"] == ""
        assert inventory["Review Status"] == {"label": "approved"}
        assert "Local Deal ID" not in inventory
        sync = next(values for board, _name, values in fake.created if board == "board-4")
        assert sync["Trigger Type"] == {"label": "cli"}
        assert sync["Sync Status"] == {"label": "success"}


def test_schema_preflight_blocks_before_any_write_when_status_label_is_missing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    fake = LegacySchemaMonday(include_guest_post=False)

    with Session() as session:
        _seed_approved_projection(session)
        preview = DealsOnlyProjector(fake).reconcile(session, execute=False)
        assert preview["schema_compatible"] is False
        assert preview["projection_ready"] is False
        assert any("guest_post" in error for error in preview["schema_errors"])

        with pytest.raises(RuntimeError, match="preflight"):
            DealsOnlyProjector(fake).reconcile(session, execute=True)
        assert fake.created == []
        assert fake.updated == []


def test_reply_review_projection_is_unique_per_reply() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)

    with Session() as session:
        approved, publisher = _seed_approved_projection(session)
        approved.status = OfferStatus.REVIEW
        session.add(
            Offer(
                reply_id=approved.reply_id,
                domain_id=approved.domain_id,
                publisher_id=publisher.id,
                status=OfferStatus.REVIEW,
                placement_type="niche_edit",
                original_amount=Decimal("150"),
                original_currency="AUD",
            )
        )
        session.commit()

        replies = [row for row in projection_rows(session) if row.board_key == "replies"]
        assert len(replies) == 1
        assert replies[0].entity_id == approved.reply_id
        assert replies[0].values["Evidence Excerpt"] == (
            "Full correspondence is retained in the LINK OS admin console."
        )


def test_projected_review_and_inventory_rows_follow_offer_lifecycle() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    fake = LegacySchemaMonday()

    with Session() as session:
        offer, _publisher = _seed_approved_projection(session)
        offer.status = OfferStatus.REVIEW
        session.commit()
        DealsOnlyProjector(fake).reconcile(session, execute=True)

        offer.status = OfferStatus.APPROVED
        session.commit()
        approved_rows = projection_rows(session)
        reply_row = next(row for row in approved_rows if row.board_key == "replies")
        inventory_row = next(row for row in approved_rows if row.board_key == "inventory")
        assert reply_row.values["Review Action"] == "Approved"
        assert inventory_row.values["Deal Status"] == "approved"
        DealsOnlyProjector(fake).reconcile(session, execute=True)

        offer.status = OfferStatus.LOST
        session.commit()
        lost_rows = projection_rows(session)
        reply_row = next(row for row in lost_rows if row.board_key == "replies")
        inventory_row = next(row for row in lost_rows if row.board_key == "inventory")
        assert reply_row.values["Review Action"] == "Lost"
        assert inventory_row.values["Deal Status"] == "lost"


def test_create_recovers_deterministic_unmapped_item_after_crash() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    fake = LegacySchemaMonday()

    with Session() as session:
        offer, _publisher = _seed_approved_projection(session)
        row = next(row for row in projection_rows(session) if row.entity_id == offer.id)
        orphan_id = fake.create_item("board-1", _creation_name(row), {})

        preview = DealsOnlyProjector(fake).reconcile(session, execute=False)
        assert preview["recover_candidates"] == 1
        result = DealsOnlyProjector(fake).reconcile(session, execute=True)
        assert result["recovered"] == 1
        mapping = session.scalar(
            select(MondayMapping).where(MondayMapping.stable_key == row.stable_key)
        )
        assert mapping is not None
        assert mapping.external_item_id == orphan_id
        assert sum(1 for board, name, _values in fake.created if board == "board-1" and name == _creation_name(row)) == 1


def test_guest_access_blocks_projection_destination() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    fake = LegacySchemaMonday()
    fake._boards[0]["subscribers"].append(
        {"id": "guest", "is_guest": True, "enabled": True}
    )

    with Session() as session:
        _seed_approved_projection(session)
        preview = DealsOnlyProjector(fake).reconcile(session, execute=False)
        assert preview["projection_ready"] is False
        assert any("access is not approved" in error for error in preview["destination_errors"])
