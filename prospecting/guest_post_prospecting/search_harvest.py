from __future__ import annotations

import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .paths import SEARCH_HARVEST_DIR, ensure_project_dirs
from .search import SearchResult, search_bing, search_duckduckgo, search_google
from .universe import enqueue_url, generate_search_queries, start_run, finish_run, record_source, upsert_candidate_url
from .utils import now_iso


HARVEST_FIELDS = [
    "source_engine",
    "query",
    "query_family",
    "query_offset",
    "result_url",
    "root_domain",
    "title",
    "matched_phrase",
    "operator_type",
    "harvested_at",
]


@dataclass(frozen=True)
class HarvestConfig:
    query_offset: int = 0
    query_limit: int = 300
    workers: int = 8
    timeout: int = 3
    pages_per_query: int = 1
    refresh: bool = False
    delay_seconds: float = 0.0
    output: Path | None = None
    engine: str = "google"


def query_family(query: str) -> str:
    lowered = query.lower()
    if any(token in lowered for token in ["advertise", "advertising", "media kit", "media pack", "rate card", "sponsored", "advertorial", "native advertising", "link insertion"]):
        return "commercial"
    if any(token in lowered for token in ["write for us", "guest post", "guest article", "submit", "contribute", "contributor", "author", "editorial"]):
        return "editorial"
    if any(token in lowered for token in ["work with us", "partnership", "collaborat", "pr friendly"]):
        return "routing"
    return "other"


def operator_type(query: str) -> str:
    lowered = query.lower()
    operators = [token for token in ["allinurl:", "inurl:", "allintitle:", "intitle:", "intext:", "site:"] if token in lowered]
    if any(part.startswith("-") for part in lowered.split()):
        operators.append("negative")
    return "|".join(operators) or "phrase"


def harvest_result_row(result: SearchResult, *, query_offset: int, harvested_at: str) -> dict[str, str]:
    return {
        "source_engine": result.search_engine,
        "query": result.source_query,
        "query_family": query_family(result.source_query),
        "query_offset": str(query_offset),
        "result_url": result.result_url,
        "root_domain": result.root_domain,
        "title": result.title,
        "matched_phrase": result.matched_phrase,
        "operator_type": operator_type(result.source_query),
        "harvested_at": harvested_at,
    }


def default_output_path() -> Path:
    SEARCH_HARVEST_DIR.mkdir(parents=True, exist_ok=True)
    slug = now_iso().replace(":", "").replace("+", "Z")
    return SEARCH_HARVEST_DIR / f"search_operator_results_{slug}.csv"


def query_slice(query_offset: int, query_limit: int) -> list[tuple[int, str]]:
    queries = generate_search_queries()
    indexed = list(enumerate(queries))
    sliced = indexed[query_offset:]
    if query_limit:
        sliced = sliced[:query_limit]
    return sliced


def harvest_one_query(
    index_and_query: tuple[int, str],
    *,
    pages_per_query: int,
    timeout: int,
    refresh: bool,
    delay_seconds: float,
    engine: str,
) -> tuple[int, str, list[dict[str, str]], str]:
    query_index, query = index_and_query
    harvested_at = now_iso()
    try:
        if engine == "google":
            results = search_google(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
        elif engine == "bing":
            results = search_bing(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
        elif engine == "duckduckgo":
            results = search_duckduckgo(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout)
        elif engine == "all":
            results = []
            results.extend(search_google(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout))
            results.extend(search_bing(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout))
            results.extend(search_duckduckgo(query, pages=pages_per_query, refresh=refresh, delay_seconds=delay_seconds, timeout=timeout))
        else:
            raise ValueError(f"Unknown search engine: {engine}")
        rows = [harvest_result_row(result, query_offset=query_index, harvested_at=harvested_at) for result in results]
        return query_index, query, rows, ""
    except Exception as exc:
        return query_index, query, [], str(exc)


def append_rows(path: Path, rows: Iterable[dict[str, str]], *, write_header_if_missing: bool = True) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    count = 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HARVEST_FIELDS, extrasaction="ignore")
        if write_header_if_missing and not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def harvest_search_to_csv(config: HarvestConfig) -> dict[str, object]:
    ensure_project_dirs()
    output = config.output or default_output_path()
    queries = query_slice(config.query_offset, config.query_limit)
    if output.exists():
        output.unlink()
    append_rows(output, [])
    started = time.time()
    total_rows = 0
    errors = 0
    completed_queries = 0
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as executor:
        futures = {
            executor.submit(
                harvest_one_query,
                item,
                pages_per_query=config.pages_per_query,
                timeout=config.timeout,
                refresh=config.refresh,
                delay_seconds=config.delay_seconds,
                engine=config.engine,
            ): item
            for item in queries
        }
        for future in as_completed(futures):
            _query_index, _query, rows, error = future.result()
            completed_queries += 1
            if error:
                errors += 1
            if rows:
                total_rows += append_rows(output, rows, write_header_if_missing=False)
    return {
        "output": str(output),
        "queries": len(queries),
        "completed_queries": completed_queries,
        "result_rows": total_rows,
        "errors": errors,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def read_harvest_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def import_harvest_csv(paths: list[Path], *, source_label: str = "search_harvest_csv", reset_crawl: bool = True) -> dict[str, object]:
    run_id = start_run("search_harvest_import", {"paths": [str(path) for path in paths], "source_label": source_label, "reset_crawl": reset_crawl})
    rows_seen = 0
    saved_urls = 0
    queued_urls = 0
    errors: list[str] = []
    try:
        for path in paths:
            try:
                rows = read_harvest_csv(path)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
                continue
            file_saved = 0
            for row in rows:
                rows_seen += 1
                url = (row.get("result_url") or "").strip()
                if not url:
                    continue
                if upsert_candidate_url(
                    url=url,
                    source_type=source_label,
                    source_query=row.get("query", ""),
                    run_id=run_id,
                    matched_phrase=row.get("matched_phrase", ""),
                    title=row.get("title", ""),
                    evidence_score=28 if row.get("matched_phrase") else 12,
                ):
                    saved_urls += 1
                    file_saved += 1
                if enqueue_url(url, row.get("root_domain") or None, reason=source_label, priority=24):
                    queued_urls += 1
                    if reset_crawl:
                        from .db import connect

                        with connect() as connection:
                            connection.execute(
                                """
                                update crawl_queue
                                set status = 'pending', attempts = 0, last_error = '', priority = min(priority, 24), updated_at = ?
                                where url = ?
                                """,
                                (now_iso(), url),
                            )
                            connection.commit()
            record_source(
                run_id=run_id,
                source_type=source_label,
                source_key=str(path),
                source_query=path.name,
                status="ok",
                result_count=file_saved,
                cache_path=str(path),
            )
        finish_run(run_id, "completed", json.dumps({"errors": errors[:20]}))
        return {
            "discovery_run_id": run_id,
            "input_files": len(paths),
            "rows_seen": rows_seen,
            "saved_candidate_urls": saved_urls,
            "queued_urls": queued_urls,
            "errors": errors[:20],
        }
    except Exception as exc:
        finish_run(run_id, "failed", str(exc))
        raise
