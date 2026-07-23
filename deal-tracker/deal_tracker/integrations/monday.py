from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from deal_tracker.env import load_env_value
from deal_tracker.platform.enums import OfferStatus
from deal_tracker.platform.models import (
    Domain,
    MigrationSourceRecord,
    MondayMapping,
    Offer,
    PublisherDomain,
    PublisherEntity,
    Reply,
    WorkerRun,
    utcnow,
)


BOARD_NAMES = {
    "inventory": "Link OS - Publisher Inventory",
    "entities": "Link OS - Publisher Entities",
    "replies": "Link OS - Reply Review",
    "sync_log": "Link OS - Sync Log",
}

BOARD_COLUMNS = {
    "inventory": (
        ("Root Domain", "text"),
        ("Contact Email", "email"),
        ("Deal Status", "text"),
        ("Placement Type", "text"),
        ("Publisher Cost", "numbers"),
        ("Publisher Currency", "text"),
        ("Reseller Price", "numbers"),
        ("Reseller Currency", "text"),
        ("Link Insert Cost", "numbers"),
        ("Link Insert Currency", "text"),
        ("Link Insert Reseller", "numbers"),
        ("Review Status", "text"),
        ("Local Offer ID", "text"),
        ("Last Synced", "date"),
    ),
    "entities": (
        ("Entity Key", "text"),
        ("Primary Email", "email"),
        ("Primary Domain", "text"),
        ("Domain Count", "numbers"),
        ("Local Entity ID", "text"),
        ("Last Synced", "date"),
    ),
    "replies": (
        ("Email ID", "text"),
        ("Campaign ID", "text"),
        ("Lead Email", "email"),
        ("Thread ID", "text"),
        ("Subject", "text"),
        ("Classification", "text"),
        ("Review Action", "text"),
        ("Received At", "date"),
        ("Local Reply ID", "text"),
        ("Evidence Excerpt", "long_text"),
    ),
    "sync_log": (
        ("Trigger Type", "text"),
        ("Sync Status", "text"),
        ("Started At", "date"),
        ("Finished At", "date"),
        ("Summary JSON", "long_text"),
    ),
}

# The live boards pre-date canonical UUIDs and use a few legacy column names.
# PostgreSQL mappings remain authoritative, so incompatible numeric legacy-ID
# columns are deliberately skipped instead of coercing UUIDs into numbers.
COLUMN_ALIASES = {
    ("inventory", "Local Offer ID"): ("Local Offer ID", "Local Deal ID"),
    ("inventory", "Last Synced"): ("Last Synced", "Last Seen"),
    ("replies", "Evidence Excerpt"): ("Evidence Excerpt", "Body Text"),
}

OPTIONAL_COLUMNS = {
    ("inventory", "Local Offer ID"),
    ("inventory", "Reseller Currency"),
    ("inventory", "Link Insert Cost"),
    ("inventory", "Link Insert Currency"),
    ("inventory", "Link Insert Reseller"),
    ("entities", "Local Entity ID"),
    ("replies", "Local Reply ID"),
}

STATUS_COMPATIBLE_COLUMNS = {
    ("inventory", "Deal Status"),
    ("inventory", "Placement Type"),
    ("inventory", "Publisher Currency"),
    ("inventory", "Reseller Currency"),
    ("inventory", "Link Insert Currency"),
    ("inventory", "Review Status"),
    ("replies", "Classification"),
    ("replies", "Review Action"),
    ("sync_log", "Trigger Type"),
    ("sync_log", "Sync Status"),
}

STATUS_VALUE_OVERRIDES = {
    ("inventory", "Deal Status", "approved"): "confirmed",
    ("inventory", "Deal Status", "review"): "needs_review",
    ("inventory", "Deal Status", "rejected"): "needs_review",
    ("inventory", "Deal Status", "lost"): "needs_review",
    ("inventory", "Review Status", "review"): "needs_review",
    ("inventory", "Review Status", "rejected"): "needs_review",
    ("inventory", "Review Status", "lost"): "needs_review",
    ("replies", "Classification", "offer_review"): "deal_terms",
    ("replies", "Classification", "offer_approved"): "deal_terms",
    ("replies", "Review Action", "Needs review"): "Working on it",
    ("replies", "Review Action", "Approved"): "Deal Created/Updated",
    ("replies", "Review Action", "Lost"): "Ignored",
    ("sync_log", "Trigger Type", "link_os_reconcile"): "cli",
    ("sync_log", "Sync Status", "succeeded"): "success",
}


