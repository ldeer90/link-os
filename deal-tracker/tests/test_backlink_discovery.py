from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from deal_tracker.backlink_discovery import (
    DEFAULT_AUTHORITY_CEILING,
    BacklinkValidationError,
    apply_reviews,
    estimate_analysis,
    run_analysis_job,
    request_hash,
    target_allocations,
    mark_import_complete,
)
from deal_tracker.integrations.seranking import CreditStateUnknown, SERankingClient
from deal_tracker.platform.enums import (
    BacklinkAnalysisMode,
    BacklinkAnalysisStatus,
    BacklinkCandidateStatus,
)
from deal_tracker.platform.models import (
    BacklinkAnalysisRun,
    BacklinkAnalysisTarget,
    BacklinkCandidate,
    BacklinkProviderRequest,
    Base,
    ImportBatch,
    Job,
)
from deal_tracker.platform.repositories import JobQueue


class FakeProvider:
    configured = True

    class Subscription:
        balance = 10_000

    def subscription(self):
        return self.Subscription()

    def referring_domains_count(self, target_domain):
        assert target_domain in {"competitor-example.com", "client-example.com"}
        return 10

    def backlinks(self, target_domain, *, limit, authority_floor, since=None):
        assert limit == 2
        assert authority_floor == 20
        return [
            {
                "source_domain": "editorial-example.com",
                "source_url": "https://editorial-example.com/news/story",
                "target_url": f"https://{target_domain}/guide",
                "page_title": "Industry news and editorial guide",
                "anchor": "useful guide",
                "nofollow": False,
                "domain_inlink_rank": 52,
            },
            {
                "source_domain": "facebook.com",
                "source_url": "https://facebook.com/profile",
                "target_url": f"https://{target_domain}",
                "domain_inlink_rank": 99,
            },
        ]


def make_run(session, *, mode=BacklinkAnalysisMode.COMPETITOR_PROSPECTING):
    run = BacklinkAnalysisRun(
        mode=mode,
        status=BacklinkAnalysisStatus.QUEUED,
        client_domain="client-example.com",
        competitor_domains=["competitor-example.com"] if mode == BacklinkAnalysisMode.COMPETITOR_PROSPECTING else [],
        requested_by="test",
        credit_cap=4,
        confirmed_credit_cap=4,
        authority_floor=20,
        monthly_credit_cap=10_000,
        estimated_credits=4,
    )
    session.add(run)
    session.flush()
    target = BacklinkAnalysisTarget(
        run_id=run.id,
        target_domain="competitor-example.com" if mode == BacklinkAnalysisMode.COMPETITOR_PROSPECTING else "client-example.com",
        target_role="competitor" if mode == BacklinkAnalysisMode.COMPETITOR_PROSPECTING else "client",
        allocated_credits=2,
        request_hash=f"request-{run.id}",
    )
    session.add(target)
    session.flush()
    job, _ = JobQueue(session).enqueue(
        kind="backlink_analysis",
        idempotency_key=f"backlinks-{run.id}",
        subject_type="backlink_analysis",
        subject_id=run.id,
        payload={"run_id": run.id},
        max_attempts=1,
    )
    session.commit()
    return run.id, job.id


def test_credit_cap_is_divided_without_exceeding_total() -> None:
    assert DEFAULT_AUTHORITY_CEILING == 80
    allocations = target_allocations(
        BacklinkAnalysisMode.COMPETITOR_PROSPECTING,
        "client.com",
        ["one.com", "two.com", "three.com"],
        500,
    )
    assert [item[2] for item in allocations] == [167, 167, 166]
    assert sum(item[2] for item in allocations) == 500


def test_estimate_reserves_count_credits_inside_confirmed_cap(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'count-budget.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        estimate = estimate_analysis(
            session,
            mode="competitor_prospecting",
            client_domain="client.com",
            competitor_domains=["one.com", "two.com"],
            credit_cap=500,
            authority_floor=20,
            refresh_balance=False,
            provider=FakeProvider(),
        )

    assert estimate["predicted_credits"] == 500
    assert estimate["count_credit_cost"] == 4
    assert estimate["maximum_paid_records"] == 496
    assert [target["retrieval_credit_cap"] for target in estimate["targets"]] == [248, 248]


def test_source_filter_is_canonical_and_has_a_distinct_cache_key(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'source-filter.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        estimate = estimate_analysis(
            session,
            mode="competitor_prospecting",
            client_domain="client.com.au",
            competitor_domains=["competitor.com"],
            credit_cap=250,
            authority_floor=20,
            source_url_filter="  .COM.AU  ",
            refresh_balance=False,
            provider=FakeProvider(),
        )

    assert estimate["source_url_filter"] == ".com.au"
    assert estimate["targets"][0]["source_url_filter"] == ".com.au"
    assert estimate["targets"][0]["endpoint"] == "/v1/backlinks/all"
    assert request_hash("competitor.com", 248, 20) != request_hash(
        "competitor.com", 248, 20, source_url_filter=".com.au"
    )
    assert request_hash("competitor.com", 248, 0, source_url_filter=".com.au", authority_ceiling=19) != request_hash(
        "competitor.com", 248, 20, source_url_filter=".com.au"
    )


