from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape
import re
from typing import Any, Callable, Iterable

from deal_tracker.instantly import request as instantly_request


REQUIRED_LEGACY_CAMPAIGN_IDS = (
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
)

# The fixed safety allowlist plus the historical AU publisher follow-up that
# was selected by name during cutover. These IDs scope LINK OS reporting while
# all-workspace membership remains available for duplicate-contact protection.
HISTORICAL_LINK_BUILDING_CAMPAIGN_IDS = frozenset(
    (*REQUIRED_LEGACY_CAMPAIGN_IDS, "a34b4c39-7825-4c7c-bf28-35708f8458db")
)

CAMPAIGN_NAME_MARKERS = (
    "article submission info request",
    "guest post",
    "article submission",
    "paid link",
    "publisher",
    "placement",
)
MANAGED_NAME_PREFIX = "LINK OS |"
CAMPAIGN_COPY_VERSION = "verified-relaunch-v2.0"
PUBLIC_EVIDENCE_PILOT_COPY_VERSION = "public-evidence-pilot-v1.0"
REPLY_STOP_LINE = 'If you\'d prefer no more emails, just reply "stop".'
HEALTHY_ACCOUNT_STATUS = 1
ACTIVE_CAMPAIGN_STATUS = 1
PAUSED_CAMPAIGN_STATUS = 2
COMPLETED_CAMPAIGN_STATUS = 3


Transport = Callable[..., Any]


@dataclass(frozen=True)
class CampaignLimits:
    batch_size: int = 250
    upload_chunk_size: int = 1000
    pilot_new_leads_daily: int = 10
    healthy_new_leads_daily: int = 15
    sender_daily_emails: int = 30
    bounce_pause_rate: float = 0.03
    unsubscribe_pause_rate: float = 0.01


def verification_is_eligible(result: dict[str, Any]) -> bool:
    """Only explicit verified, non-catch-all results can enter outreach."""
    catch_all = result.get("catch_all")
    if isinstance(catch_all, str):
        catch_all = catch_all.strip().lower() == "true"
    return result.get("verification_status") == "verified" and catch_all is False


def sender_is_healthy(account: dict[str, Any]) -> bool:
    return (
        account.get("status") == HEALTHY_ACCOUNT_STATUS
        and account.get("setup_pending") is False
        and account.get("autofix_failed") is not True
    )


