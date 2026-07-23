from __future__ import annotations

import json
from pathlib import Path

import pytest

from deal_tracker.ops.api import ApiClientError, LinkOsApiClient
from deal_tracker.ops.commands import CommandRunner, iter_import_file
from deal_tracker.ops.models import SourceSpec
from link_os_cli import main


class FakeApi:
    def __init__(self, *, safe_to_resume: bool = False, migration_complete: bool = False) -> None:
        self.calls: list[tuple[str, object]] = []
        self.safe_to_resume = safe_to_resume
        self.migration_complete = migration_complete

    def health(self):
        self.calls.append(("health", None))
        return {
            "outreach": {"safe_to_resume": self.safe_to_resume},
            "migration": {"complete": self.migration_complete},
        }

    def create_import(self, values, *, source_name, queue):
        self.calls.append(("import", list(values)))
        return {"import_id": len(self.calls), "accepted": len(values)}

    def reconcile_instantly(
        self,
        *,
        dry_run,
        pause_legacy=False,
        full_reconcile=True,
        expected_legacy_count=13,
    ):
        self.calls.append(("instantly", dry_run))
        return {"dry_run": dry_run, "campaigns": 12}

    def reconcile_monday(self, *, dry_run):
        self.calls.append(("monday", dry_run))
        return {"dry_run": dry_run}

    def prepare_campaign(self, **kwargs):
        self.calls.append(("prepare_campaign", kwargs))
        return {"job_id": "job-1", "status": "queued", **kwargs}

    def activate_campaign(self, batch_id, *, reason):
        self.calls.append(("activate_campaign", (batch_id, reason)))
        return {"batch_id": batch_id, "job_id": "activate-job", "status": "queued"}

    def estimate_campaign_set(self):
        self.calls.append(("campaign_set_estimate", None))
        return {"allowed": True, "required_contacts": 75}

    def prepare_campaign_set(self):
        self.calls.append(("prepare_campaign_set", None))
        return {"id": "launch-1", "job_ids": ["a", "b", "c"]}

    def activate_campaign_set(self, launch_id):
        self.calls.append(("activate_campaign_set", launch_id))
        return {"id": launch_id, "job_ids": ["a", "b", "c"]}

    def pause_campaign(self, batch_id, *, reason):
        self.calls.append(("pause_campaign", (batch_id, reason)))
        return {"id": batch_id, "status": "paused"}

    def retry_job(self, job_id):
        self.calls.append(("retry", job_id))
        return {"status": "queued"}

    def set_outreach_pause(self, *, paused, reason, confirm_sender_health=False):
        self.calls.append(("outreach", (paused, reason, confirm_sender_health)))
        return {"paused": paused}

    def estimate_backlink_analysis(self, **kwargs):
        self.calls.append(("backlink_estimate", kwargs))
        return {**kwargs, "allowed": True, "predicted_credits": kwargs["credit_cap"], "maximum_paid_records": kwargs["credit_cap"], "monthly_usage": 0, "monthly_cap": 10000, "monthly_remaining": 10000, "blocking_reasons": [], "targets": [], "confirmation_required": True}

    def create_backlink_analysis(self, **kwargs):
        self.calls.append(("backlink_create", kwargs))
        return {"id": "analysis-1", "status": "queued", "estimated_credits": kwargs["credit_cap"]}

    def backlink_candidates(self, analysis_id, *, status, limit):
        self.calls.append(("backlink_candidates", analysis_id))
        return {"items": [{"id": "candidate-1", "domain": "publisher.com", "status": status, "evidence": [{"source_url": "https://publisher.com/story"}]}], "count": 1, "run_status": "awaiting_codex_review"}

    def backlink_analyses(self, *, status, limit):
        self.calls.append(("backlink_analyses", status))
        return {"items": [{"id": "analysis-1", "status": status}], "count": 1}

    def review_backlinks(self, analysis_id, decisions):
        self.calls.append(("review_backlinks", analysis_id))
        return {"applied": len(decisions), "approved": 0, "status": "awaiting_codex_review"}


