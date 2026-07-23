"""Credit-safe SE Ranking Data API client used by backlink discovery.

Only sanitized request metadata leaves this module. Authorization values and
provider response bodies are deliberately excluded from raised errors.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://api.seranking.com"


class SERankingError(RuntimeError):
    """A safe provider error that contains no response body or credentials."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class CreditStateUnknown(SERankingError):
    """The paid request may have reached the provider; it must not be replayed."""


@dataclass(frozen=True, slots=True)
class SubscriptionBalance:
    balance: int | None
    raw_plan: str | None = None


class SERankingClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        max_retries: int = 2,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("SE_RANKING_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("SE_RANKING_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.client = client or httpx.Client(timeout=30.0)
        self.max_retries = max(0, max_retries)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise SERankingError("not_configured", "SE Ranking API key is not configured")
        return {"Authorization": f"Token {self.api_key}", "Accept": "application/json"}

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SERankingError("invalid_response", "SE Ranking returned invalid JSON", status_code=response.status_code) from exc

    def _get(self, path: str, *, params: dict[str, Any] | None = None, paid: bool) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries + 1):
            transmitted = False
            try:
                transmitted = True
                response = self.client.get(url, headers=self._headers(), params=params)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                if paid and transmitted:
                    raise CreditStateUnknown(
                        "credit_state_unknown",
                        "Connection failed after the paid request may have been transmitted; operator review is required",
                    ) from exc
                if attempt >= self.max_retries:
                    raise SERankingError("transport_error", "Could not reach SE Ranking") from exc
                time.sleep(min(2**attempt, 4))
                continue

            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt < self.max_retries:
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = min(float(retry_after), 8.0) if retry_after else min(2**attempt, 4)
                    except ValueError:
                        delay = min(2**attempt, 4)
                    time.sleep(max(0.1, delay))
                    continue
                raise SERankingError(
                    "rate_limited" if response.status_code == 429 else "provider_unavailable",
                    "SE Ranking is temporarily unavailable",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise SERankingError(
                    "provider_rejected_request",
                    f"SE Ranking rejected the request with HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            return self._safe_json(response)
        raise SERankingError("provider_unavailable", "SE Ranking request failed")

    def subscription(self) -> SubscriptionBalance:
        payload = self._get("/v1/account/subscription", params={"output": "json"}, paid=False)
        data = payload.get("subscription_info", payload.get("data", payload)) if isinstance(payload, dict) else {}
        balance_value = None
        for key in ("units_left", "balance", "credits", "backlinks_balance", "backlink_credits"):
            if data.get(key) is not None:
                try:
                    balance_value = int(float(data[key]))
                except (TypeError, ValueError):
                    pass
                break
        return SubscriptionBalance(balance=balance_value, raw_plan=str(data.get("status") or data.get("plan") or data.get("tariff") or "") or None)

    def referring_domains_count(self, target_domain: str) -> int:
        """Return the provider's raw unique referring-domain count.

        SE Ranking charges two credits per successful target for this endpoint.
        The call is therefore treated with the same unknown-credit protection as
        backlink retrieval and must never be replayed after an ambiguous
        transport failure.
        """

        payload = self._get(
            "/v1/backlinks/refdomains/count",
            params={"target": target_domain, "mode": "domain", "output": "json"},
            paid=True,
        )
        rows: list[Any] = []
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            for key in ("metrics", "data", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    rows = value
                    break
                if isinstance(value, dict):
                    rows = [value]
                    break
            if not rows and payload.get("refdomains") is not None:
                rows = [payload]
        for row in rows:
            if not isinstance(row, dict) or row.get("refdomains") is None:
                continue
            try:
                return max(0, int(float(row["refdomains"])))
            except (TypeError, ValueError):
                break
        raise SERankingError(
            "invalid_response",
            "SE Ranking response did not contain a referring-domain count",
        )

    def backlinks(
        self,
        target_domain: str,
        *,
        limit: int,
        authority_floor: int = 20,
        authority_ceiling: int | None = None,
        since: str | None = None,
        source_url_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        params: dict[str, Any] = {
            "target": target_domain,
            "mode": "domain",
            "limit": limit,
            "order_by": "domain_inlink_rank",
            "output": "json",
        }
        if since:
            path = "/v1/backlinks/refdomains/history"
            params.update({"new_lost_type": "new", "date_from": since, "date_to": time.strftime("%Y-%m-%d")})
        else:
            path = "/v1/backlinks/all"
            params.update({"per_domain": 1, "domain_inlink_rank_from": authority_floor})
            if authority_ceiling is not None:
                params["domain_inlink_rank_to"] = authority_ceiling
            if source_url_filter:
                params.update({
                    "url_from_filter": source_url_filter,
                    "url_from_filter_mode": "contains",
                })
        payload = self._get(path, params=params, paid=True)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)][:limit]
        if isinstance(payload, dict):
            for key in ("data", "backlinks", "new_lost_refdomains", "items", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    rows = [item for item in value if isinstance(item, dict)][:limit]
                    if since:
                        return [
                            {
                                **item,
                                "source_domain": item.get("refdomain"),
                                "source_url": f"https://{item.get('refdomain')}",
                                "target_url": f"https://{target_domain}",
                                "nofollow": not bool(int(item.get("dofollow_backlinks") or 0)),
                            }
                            for item in rows
                        ]
                    return rows
                if isinstance(value, dict):
                    nested = value.get("items") or value.get("backlinks") or value.get("data")
                    if isinstance(nested, list):
                        return [item for item in nested if isinstance(item, dict)][:limit]
        raise SERankingError("invalid_response", "SE Ranking response did not contain backlink records")
