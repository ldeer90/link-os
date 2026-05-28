#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.instantly import ACTIVE_CAMPAIGN_ID, list_campaign_leads, request  # noqa: E402


CSV_DEFAULT = Path("/Users/laurencedeer/Downloads/Laurencedeer.com.au _ Client - avenuehampers.com.au.csv")
OUT_DEFAULT = ROOT / "generated" / "imports" / "avenuehampers_instantly_import_results.csv"

USER_AGENT = "Mozilla/5.0 (compatible; LDSearchProspectContactBot/1.0; +https://ldsearch.com.au)"
FETCH_TIMEOUT = 2.5

CONTACT_PATHS = [
    "/contact",
    "/advertise",
    "/advertise-with-us",
    "/media-kit",
    "/write-for-us",
]

ROLE_PRIORITY = [
    "advertising",
    "ads",
    "media",
    "partnerships",
    "partner",
    "sponsored",
    "sponsorship",
    "editor",
    "editorial",
    "content",
    "submissions",
    "submit",
    "sales",
    "hello",
    "info",
    "contact",
    "admin",
]

BAD_EMAIL_PARTS = [
    "example.",
    "email.com",
    "domain.com",
    "yourname",
    "name@",
    "test@",
    "privacy@",
    "abuse@",
    "postmaster@",
    "webmaster@",
    "noreply@",
    "no-reply@",
    "donotreply@",
]

MANUAL_PUBLIC_EMAILS: dict[str, list[tuple[str, str, str]]] = {
    "haveagonews.com.au": [
        ("advertise@haveagonews.com.au", "https://www.haveagonews.com.au/contact/", "public_search_contact_page"),
    ],
    "oceanroadmagazine.com.au": [
        ("brian@oceanroadmagazine.com.au", "https://www.oceanroadmagazine.com.au/diving-deeper-how-ocean-road-magazine-elevates-storytelling-in-print-and-online/", "public_search_advertise_article"),
    ],
    "re-thinkingthefuture.com": [
        ("rtf@re-thinkingthefuture.com", "https://awards.re-thinkingthefuture.com/contact/", "public_search_contact_page"),
    ],
    "sweetsofties.com": [
        ("sweetsofties@gmail.com", "https://omail.io/leads/sweetsofties.com", "public_search_profile"),
    ],
    "beanstalkmums.com.au": [
        ("lucy@beanstalkmums.com.au", "https://beanstalkmums.com.au/wp-content/uploads/2020/10/Beanstalk-Article-Guidelines.pdf", "public_search_pdf"),
    ],
    "bbntimes.com": [
        ("hello@bbntimes.com", "https://bbntimes.com.5-196-1-209.prev.bbntimes.com/support/contact-us", "public_search_contact_page"),
    ],
    "entrepreneurshiplife.com": [
        ("info@entrepreneurshiplife.com", "https://www.entrepreneurshiplife.com/write-for-us/", "public_search_write_for_us"),
    ],
}


@dataclass
class Prospect:
    root_domain: str
    original_prospect: str
    site_name: str
    source_query: str = ""
    dr: str = ""
    da: str = ""
    client_approval: str = ""
    csv_price_aud: str = ""


@dataclass
class EmailCandidate:
    email: str
    source_url: str
    method: str
    confidence: int
    email_type: str
    notes: str = ""


@dataclass
class ImportResult:
    prospect: Prospect
    status: str
    lead_email: str = ""
    email_type: str = ""
    source_url: str = ""
    method: str = ""
    instantly_id: str = ""
    sent_state: str = ""
    notes: str = ""
    candidate_count: int = 0
    all_candidates: str = ""


def clean_domain(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", "", value)
    value = value.replace("http://", "").replace("https://", "").split("/")[0]
    value = value.split("?")[0].split("#")[0].strip(".")
    if value.startswith("www."):
        value = value[4:]
    return value


def site_name_from_domain(domain: str) -> str:
    base = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in base.split()) or domain


def read_prospects(path: Path) -> list[Prospect]:
    prospects: dict[str, Prospect] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw = row.get("Prospect ") or row.get("Prospect") or row.get("domain") or row.get("Domain") or ""
            domain = clean_domain(raw)
            if not domain or "." not in domain:
                continue
            prospects.setdefault(
                domain,
                Prospect(
                    root_domain=domain,
                    original_prospect=raw.strip(),
                    site_name=site_name_from_domain(domain),
                    source_query=(row.get("Google Query ") or row.get("Google Query") or "").strip(),
                    dr=(row.get("DR") or "").strip(),
                    da=(row.get("DA") or "").strip(),
                    client_approval=(row.get("Client Approval") or "").strip(),
                    csv_price_aud=(row.get("Price (AUD)") or row.get("Price") or "").strip(),
                ),
            )
    return list(prospects.values())