def test_api_client_blocks_remote_hosts() -> None:
    with pytest.raises(ApiClientError) as exc_info:
        LinkOsApiClient("https://example.com")
    assert exc_info.value.code == "remote_api_blocked"


def test_doctor_treats_unmounted_sources_as_recovery_warning_after_migration(
    tmp_path: Path,
) -> None:
    source = SourceSpec(
        label="legacy-required",
        kind="deal_tracker",
        path=tmp_path / "not-mounted.sqlite3",
        precedence=1,
        required=True,
    )
    result = CommandRunner(
        FakeApi(migration_complete=True), source_specs=[source]
    ).doctor()

    assert result.ok is True
    assert result.data["missing_recovery_sources"] == ["legacy-required"]
    assert "missing_required_sources" not in result.data
    assert result.warnings


def test_doctor_still_fails_if_required_sources_are_missing_before_migration(
    tmp_path: Path,
) -> None:
    source = SourceSpec(
        label="legacy-required",
        kind="deal_tracker",
        path=tmp_path / "missing.sqlite3",
        precedence=1,
        required=True,
    )
    result = CommandRunner(FakeApi(), source_specs=[source]).doctor()

    assert result.ok is False
    assert result.data["missing_required_sources"] == ["legacy-required"]


def test_global_manual_review_search_scans_completed_analyses() -> None:
    api = FakeApi()
    result = CommandRunner(api, source_specs=[]).backlink_candidates(
        None,
        status="manual_review",
        limit=100,
    )

    assert result.ok is True
    assert ("backlink_analyses", "") in api.calls
    assert result.data["items"][0]["status"] == "manual_review"


def test_import_is_dry_run_by_default_and_chunks_when_executed() -> None:
    api = FakeApi()
    runner = CommandRunner(api, source_specs=[])

    dry_run = runner.import_values(
        (f"example-{index}.com" for index in range(5)),
        source_name="test",
        execute=False,
        queue=True,
        chunk_size=2,
    )
    assert dry_run.ok is True
    assert dry_run.data["submitted_values"] == 5
    assert dry_run.data["batch_count"] == 3
    assert api.calls == []

    executed = runner.import_values(
        (f"example-{index}.com" for index in range(5)),
        source_name="test",
        execute=True,
        queue=True,
        chunk_size=2,
    )
    assert executed.ok is True
    assert [len(call[1]) for call in api.calls if call[0] == "import"] == [2, 2, 1]


def test_csv_import_streams_recognised_fields(tmp_path: Path) -> None:
    source = tmp_path / "domains.csv"
    source.write_text(
        "domain,contact_email,ignored\nexample.com,editor@example.com,x\n"
        "example.org,,y\n",
        encoding="utf-8",
    )
    assert list(iter_import_file(source)) == [
        "example.com",
        "editor@example.com",
        "example.org",
    ]


def test_reconcile_defaults_to_read_only_api_mode() -> None:
    api = FakeApi()
    runner = CommandRunner(api, source_specs=[])
    result = runner.reconcile_instantly(execute=False)
    assert result.ok is True
    assert ("instantly", True) in api.calls
    assert result.mode == "dry_run"


def test_public_evidence_campaign_requires_confirmation_and_stays_prepare_only() -> None:
    api = FakeApi()
    runner = CommandRunner(api, source_specs=[])
    blocked = runner.prepare_campaign(
        execute=True,
        public_evidence=True,
        confirm_verification_skipped=False,
    )
    assert blocked.ok is False
    assert all(call[0] != "prepare_campaign" for call in api.calls)

    executed = runner.prepare_campaign(
        execute=True,
        public_evidence=True,
        confirm_verification_skipped=True,
    )
    assert executed.ok is True
    call = [item for item in api.calls if item[0] == "prepare_campaign"][-1]
    assert call[1]["contact_policy"] == "public_evidence_verification_skipped"
    assert call[1]["prepare_only"] is True


def test_campaign_activation_is_dry_run_by_default() -> None:
    api = FakeApi()
    runner = CommandRunner(api, source_specs=[])
    preview = runner.activate_campaign(
        "batch-1",
        reason="approved pilot",
        execute=False,
    )
    assert preview.ok is True
    assert all(call[0] != "activate_campaign" for call in api.calls)


