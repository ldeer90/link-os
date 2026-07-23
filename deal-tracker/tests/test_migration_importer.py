from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from deal_tracker.auth import hash_password
from deal_tracker.migration_importer import import_migration_records
from deal_tracker.platform.models import (
    Base,
    CatalogueListing,
    Domain,
    MigrationSourceRecord,
    Offer,
    UserAccount,
)
from deal_tracker.platform.repositories import ensure_fail_closed_defaults


def record(category: str, natural_key: str, payload: dict, *, manual: bool = False) -> dict:
    return {
        "category": category,
        "natural_key": natural_key,
        "source_label": "test_snapshot",
        "source_precedence": 400,
        "source_table": category,
        "manual_authoritative": manual,
        "freshness": "2026-07-15T00:00:00+00:00",
        "payload": payload,
    }


def test_materialises_dependent_records_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    password_hash = hash_password("current-password")
    records = [
        record(
            "users",
            "hello@example.com",
            {
                "id": 1,
                "email": "hello@example.com",
                "password_hash": password_hash,
                "role": "admin",
                "visibility_tier": "full",
                "is_active": 1,
            },
            manual=True,
        ),
        record("domains", "publisher.com", {"root_domain": "publisher.com"}),
        record("contacts", "editor@publisher.com", {"email": "editor@publisher.com"}),
        record(
            "domain_contacts",
            "publisher.com|editor@publisher.com",
            {
                "root_domain": "publisher.com",
                "email": "editor@publisher.com",
                "evidence_url": "https://publisher.com/contact",
                "confidence": 0.9,
            },
        ),
        record(
            "replies",
            "email-1",
            {
                "email_id": "email-1",
                "thread_id": "thread-1",
                "lead_email": "editor@publisher.com",
                "body_text": "Guest post is AUD 200.",
                "received_at": "2026-07-14T00:00:00+00:00",
            },
        ),
        record(
            "offers",
            "publisher.com|thread-1|guest_post",
            {
                "id": 10,
                "root_domain": "publisher.com",
                "contact_email": "editor@publisher.com",
                "instantly_thread_id": "thread-1",
                "placement_type": "guest_post",
                "publisher_cost_amount": 200,
                "publisher_cost_currency": "AUD",
                "reseller_price_amount": 300,
                "reseller_price_currency": "AUD",
                "admin_review_status": "approved",
                "is_listed": 1,
            },
            manual=True,
        ),
        record(
            "catalogue_listings",
            "publisher.com|thread-1|guest_post",
            {
                "id": 10,
                "root_domain": "publisher.com",
                "site_name": "Publisher",
                "visibility_min_tier": "basic",
                "updated_at": "2026-07-15T00:00:00+00:00",
            },
            manual=True,
        ),
    ]

    with Session() as session:
        ensure_fail_closed_defaults(session)
        first = import_migration_records(session, records, migration_id="migration-one")
        session.commit()
        assert first["errors"] == 0
        assert first["materialized"] == len(records)
        assert session.scalar(select(Domain).where(Domain.normalized_domain == "publisher.com")) is not None
        assert session.scalar(select(Offer)) is not None
        assert session.scalar(select(CatalogueListing)) is not None
        user = session.scalar(select(UserAccount).where(UserAccount.email == "hello@example.com"))
        assert user is not None and user.password_hash == password_hash
        source = session.scalar(
            select(MigrationSourceRecord).where(MigrationSourceRecord.category == "users")
        )
        assert "password_hash" not in source.source_payload

    with Session() as session:
        second = import_migration_records(session, records, migration_id="migration-two")
        session.commit()
        assert second["skipped"] == len(records)
        assert second["materialized"] == 0