def matching_campaigns(campaigns: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return legacy guest-post campaigns only.

    Managed LINK OS campaigns deliberately use a separate selector so an
    emergency legacy pause cannot accidentally target a new batch.
    """

    selected: dict[str, dict[str, Any]] = {}
    required = set(REQUIRED_LEGACY_CAMPAIGN_IDS)
    for campaign in campaigns:
        campaign_id = str(campaign.get("id") or "")
        name = str(campaign.get("name") or "")
        if name.startswith(MANAGED_NAME_PREFIX):
            continue
        if campaign_id in required or any(marker in name.lower() for marker in CAMPAIGN_NAME_MARKERS):
            selected[campaign_id] = campaign
    return sorted(selected.values(), key=lambda row: (str(row.get("name") or ""), str(row.get("id") or "")))


def campaign_is_link_building(campaign: dict[str, Any]) -> bool:
    campaign_id = str(campaign.get("id") or "")
    name = str(campaign.get("name") or "")
    return (
        name.startswith(MANAGED_NAME_PREFIX)
        or campaign_id in HISTORICAL_LINK_BUILDING_CAMPAIGN_IDS
        or any(marker in name.lower() for marker in CAMPAIGN_NAME_MARKERS)
    )


def reply_sync_campaigns(campaigns: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(campaigns)
    selected = {str(row.get("id") or ""): row for row in matching_campaigns(rows)}
    for row in rows:
        if str(row.get("name") or "").startswith(MANAGED_NAME_PREFIX):
            selected[str(row.get("id") or "")] = row
    selected.pop("", None)
    return sorted(selected.values(), key=lambda row: (str(row.get("name") or ""), str(row.get("id") or "")))


def _step(subject: str, body: str, delay: int) -> dict[str, Any]:
    return {
        "type": "email",
        "delay": delay,
        "delay_unit": "days",
        "pre_delay": delay,
        "pre_delay_unit": "days",
        "variants": [{"subject": subject, "body": body, "v_disabled": False}],
    }


def verified_relaunch_steps(subject: str = "Article Submission Info Request") -> list[dict[str, Any]]:
    paragraph_groups = (
        (
            "Hi {{firstName}},",
            "I'm Laurence Deer. I manage SEO and content promotion for Australian ecommerce stores.",
            "Does {{site_name}} accept article submissions, sponsored articles, guest posts, advertorials, or similar editorial placements?",
            "If so, could you share the fees, topic guidelines, link requirements, and other conditions?",
        ),
        (
            "Hi {{firstName}},",
            "Just following up.",
            "Is there a rate card, media kit, or contributor guideline page for article submissions or sponsored/editorial placements on {{site_name}}?",
        ),
        (
            "Hi {{firstName}},",
            "We work with Australian ecommerce brands and often need relevant sites for useful content placements.",
            "Do you accept paid article submissions or sponsored content, and what are the usual fees or requirements?",
        ),
        (
            "Hi {{firstName}},",
            "Is there someone else who handles article submissions, advertising, or sponsored content enquiries for {{site_name}}?",
            "If so, could you point me in the right direction?",
        ),
        (
            "Hi {{firstName}},",
            "I'm checking whether {{site_name}} offers paid editorial, guest post, sponsored article, or content placement options.",
            "A short yes/no reply would be helpful.",
        ),
        (
            "Hi {{firstName}},",
            "Last follow-up from me.",
            "If {{site_name}} accepts paid article submissions or content placements, could you send the fees and requirements?",
            "If not, no worries.",
        ),
    )
    # Instantly's delay is the interval before the next email, not an absolute
    # campaign day. Operator-approved gaps are 2, 3, 5, 7, 10 and 14 days;
    # the final value is retained as a non-zero terminal setting even though
    # there is no seventh step for it to schedule.
    return [
        _step(
            subject,
            "\n\n".join((*paragraphs, "Regards,", "Laurence\nLD Search\nldsearch.com.au", REPLY_STOP_LINE)),
            delay,
        )
        for paragraphs, delay in zip(paragraph_groups, (2, 3, 5, 7, 10, 14), strict=True)
    ]


def has_reply_stop_instruction(sequences: list[dict[str, Any]]) -> bool:
    variants = [
        variant
        for sequence in sequences
        for step in (sequence.get("steps") or [])
        for variant in (step.get("variants") or [])
    ]
    return bool(variants) and all(
        REPLY_STOP_LINE.lower() in str(variant.get("body") or "").lower()
        for variant in variants
    )


TEMPLATE_TOKEN = re.compile(r"{{\s*([A-Za-z0-9_]+)\s*}}")


def render_campaign_text(body: str, variables: dict[str, str]) -> str:
    """Render the small LINK OS template vocabulary and reject leftovers."""

    rendered = TEMPLATE_TOKEN.sub(
        lambda match: str(variables.get(match.group(1), match.group(0))),
        body,
    )
    unresolved = sorted(set(TEMPLATE_TOKEN.findall(rendered)))
    if unresolved:
        raise ValueError("Unresolved campaign variables: " + ", ".join(unresolved))
    return rendered


def sequence_signature(sequences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return provider-stable copy/timing fields for exact readback checks."""

    signature: list[dict[str, Any]] = []
    for sequence in sequences:
        steps: list[dict[str, Any]] = []
        for step in sequence.get("steps") or []:
            steps.append(
                {
                    "type": step.get("type"),
                    "delay": step.get("delay"),
                    "delay_unit": step.get("delay_unit") or "days",
                    "variants": [
                        {
                            "subject": variant.get("subject"),
                            "body": variant.get("body"),
                        }
                        for variant in step.get("variants") or []
                    ],
                }
            )
        signature.append({"steps": steps})
    return signature


def safe_campaign_payload(
    *,
    batch_number: int,
    sender: str,
    pilot: bool = True,
    subject: str = "Article Submission Info Request",
    today: str | None = None,
    limits: CampaignLimits = CampaignLimits(),
    provider_bounce_protection_enabled: bool = True,
) -> dict[str, Any]:
    daily_new = limits.pilot_new_leads_daily if pilot else limits.healthy_new_leads_daily
    return {
        "name": f"{MANAGED_NAME_PREFIX} Verified Relaunch | Batch {batch_number:04d} | {CAMPAIGN_COPY_VERSION}",
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "Melbourne weekdays",
                    "timing": {"from": "09:00", "to": "17:00"},
                    "days": {"0": True, "1": True, "2": True, "3": True, "4": True, "5": False, "6": False},
                    "timezone": "Australia/Melbourne",
                }
            ],
            "start_date": today or date.today().isoformat(),
        },
        "sequences": [{"steps": verified_relaunch_steps(subject)}],
        "email_list": [sender],
        "daily_limit": limits.sender_daily_emails,
        "daily_max_leads": daily_new,
        "email_gap": 45,
        "random_wait_max": 15,
        "text_only": True,
        "first_email_text_only": True,
        "open_tracking": False,
        "link_tracking": False,
        "insert_unsubscribe_header": False,
        "stop_on_reply": True,
        "stop_on_auto_reply": True,
        "stop_for_company": True,
        "allow_risky_contacts": False,
        "disable_bounce_protect": not provider_bounce_protection_enabled,
    }


