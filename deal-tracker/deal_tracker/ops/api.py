"""Minimal local API client used by the LINK OS operations CLI.

This client intentionally accepts loopback URLs only.  Migration records can
contain private publisher correspondence, so silently sending them to a remote
host is not a safe fallback.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


class ApiClientError(RuntimeError):
    """A deliberately terse API error that never includes response content."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _validate_loopback_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ApiClientError("invalid_api_url", "LINK OS API URL must use HTTP or HTTPS")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ApiClientError(
            "remote_api_blocked",
            "LINK OS operational payloads may only be sent to the local API",
        )
    return value.rstrip("/") + "/"


@dataclass
class LinkOsApiClient:
    base_url: str = "http://127.0.0.1:8766"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.base_url = _validate_loopback_url(self.base_url)

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        cli_token = os.getenv("LINK_OS_CLI_TOKEN", "").strip()
        if cli_token:
            headers["X-Link-OS-CLI-Token"] = cli_token
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=timeout_seconds or self.timeout_seconds,
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            raise ApiClientError(
                "api_http_error",
                f"LINK OS API returned HTTP {exc.code}",
                status=exc.code,
            ) from None
        except URLError:
            raise ApiClientError(
                "api_unavailable",
                "LINK OS API is unavailable on the configured local address",
            ) from None
        except TimeoutError:
            raise ApiClientError("api_timeout", "LINK OS API request timed out") from None

        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiClientError(
                "invalid_api_response",
                "LINK OS API returned a non-JSON response",
            ) from None
        if not isinstance(decoded, dict):
            raise ApiClientError(
                "invalid_api_response",
                "LINK OS API response must be a JSON object",
            )
        return decoded

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/system/health", timeout_seconds=5.0)

    def create_import(
        self,
        values: Sequence[str],
        *,
        source_name: str,
        queue: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/imports",
            {"values": list(values), "source_name": source_name, "queue": queue},
        )

    def estimate_backlink_analysis(
        self,
        *,
        mode: str,
        client_domain: str,
        competitor_domains: Sequence[str],
        credit_cap: int,
        authority_floor: int,
        source_url_filter: str | None = None,
        authority_ceiling: int | None = 80,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/backlink-analyses/estimate",
            {
                "mode": mode,
                "client_domain": client_domain,
                "competitor_domains": list(competitor_domains),
                "credit_cap": credit_cap,
                "authority_floor": authority_floor,
                "source_url_filter": source_url_filter,
                "authority_ceiling": authority_ceiling,
            },
        )

    def create_backlink_analysis(
        self,
        *,
        mode: str,
        client_domain: str,
        competitor_domains: Sequence[str],
        credit_cap: int,
        confirmed_credit_cap: int,
        authority_floor: int,
        source_url_filter: str | None = None,
        authority_ceiling: int | None = 80,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/backlink-analyses",
            {
                "mode": mode,
                "client_domain": client_domain,
                "competitor_domains": list(competitor_domains),
                "credit_cap": credit_cap,
                "confirmed_credit_cap": confirmed_credit_cap,
                "authority_floor": authority_floor,
                "source_url_filter": source_url_filter,
                "authority_ceiling": authority_ceiling,
            },
        )

    def backlink_candidates(
        self,
        analysis_id: str,
        *,
        status: str = "awaiting_codex_review",
        limit: int = 100,
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        query = urlencode({"status": status, "limit": limit})
        return self._request("GET", f"/api/v1/backlink-analyses/{analysis_id}/candidates?{query}")

    def backlink_analyses(self, *, status: str = "awaiting_codex_review", limit: int = 25) -> dict[str, Any]:
        from urllib.parse import urlencode

        query = urlencode({"status": status, "limit": limit})
        return self._request("GET", f"/api/v1/backlink-analyses?{query}")

    def review_backlinks(self, analysis_id: str, decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/backlink-analyses/{analysis_id}/reviews",
            {"decisions": list(decisions)},
        )

    def reconcile_instantly(
        self,
        *,
        dry_run: bool,
        pause_legacy: bool = False,
        full_reconcile: bool = True,
        expected_legacy_count: int = 13,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/integrations/instantly/reconcile",
            {
                "dry_run": dry_run,
                "pause_legacy": pause_legacy,
                "full_reconcile": full_reconcile,
                "expected_legacy_count": expected_legacy_count,
                "requested_by": "link_os_cli",
            },
            timeout_seconds=max(self.timeout_seconds, 900.0),
        )

    def prepare_campaign(
        self,
        *,
        pilot: bool,
        target_size: int,
        contact_policy: str,
        prepare_only: bool,
        confirm_verification_skipped: bool,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/campaigns/prepare",
            {
                "pilot": pilot,
                "target_size": target_size,
                "contact_policy": contact_policy,
                "prepare_only": prepare_only,
                "confirm_verification_skipped": confirm_verification_skipped,
            },
        )

    def activate_campaign(self, batch_id: str, *, reason: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/campaigns/{batch_id}/activate",
            {
                "confirm_activation": True,
                "reason": reason,
            },
        )

    def estimate_campaign_set(self) -> dict[str, Any]:
        return self._request("POST", "/api/v1/campaign-sets/estimate", {})

    def prepare_campaign_set(self) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/campaign-sets",
            {"batch_size": 25, "confirm_verification_skipped": True},
        )

    def activate_campaign_set(self, launch_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/campaign-sets/{launch_id}/activate",
            {"confirm_activation": True},
        )

    def pause_campaign(self, batch_id: str, *, reason: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/v1/campaigns/{batch_id}/pause", {"reason": reason}
        )

    def reconcile_monday(self, *, dry_run: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/integrations/monday/reconcile",
            {"dry_run": dry_run, "requested_by": "link_os_cli"},
        )

    def retry_job(self, job_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/jobs/{job_id}/retry",
            {"requested_by": "link_os_cli"},
        )

    def set_outreach_pause(
        self,
        *,
        paused: bool,
        reason: str,
        confirm_sender_health: bool = False,
    ) -> dict[str, Any]:
        action = "pause" if paused else "resume"
        return self._request(
            "POST",
            f"/api/v1/system/outreach/{action}",
            {
                "reason": reason,
                "confirm_sender_health": confirm_sender_health,
                "requested_by": "link_os_cli",
            },
        )

    def import_migration_records(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        migration_id: str,
        batch_number: int,
        final_batch: bool,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/migrations/import-records",
            {
                "migration_id": migration_id,
                "batch_number": batch_number,
                "final_batch": final_batch,
                "records": list(records),
            },
            timeout_seconds=max(self.timeout_seconds, 120.0),
        )