def test_segmented_campaign_set_requires_confirmation_and_is_dry_run_first() -> None:
    api = FakeApi()
    runner = CommandRunner(api, source_specs=[])
    preview = runner.prepare_campaign_set(execute=False, confirmed=False)
    assert preview.ok is True
    assert ("campaign_set_estimate", None) in api.calls
    assert all(call[0] != "prepare_campaign_set" for call in api.calls)

    blocked = runner.prepare_campaign_set(execute=True, confirmed=False)
    assert blocked.ok is False
    executed = runner.prepare_campaign_set(execute=True, confirmed=True)
    assert executed.ok is True
    assert ("prepare_campaign_set", None) in api.calls

    activation_preview = runner.activate_campaign_set("launch-1", execute=False)
    assert activation_preview.ok is True
    assert all(call[0] != "activate_campaign_set" for call in api.calls)

    executed = runner.activate_campaign(
        "batch-1",
        reason="approved pilot",
        execute=True,
    )
    assert executed.ok is True
    assert ("activate_campaign", ("batch-1", "approved pilot")) in api.calls


def test_outreach_resume_is_fail_closed() -> None:
    api = FakeApi(safe_to_resume=False)
    runner = CommandRunner(api, source_specs=[])

    missing_confirmation = runner.resume_outreach(
        reason="test",
        execute=True,
        confirm_sender_health=False,
    )
    assert missing_confirmation.ok is False
    assert api.calls == []

    failed_health = runner.resume_outreach(
        reason="test",
        execute=True,
        confirm_sender_health=True,
    )
    assert failed_health.ok is False
    assert all(call[0] != "outreach" for call in api.calls)


def test_outreach_resume_requires_positive_live_health() -> None:
    api = FakeApi(safe_to_resume=True)
    runner = CommandRunner(api, source_specs=[])
    result = runner.resume_outreach(
        reason="approved pilot",
        execute=True,
        confirm_sender_health=True,
    )
    assert result.ok is True
    assert ("outreach", (False, "approved pilot", True)) in api.calls


def test_cli_prints_structured_json_for_dry_run(capsys) -> None:
    exit_code = main(["import", "--value", "example.com"])
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["command"] == "import"
    assert output["mode"] == "dry_run"
    assert output["data"]["submitted_values"] == 1


def test_backlink_analysis_is_dry_run_and_exact_confirmation_is_required() -> None:
    api = FakeApi()
    runner = CommandRunner(api, source_specs=[])
    preview = runner.backlink_analysis(mode="competitor_prospecting", client_domain="client.com", competitor_domains=["competitor.com"], credit_cap=500, authority_floor=20, execute=False, confirmed_credit_cap=None)
    assert preview.ok is True
    assert api.calls[-1][1]["authority_ceiling"] == 80
    assert all(call[0] != "backlink_create" for call in api.calls)

    blocked = runner.backlink_analysis(mode="competitor_prospecting", client_domain="client.com", competitor_domains=["competitor.com"], credit_cap=500, authority_floor=20, execute=True, confirmed_credit_cap=499)
    assert blocked.ok is False
    assert all(call[0] != "backlink_create" for call in api.calls)

    executed = runner.backlink_analysis(mode="competitor_prospecting", client_domain="client.com", competitor_domains=["competitor.com"], credit_cap=500, authority_floor=20, execute=True, confirmed_credit_cap=500)
    assert executed.ok is True
    assert any(call[0] == "backlink_create" for call in api.calls)

    filtered = runner.backlink_analysis(mode="competitor_prospecting", client_domain="client.com.au", competitor_domains=["competitor.com"], credit_cap=500, authority_floor=20, execute=True, confirmed_credit_cap=500, source_url_filter=".com.au")
    assert filtered.ok is True
    filtered_create = [call for call in api.calls if call[0] == "backlink_create"][-1]
    assert filtered_create[1]["source_url_filter"] == ".com.au"
