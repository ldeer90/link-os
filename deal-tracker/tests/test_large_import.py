from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from deal_tracker.platform.models import Base, Domain, ImportBatch, Job
from deal_tracker.platform.repositories import JobQueue
from deal_tracker.worker import WorkerConfig, process_import_job


@pytest.mark.skipif(
    os.getenv("RUN_LINK_OS_LARGE_IMPORT_TEST") != "1",
    reason="set RUN_LINK_OS_LARGE_IMPORT_TEST=1 for the 100,000-row acceptance test",
)
def test_100k_row_import_streams_and_deduplicates_jobs(tmp_path, monkeypatch) -> None:
    source = tmp_path / "domains.txt"
    with source.open("w", encoding="utf-8") as handle:
        for index in range(100_000):
            handle.write(f"publisher-{index % 100}.com\n")

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'large-import.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    worker_id = "large-import-test"

    with Session() as session:
        import_batch = ImportBatch(source_type="file", filename=source.name)
        session.add(import_batch)
        session.flush()
        queued, _created = JobQueue(session).enqueue(
            kind="process_import",
            idempotency_key="large-import-100k",
            subject_type="import",
            subject_id=import_batch.id,
            payload={
                "import_id": import_batch.id,
                "path": str(source),
                "queue": True,
            },
        )
        session.commit()
        leased = JobQueue(session).lease(worker_id=worker_id)
        assert len(leased) == 1 and leased[0].id == queued.id
        session.commit()
        job_id = queued.id
        import_id = import_batch.id

    monkeypatch.setattr("deal_tracker.worker.session_factory", lambda: Session)
    config = WorkerConfig(import_chunk_size=1_000)
    first = process_import_job(job_id, worker_id, config)
    second = process_import_job(job_id, worker_id, config)

    assert first == {"accepted": 100, "duplicate": 99_900}
    assert second == first
    with Session() as session:
        batch = session.get(ImportBatch, import_id)
        assert batch is not None
        assert batch.total_rows == 100_000
        assert batch.processed_rows == 100_000
        assert batch.accepted_count == 100
        assert batch.duplicate_count == 99_900
        assert session.scalar(select(func.count(Domain.id))) == 100
        assert session.scalar(
            select(func.count(Job.id)).where(Job.kind == "scrape_domain")
        ) == 100
