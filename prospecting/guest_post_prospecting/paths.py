from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
GENERATED_DIR = ROOT / "generated"
DB_PATH = DATA_DIR / "guest_post_prospecting.db"
RAW_DOMAIN_DIR = DATA_DIR / "raw_domains"
SEARCH_CACHE_DIR = RAW_DOMAIN_DIR / "search_cache"
CRAWL_DIR = DATA_DIR / "crawls"
CONTACT_DISCOVERY_DIR = DATA_DIR / "contact_discovery"
REVIEW_DIR = GENERATED_DIR / "guest_post_prospecting"
SEARCH_HARVEST_DIR = GENERATED_DIR / "search_harvest"
QA_DIR = GENERATED_DIR / "QA"
REVIEW_CSV = REVIEW_DIR / "au_guest_post_candidates_review.csv"


def ensure_project_dirs() -> None:
    for path in [
        DATA_DIR,
        RAW_DOMAIN_DIR,
        SEARCH_CACHE_DIR,
        CRAWL_DIR,
        CONTACT_DISCOVERY_DIR,
        REVIEW_DIR,
        SEARCH_HARVEST_DIR,
        QA_DIR,
        GENERATED_DIR / "campaign_drafts",
        GENERATED_DIR / "instantly_uploads",
    ]:
        path.mkdir(parents=True, exist_ok=True)