def analytics_blockers(analytics: dict[str, Any], limits: CampaignLimits = CampaignLimits()) -> list[str]:
    sent = max(int(analytics.get("emails_sent_count") or 0), 1)
    bounces = int(analytics.get("bounced_count") or 0)
    unsubscribes = int(analytics.get("unsubscribed_count") or 0)
    blockers: list[str] = []
    if bounces / sent >= limits.bounce_pause_rate:
        blockers.append("rolling_bounce_rate_at_or_above_3_percent")
    if unsubscribes / sent >= limits.unsubscribe_pause_rate:
        blockers.append("rolling_unsubscribe_rate_at_or_above_1_percent")
    return blockers


def activation_blockers(
    *,
    outreach_paused: bool,
    account: dict[str, Any] | None,
    reserved_count: int,
    uploaded_count: int,
    pending_verification_count: int,
    sender_has_other_active_managed_campaign: bool,
    analytics: dict[str, Any] | None = None,
) -> list[str]:
    blockers: list[str] = []
    if outreach_paused:
        blockers.append("global_outreach_pause_enabled")
    if not account or not sender_is_healthy(account):
        blockers.append("sender_not_healthy")
    if reserved_count <= 0 or uploaded_count != reserved_count:
        blockers.append("campaign_upload_count_mismatch")
    if pending_verification_count:
        blockers.append("pending_email_verification")
    if sender_has_other_active_managed_campaign:
        blockers.append("sender_already_assigned_to_active_link_os_campaign")
    if analytics:
        blockers.extend(analytics_blockers(analytics))
    return blockers


