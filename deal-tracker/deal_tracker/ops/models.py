"""Safe, JSON-serialisable models for LINK OS operational commands.

The models in this module deliberately separate operator-facing summaries from
record payloads.  Migration payloads may contain private reply evidence or
password hashes and must never be rendered by the CLI or reconciliation report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class CommandError:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class CommandResult:
    command: str
    ok: bool = True
    mode: str = "read_only"
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[CommandError] = field(default_factory=list)
    generated_at: str = field(default_factory=utc_now_iso)

    def add_error(self, code: str, message: str) -> None:
        self.ok = False
        self.errors.append(CommandError(code=code, message=message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": self.command,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "data": self.data,
            "warnings": self.warnings,
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class SourceSpec:
    """One migration input.

    Higher precedence wins.  The labels are stable migration identifiers and
    are intentionally separate from paths so reports remain comparable when a
    recovery source is moved.
    """

    label: str
    kind: str
    path: Path
    precedence: int
    required: bool = False

    def safe_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "path": str(self.path),
            "precedence": self.precedence,
            "required": self.required,
        }


@dataclass(frozen=True)
class SourceInventory:
    label: str
    kind: str
    path: Path
    precedence: int
    available: bool
    size_bytes: int = 0
    table_counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "path": str(self.path),
            "precedence": self.precedence,
            "available": self.available,
            "size_bytes": self.size_bytes,
            "table_counts": dict(sorted(self.table_counts.items())),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MigrationRecord:
    """A private migration record.

    ``payload`` is never included in ``safe_dict``.  Callers may only send it to
    the local canonical API after an explicit execute gate and successful source
    snapshots.
    """

    category: str
    natural_key: str
    source_label: str
    source_precedence: int
    source_table: str
    payload: Mapping[str, Any]
    manual_authoritative: bool = False
    freshness: str = ""

    def safe_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "natural_key": self.natural_key,
            "source_label": self.source_label,
            "source_precedence": self.source_precedence,
            "source_table": self.source_table,
            "manual_authoritative": self.manual_authoritative,
            "freshness": self.freshness,
        }

    def api_dict(self) -> dict[str, Any]:
        return {**self.safe_dict(), "payload": dict(self.payload)}


@dataclass(frozen=True)
class SnapshotResult:
    source_label: str
    source_path: Path
    snapshot_path: Path
    size_bytes: int
    sha256: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_label": self.source_label,
            "source_path": str(self.source_path),
            "snapshot_path": str(self.snapshot_path),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }


@dataclass
class ReconciliationReport:
    source_inventories: list[SourceInventory]
    source_counts: dict[str, dict[str, int]]
    final_counts: dict[str, int]
    duplicates_discarded: dict[str, int]
    contacted_domains: int
    missing_contact_evidence: int
    campaign_memberships: dict[str, int]
    precedence: list[str]
    warnings: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "precedence": self.precedence,
            "sources": [inventory.to_dict() for inventory in self.source_inventories],
            "source_counts": {
                source: dict(sorted(counts.items()))
                for source, counts in sorted(self.source_counts.items())
            },
            "final_counts": dict(sorted(self.final_counts.items())),
            "duplicates_discarded": dict(sorted(self.duplicates_discarded.items())),
            "contacted_domains": self.contacted_domains,
            "missing_contact_evidence": self.missing_contact_evidence,
            "campaign_memberships": dict(sorted(self.campaign_memberships.items())),
            "warnings": self.warnings,
        }