def _key() -> str:
    token = load_env_value("MONDAY_API_KEY")
    if not token:
        raise RuntimeError("MONDAY_API_KEY is not configured")
    return token


def _normal_title(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").split())


class MondayClient:
    def __init__(self, api_key: str | None = None, api_url: str = "https://api.monday.com/v2") -> None:
        self.api_key = api_key or _key()
        self.api_url = api_url

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error = ""
        for attempt in range(3):
            try:
                response = httpx.post(
                    self.api_url,
                    headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                    json={"query": query, "variables": variables or {}},
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    raise RuntimeError(f"Monday GraphQL error: {json.dumps(payload['errors'])[:1200]}")
                return dict(payload.get("data") or {})
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Monday API failed after retries: {last_error}")

    def boards(self, board_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(board_id) for board_id in (board_ids or []) if board_id))
        if not ids:
            return []
        data = self.graphql(
            "query ($ids: [ID!]) { boards(ids: $ids) { "
            "id name board_kind workspace { id } subscribers { id is_guest enabled } "
            "columns { id title type settings_str } } }",
            {"ids": ids},
        )
        return [row for row in data.get("boards", []) if isinstance(row, dict)]

    def create_item(self, board_id: str, name: str, values: dict[str, Any]) -> str:
        data = self.graphql(
            "mutation ($board: ID!, $name: String!, $values: JSON!) { create_item(board_id: $board, item_name: $name, column_values: $values) { id } }",
            {"board": board_id, "name": name[:255], "values": json.dumps(values, separators=(",", ":"))},
        )
        return str(data["create_item"]["id"])

    def update_item(self, board_id: str, item_id: str, values: dict[str, Any]) -> None:
        self.graphql(
            "mutation ($board: ID!, $item: ID!, $values: JSON!) { change_multiple_column_values(board_id: $board, item_id: $item, column_values: $values) { id } }",
            {"board": board_id, "item": item_id, "values": json.dumps(values, separators=(",", ":"))},
        )

    def item_board_ids(self, item_ids: Iterable[str]) -> dict[str, str]:
        locations: dict[str, str] = {}
        unique_ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        for start in range(0, len(unique_ids), 100):
            batch = unique_ids[start : start + 100]
            data = self.graphql(
                "query ($ids: [ID!]!) { items(ids: $ids, limit: 100) { id board { id } } }",
                {"ids": batch},
            )
            for item in data.get("items", []):
                if isinstance(item, dict):
                    locations[str(item.get("id") or "")] = str(
                        (item.get("board") or {}).get("id") or ""
                    )
        return locations

    def item_ids_by_name(self, board_id: str, name: str) -> list[str]:
        data = self.graphql(
            "query ($boards: [ID!], $query: ItemsQuery) { boards(ids: $boards) { "
            "items_page(limit: 10, query_params: $query) { items { id name } } } }",
            {
                "boards": [board_id],
                "query": {
                    "rules": [
                        {
                            "column_id": "name",
                            "compare_value": [name],
                            "operator": "any_of",
                        }
                    ]
                },
            },
        )
        boards = list(data.get("boards") or [])
        items = list((boards[0].get("items_page") or {}).get("items") or []) if boards else []
        return [
            str(item.get("id"))
            for item in items
            if isinstance(item, dict) and str(item.get("name") or "") == name
        ]


@dataclass(frozen=True)
class ProjectionRow:
    board_key: str
    entity_type: str
    entity_id: str
    stable_key: str
    name: str
    values: dict[str, Any]


def projection_rows(session: Session) -> list[ProjectionRow]:
    rows: list[ProjectionRow] = []
    offers_by_reply: dict[str, list[Offer]] = {}
    now = utcnow().date().isoformat()
    offers = list(session.scalars(select(Offer).order_by(Offer.created_at)))
    for offer in offers:
        domain = session.get(Domain, offer.domain_id)
        reply = session.get(Reply, offer.reply_id)
        if domain is None or reply is None:
            continue
        offers_by_reply.setdefault(reply.id, []).append(offer)
        email = reply.from_address or ""
        inventory_key = f"inventory:offer:{offer.id}"
        inventory_was_projected = session.scalar(
            select(MondayMapping.id).where(MondayMapping.stable_key == inventory_key)
        ) is not None
        if offer.status == OfferStatus.APPROVED or inventory_was_projected:
            rows.append(
                ProjectionRow(
                    board_key="inventory",
                    entity_type="offer",
                    entity_id=offer.id,
                    stable_key=inventory_key,
                    name=domain.normalized_domain,
                    values={
                        "Root Domain": domain.normalized_domain,
                        "Contact Email": email,
                        "Deal Status": offer.status.value,
                        "Placement Type": offer.placement_type or "",
                        "Publisher Cost": float(offer.original_amount) if offer.original_amount is not None else "",
                        "Publisher Currency": offer.original_currency or "",
                        "Reseller Price": float(offer.reseller_price_aud) if offer.reseller_price_aud is not None else "",
                        "Reseller Currency": "AUD" if offer.reseller_price_aud is not None else "",
                        # Canonical LINK OS projects one placement per row. Clear
                        # the legacy combined-offer columns so an old link-edit
                        # price cannot conflict with its canonical niche-edit row.
                        "Link Insert Cost": "",
                        "Link Insert Currency": "",
                        "Link Insert Reseller": "",
                        "Review Status": offer.status.value,
                        "Local Offer ID": offer.id,
                        "Last Synced": now,
                    },
                )
            )

    for reply_id, reply_offers in offers_by_reply.items():
        reply = session.get(Reply, reply_id)
        if reply is None:
            continue
        review_key = f"review:reply:{reply.id}"
        review_was_projected = session.scalar(
            select(MondayMapping.id).where(MondayMapping.stable_key == review_key)
        ) is not None
        statuses = {offer.status for offer in reply_offers}
        if OfferStatus.REVIEW not in statuses and not review_was_projected:
            continue
        if OfferStatus.REVIEW in statuses:
            classification, review_action = "offer_review", "Needs review"
        elif OfferStatus.APPROVED in statuses:
            classification, review_action = "offer_approved", "Approved"
        elif OfferStatus.REJECTED in statuses:
            classification, review_action = "rejected", "Rejected"
        else:
            classification, review_action = "ignore", "Lost"
        domain = session.get(Domain, reply.domain_id) if reply.domain_id else None
        if domain is None:
            domain = session.get(Domain, reply_offers[0].domain_id)
        if domain is None:
            continue
        rows.append(
            ProjectionRow(
                board_key="replies",
                entity_type="reply",
                entity_id=reply.id,
                stable_key=review_key,
                name=f"Review · {domain.normalized_domain}",
                values={
                    "Email ID": reply.external_email_id,
                    "Campaign ID": "",
                    "Lead Email": reply.from_address or "",
                    "Thread ID": reply.external_thread_id or "",
                    "Subject": reply.subject or "",
                    "Classification": classification,
                    "Review Action": review_action,
                    "Received At": reply.received_at.date().isoformat(),
                    "Local Reply ID": reply.id,
                    "Evidence Excerpt": "Full correspondence is retained in the LINK OS admin console.",
                },
            )
        )

    entities = list(session.scalars(select(PublisherEntity).order_by(PublisherEntity.created_at)))
    for entity in entities:
        relationships = list(session.scalars(select(PublisherDomain).where(PublisherDomain.publisher_id == entity.id)))
        domains = [session.get(Domain, relationship.domain_id) for relationship in relationships]
        clean_domains = [domain.normalized_domain for domain in domains if domain]
        primary_email = ""
        if entity.primary_contact_id:
            from deal_tracker.platform.models import Contact

            contact = session.get(Contact, entity.primary_contact_id)
            primary_email = contact.normalized_email if contact else ""
        rows.append(
            ProjectionRow(
                board_key="entities",
                entity_type="publisher",
                entity_id=entity.id,
                stable_key=f"entity:publisher:{entity.id}",
                name=entity.canonical_name,
                values={
                    "Entity Key": entity.normalized_key,
                    "Primary Email": primary_email,
                    "Primary Domain": clean_domains[0] if clean_domains else "",
                    "Domain Count": len(clean_domains),
                    "Local Entity ID": entity.id,
                    "Last Synced": now,
                },
            )
        )
    return rows


def _column_settings(column: dict[str, Any]) -> dict[str, Any]:
    raw = column.get("settings_str")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _status_label(column: dict[str, Any], value: Any) -> str | None:
    labels = _column_settings(column).get("labels") or {}
    wanted = _normal_title(str(value))
    for label in labels.values() if isinstance(labels, dict) else []:
        if _normal_title(str(label)) == wanted:
            return str(label)
    return None


def _translated_value(board_key: str, title: str, value: Any) -> Any:
    return STATUS_VALUE_OVERRIDES.get((board_key, title, str(value)), value)


def _column_value(column: dict[str, Any], value: Any) -> Any:
    kind = str(column.get("type") or "text")
    if value is None or value == "":
        return ""
    if kind == "email":
        return {"email": str(value), "text": str(value)}
    if kind == "date":
        return {"date": str(value)[:10]}
    if kind == "numbers":
        return str(value)
    if kind in {"status", "color"}:
        label = _status_label(column, value)
        if label is None:
            raise ValueError(
                f"Status label {value!r} is unavailable for {column.get('title')!r}"
            )
        return {"label": label}
    return str(value)


def _column_is_compatible(
    board_key: str,
    title: str,
    expected_type: str,
    column: dict[str, Any],
) -> bool:
    actual_type = str(column.get("type") or "text")
    if actual_type == expected_type:
        return True
    if expected_type == "long_text" and actual_type == "text":
        return True
    if (
        expected_type == "text"
        and actual_type in {"status", "color"}
        and (board_key, title) in STATUS_COMPATIBLE_COLUMNS
    ):
        return True
    return False


def _resolved_column(
    board_key: str,
    title: str,
    column_map: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    candidates = COLUMN_ALIASES.get((board_key, title), (title,))
    for candidate in candidates:
        column = column_map.get(_normal_title(candidate))
        if column is not None:
            return column, candidate
    return None, None


def _creation_name(row: ProjectionRow) -> str:
    suffix = f" · LINK-{hashlib.sha256(row.stable_key.encode('utf-8')).hexdigest()[:10]}"
    return f"{row.name[: max(1, 255 - len(suffix))]}{suffix}"


class DealsOnlyProjector:
    def __init__(self, client: MondayClient | None = None) -> None:
        self.client = client or MondayClient()

    @staticmethod
    def _expected_board_ids(session: Session) -> tuple[dict[str, str], list[str]]:
        expected: dict[str, str] = {}
        errors: list[str] = []
        for key in BOARD_NAMES:
            env_name = f"MONDAY_{key.upper()}_BOARD_ID"
            configured = os.getenv(env_name, "").strip()
            mapped_ids = {
                str(value)
                for value in session.scalars(
                    select(MondayMapping.external_board_id)
                    .where(MondayMapping.board_key == key)
                    .distinct()
                )
                if value
            }
            if configured:
                if mapped_ids and mapped_ids != {configured}:
                    errors.append(
                        f"{key}: {env_name} conflicts with canonical Monday mappings"
                    )
                expected[key] = configured
            elif len(mapped_ids) == 1:
                expected[key] = next(iter(mapped_ids))
            elif not mapped_ids:
                errors.append(
                    f"{key}: no pinned board ID is configured or present in canonical mappings"
                )
            else:
                errors.append(f"{key}: canonical mappings contain multiple board IDs")
        return expected, errors

    def _boards(
        self,
        expected_ids: dict[str, str],
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        all_boards = self.client.boards(expected_ids.values())
        by_id = {str(board.get("id") or ""): board for board in all_boards}
        resolved: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        expected_workspace = os.getenv("MONDAY_WORKSPACE_ID", "").strip()
        for key, name in BOARD_NAMES.items():
            expected_id = expected_ids.get(key, "")
            board = by_id.get(expected_id)
            if board is None:
                errors.append(f"{key}: pinned board {expected_id!r} was not found")
                continue
            if str(board.get("name") or "") != name:
                errors.append(
                    f"{key}: pinned board name is {board.get('name')!r}, expected {name!r}"
                )
                continue
            workspace_id = str((board.get("workspace") or {}).get("id") or "")
            if expected_workspace and workspace_id != expected_workspace:
                errors.append(
                    f"{key}: board workspace {workspace_id!r} does not match configured workspace"
                )
                continue
            board_kind = str(board.get("board_kind") or "")
            subscribers = [
                subscriber
                for subscriber in board.get("subscribers", [])
                if isinstance(subscriber, dict) and subscriber.get("enabled") is not False
            ]
            has_guest = any(subscriber.get("is_guest") is True for subscriber in subscribers)
            if board_kind not in {"private", "share"} or has_guest:
                errors.append(
                    f"{key}: board access is not approved (kind={board_kind!r}, guests={has_guest})"
                )
                continue
            resolved[key] = board
        return resolved, errors

    def _schema_preflight(
        self,
        rows: list[ProjectionRow],
        boards: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]]]:
        errors: list[str] = []
        warnings: list[str] = []
        aliases_used: dict[str, str] = {}
        skipped_optional: list[str] = []
        resolved: dict[str, dict[str, dict[str, Any]]] = {}

        values_by_column: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            for title, value in row.values.items():
                if value not in {None, ""}:
                    values_by_column.setdefault((row.board_key, title), set()).add(str(value))
        for title, value in {
            "Trigger Type": "link_os_reconcile",
            "Sync Status": "succeeded",
        }.items():
            values_by_column.setdefault(("sync_log", title), set()).add(value)

        for board_key, expected_columns in BOARD_COLUMNS.items():
            board = boards.get(board_key)
            if board is None:
                errors.append(f"{board_key}: required board is missing")
                continue
            column_map = {
                _normal_title(str(column.get("title") or "")): column
                for column in board.get("columns", [])
            }
            board_resolved: dict[str, dict[str, Any]] = {}
            for title, expected_type in expected_columns:
                column, matched_title = _resolved_column(board_key, title, column_map)
                key = f"{board_key}.{title}"
                if column is None:
                    if (board_key, title) in OPTIONAL_COLUMNS:
                        skipped_optional.append(key)
                        warnings.append(f"{key}: optional legacy ID column is unavailable and will be skipped")
                    else:
                        errors.append(f"{key}: required column is missing")
                    continue
                if not _column_is_compatible(board_key, title, expected_type, column):
                    actual_type = str(column.get("type") or "unknown")
                    if (board_key, title) in OPTIONAL_COLUMNS:
                        skipped_optional.append(key)
                        warnings.append(
                            f"{key}: {actual_type} cannot store canonical UUIDs and will be skipped"
                        )
                    else:
                        errors.append(
                            f"{key}: expected {expected_type}, found incompatible {actual_type}"
                        )
                    continue
                board_resolved[title] = column
                if matched_title and _normal_title(matched_title) != _normal_title(title):
                    aliases_used[key] = matched_title
                if str(column.get("type") or "") in {"status", "color"}:
                    for value in sorted(values_by_column.get((board_key, title), set())):
                        translated = _translated_value(board_key, title, value)
                        if _status_label(column, translated) is None:
                            errors.append(
                                f"{key}: status label {translated!r} is unavailable"
                            )
            resolved[board_key] = board_resolved

        return (
            {
                "schema_compatible": not errors,
                "schema_errors": errors,
                "schema_warnings": warnings,
                "column_aliases": aliases_used,
                "skipped_optional_columns": skipped_optional,
            },
            resolved,
        )

    @staticmethod
    def _mapping_for_row(
        session: Session,
        row: ProjectionRow,
    ) -> tuple[MondayMapping | None, bool]:
        mapping = session.scalar(
            select(MondayMapping).where(MondayMapping.stable_key == row.stable_key)
        )
        if mapping is not None:
            return mapping, False
        target_type = {
            "offer": "offer",
            "publisher": "publisher_entity",
            "reply": "reply",
        }.get(row.entity_type)
        source_records = (
            list(
                session.scalars(
                    select(MigrationSourceRecord).where(
                        MigrationSourceRecord.target_type == target_type,
                        MigrationSourceRecord.target_id == row.entity_id,
                    )
                )
            )
            if target_type
            else []
        )
        legacy_ids = {
            str(record.source_payload.get("id") or "")
            for record in source_records
            if record.source_payload.get("id") is not None
        }
        if not legacy_ids:
            return None, False
        mapping = session.scalar(
            select(MondayMapping)
            .where(
                MondayMapping.board_key == row.board_key,
                MondayMapping.entity_id.in_(legacy_ids),
            )
            .order_by(MondayMapping.updated_at.desc())
            .limit(1)
        )
        return mapping, mapping is not None

    def _mapping_preflight(
        self,
        session: Session,
        rows: list[ProjectionRow],
        boards: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        mapping_errors: list[str] = []
        mapped: list[tuple[ProjectionRow, MondayMapping]] = []
        rows_by_external_item: dict[tuple[str, str], list[str]] = {}
        legacy_candidates = 0
        create_candidates = 0
        recover_candidates = 0
        for row in rows:
            mapping, legacy = self._mapping_for_row(session, row)
            if mapping is None:
                board_id = str((boards.get(row.board_key) or {}).get("id") or "")
                orphan_ids = self.client.item_ids_by_name(board_id, _creation_name(row)) if board_id else []
                if len(orphan_ids) > 1:
                    mapping_errors.append(
                        f"{row.stable_key}: multiple unmapped items match its deterministic name"
                    )
                elif len(orphan_ids) == 1:
                    recover_candidates += 1
                else:
                    create_candidates += 1
                continue
            mapped.append((row, mapping))
            rows_by_external_item.setdefault(
                (row.board_key, str(mapping.external_item_id)), []
            ).append(row.stable_key)
            legacy_candidates += int(legacy)
            expected_board_id = str((boards.get(row.board_key) or {}).get("id") or "")
            if str(mapping.external_board_id) != expected_board_id:
                mapping_errors.append(
                    f"{row.stable_key}: mapped board {mapping.external_board_id!r} "
                    f"does not match {expected_board_id!r}"
                )

        for (board_key, item_id), stable_keys in rows_by_external_item.items():
            if len(stable_keys) > 1:
                mapping_errors.append(
                    f"{board_key}: external item {item_id!r} resolves from "
                    f"{len(stable_keys)} projection rows"
                )

        locations = self.client.item_board_ids(
            mapping.external_item_id for _row, mapping in mapped
        ) if mapped else {}
        for row, mapping in mapped:
            expected_board_id = str((boards.get(row.board_key) or {}).get("id") or "")
            actual_board_id = locations.get(str(mapping.external_item_id))
            if actual_board_id is None:
                mapping_errors.append(
                    f"{row.stable_key}: mapped item {mapping.external_item_id!r} was not found"
                )
            elif actual_board_id != expected_board_id:
                mapping_errors.append(
                    f"{row.stable_key}: item {mapping.external_item_id!r} belongs to "
                    f"board {actual_board_id!r}, not {expected_board_id!r}"
                )
        return {
            "mapping_compatible": not mapping_errors,
            "mapping_errors": mapping_errors,
            "mapped_rows": len(mapped),
            "legacy_mapping_candidates": legacy_candidates,
            "update_candidates": len(mapped),
            "create_candidates": create_candidates,
            "recover_candidates": recover_candidates,
            "external_items_checked": len(locations),
        }

    def reconcile(self, session: Session, *, execute: bool = False) -> dict[str, Any]:
        started_at = utcnow()
        rows = projection_rows(session)
        expected_board_ids, pin_errors = self._expected_board_ids(session)
        boards, board_errors = self._boards(expected_board_ids) if not pin_errors else ({}, [])
        destination_errors = pin_errors + board_errors
        missing = sorted(set(BOARD_NAMES) - set(boards))
        summary = {
            "dry_run": not execute,
            "rows": len(rows),
            "boards_found": sorted(boards),
            "board_ids": {key: str(board.get("id") or "") for key, board in boards.items()},
            "boards_missing": missing,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "legacy_mappings_reused": 0,
            "destination_pinned": not destination_errors and not missing,
            "destination_errors": destination_errors,
        }
        preflight, resolved_columns = self._schema_preflight(rows, boards)
        summary.update(preflight)
        mapping_preflight = self._mapping_preflight(session, rows, boards) if not missing else {
            "mapping_compatible": False,
            "mapping_errors": ["Mapping preflight requires all four boards"],
            "mapped_rows": 0,
            "legacy_mapping_candidates": 0,
            "update_candidates": 0,
            "create_candidates": len(rows),
            "recover_candidates": 0,
            "external_items_checked": 0,
        }
        summary.update(mapping_preflight)
        summary["projection_ready"] = bool(
            summary["schema_compatible"]
            and summary["mapping_compatible"]
            and not missing
            and not destination_errors
        )
        if not execute:
            summary["by_board"] = {key: sum(1 for row in rows if row.board_key == key) for key in BOARD_NAMES}
            return summary
        if missing:
            raise RuntimeError("Monday boards could not be resolved: " + ", ".join(missing))
        if not summary["projection_ready"]:
            raise RuntimeError(
                "Monday projection blocked by preflight: "
                + "; ".join(
                    summary["destination_errors"]
                    + summary["schema_errors"]
                    + summary["mapping_errors"]
                )
            )

        for row in rows:
            board = boards[row.board_key]
            board_columns = resolved_columns[row.board_key]
            values: dict[str, Any] = {}
            for title, value in row.values.items():
                column = board_columns.get(title)
                if not column:
                    continue
                translated = _translated_value(row.board_key, title, value)
                values[str(column["id"])] = _column_value(column, translated)
            digest = hashlib.sha256(json.dumps({"name": row.name, "values": values}, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            mapping, legacy_mapping = self._mapping_for_row(session, row)
            if mapping is not None and legacy_mapping:
                mapping.stable_key = row.stable_key
                mapping.entity_type = row.entity_type
                mapping.entity_id = row.entity_id
                summary["legacy_mappings_reused"] += 1
            if mapping and mapping.last_payload_hash == digest and mapping.sync_status == "ok":
                summary["unchanged"] += 1
                continue
            if mapping:
                self.client.update_item(str(board["id"]), mapping.external_item_id, values)
                summary["updated"] += 1
            else:
                create_name = _creation_name(row)
                orphan_ids = self.client.item_ids_by_name(str(board["id"]), create_name)
                if len(orphan_ids) > 1:
                    raise RuntimeError(
                        f"Monday create blocked: multiple items match {row.stable_key!r}"
                    )
                if orphan_ids:
                    item_id = orphan_ids[0]
                    self.client.update_item(str(board["id"]), item_id, values)
                    summary["recovered"] = int(summary.get("recovered") or 0) + 1
                else:
                    item_id = self.client.create_item(str(board["id"]), create_name, values)
                    summary["created"] += 1
                mapping = MondayMapping(
                    stable_key=row.stable_key,
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    board_key=row.board_key,
                    external_board_id=str(board["id"]),
                    external_item_id=item_id,
                )
                session.add(mapping)
            mapping.last_payload_hash = digest
            mapping.last_synced_at = utcnow()
            mapping.sync_status = "ok"
            mapping.last_error = None
            session.commit()

        sync_board = boards["sync_log"]
        sync_columns = resolved_columns["sync_log"]
        finished_at = utcnow()
        log_values_by_title = {
            "Trigger Type": "link_os_reconcile",
            "Sync Status": "succeeded",
            "Started At": started_at.date().isoformat(),
            "Finished At": finished_at.date().isoformat(),
            "Summary JSON": json.dumps(summary, sort_keys=True, default=str)[:4000],
        }
        log_values: dict[str, Any] = {}
        for title, value in log_values_by_title.items():
            column = sync_columns.get(title)
            if column:
                translated = _translated_value("sync_log", title, value)
                log_values[str(column["id"])] = _column_value(column, translated)
        run_id = str(uuid.uuid4())
        log_item_id = self.client.create_item(
            str(sync_board["id"]),
            f"LINK OS sync · {finished_at.isoformat(timespec='seconds')}",
            log_values,
        )
        session.add(
            MondayMapping(
                stable_key=f"sync_log:run:{run_id}",
                entity_type="sync_run",
                entity_id=run_id,
                board_key="sync_log",
                external_board_id=str(sync_board["id"]),
                external_item_id=log_item_id,
                last_payload_hash=hashlib.sha256(
                    json.dumps(log_values, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                last_synced_at=finished_at,
                sync_status="ok",
            )
        )
        session.commit()
        summary["sync_log_created"] = 1
        return summary