@pytest.mark.parametrize(
    ("returned_count", "expected_endpoint"),
    [
        (10, "/v1/backlinks/all"),
        (9, "/v1/backlinks/refdomains/history"),
    ],
)
def test_larger_run_expands_a_truncated_snapshot_before_incremental_refresh(
    tmp_path, returned_count, expected_endpoint
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / f'checkpoint-{returned_count}.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        run = BacklinkAnalysisRun(
            mode=BacklinkAnalysisMode.COMPETITOR_PROSPECTING,
            status=BacklinkAnalysisStatus.COMPLETE,
            client_domain="client.com",
            competitor_domains=["competitor.com"],
            requested_by="test",
            credit_cap=10,
            confirmed_credit_cap=10,
            authority_floor=20,
            monthly_credit_cap=10_000,
            estimated_credits=10,
        )
        session.add(run)
        session.flush()
        target = BacklinkAnalysisTarget(
            run_id=run.id,
            target_domain="competitor.com",
            target_role="competitor",
            allocated_credits=10,
            status="complete",
            request_hash=f"pilot-{returned_count}",
            returned_count=returned_count,
            actual_credits=returned_count,
            checkpoint_date=date(2026, 7, 20),
        )
        session.add(target)
        session.flush()
        session.add(
            BacklinkProviderRequest(
                run_id=run.id,
                target_id=target.id,
                endpoint="/v1/backlinks/all",
                request_hash=target.request_hash,
                status="complete",
                predicted_credits=10,
                actual_credits=returned_count,
                completed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
        )
        session.commit()

        estimate = estimate_analysis(
            session,
            mode="competitor_prospecting",
            client_domain="client.com",
            competitor_domains=["competitor.com"],
            credit_cap=500,
            authority_floor=20,
            refresh_balance=False,
            provider=FakeProvider(),
        )

    assert estimate["targets"][0]["endpoint"] == expected_endpoint


def test_worker_filters_locally_and_review_creates_one_canonical_import(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'backlinks.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        run_id, job_id = make_run(session)

    monkeypatch.setattr("deal_tracker.backlink_discovery.session_factory", lambda: Session)
    monkeypatch.setattr("deal_tracker.backlink_discovery.platform_engine", lambda: engine)
    monkeypatch.setenv("LINK_OS_DATA_DIR", str(tmp_path / "data"))
    result = run_analysis_job(job_id, provider=FakeProvider())
    assert result["returned"] == 2
    assert result["total_referring_domains"] == 10
    assert result["coverage_percent"] == 20.0
    assert result["pending_review"] == 1
    assert result["excluded"] == 1

    with Session() as session:
        candidate = session.scalar(select(BacklinkCandidate).where(BacklinkCandidate.status == BacklinkCandidateStatus.AWAITING_CODEX_REVIEW))
        assert candidate.normalized_domain == "editorial-example.com"
        decision = {
            "candidate_id": candidate.id,
            "decision": "approved",
            "confidence": 0.9,
            "reason_codes": ["editorial_relevance"],
            "summary": "Public news page demonstrates editorial relevance.",
        }
        first = apply_reviews(session, run_id=run_id, decisions=[decision], actor_type="codex", actor_id="test")
        second = apply_reviews(session, run_id=run_id, decisions=[decision], actor_type="codex", actor_id="test")
        session.commit()
        assert first["import_id"]
        assert second["import_id"] == first["import_id"]
        assert session.scalar(select(func.count(ImportBatch.id))) == 1
        assert session.scalar(select(func.count(Job.id)).where(Job.kind == "process_import")) == 1


def test_client_profile_audit_cannot_queue_outreach(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        run_id, job_id = make_run(session, mode=BacklinkAnalysisMode.CLIENT_PROFILE_AUDIT)
    monkeypatch.setattr("deal_tracker.backlink_discovery.session_factory", lambda: Session)
    monkeypatch.setattr("deal_tracker.backlink_discovery.platform_engine", lambda: engine)
    run_analysis_job(job_id, provider=FakeProvider())

    with Session() as session:
        candidate = session.scalar(select(BacklinkCandidate).where(BacklinkCandidate.status == BacklinkCandidateStatus.AUDIT_ONLY))
        with pytest.raises(BacklinkValidationError, match="cannot approve"):
            apply_reviews(session, run_id=run_id, decisions=[{
                "candidate_id": candidate.id,
                "decision": "approved",
                "confidence": 0.9,
                "reason_codes": ["editorial_relevance"],
                "summary": "Should remain audit only.",
            }], actor_type="user", actor_id="test")
        assert session.scalar(select(func.count(ImportBatch.id))) == 0


def test_hard_exclusions_cannot_be_overridden_by_an_approval(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'protected.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        run_id, _job_id = make_run(session)
        candidate = BacklinkCandidate(run_id=run_id, normalized_domain="facebook.com", status=BacklinkCandidateStatus.HARD_REJECTED, reason_codes=["platform_or_ugc_host"])
        session.add(candidate)
        session.commit()
        with pytest.raises(BacklinkValidationError, match="hard-excluded"):
            apply_reviews(session, run_id=run_id, decisions=[{
                "candidate_id": candidate.id,
                "decision": "approved",
                "confidence": 0.99,
                "reason_codes": ["manual_override"],
                "summary": "Attempted protected override.",
            }], actor_type="user", actor_id="test")


def test_completed_backlink_import_refreshes_run_counters(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'counter-refresh.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        run_id, _job_id = make_run(session)
        import_batch = ImportBatch(source_type="backlink_discovery", source_metadata={"backlink_analysis_id": run_id})
        session.add(import_batch)
        session.flush()
        candidate = BacklinkCandidate(run_id=run_id, normalized_domain="publisher.com", status=BacklinkCandidateStatus.IMPORT_QUEUED, reason_codes=["editorial"], import_id=import_batch.id)
        session.add(candidate)
        session.commit()

        mark_import_complete(session, import_batch)
        session.commit()
        run = session.get(BacklinkAnalysisRun, run_id)
        assert run.counters["imported"] == 1
        assert run.counters["scrape_queued"] == 1


def test_partial_backlink_import_returns_run_to_codex_review(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'partial-review.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        run_id, _job_id = make_run(session)
        import_batch = ImportBatch(
            source_type="backlink_discovery",
            source_metadata={"backlink_analysis_id": run_id},
        )
        session.add(import_batch)
        session.flush()
        session.add_all([
            BacklinkCandidate(
                run_id=run_id,
                normalized_domain="imported-publisher.com",
                status=BacklinkCandidateStatus.IMPORT_QUEUED,
                reason_codes=["editorial"],
                import_id=import_batch.id,
            ),
            BacklinkCandidate(
                run_id=run_id,
                normalized_domain="pending-publisher.com",
                status=BacklinkCandidateStatus.AWAITING_CODEX_REVIEW,
                reason_codes=["requires_publisher_relevance_review"],
            ),
        ])
        session.commit()

        mark_import_complete(session, import_batch)
        session.commit()
        run = session.get(BacklinkAnalysisRun, run_id)
        assert run.status == BacklinkAnalysisStatus.AWAITING_CODEX_REVIEW
        assert run.completed_at is None
        assert run.counters["pending_review"] == 1


def test_adapter_retries_provider_5xx_but_never_replays_transport_unknown(monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"data": []}, request=request)

    monkeypatch.setattr("deal_tracker.integrations.seranking.time.sleep", lambda _seconds: None)
    client = SERankingClient(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=2)
    assert client.backlinks("example.com", limit=1) == []
    assert calls == 2

    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("lost after transmission")

    unsafe = SERankingClient(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(broken)), max_retries=2)
    with pytest.raises(CreditStateUnknown):
        unsafe.backlinks("example.com", limit=1)


def test_subscription_balance_and_incremental_refdomains_are_parsed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/account/subscription"):
            return httpx.Response(200, json={"subscription_info": {"status": "active", "units_left": 4321}}, request=request)
        if request.url.path.endswith("/backlinks/refdomains/count"):
            return httpx.Response(200, json={"metrics": [{"target": "competitor.com", "refdomains": 1379}]}, request=request)
        return httpx.Response(200, json={"new_lost_refdomains": [{"refdomain": "publisher.com", "dofollow_backlinks": 2, "domain_inlink_rank": 44}]}, request=request)

    client = SERankingClient(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.subscription().balance == 4321
    assert client.referring_domains_count("competitor.com") == 1379
    rows = client.backlinks("competitor.com", limit=10, since="2026-06-01")
    assert rows[0]["source_domain"] == "publisher.com"
    assert rows[0]["nofollow"] is False
    assert requests[-1].url.path.endswith("/backlinks/refdomains/history")
    assert requests[-1].url.params["new_lost_type"] == "new"


def test_backlink_adapter_passes_referring_url_contains_filter() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []}, request=request)

    client = SERankingClient(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.backlinks("competitor.com", limit=248, authority_floor=0, authority_ceiling=19, source_url_filter=".com.au") == []
    assert requests[-1].url.params["url_from_filter"] == ".com.au"
    assert requests[-1].url.params["url_from_filter_mode"] == "contains"
    assert requests[-1].url.params["domain_inlink_rank_from"] == "0"
    assert requests[-1].url.params["domain_inlink_rank_to"] == "19"
