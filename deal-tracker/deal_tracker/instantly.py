from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .env import load_env_value


BASE_URL = "https://api.instantly.ai/api/v2"
ACTIVE_CAMPAIGN_ID = "7bc7d5e3-924d-463b-b875-8e8301be264b"
PRIORITY_CAMPAIGN_ID = "48508c88-901d-4ba7-963b-031514a9ef73"
OLD_CAMPAIGN_ID = "945b0afd-7c53-492d-9afe-4c44f57173ad"
DEFAULT_CAMPAIGN_IDS = [ACTIVE_CAMPAIGN_ID, PRIORITY_CAMPAIGN_ID, OLD_CAMPAIGN_ID]


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
    try:
        with urlopen(req, timeout=90) as response:
            return json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8") or "{}")
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Instantly {method} {path} failed with HTTP {exc.code}: {payload[:1000]}") from exc


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
