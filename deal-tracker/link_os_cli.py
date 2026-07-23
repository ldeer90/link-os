#!/usr/bin/env python3
"""Structured, fail-closed CLI for LINK OS operators and Codex automations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from deal_tracker.ops import (
    ApiClientError,
    CommandResult,
    CommandRunner,
    LinkOsApiClient,
    SourceSpec,
    default_source_specs,
)


class CliUsageError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _add_api_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-url",
        default=os.environ.get("LINK_OS_API_URL", "http://127.0.0.1:8766"),
        help="Local LINK OS API base URL (loopback only)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="link-os", description=__doc__)
    parser.add_argument("--json-indent", type=int, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Read-only platform and source health")
    _add_api_option(doctor)

    intake = subparsers.add_parser(
        "import",
        help="Import pasted values or a streamed file",
    )
    _add_api_option(intake)
    input_group = intake.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--file", type=Path)
    input_group.add_argument("--text")
    input_group.add_argument("--value", action="append")
    intake.add_argument("--source-name", default="link_os_cli")
    intake.add_argument("--chunk-size", type=int, default=5_000)
    intake.add_argument("--import-only", action="store_true", help="Do not queue scraping")
    intake.add_argument("--execute", action="store_true")

    instantly = subparsers.add_parser(
        "reconcile-instantly",
        help="Reconcile campaign, lead, reply, and sender state",
    )
    _add_api_option(instantly)
    instantly.add_argument(
        "--pause-legacy",
        action="store_true",
        help="Pause and read back the guarded legacy campaign set",
    )
    instantly.add_argument(
        "--expected-legacy-count",
        type=int,
        default=13,
        help="Fail before mutation unless this many legacy campaigns match",
    )
    instantly.add_argument(
        "--incremental",
        action="store_true",
        help="Skip the full workspace membership reconciliation",
    )
    instantly.add_argument("--execute", action="store_true")

    campaign = subparsers.add_parser(
        "prepare-campaign",
        help="Prepare and upload a paused LINK OS pilot without activating it",
    )
    _add_api_option(campaign)
    campaign.add_argument("--target-size", type=int, default=25)
    campaign.add_argument(
        "--public-evidence",
        action="store_true",
        help="Use strong public-site evidence without Instantly verification",
    )
    campaign.add_argument("--confirm-verification-skipped", action="store_true")
    campaign.add_argument("--execute", action="store_true")

    activate_campaign = subparsers.add_parser(
        "activate-campaign",
        help="Activate one prepared paused LINK OS campaign through live safety gates",
    )
    _add_api_option(activate_campaign)
    activate_campaign.add_argument("batch_id")
    activate_campaign.add_argument(
        "--reason",
        default="Operator-approved campaign activation",
    )
    activate_campaign.add_argument("--execute", action="store_true")

    campaign_set_estimate = subparsers.add_parser("campaign-set-estimate", help="Estimate the canonical three-campaign segmented launch")
    _add_api_option(campaign_set_estimate)
    prepare_campaign_set = subparsers.add_parser("prepare-campaign-set", help="Reserve and upload the three segmented campaigns paused")
    _add_api_option(prepare_campaign_set)
    prepare_campaign_set.add_argument("--confirm-verification-skipped", action="store_true")
    prepare_campaign_set.add_argument("--execute", action="store_true")
    activate_campaign_set = subparsers.add_parser("activate-campaign-set", help="Activate all three readback-complete segmented campaigns")
    _add_api_option(activate_campaign_set)
    activate_campaign_set.add_argument("launch_id")
    activate_campaign_set.add_argument("--execute", action="store_true")
    pause_campaign = subparsers.add_parser("pause-campaign", help="Pause one managed campaign without stopping clean campaigns")
    _add_api_option(pause_campaign)
    pause_campaign.add_argument("batch_id")
    pause_campaign.add_argument("--reason", default="Operator paused campaign")
    pause_campaign.add_argument("--execute", action="store_true")

    monday = subparsers.add_parser(
        "reconcile-monday",
        help="Reconcile the deals-only monday.com projection",
    )
    _add_api_option(monday)
    monday.add_argument("--execute", action="store_true")

    backlinks = subparsers.add_parser(
        "backlink-analysis",
        help="Estimate or execute canonical SE Ranking backlink discovery",
    )
    _add_api_option(backlinks)
    backlinks.add_argument("--mode", choices=("competitor_prospecting", "client_profile_audit"), required=True)
    backlinks.add_argument("--client", required=True)
    backlinks.add_argument("--competitor", action="append", default=[])
    backlinks.add_argument("--credit-cap", type=int, default=500)
    backlinks.add_argument("--authority-floor", type=int, default=20)
    backlinks.add_argument("--authority-ceiling", type=int, default=80, help="Maximum Domain InLink Rank (default: 80; use 100 only for an explicitly unbounded authority range)")
    backlinks.add_argument("--source-url-filter", help="Only return backlinks whose referring URL contains this text, e.g. .com.au")
    backlinks.add_argument("--execute", action="store_true")
    backlinks.add_argument("--confirm-credit-cap", type=int)

    candidates = subparsers.add_parser("backlink-candidates", help="Read sanitized candidate evidence awaiting review")
    _add_api_option(candidates)
    candidates.add_argument("analysis_id", nargs="?", help="Omit to read pending candidates across all awaiting-review runs")
    candidates.add_argument("--status", default="awaiting_codex_review")
    candidates.add_argument("--limit", type=int, default=100)

    reviews = subparsers.add_parser("review-backlinks", help="Validate or apply structured backlink decisions")
    _add_api_option(reviews)
    reviews.add_argument("analysis_id")
    decision_input = reviews.add_mutually_exclusive_group(required=True)
    decision_input.add_argument("--decisions-file", type=Path)
    decision_input.add_argument("--decisions-json")
    reviews.add_argument("--execute", action="store_true")

    retry = subparsers.add_parser("retry-job", help="Retry one failed leased job")
    _add_api_option(retry)
    retry.add_argument("job_id")
    retry.add_argument("--execute", action="store_true")

    pause = subparsers.add_parser("pause-outreach", help="Set the global outreach pause")
    _add_api_option(pause)
    pause.add_argument("--reason", default="Operator-requested emergency pause")
    pause.add_argument("--execute", action="store_true")

    resume = subparsers.add_parser(
        "resume-outreach",
        help="Resume only after explicit confirmation and a passing health gate",
    )
    _add_api_option(resume)
    resume.add_argument("--reason", default="Operator-approved outreach resume")
    resume.add_argument("--confirm-sender-health", action="store_true")
    resume.add_argument("--execute", action="store_true")

    migrate = subparsers.add_parser(
        "migrate",
        help="Shadow-plan or execute snapshot-first legacy migration",
    )
    _add_api_option(migrate)
    migrate.add_argument("--execute", action="store_true")
    migrate.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data/migration-snapshots",
    )
    migrate.add_argument("--batch-size", type=int, default=500)
    migrate.add_argument("--no-default-sources", action="store_true")
    migrate.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="LABEL=KIND,PRECEDENCE,PATH",
        help="Add a SQLite migration source",
    )
    migrate.add_argument(
        "--bigquery-manifest",
        type=Path,
        help="Optional aggregate-only JSON manifest; raw BigQuery data is not read",
    )
    return parser


def _parse_source(value: str) -> SourceSpec:
    try:
        label, rest = value.split("=", 1)
        kind, precedence, path = rest.split(",", 2)
        precedence_value = int(precedence)
    except (ValueError, TypeError):
        raise CliUsageError(
            "--source must use LABEL=KIND,PRECEDENCE,PATH"
        ) from None
    if kind not in {"deal_tracker", "current_link_os", "crawler"}:
        raise CliUsageError(
            "--source KIND must be deal_tracker, current_link_os, or crawler"
        )
    if not label.strip() or not path.strip():
        raise CliUsageError("--source label and path cannot be empty")
    return SourceSpec(
        label=label.strip(),
        kind=kind,
        path=Path(path).expanduser(),
        precedence=precedence_value,
    )


def _migration_specs(args: argparse.Namespace) -> list[SourceSpec]:
    specs = [] if args.no_default_sources else default_source_specs()
    specs.extend(_parse_source(value) for value in args.source)
    if args.bigquery_manifest:
        specs.append(
            SourceSpec(
                label="bigquery_historical_aggregate",
                kind="bigquery_aggregate",
                path=args.bigquery_manifest.expanduser(),
                precedence=100,
            )
        )
    if not specs:
        raise CliUsageError("At least one migration source is required")
    labels = [spec.label for spec in specs]
    if len(labels) != len(set(labels)):
        raise CliUsageError("Migration source labels must be unique")
    return specs


def dispatch(args: argparse.Namespace) -> CommandResult:
    api = LinkOsApiClient(args.api_url)
    runner = CommandRunner(api)
    if args.command == "doctor":
        return runner.doctor()
    if args.command == "import":
        kwargs = {
            "source_name": args.source_name,
            "execute": args.execute,
            "queue": not args.import_only,
            "chunk_size": args.chunk_size,
        }
        if args.file is not None:
            return runner.import_file(args.file.expanduser(), **kwargs)
        if args.text is not None:
            return runner.import_text(args.text, **kwargs)
        return runner.import_values(args.value or [], **kwargs)
    if args.command == "reconcile-instantly":
        return runner.reconcile_instantly(
            execute=args.execute,
            pause_legacy=args.pause_legacy,
            full_reconcile=not args.incremental,
            expected_legacy_count=args.expected_legacy_count,
        )
    if args.command == "prepare-campaign":
        return runner.prepare_campaign(
            execute=args.execute,
            target_size=args.target_size,
            public_evidence=args.public_evidence,
            confirm_verification_skipped=args.confirm_verification_skipped,
        )
    if args.command == "activate-campaign":
        return runner.activate_campaign(
            args.batch_id,
            reason=args.reason,
            execute=args.execute,
        )
    if args.command == "campaign-set-estimate":
        return runner.campaign_set_estimate()
    if args.command == "prepare-campaign-set":
        return runner.prepare_campaign_set(execute=args.execute, confirmed=args.confirm_verification_skipped)
    if args.command == "activate-campaign-set":
        return runner.activate_campaign_set(args.launch_id, execute=args.execute)
    if args.command == "pause-campaign":
        return runner.pause_campaign(args.batch_id, reason=args.reason, execute=args.execute)
    if args.command == "reconcile-monday":
        return runner.reconcile_monday(execute=args.execute)
    if args.command == "backlink-analysis":
        return runner.backlink_analysis(
            mode=args.mode,
            client_domain=args.client,
            competitor_domains=args.competitor,
            credit_cap=args.credit_cap,
            authority_floor=args.authority_floor,
            source_url_filter=args.source_url_filter,
            authority_ceiling=args.authority_ceiling,
            execute=args.execute,
            confirmed_credit_cap=args.confirm_credit_cap,
        )
    if args.command == "backlink-candidates":
        return runner.backlink_candidates(args.analysis_id, status=args.status, limit=max(1, min(args.limit, 100)))
    if args.command == "review-backlinks":
        raw = args.decisions_file.expanduser().read_text(encoding="utf-8") if args.decisions_file else args.decisions_json
        try:
            decisions = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            raise CliUsageError(f"Could not read decisions JSON: {exc}") from exc
        if isinstance(decisions, dict):
            decisions = decisions.get("decisions")
        if not isinstance(decisions, list):
            raise CliUsageError("Decisions JSON must be a list or an object with a decisions list")
        return runner.review_backlinks(args.analysis_id, decisions, execute=args.execute)
    if args.command == "retry-job":
        return runner.retry_job(args.job_id, execute=args.execute)
    if args.command == "pause-outreach":
        return runner.pause_outreach(reason=args.reason, execute=args.execute)
    if args.command == "resume-outreach":
        return runner.resume_outreach(
            reason=args.reason,
            execute=args.execute,
            confirm_sender_health=args.confirm_sender_health,
        )
    if args.command == "migrate":
        specs = _migration_specs(args)
        return runner.migrate(
            specs,
            execute=args.execute,
            snapshot_dir=args.snapshot_dir.expanduser(),
            batch_size=args.batch_size,
        )
    raise CliUsageError(f"Unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    command = "unknown"
    indent = None
    try:
        args = parser.parse_args(argv)
        command = args.command
        indent = args.json_indent
        result = dispatch(args)
    except CliUsageError as exc:
        result = CommandResult(command=command, ok=False, mode="rejected")
        result.add_error("invalid_arguments", str(exc))
    except ApiClientError as exc:
        result = CommandResult(command=command, ok=False, mode="rejected")
        result.add_error(exc.code, str(exc))
    print(json.dumps(result.to_dict(), indent=indent, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
