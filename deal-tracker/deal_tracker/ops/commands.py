"""Operational command implementations shared by the CLI and future automation."""

from __future__ import annotations

import csv
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from .api import ApiClientError, LinkOsApiClient
from .migration import (
    MigrationPlanner,
    adapter_for,
    default_source_specs,
    execute_migration,
)
from .models import CommandResult, SourceSpec


PREFERRED_IMPORT_COLUMNS = (
    "domain",
    "website",
    "url",
    "referring_domain",
    "source_domain",
    "contact_email",
    "email",
)

_PRIVATE_RESPONSE_KEY_PARTS = (
    "api_key",
    "authorization",
    "body",
    "contact",
    "domain",
    "email",
    "lead",
    "password",
    "raw",
    "reply",
    "secret",
    "thread",
    "token",
)


def _safe_api_summary(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Remove payload-like data before a response reaches CLI JSON output."""

    lowered = key.lower()
    if any(part in lowered for part in _PRIVATE_RESPONSE_KEY_PARTS) and not isinstance(
        value, (int, float, bool, type(None))
    ):
        return "[redacted]"
    if depth >= 5:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(child_key): _safe_api_summary(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        rendered = [
            _safe_api_summary(item, key=key, depth=depth + 1) for item in value[:50]
        ]
        if len(value) > 50:
            rendered.append({"truncated_items": len(value) - 50})
        return rendered
    return value


def _safe_backlink_payload(value: Any) -> Any:
    """Keep public backlink evidence useful while dropping private/provider fields."""

    if not isinstance(value, dict):
        return {}
    allowed_top = {
        "mode", "client_domain", "competitor_domains", "credit_cap", "predicted_credits",
        "maximum_paid_records", "count_credit_cost", "authority_floor", "monthly_usage", "monthly_cap",
        "monthly_remaining", "balance", "allowed", "blocking_reasons", "targets",
        "confirmation_required", "items", "count", "offset", "run_status", "id", "status",
        "estimated_credits", "applied", "approved", "import_id",
    }
    payload = {key: val for key, val in value.items() if key in allowed_top}
    if isinstance(payload.get("items"), list):
        safe_items = []
        for item in payload["items"][:100]:
            if not isinstance(item, dict):
                continue
            safe_item = {
                key: item.get(key)
                for key in (
                    "id", "domain", "normalized_domain", "status", "occurrence_count",
                    "highest_domain_inlink_rank", "has_dofollow", "local_score",
                    "reason_codes", "codex_confidence", "decision_summary", "existing_domain_id",
                )
                if key in item
            }
            safe_item["evidence"] = [
                {
                    key: evidence.get(key)
                    for key in ("source_url", "target_url", "page_title", "anchor_text", "nofollow", "inlink_rank", "domain_inlink_rank", "first_seen", "last_visited")
                    if key in evidence
                }
                for evidence in item.get("evidence", [])[:10]
                if isinstance(evidence, dict)
            ]
            safe_items.append(safe_item)
        payload["items"] = safe_items
    return payload


def _nonempty_lines(value: str) -> Iterator[str]:
    for line in value.splitlines():
        stripped = line.strip()
        if stripped:
            yield stripped


def iter_import_file(path: Path) -> Iterator[str]:
    """Stream values from plain text, CSV, or TSV without a row cap."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(65536)
        handle.seek(0)
        suffix = path.suffix.lower()
        likely_tabular = suffix in {".csv", ".tsv"} or "\t" in sample
        if likely_tabular:
            delimiter = "\t" if suffix == ".tsv" else ","
            if suffix not in {".csv", ".tsv"}:
                try:
                    delimiter = csv.Sniffer().sniff(sample, delimiters=",\t").delimiter
                except csv.Error:
                    delimiter = "\t" if "\t" in sample else ","
            reader = csv.DictReader(handle, delimiter=delimiter)
            fields = {
                str(field or "").strip().lower(): field
                for field in reader.fieldnames or []
            }
            selected = [
                fields[name] for name in PREFERRED_IMPORT_COLUMNS if name in fields
            ]
            if selected:
                for row in reader:
                    for field in selected:
                        value = str(row.get(field) or "").strip()
                        if value:
                            yield value
                return
        handle.seek(0)
        for line in handle:
            value = line.strip()
            if value:
                yield value


def _chunks(values: Iterable[str], size: int) -> Iterator[list[str]]:
    chunk: list[str] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


class CommandRunner:
    def __init__(
        self,
        api: LinkOsApiClient,
        *,
        source_specs: Sequence[SourceSpec] | None = None,
    ) -> None:
        self.api = api
        self.source_specs = list(
            default_source_specs() if source_specs is None else source_specs
        )

    def doctor(self) -> CommandResult:
        result = CommandResult(command="doctor", mode="read_only")
        migration_complete = False
        try:
            health = self.api.health()
            migration = health.get("migration", {}) if isinstance(health, dict) else {}
            migration_complete = bool(
                isinstance(migration, dict) and migration.get("complete") is True
            )
            result.data["api"] = {
                "reachable": True,
                "health": _safe_api_summary(health),
            }
        except ApiClientError as exc:
            result.data["api"] = {"reachable": False}
            result.add_error(exc.code, str(exc))

        inventories = [adapter_for(spec).inventory() for spec in self.source_specs]
        result.data["migration_sources"] = [item.to_dict() for item in inventories]
        result.data["configuration"] = {
            "database_url_configured": bool(os.environ.get("DATABASE_URL")),
            "instantly_key_configured": bool(os.environ.get("INSTANTLY_API_KEY")),
            "monday_key_configured": bool(os.environ.get("MONDAY_API_KEY")),
        }
        missing_required = [
            spec.label
            for spec, inventory in zip(self.source_specs, inventories, strict=True)
            if spec.required and not inventory.available
        ]
        if missing_required:
            if migration_complete:
                result.warnings.append(
                    "Legacy recovery sources are not mounted in this runtime; "
                    "the canonical PostgreSQL migration ledger confirms cutover completion."
                )
                result.data["missing_recovery_sources"] = missing_required
            else:
                result.add_error(
                    "required_migration_source_missing",
                    "One or more required migration sources are unavailable",
                )
                result.data["missing_required_sources"] = missing_required
        return result

    def import_values(
        self,
        values: Iterable[str],
        *,
        source_name: str,
        execute: bool,
        queue: bool,
        chunk_size: int,
    ) -> CommandResult:
        result = CommandResult(
            command="import",
            mode="execute" if execute else "dry_run",
        )
        if chunk_size < 1 or chunk_size > 10_000:
            result.add_error("invalid_chunk_size", "Chunk size must be between 1 and 10000")
            return result

        submitted = 0
        batches = 0
        import_ids: list[Any] = []
        responses: list[dict[str, Any]] = []
        try:
            cleaned = (value.strip() for value in values if value.strip())
            for batch in _chunks(cleaned, chunk_size):
                batches += 1
                submitted += len(batch)
                if not execute:
                    continue
                response = self.api.create_import(
                    batch,
                    source_name=source_name,
                    queue=queue,
                )
                import_id = response.get("import_id") or response.get("id")
                if import_id is not None:
                    import_ids.append(import_id)
                responses.append(
                    {
                        key: response[key]
                        for key in (
                            "import_id",
                            "id",
                            "accepted",
                            "duplicate",
                            "invalid",
                            "queued",
                        )
                        if key in response
                    }
                )
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
            return result

        result.data.update(
            {
                "source_name": source_name,
                "submitted_values": submitted,
                "batch_count": batches,
                "chunk_size": chunk_size,
                "queue": queue,
                "import_ids": import_ids,
                "batch_summaries": responses,
            }
        )
        if not execute:
            result.warnings.append(
                "Dry run only: no import rows or scrape jobs were created. Re-run with --execute."
            )
        elif submitted == 0:
            result.add_error("empty_import", "No non-empty import values were provided")
        return result

    def import_text(self, text: str, **kwargs: Any) -> CommandResult:
        return self.import_values(_nonempty_lines(text), **kwargs)

    def import_file(self, path: Path, **kwargs: Any) -> CommandResult:
        if not path.is_file():
            result = CommandResult(command="import", mode="dry_run")
            result.add_error("import_file_not_found", f"Import file not found: {path}")
            return result
        return self.import_values(iter_import_file(path), **kwargs)

    def backlink_analysis(
        self,
        *,
        mode: str,
        client_domain: str,
        competitor_domains: Sequence[str],
        credit_cap: int,
        authority_floor: int,
        execute: bool,
        confirmed_credit_cap: int | None,
        source_url_filter: str | None = None,
        authority_ceiling: int | None = 80,
    ) -> CommandResult:
        result = CommandResult(command="backlink-analysis", mode="execute" if execute else "dry_run")
        if execute and confirmed_credit_cap != credit_cap:
            result.add_error("credit_confirmation_required", "--confirm-credit-cap must exactly match --credit-cap")
            return result
        try:
            estimate = self.api.estimate_backlink_analysis(
                mode=mode,
                client_domain=client_domain,
                competitor_domains=competitor_domains,
                credit_cap=credit_cap,
                authority_floor=authority_floor,
                source_url_filter=source_url_filter,
                authority_ceiling=authority_ceiling,
            )
            result.data = _safe_backlink_payload(estimate)
            if not estimate.get("allowed"):
                result.add_error("backlink_analysis_blocked", "SE Ranking credit or configuration preflight did not pass")
                return result
            if execute:
                created = self.api.create_backlink_analysis(
                    mode=mode,
                    client_domain=client_domain,
                    competitor_domains=competitor_domains,
                    credit_cap=credit_cap,
                    confirmed_credit_cap=int(confirmed_credit_cap),
                    authority_floor=authority_floor,
                    source_url_filter=source_url_filter,
                    authority_ceiling=authority_ceiling,
                )
                result.data["created"] = _safe_backlink_payload(created)
            else:
                result.warnings.append("Dry run only: no paid backlink call or analysis job was created. Re-run with --execute --confirm-credit-cap N.")
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def backlink_candidates(self, analysis_id: str | None, *, status: str, limit: int) -> CommandResult:
        result = CommandResult(command="backlink-candidates", mode="read_only")
        try:
            if analysis_id:
                result.data = _safe_backlink_payload(self.api.backlink_candidates(analysis_id, status=status, limit=limit))
                result.data["analysis_id"] = analysis_id
            else:
                # Awaiting-Codex candidates only exist on awaiting-review runs.
                # Manual holds may belong to runs that have otherwise completed,
                # so scan all analyses when an operator requests another status.
                run_status = "awaiting_codex_review" if status == "awaiting_codex_review" else ""
                runs = self.api.backlink_analyses(status=run_status, limit=250)
                gathered: list[dict[str, Any]] = []
                run_ids: list[str] = []
                for run in runs.get("items", []):
                    if not isinstance(run, dict) or not run.get("id") or len(gathered) >= limit:
                        continue
                    run_id = str(run["id"])
                    run_ids.append(run_id)
                    payload = _safe_backlink_payload(self.api.backlink_candidates(run_id, status=status, limit=limit - len(gathered)))
                    for item in payload.get("items", []):
                        if isinstance(item, dict):
                            gathered.append({**item, "analysis_id": run_id})
                result.data = {"items": gathered, "count": len(gathered), "analysis_ids": run_ids, "status": status}
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def review_backlinks(
        self,
        analysis_id: str,
        decisions: Sequence[dict[str, Any]],
        *,
        execute: bool,
    ) -> CommandResult:
        result = CommandResult(command="review-backlinks", mode="execute" if execute else "dry_run")
        invalid = [index for index, item in enumerate(decisions) if not isinstance(item, dict) or item.get("decision") not in {"approved", "rejected", "manual_review"}]
        if invalid:
            result.add_error("invalid_decisions", f"Invalid decisions at indexes: {invalid[:10]}")
            return result
        result.data = {"analysis_id": analysis_id, "decision_count": len(decisions)}
        if not execute:
            result.warnings.append("Dry run only: review decisions were validated but not recorded and no canonical import was queued.")
            return result
        try:
            result.data.update(_safe_backlink_payload(self.api.review_backlinks(analysis_id, decisions)))
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def reconcile_instantly(
        self,
        *,
        execute: bool,
        pause_legacy: bool = False,
        full_reconcile: bool = True,
        expected_legacy_count: int = 13,
    ) -> CommandResult:
        result = CommandResult(
            command="reconcile-instantly",
            mode="execute" if execute else "dry_run",
        )
        try:
            result.data = _safe_api_summary(
                self.api.reconcile_instantly(
                    dry_run=not execute,
                    pause_legacy=pause_legacy,
                    full_reconcile=full_reconcile,
                    expected_legacy_count=expected_legacy_count,
                )
            )
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        if not execute:
            result.warnings.append(
                "Dry run only: no campaign, lead, checkpoint, or sender state was changed."
            )
        return result

    def prepare_campaign(
        self,
        *,
        execute: bool,
        target_size: int = 25,
        public_evidence: bool = False,
        confirm_verification_skipped: bool = False,
    ) -> CommandResult:
        result = CommandResult(
            command="prepare-campaign",
            mode="execute" if execute else "dry_run",
            data={
                "pilot": True,
                "target_size": target_size,
                "contact_policy": (
                    "public_evidence_verification_skipped"
                    if public_evidence
                    else "strict_verified"
                ),
                "prepare_only": True,
            },
        )
        if not 1 <= target_size <= 25:
            result.add_error(
                "invalid_pilot_size",
                "Paused pilot size must be between 1 and 25",
            )
            return result
        if public_evidence and not confirm_verification_skipped:
            result.add_error(
                "verification_skip_confirmation_required",
                "Public-evidence preparation requires --confirm-verification-skipped",
            )
            return result
        if not execute:
            result.warnings.append(
                "Dry run only: no Instantly campaign or campaign membership was created."
            )
            return result
        try:
            result.data.update(
                _safe_api_summary(
                    self.api.prepare_campaign(
                        pilot=True,
                        target_size=target_size,
                        contact_policy=result.data["contact_policy"],
                        prepare_only=True,
                        confirm_verification_skipped=confirm_verification_skipped,
                    )
                )
            )
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def activate_campaign(
        self,
        batch_id: str,
        *,
        reason: str,
        execute: bool,
    ) -> CommandResult:
        result = CommandResult(
            command="activate-campaign",
            mode="execute" if execute else "dry_run",
            data={"batch_id": batch_id, "reason": reason},
        )
        if not execute:
            result.warnings.append(
                "Dry run only: the prepared campaign remains paused."
            )
            return result
        try:
            result.data.update(
                _safe_api_summary(
                    self.api.activate_campaign(batch_id, reason=reason)
                )
            )
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def campaign_set_estimate(self) -> CommandResult:
        result = CommandResult(command="campaign-set-estimate", mode="dry_run")
        try:
            result.data = _safe_api_summary(self.api.estimate_campaign_set())
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def prepare_campaign_set(self, *, execute: bool, confirmed: bool) -> CommandResult:
        result = CommandResult(command="prepare-campaign-set", mode="execute" if execute else "dry_run")
        if not execute:
            try:
                result.data = _safe_api_summary(self.api.estimate_campaign_set())
            except ApiClientError as exc:
                result.add_error(exc.code, str(exc))
            result.warnings.append("Dry run only: no contacts were reserved or uploaded.")
            return result
        if not confirmed:
            result.add_error("verification_skip_confirmation_required", "--confirm-verification-skipped is required")
            return result
        try:
            result.data = _safe_api_summary(self.api.prepare_campaign_set())
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def activate_campaign_set(self, launch_id: str, *, execute: bool) -> CommandResult:
        result = CommandResult(command="activate-campaign-set", mode="execute" if execute else "dry_run", data={"launch_id": launch_id})
        if not execute:
            result.warnings.append("Dry run only: all campaigns remain paused.")
            return result
        try:
            result.data.update(_safe_api_summary(self.api.activate_campaign_set(launch_id)))
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def pause_campaign(self, batch_id: str, *, reason: str, execute: bool) -> CommandResult:
        result = CommandResult(command="pause-campaign", mode="execute" if execute else "dry_run", data={"batch_id": batch_id, "reason": reason})
        if not execute:
            result.warnings.append("Dry run only: the campaign state was not changed.")
            return result
        try:
            result.data.update(_safe_api_summary(self.api.pause_campaign(batch_id, reason=reason)))
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def reconcile_monday(self, *, execute: bool) -> CommandResult:
        result = CommandResult(
            command="reconcile-monday",
            mode="execute" if execute else "dry_run",
        )
        try:
            result.data = _safe_api_summary(
                self.api.reconcile_monday(dry_run=not execute)
            )
            if result.data.get("projection_ready") is False:
                result.add_error(
                    "monday_preflight_failed",
                    "Monday projection is blocked until live schema and mapping readbacks pass",
                )
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        if not execute:
            result.warnings.append(
                "Dry run only: no monday.com boards, items, updates, or mappings were changed."
            )
        return result

    def retry_job(self, job_id: str, *, execute: bool) -> CommandResult:
        result = CommandResult(
            command="retry-job",
            mode="execute" if execute else "dry_run",
            data={"job_id": job_id},
        )
        if not execute:
            result.warnings.append("Dry run only: the job was not requeued.")
            return result
        try:
            result.data.update(_safe_api_summary(self.api.retry_job(job_id)))
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def pause_outreach(self, *, reason: str, execute: bool) -> CommandResult:
        result = CommandResult(
            command="pause-outreach",
            mode="execute" if execute else "dry_run",
            data={"reason": reason},
        )
        if not execute:
            result.warnings.append(
                "Dry run only: the global outreach state was not changed."
            )
            return result
        try:
            result.data.update(
                _safe_api_summary(
                    self.api.set_outreach_pause(paused=True, reason=reason)
                )
            )
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def resume_outreach(
        self,
        *,
        reason: str,
        execute: bool,
        confirm_sender_health: bool,
    ) -> CommandResult:
        result = CommandResult(
            command="resume-outreach",
            mode="execute" if execute else "dry_run",
            data={"reason": reason},
        )
        if not execute:
            result.warnings.append(
                "Dry run only: the global outreach state was not changed."
            )
            return result
        if not confirm_sender_health:
            result.add_error(
                "sender_health_confirmation_required",
                "Resume requires --confirm-sender-health and a passing live health readback",
            )
            return result
        try:
            health = self.api.health()
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
            return result
        outreach_health = health.get("outreach", {})
        safe_to_resume = (
            outreach_health.get("safe_to_resume")
            if isinstance(outreach_health, dict)
            else None
        )
        if safe_to_resume is not True:
            result.add_error(
                "outreach_health_gate_failed",
                "Live LINK OS health did not explicitly report safe_to_resume=true",
            )
            result.data["health_gate"] = "failed"
            return result
        result.data["health_gate"] = "passed"
        try:
            result.data.update(
                _safe_api_summary(
                    self.api.set_outreach_pause(
                        paused=False,
                        reason=reason,
                        confirm_sender_health=True,
                    )
                )
            )
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        return result

    def migration_plan(self, specs: Sequence[SourceSpec]) -> CommandResult:
        result = CommandResult(command="migrate", mode="dry_run")
        try:
            plan = MigrationPlanner(specs).build()
        except (OSError, ValueError, sqlite3.Error) as exc:
            result.add_error("migration_plan_failed", _safe_exception_message(exc))
            return result
        result.data = {
            "record_count": len(plan.records),
            "reconciliation": plan.report.to_dict(),
        }
        result.warnings.append(
            "Shadow mode only: no snapshots or canonical records were created. "
            "Re-run with --execute."
        )
        return result

    def migrate(
        self,
        specs: Sequence[SourceSpec],
        *,
        execute: bool,
        snapshot_dir: Path,
        batch_size: int,
    ) -> CommandResult:
        if not execute:
            return self.migration_plan(specs)
        result = CommandResult(command="migrate", mode="execute")
        try:
            execution = execute_migration(
                self.api,
                specs,
                snapshot_dir=snapshot_dir,
                batch_size=batch_size,
            )
            result.data = execution.to_dict()
        except ApiClientError as exc:
            result.add_error(exc.code, str(exc))
        except (OSError, ValueError, RuntimeError) as exc:
            result.add_error("migration_failed", _safe_exception_message(exc))
        return result


def _safe_exception_message(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "A configured migration source was not found"
    if isinstance(exc, PermissionError):
        return "Permission was denied while reading or snapshotting migration data"
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "Migration operation failed; inspect local application logs"
