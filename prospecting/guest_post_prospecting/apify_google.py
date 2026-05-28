from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .classifier import classify_opportunity
from .paths import ROOT, SEARCH_HARVEST_DIR, ensure_project_dirs
from .search_harvest import HARVEST_FIELDS, append_rows, default_output_path, operator_type, query_family, query_slice
from .utils import clean_domain, is_australian_candidate_domain, normalize_url, now_iso, root_domain_from_url


ACTOR_ID = "nFJndFXA5zjCTuudP"
APIFY_SYNC_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


@dataclass(frozen=True)
class ApifyGoogleHarvestConfig:
    query_offset: int = 0
    query_limit: int = 25
    pages_per_query: int = 1
    batch_size: int = 5
    output: Path | None = None
    raw_output: Path | None = None
    country_code: str = "au"
    language_code: str = "en"
    wait_timeout_seconds: int = 180
    delay_seconds: float = 0.0


def load_env_value(key: str, env_path: Path | None = None) -> str:
    path = env_path or ROOT / ".env"
    if not path.exists():
        return ""
    prefix = f"{key}="
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value
    return ""


def default_raw_output_path() -> Path:
    SEARCH_HARVEST_DIR.mkdir(parents=True, exist_ok=True)
    slug = now_iso().replace(":", "").replace("+", "Z")
    return SEARCH_HARVEST_DIR / f"apify_google_raw_{slug}.json"


def apify_actor_input(queries: list[str], config: ApifyGoogleHarvestConfig) -> dict[str, Any]:
    return {
        "queries": "\n".join(queries),
        "maxPagesPerQuery": config.pages_per_query,
        "countryCode": config.country_code,
        "languageCode": config.language_code,
    }


def run_apify_google_search(queries: list[str], config: ApifyGoogleHarvestConfig, token: str) -> list[dict[str, Any]]:
    params = urlencode({"token": token, "timeout": str(config.wait_timeout_seconds), "clean": "true"})
    url = f"{APIFY_SYNC_URL.format(actor=ACTOR_ID)}?{params}"
    body = json.dumps(apify_actor_input(queries, config)).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urlopen(request, timeout=config.wait_timeout_seconds + 30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    data = json.loads(payload or "[]")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def item_query(item: dict[str, Any], fallback: str) -> str:
    search_query = item.get("searchQuery")
    if isinstance(search_query, dict):
        for key in ("term", "query", "value"):
            if search_query.get(key):
                return str(search_query[key])
    for key in ("query", "searchTerm", "keyword"):
        if item.get(key):
            return str(item[key])
    return fallback


def organic_results(item: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("organicResults", "organic_results", "results"):
        value = item.get(key)
        if isinstance(value, list):
            return [result for result in value if isinstance(result, dict)]
    return []


def result_url(result: dict[str, Any]) -> str:
    for key in ("url", "link", "displayedUrl"):
        value = result.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return normalize_url(value)
    return ""


def result_title(result: dict[str, Any]) -> str:
    for key in ("title", "name"):
        value = result.get(key)
        if value:
            return str(value)
    return ""


def result_snippet(result: dict[str, Any]) -> str:
    for key in ("description", "snippet", "text"):
        value = result.get(key)
        if value:
            return str(value)
    return ""


def apify_items_to_rows(items: list[dict[str, Any]], *, fallback_query: str, query_offsets: dict[str, int]) -> list[dict[str, str]]:
    harvested_at = now_iso()
    rows: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in items:
        query = item_query(item, fallback_query)
        for result in organic_results(item):
            url = result_url(result)
            if not url or url in seen_urls:
                continue
            root_domain = clean_domain(root_domain_from_url(url))
            title = result_title(result)
            snippet = result_snippet(result)
            if not is_australian_candidate_domain(root_domain, f"{query} {title} {snippet} {url}"):
                continue
            seen_urls.add(url)
            match = classify_opportunity(f"{query} {title} {snippet}", url)
            rows.append(
                {
                    "source_engine": "apify_google",
                    "query": query,
                    "query_family": query_family(query),
                    "query_offset": str(query_offsets.get(query, "")),
                    "result_url": url,
                    "root_domain": root_domain,
                    "title": title[:250],
                    "matched_phrase": match.matched_phrase,
                    "operator_type": operator_type(query),
                    "harvested_at": harvested_at,
                }
            )
    return rows


def batched(items: list[tuple[int, str]], size: int) -> list[list[tuple[int, str]]]:
    size = max(1, size)
    return [items[index : index + size] for index in range(0, len(items), size)]


def harvest_apify_google_to_csv(config: ApifyGoogleHarvestConfig) -> dict[str, object]:
    ensure_project_dirs()
    token = load_env_value("APIFY_API_TOKEN") or load_env_value("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is missing from this repo's .env")
    output = config.output or default_output_path()
    raw_output = config.raw_output or default_raw_output_path()
    if output.exists():
        output.unlink()
    append_rows(output, [])

    query_items = query_slice(config.query_offset, config.query_limit)
    all_raw_items: list[dict[str, Any]] = []
    total_rows = 0
    errors: list[str] = []
    started = time.time()
    for batch in batched(query_items, config.batch_size):
        queries = [query for _index, query in batch]
        offsets = {query: index for index, query in batch}
        try:
            raw_items = run_apify_google_search(queries, config, token)
            all_raw_items.extend(raw_items)
            rows = apify_items_to_rows(raw_items, fallback_query=queries[0] if len(queries) == 1 else "", query_offsets=offsets)
            total_rows += append_rows(output, rows, write_header_if_missing=False)
        except Exception as exc:
            errors.append(f"{queries[0]}: {exc}")
        if config.delay_seconds:
            time.sleep(config.delay_seconds)

    raw_output.parent.mkdir(parents=True, exist_ok=True)
    raw_output.write_text(json.dumps(all_raw_items, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "output": str(output),
        "raw_output": str(raw_output),
        "queries": len(query_items),
        "result_rows": total_rows,
        "errors": len(errors),
        "error_samples": errors[:5],
        "elapsed_seconds": round(time.time() - started, 2),
    }
