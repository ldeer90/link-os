from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .env import load_env_value


BASE_URL = "https://api.instantly.ai/api/v2"
SCOPED_CAMPAIGN_IDS = [
    "f6b0f05e-d85c-41a3-b3d6-c261cfca1f7a",
    "d122a73e-d0d3-4038-a656-a825bec49c36",
    "b081e627-c2bd-4298-b949-ea183ac732f3",
    "ac0c5f0c-e230-470f-8065-2de543526ef7",
    "97dc779e-4eb5-4727-9346-5215fd7200c4",
    "945b0afd-7c53-492d-9afe-4c44f57173ad",
    "91ef27e3-af46-41d7-8211-908d8cd34262",
    "7bc7d5e3-924d-463b-b875-8e8301be264b",
    "48508c88-901d-4ba7-963b-031514a9ef73",
    "1d924dab-2fb9-4d2c-89f3-0744ebe9b065",
    "15d106b2-6205-44bd-b595-825c5fff90ce",
    "7c9bcfdd-da4b-4da8-9563-27561d8c100a",
]
ACTIVE_CAMPAIGN_ID = "7bc7d5e3-924d-463b-b875-8e8301be264b"
PRIORITY_CAMPAIGN_ID = "48508c88-901d-4ba7-963b-031514a9ef73"
ARTICLE_SUBMISSION_CAMPAIGN_ID = "91ef27e3-af46-41d7-8211-908d8cd34262"
ARTICLE_SUBMISSION_SECONDARY_CAMPAIGN_ID = "15d106b2-6205-44bd-b595-825c5fff90ce"
OLD_CAMPAIGN_ID = "945b0afd-7c53-492d-9afe-4c44f57173ad"
DEFAULT_CAMPAIGN_IDS = SCOPED_CAMPAIGN_IDS[:]
GUEST_POST_CAMPAIGN_NAME_MARKERS = (
    "article submission info request",
    "guest post",
    "article submission",
    "paid link",
    "publisher",
    "placement",
)


def api_token() -> str:
    token = load_env_value("INSTANTLY_API_KEY")
    if not token:
        raise RuntimeError("INSTANTLY_API_KEY is missing from the environment or this project's .env")
    return token


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "GuestPostDealTracker/1.0 (+https://ldsearch.com.au)",
    }


def request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    qs = f"?{urlencode(params)}" if params else ""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(f"{BASE_URL}{path}{qs}", data=data, method=method, headers=headers())
    attempts = 4
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8") or "{}")
        except HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < attempts:
                time.sleep(15 * attempt)
                continue
            raise RuntimeError(f"Instantly {method} {path} failed with HTTP {exc.code}: {payload[:1000]}") from exc
    raise RuntimeError(f"Instantly {method} {path} failed after {attempts} attempts")


def list_campaign_emails(campaign_id: str) -> list[dict[str, Any]]:
    emails: list[dict[str, Any]] = []
    starting_after = ""
    while True:
        params: dict[str, Any] = {"campaign_id": campaign_id, "limit": 100, "sort_order": "desc"}
        if starting_after:
            params["starting_after"] = starting_after
        payload = request("/emails", params=params)
        batch = payload.get("items") or []
        emails.extend(item for item in batch if isinstance(item, dict))
        starting_after = payload.get("next_starting_after") or ""
        if not starting_after or not batch:
            return emails


def list_campaign_leads(campaign_id: str) -> dict[str, dict[str, Any]]:
    leads: dict[str, dict[str, Any]] = {}
    starting_after = ""
    while True:
        body: dict[str, Any] = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        payload = request("/leads/list", method="POST", body=body)
        batch = payload.get("items") or []
        for item in batch:
            if isinstance(item, dict) and item.get("email"):
                leads[str(item["email"]).lower()] = item
        starting_after = payload.get("next_starting_after") or ""
        if not starting_after or not batch:
            return leads


def list_campaigns() -> list[dict[str, Any]]:
    campaigns: list[dict[str, Any]] = []
    starting_after = ""
    while True:
        params: dict[str, Any] = {"limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        payload = request("/campaigns", params=params)
        batch = payload.get("items") or []
        campaigns.extend(item for item in batch if isinstance(item, dict))
        starting_after = payload.get("next_starting_after") or ""
        if not starting_after or not batch:
            return campaigns


def matching_guest_post_campaigns(campaigns: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    items = campaigns if campaigns is not None else list_campaigns()
    matches: list[dict[str, Any]] = []
    for campaign in items:
        name = str(campaign.get("name") or "").strip()
        lowered = name.lower()
        if any(marker in lowered for marker in GUEST_POST_CAMPAIGN_NAME_MARKERS):
            matches.append(campaign)
    return matches