class InstantlyControl:
    def __init__(self, transport: Transport = instantly_request) -> None:
        self.transport = transport

    def _request(self, path: str, **kwargs: Any) -> Any:
        return self.transport(path, **kwargs)

    def _list(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["starting_after"] = cursor
            payload = self._request(path, params=params)
            if isinstance(payload, list):
                rows.extend(row for row in payload if isinstance(row, dict))
                return rows
            batch = list((payload or {}).get("items") or [])
            rows.extend(row for row in batch if isinstance(row, dict))
            cursor = str((payload or {}).get("next_starting_after") or "")
            if not batch or not cursor:
                return rows

    def list_accounts(self) -> list[dict[str, Any]]:
        return self._list("/accounts")

    def list_campaigns(self) -> list[dict[str, Any]]:
        return self._list("/campaigns")

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        return dict(self._request(f"/campaigns/{campaign_id}") or {})

    def update_paused_campaign_copy(
        self,
        campaign_id: str,
        *,
        name: str,
        sequences: list[dict[str, Any]],
        insert_unsubscribe_header: bool | None = None,
        disable_bounce_protect: bool | None = None,
    ) -> dict[str, Any]:
        before = self.get_campaign(campaign_id)
        if before.get("status") in {ACTIVE_CAMPAIGN_STATUS, 4}:
            raise RuntimeError("Campaign copy cannot be changed while active")
        patch: dict[str, Any] = {"name": name, "sequences": sequences}
        if insert_unsubscribe_header is not None:
            patch["insert_unsubscribe_header"] = insert_unsubscribe_header
        if disable_bounce_protect is not None:
            patch["disable_bounce_protect"] = disable_bounce_protect
        self._request(
            f"/campaigns/{campaign_id}",
            method="PATCH",
            body=patch,
        )
        readback = self.get_campaign(campaign_id)
        if readback.get("status") in {ACTIVE_CAMPAIGN_STATUS, 4}:
            raise RuntimeError("Campaign became active during copy update")
        if (
            readback.get("name") != name
            or sequence_signature(readback.get("sequences") or [])
            != sequence_signature(sequences)
        ):
            raise RuntimeError("Campaign copy readback did not match the requested sequence")
        if insert_unsubscribe_header is False and readback.get("insert_unsubscribe_header") is True:
            raise RuntimeError("Campaign unsubscribe-header readback remained enabled")
        if disable_bounce_protect is True and readback.get("disable_bounce_protect") is not True:
            raise RuntimeError("Campaign bounce-protection readback remained enabled")
        return readback

    def send_test_email(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        text: str,
    ) -> dict[str, Any]:
        if not sender or not recipient or not subject or not text:
            raise ValueError("Test email requires sender, recipient, subject, and body")
        html = (
            '<div style="white-space:pre-wrap;font-family:Arial,sans-serif;'
            'font-size:15px;line-height:1.5">'
            + escape(text)
            + "</div>"
        )
        result = dict(
            self._request(
                "/emails/test",
                method="POST",
                body={
                    "eaccount": sender,
                    "to_address_email_list": recipient,
                    "subject": subject,
                    "body": {"text": text, "html": html},
                },
            )
            or {}
        )
        if result.get("status") != "success":
            raise RuntimeError("Instantly test email did not report success")
        return result

    def analytics(
        self,
        campaign_id: str | None = None,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if campaign_id:
            params["id"] = campaign_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        payload = self._request("/campaigns/analytics", params=params)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        return [row for row in (payload or {}).get("items", []) if isinstance(row, dict)]

    def verification(self, email: str) -> dict[str, Any]:
        return dict(self._request(f"/email-verification/{email}") or {})

    def request_verification(self, email: str) -> dict[str, Any]:
        return dict(self._request("/email-verification", method="POST", body={"email": email}) or {})

    def list_campaign_leads(self, campaign_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            body: dict[str, Any] = {"campaign": campaign_id, "limit": 100}
            if cursor:
                body["starting_after"] = cursor
            payload = self._request("/leads/list", method="POST", body=body)
            batch = list((payload or {}).get("items") or [])
            rows.extend(row for row in batch if isinstance(row, dict))
            cursor = str((payload or {}).get("next_starting_after") or "")
            if not batch or not cursor:
                return rows

    def list_campaign_emails(
        self,
        campaign_id: str,
        *,
        starting_after: str = "",
        min_timestamp_created: str = "",
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = starting_after
        while True:
            params: dict[str, Any] = {"campaign_id": campaign_id, "limit": 100}
            if cursor:
                params["starting_after"] = cursor
            if min_timestamp_created:
                params["min_timestamp_created"] = min_timestamp_created
            payload = self._request("/emails", params=params)
            batch = list((payload or {}).get("items") or [])
            rows.extend(row for row in batch if isinstance(row, dict))
            cursor = str((payload or {}).get("next_starting_after") or "")
            if not batch or not cursor:
                return rows

    def list_thread_emails(self, thread_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "search": f"thread:{thread_id}",
            }
            if cursor:
                params["starting_after"] = cursor
            payload = self._request("/emails", params=params)
            batch = list((payload or {}).get("items") or [])
            rows.extend(row for row in batch if isinstance(row, dict))
            cursor = str((payload or {}).get("next_starting_after") or "")
            if not batch or not cursor:
                return rows

    def pause_legacy_campaigns(
        self,
        *,
        execute: bool = False,
        expected_count: int | None = None,
    ) -> dict[str, Any]:
        matches = matching_campaigns(self.list_campaigns())
        matched_ids = {str(row.get("id") or "") for row in matches}
        missing_required = sorted(set(REQUIRED_LEGACY_CAMPAIGN_IDS) - matched_ids)
        if missing_required:
            raise RuntimeError(
                f"Legacy campaign pause blocked: {len(missing_required)} required campaign IDs are missing"
            )
        if expected_count is not None and len(matches) != expected_count:
            raise RuntimeError(
                f"Legacy campaign pause blocked: expected {expected_count} matches, found {len(matches)}"
            )
        result: dict[str, Any] = {
            "dry_run": not execute,
            "matched": len(matches),
            "required_ids_present": len(REQUIRED_LEGACY_CAMPAIGN_IDS),
            "campaigns": [],
        }
        for campaign in matches:
            campaign_id = str(campaign.get("id") or "")
            before = dict(self._request(f"/campaigns/{campaign_id}") or {})
            before_status = before.get("status")
            row = {"id": campaign_id, "name": before.get("name") or campaign.get("name"), "before_status": before_status}
            row["would_pause"] = before_status in {-99, -2, -1, 1, 4}
            if execute and row["would_pause"]:
                self._request(f"/campaigns/{campaign_id}/pause", method="POST", body={})
            readback = dict(self._request(f"/campaigns/{campaign_id}") or {})
            row["after_status"] = readback.get("status")
            row["paused"] = readback.get("status") in {0, 2, 3}
            if execute and not row["paused"]:
                raise RuntimeError(f"Instantly campaign pause readback failed for {campaign_id}")
            result["campaigns"].append(row)
        return result

    def create_paused_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not str(payload.get("name") or "").startswith(MANAGED_NAME_PREFIX):
            raise ValueError("LINK OS campaigns must use the managed name prefix")
        campaign = dict(self._request("/campaigns", method="POST", body=payload) or {})
        campaign_id = str(campaign.get("id") or "")
        if not campaign_id:
            raise RuntimeError("Instantly campaign creation returned no id")
        self._request(f"/campaigns/{campaign_id}/pause", method="POST", body={})
        readback = dict(self._request(f"/campaigns/{campaign_id}") or {})
        if readback.get("status") == ACTIVE_CAMPAIGN_STATUS:
            raise RuntimeError("New LINK OS campaign did not remain paused")
        return readback or campaign

    def upload_leads(
        self,
        campaign_id: str,
        leads: list[dict[str, Any]],
        *,
        chunk_size: int = 1000,
        verify_leads_on_import: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= chunk_size <= 1000:
            raise ValueError("Instantly bulk lead chunk size must be between 1 and 1000")
        results: list[dict[str, Any]] = []
        total_sent = 0
        for index in range(0, len(leads), chunk_size):
            batch = leads[index : index + chunk_size]
            payload = {
                "campaign_id": campaign_id,
                "leads": batch,
                "verify_leads_on_import": verify_leads_on_import,
                "skip_if_in_workspace": True,
                "skip_if_in_campaign": True,
            }
            response = dict(self._request("/leads/add", method="POST", body=payload) or {})
            results.append(response)
            total_sent += int(response.get("total_sent") or 0)
        return {"requested": len(leads), "total_sent": total_sent, "matches": total_sent == len(leads), "results": results}

    def activate(self, campaign_id: str, *, blockers: list[str]) -> dict[str, Any]:
        if blockers:
            raise RuntimeError("Campaign activation blocked: " + ", ".join(blockers))
        result = dict(self._request(f"/campaigns/{campaign_id}/activate", method="POST", body={}) or {})
        readback = dict(self._request(f"/campaigns/{campaign_id}") or {})
        if readback.get("status") != ACTIVE_CAMPAIGN_STATUS:
            raise RuntimeError("Campaign activation readback did not report active status")
        return {"activation": result, "campaign": readback}

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        self._request(f"/campaigns/{campaign_id}/pause", method="POST", body={})
        readback = dict(self._request(f"/campaigns/{campaign_id}") or {})
        if readback.get("status") == ACTIVE_CAMPAIGN_STATUS:
            raise RuntimeError(f"Instantly campaign pause readback failed for {campaign_id}")
        return readback

    def health_snapshot(self) -> dict[str, Any]:
        accounts = self.list_accounts()
        campaigns = self.list_campaigns()
        healthy = [account for account in accounts if sender_is_healthy(account)]
        return {
            "accounts_total": len(accounts),
            "healthy_accounts": len(healthy),
            "unhealthy_accounts": len(accounts) - len(healthy),
            "senders": [
                {
                    "email": account.get("email"),
                    "status": account.get("status"),
                    "setup_pending": account.get("setup_pending"),
                    "healthy": sender_is_healthy(account),
                    "status_message": account.get("status_message"),
                }
                for account in accounts
            ],
            "matched_legacy_campaigns": len(matching_campaigns(campaigns)),
            "managed_campaigns": sum(
                1 for campaign in campaigns if str(campaign.get("name") or "").startswith(MANAGED_NAME_PREFIX)
            ),
            "activation_allowed": bool(healthy),
        }