def lead_domains(lead: dict[str, Any]) -> set[str]:
    domains: set[str] = set()
    payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
    for value in [
        lead.get("company_domain"),
        lead.get("website"),
        lead.get("email"),
        payload.get("root_domain"),
        payload.get("website"),
        payload.get("source_url"),
    ]:
        text = str(value or "")
        if "@" in text and not text.startswith("http"):
            text = text.split("@")[-1]
        domain = clean_domain(text)
        if domain and "." in domain:
            domains.add(domain)
    return domains


def lead_has_been_contacted(lead: dict[str, Any]) -> bool:
    summary = lead.get("status_summary") if isinstance(lead.get("status_summary"), dict) else {}
    last_step = summary.get("lastStep") if isinstance(summary.get("lastStep"), dict) else {}
    return bool(
        lead.get("timestamp_last_contact")
        or lead.get("timestamp_last_touch")
        or last_step.get("timestamp_executed")
    )


def fetch_url(url: str) -> tuple[str, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    with urlopen(req, timeout=FETCH_TIMEOUT) as response:
        final_url = response.geturl()
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return final_url, ""
        raw = response.read(900_000)
        charset = response.headers.get_content_charset() or "utf-8"
        return final_url, raw.decode(charset, errors="replace")


def deobfuscate(text: str) -> str:
    text = html.unescape(text)
    replacements = [
        (r"\s*\[\s*at\s*\]\s*", "@"),
        (r"\s*\(\s*at\s*\)\s*", "@"),
        (r"\s+at\s+", "@"),
        (r"\s*\[\s*dot\s*\]\s*", "."),
        (r"\s*\(\s*dot\s*\)\s*", "."),
        (r"\s+dot\s+", "."),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def extract_emails(text: str) -> set[str]:
    text = deobfuscate(text)
    text = re.sub(r"(?:\\u003e|\\u003c|u003e|u003c|003e|003c|&gt;|&lt;)+(?=[\w.+-]{1,64}@)", " ", text, flags=re.I)
    found = set()
    for encoded in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', text):
        decoded = decode_cloudflare_email(encoded)
        if decoded:
            found.add(decoded.lower())
    for email in re.findall(r"(?<![\w.+-])[\w.+-]{1,64}@[\w.-]+\.[a-zA-Z]{2,24}(?![\w.-])", text):
        email = email.strip(".,;:()[]{}<>\"'").lower()
        email = re.sub(r"^(?:u003e|u003c|003e|003c|3e|3c)+", "", email)
        if any(part in email for part in BAD_EMAIL_PARTS):
            continue
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        found.add(email)
    return found


def decode_cloudflare_email(encoded: str) -> str:
    try:
        data = bytes.fromhex(encoded)
        if not data:
            return ""
        key = data[0]
        return "".join(chr(byte ^ key) for byte in data[1:])
    except ValueError:
        return ""


def email_type(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    for role in ROLE_PRIORITY:
        if role in local:
            if role in {"ads", "advertising"}:
                return "advertising"
            if role in {"partner", "partnerships", "sponsorship", "sponsored"}:
                return "partnerships"
            if role in {"editor", "editorial", "content", "submissions", "submit"}:
                return "editorial"
            if role == "sales":
                return "sales"
            return "generic"
    return "public_business"


def rank_email(email: str, root_domain: str, source_url: str) -> int:
    score = 40
    local = email.split("@", 1)[0].lower()
    domain = clean_domain(email.split("@", 1)[1])
    if domain == root_domain or domain.endswith("." + root_domain):
        score += 35
    elif root_domain in domain or domain in root_domain:
        score += 20
    else:
        score -= 10
    for i, role in enumerate(ROLE_PRIORITY):
        if role in local:
            score += max(5, 28 - i)
            break
    lowered_url = source_url.lower()
    if any(hint in lowered_url for hint in ["advertis", "media", "sponsor", "guest", "write-for-us", "contact"]):
        score += 8
    return max(1, min(100, score))


def page_urls_for(prospect: Prospect) -> list[str]:
    root = f"https://{prospect.root_domain}"
    urls = [root]
    if "/" in prospect.original_prospect.strip("/"):
        urls.append(f"https://{prospect.original_prospect.strip()}")
    urls.extend(urljoin(root, path) for path in CONTACT_PATHS)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def discover_for(prospect: Prospect) -> list[EmailCandidate]:
    candidates: dict[str, EmailCandidate] = {}
    for email, source_url, method in MANUAL_PUBLIC_EMAILS.get(prospect.root_domain, []):
        candidates[email] = EmailCandidate(
            email=email,
            source_url=source_url,
            method=method,
            confidence=rank_email(email, prospect.root_domain, source_url) + 10,
            email_type=email_type(email),
            notes="manual_public_web_result",
        )
    for url in page_urls_for(prospect)[:7]:
        try:
            final_url, body = fetch_url(url)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
        if not body:
            continue
        for email in extract_emails(body):
            score = rank_email(email, prospect.root_domain, final_url)
            candidate = EmailCandidate(
                email=email,
                source_url=final_url,
                method="site_page_scrape",
                confidence=score,
                email_type=email_type(email),
            )
            current = candidates.get(email)
            if not current or candidate.confidence > current.confidence:
                candidates[email] = candidate

        # Follow a small number of obvious same-site contact/media links from the homepage/source page.
        if any(candidate.confidence >= 88 for candidate in candidates.values()):
            break
        time.sleep(0.15)
    return sorted(candidates.values(), key=lambda item: item.confidence, reverse=True)


def build_lead(prospect: Prospect, candidate: EmailCandidate) -> dict[str, Any]:
    return {
        "email": candidate.email,
        "first_name": "there",
        "company_name": prospect.site_name,
        "website": f"https://{prospect.root_domain}",
        "personalization": (
            f"I found {prospect.site_name} while reviewing publisher sites that may accept "
            "article submissions, sponsored posts, or editorial placements."
        ),
        "custom_variables": {
            "root_domain": prospect.root_domain,
            "site_name": prospect.site_name,
            "opportunity_type": "sponsored_post",
            "source_url": candidate.source_url,
            "niche": "gift/lifestyle",
            "contact_email_type": candidate.email_type,
            "why_relevant": "the site appears relevant to gift, lifestyle, ecommerce, or publisher link placement outreach",
            "contact_source": candidate.source_url,
            "contact_source_method": candidate.method,
            "csv_source": "avenuehampers_prospect_csv",
            "csv_dr": prospect.dr,
            "csv_da": prospect.da,
            "csv_price_aud": prospect.csv_price_aud,
        },
    }


def add_leads(campaign_id: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
    if not leads:
        return {"items": []}
    return request(
        "/leads/add",
        method="POST",
        body={
            "campaign_id": campaign_id,
            "leads": leads,
            "verify_leads_on_import": False,
            "skip_if_in_workspace": True,
            "skip_if_in_campaign": True,
        },
    )


def write_results(path: Path, results: list[ImportResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "root_domain",
        "site_name",
        "status",
        "lead_email",
        "email_type",
        "source_url",
        "method",
        "instantly_id",
        "sent_state",
        "notes",
        "candidate_count",
        "all_candidates",
        "original_prospect",
        "dr",
        "da",
        "csv_price_aud",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "root_domain": result.prospect.root_domain,
                    "site_name": result.prospect.site_name,
                    "status": result.status,
                    "lead_email": result.lead_email,
                    "email_type": result.email_type,
                    "source_url": result.source_url,
                    "method": result.method,
                    "instantly_id": result.instantly_id,
                    "sent_state": result.sent_state,
                    "notes": result.notes,
                    "candidate_count": result.candidate_count,
                    "all_candidates": result.all_candidates,
                    "original_prospect": result.prospect.original_prospect,
                    "dr": result.prospect.dr,
                    "da": result.prospect.da,
                    "csv_price_aud": result.prospect.csv_price_aud,
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape contacts from a prospect CSV and add missing domains to Instantly.")
    parser.add_argument("--csv", type=Path, default=CSV_DEFAULT)
    parser.add_argument("--campaign-id", default=ACTIVE_CAMPAIGN_ID)
    parser.add_argument("--output", type=Path, default=OUT_DEFAULT)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    prospects = read_prospects(args.csv)
    leads_by_email = list_campaign_leads(args.campaign_id)
    leads_by_domain: dict[str, list[dict[str, Any]]] = {}
    for lead in leads_by_email.values():
        for domain in lead_domains(lead):
            leads_by_domain.setdefault(domain, []).append(lead)

    results: list[ImportResult] = []
    missing: list[Prospect] = []
    for prospect in prospects:
        existing = leads_by_domain.get(prospect.root_domain, [])
        if existing:
            best = existing[0]
            sent_state = "already_contacted" if lead_has_been_contacted(best) else "not_contacted_yet"
            results.append(
                ImportResult(
                    prospect=prospect,
                    status="already_in_campaign",
                    lead_email=str(best.get("email") or ""),
                    instantly_id=str(best.get("id") or ""),
                    sent_state=sent_state,
                    notes="Existing lead retained. If not_contacted_yet, it is already eligible in the campaign queue.",
                )
            )
        else:
            missing.append(prospect)

    discovered: dict[str, list[EmailCandidate]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {executor.submit(discover_for, prospect): prospect for prospect in missing}
        for future in as_completed(future_map):
            prospect = future_map[future]
            try:
                discovered[prospect.root_domain] = future.result()
            except Exception as exc:  # noqa: BLE001
                discovered[prospect.root_domain] = []
                results.append(ImportResult(prospect=prospect, status="scrape_error", notes=str(exc)[:500]))

    leads_to_add: list[dict[str, Any]] = []
    prospect_for_email: dict[str, tuple[Prospect, EmailCandidate, list[EmailCandidate]]] = {}
    for prospect in missing:
        candidates = discovered.get(prospect.root_domain, [])
        all_candidates = "; ".join(f"{c.email} ({c.email_type}, {c.confidence}, {c.source_url})" for c in candidates[:8])
        if not candidates:
            results.append(
                ImportResult(
                    prospect=prospect,
                    status="no_email_found",
                    notes="No public email found from homepage/contact/media/advertise/guest-post path scrape.",
                )
            )
            continue
        best = candidates[0]
        lead = build_lead(prospect, best)
        leads_to_add.append(lead)
        prospect_for_email[best.email] = (prospect, best, candidates)
        if args.dry_run:
            results.append(
                ImportResult(
                    prospect=prospect,
                    status="dry_run_would_add",
                    lead_email=best.email,
                    email_type=best.email_type,
                    source_url=best.source_url,
                    method=best.method,
                    candidate_count=len(candidates),
                    all_candidates=all_candidates,
                )
            )

    add_response: dict[str, Any] = {}
    if not args.dry_run and leads_to_add:
        add_response = add_leads(args.campaign_id, leads_to_add)
        added_items = add_response.get("items") if isinstance(add_response.get("items"), list) else []
        item_by_email = {
            str(item.get("email") or item.get("lead_email") or "").lower(): item
            for item in added_items
            if isinstance(item, dict)
        }
        for lead in leads_to_add:
            email = str(lead["email"]).lower()
            prospect, best, candidates = prospect_for_email[email]
            item = item_by_email.get(email, {})
            all_candidates = "; ".join(f"{c.email} ({c.email_type}, {c.confidence}, {c.source_url})" for c in candidates[:8])
            results.append(
                ImportResult(
                    prospect=prospect,
                    status="added_to_campaign",
                    lead_email=email,
                    email_type=best.email_type,
                    source_url=best.source_url,
                    method=best.method,
                    instantly_id=str(item.get("id") or ""),
                    sent_state="new_next_queue_candidate",
                    notes=f"Added via /leads/add. API item: {item}"[:900],
                    candidate_count=len(candidates),
                    all_candidates=all_candidates,
                )
            )

    # Stable output ordering: CSV order first.
    index = {prospect.root_domain: i for i, prospect in enumerate(prospects)}
    results.sort(key=lambda result: (index.get(result.prospect.root_domain, 999999), result.status))
    write_results(args.output, results)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(f"Prospects: {len(prospects)}")
    print(f"Already in campaign: {counts.get('already_in_campaign', 0)}")
    print(f"Added: {counts.get('added_to_campaign', 0)}")
    print(f"No email found: {counts.get('no_email_found', 0)}")
    print(f"Not contacted existing: {sum(1 for r in results if r.sent_state == 'not_contacted_yet')}")
    print(f"Output: {args.output}")
    if add_response and not add_response.get("items"):
        print(f"Add response: {add_response}")


if __name__ == "__main__":
    main()
