from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass


DEAL_STATUSES = {"needs_review", "confirmed", "rejected", "archived"}
PLACEMENT_TYPES = {"guest_post", "sponsored_post", "advertorial", "niche_edit", "media_package", "other"}


@dataclass(frozen=True)
class PriceOption:
    root_domain: str
    site_name: str
    price_amount: float
    price_currency: str
    price_notes: str


@dataclass(frozen=True)
class ReplyExtraction:
    classification: str
    deal_status: str
    placement_type: str
    price_amount: float | None
    price_currency: str
    price_notes: str
    link_insertion_cost_amount: float | None
    link_insertion_cost_currency: str
    link_insertion_notes: str
    writing_requirements: str
    link_requirements: str
    turnaround_time: str
    publisher_url: str
    reply_summary: str
    create_deal: bool
    price_options: list[PriceOption]

    def to_json(self) -> str:
        payload = self.__dict__.copy()
        payload["price_options"] = [asdict(option) for option in self.price_options]
        return json.dumps(payload, indent=2, sort_keys=True)


def top_reply_text(body_text: str) -> str:
    text = body_text or ""
    markers = ["\n-----Original Message-----", "\nFrom:", "\nOn ", "\nSent:"]
    cut = len(text)
    for marker in markers:
        index = text.find(marker)
        if index > 0:
            cut = min(cut, index)
    return text[:cut].strip() or text.strip()


def first_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>)\"']+", text)
    return match.group(0).rstrip(".,") if match else ""


def first_price(text: str) -> tuple[float | None, str]:
    patterns = [
        r"(?:AUD|USD|AU\$|US\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:AUD|USD|Australian dollars|US dollars)",
        r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            try:
                return float(match.group(1).replace(",", "")), match.group(0)
            except ValueError:
                return None, match.group(0)
    return None, ""


def link_insertion_price(text: str) -> tuple[float | None, str, str]:
    top_text = top_reply_text(text)
    patterns = [
        r"link insertion[^$\n]{0,80}\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:\(?\s*\$?\s*(AUD|USD)\s*\)?)?",
        r"link insertion[^.\n]{0,120}?([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(AUD|USD)",
        r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(AUD|USD)[^.\n]{0,80}?link insertion",
    ]
    for pattern in patterns:
        match = re.search(pattern, top_text, flags=re.I)
        if not match:
            continue
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        currency = (match.group(2) or currency_for_price_note(match.group(0))).upper()
        sentence = sentence_with_any(top_text, ["link insertion"]) or match.group(0)
        return amount, currency, sentence[:700]
    return None, "", ""


def normalize_priced_domain(raw_domain: str) -> tuple[str, str]:
    site_name = raw_domain.strip().strip(" .,-;:*")
    site_name = re.sub(r"\s+", " ", site_name)
    compact = re.sub(r"\s+", "", site_name)
    compact = re.sub(r"\.+", ".", compact)
    compact = compact.strip(" .,-;:*").lower()
    compact = compact.replace("www.", "", 1)
    return compact, site_name


def extract_price_options(text: str) -> list[PriceOption]:
    options: dict[str, PriceOption] = {}
    for line in top_reply_text(text).splitlines():
        clean_line = line.strip().strip("*").strip()
        match = re.match(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(AUD|USD)\s+(.+?)\s*$", clean_line, flags=re.I)
        if not match:
            continue
        raw_tail = match.group(3)
        root_domain, site_name = normalize_priced_domain(raw_tail)
        if not re.search(r"\.[a-z]{2,}(?:\.[a-z]{2})?$", root_domain):
            continue
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        currency = match.group(2).upper()
        options[root_domain] = PriceOption(
            root_domain=root_domain,
            site_name=site_name,
            price_amount=amount,
            price_currency=currency,
            price_notes=f"{amount:g} {currency}",
        )
    return list(options.values())


def currency_for_price_note(price_note: str) -> str:
    lowered = price_note.lower()
    if "usd" in lowered:
        return "USD"
    return "AUD"


def classify_placement(text: str) -> str:
    lowered = text.lower()
    if "niche edit" in lowered or "link insertion" in lowered:
        return "niche_edit"
    if "advertorial" in lowered:
        return "advertorial"
    if "media kit" in lowered or "rate card" in lowered or "advertising package" in lowered:
        return "media_package"
    if "sponsored" in lowered:
        return "sponsored_post"
    if "guest post" in lowered or "article submission" in lowered or "contributor" in lowered:
        return "guest_post"
    return "other"


def sentence_with_any(text: str, needles: list[str]) -> str:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        lowered = sentence.lower()
        if any(needle in lowered for needle in needles):
            return sentence.strip()[:700]
    return ""


def compact_sentences_with_any(text: str, needles: list[str], limit: int = 5) -> str:
    sentences: list[str] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", top_reply_text(text)):
        clean = re.sub(r"\s+", " ", sentence).strip(" -*")
        if not clean:
            continue
        if re.match(r"^\$?\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?\s*(?:AUD|USD)\s+", clean, flags=re.I):
            continue
        if "drive.google.com" in clean.lower() or clean.startswith("<http"):
            continue
        lowered = clean.lower()
        if any(needle in lowered for needle in needles) and clean not in seen:
            sentences.append(clean)
            seen.add(clean)
        if len(sentences) >= limit:
            break
    return " ".join(sentences)[:1200]


def writing_requirements_summary(text: str) -> str:
    return compact_sentences_with_any(
        text,
        [
            "word limit",
            "420 words",
            "image",
            "licensed",
            "copyright",
            "getty",
            "auto numbering",
            "auto bullet",
            "bullet points",
            "content about",
            "casino",
            "gambling",
        ],
        limit=6,
    )


def link_requirements_summary(text: str) -> str:
    return compact_sentences_with_any(
        text,
        [
            "business links",
            "do follow",
            "dofollow",
            "reference links",
            "sponsor tags",
            "marked as sponsored",
            "guarantee",
            "links will",
        ],
        limit=6,
    )


def turnaround_summary(text: str) -> str:
    return compact_sentences_with_any(text, ["turn around", "turnaround", "24 hours", "weekends", "public holidays", "fast publishing"], limit=3)


def price_notes_summary(text: str, fallback_note: str, price_options: list[PriceOption]) -> str:
    snippets = compact_sentences_with_any(
        text,
        ["pricing", "prices", "generally", "except for", "bundle", "paypal", "billing", "discount", "price increase"],
        limit=6,
    )
    if price_options:
        list_note = f"{len(price_options)} priced publisher domains extracted from reply."
        return f"{list_note} {snippets}".strip()
    return fallback_note or snippets


def classify_reply(body_text: str) -> ReplyExtraction:
    raw_top_text = top_reply_text(body_text)
    text = re.sub(r"\s+", " ", raw_top_text).strip()
    lowered = text.lower()
    amount, price_note = first_price(text)
    link_amount, link_currency, link_note = link_insertion_price(raw_top_text)
    price_options = extract_price_options(raw_top_text)
    if price_options and amount is None:
        amount = price_options[0].price_amount
        price_note = price_options[0].price_notes
    placement_type = classify_placement(text)
    publisher_url = first_url(text)

    auto_markers = ["out of office", "auto-reply", "autoreply", "automatic reply", "only applicants with successful submissions", "do not reply"]
    strong_reject_markers = ["isn't one for us", "is not one for us", "not one for us", "unable to assist"]
    placement_reject_patterns = [
        r"\b(?:we\s+)?do not accept\s+(?:paid\s+)?(?:guest posts?|sponsored posts?|advertorials?|link placements?|paid placements?)",
        r"\b(?:we\s+)?don't accept\s+(?:paid\s+)?(?:guest posts?|sponsored posts?|advertorials?|link placements?|paid placements?)",
        r"\bnot accepting\s+(?:paid\s+)?(?:guest posts?|sponsored posts?|advertorials?|link placements?|paid placements?)",
        r"\bno\s+(?:sponsored posts?|paid links?|paid placements?|guest posts?)",
        r"\bwe\s+(?:do not|don't)\s+offer\s+(?:sponsored posts?|paid links?|paid placements?|guest posts?)",
    ]
    money_markers = ["rate card", "media kit", "pricing", "price", "cost", "fee", "package", "sponsored", "advertorial", "paid placement", "guest post"]
    process_markers = ["send through", "guidelines", "requirements", "word count", "dofollow", "turnaround", "invoice", "payment", "editorial"]
    has_money_terms = bool(price_options) or amount is not None or any(marker in lowered for marker in money_markers)
    is_rejection = any(marker in lowered[:600] for marker in strong_reject_markers) or any(re.search(pattern, lowered) for pattern in placement_reject_patterns)

    if is_rejection:
        classification = "rejected"
        deal_status = "rejected"
        create_deal = False
        summary = "Reply indicates they do not accept the requested placement."
    elif any(marker in lowered for marker in auto_markers):
        classification = "auto_reply"
        deal_status = "archived"
        create_deal = False
        summary = "Auto-reply or submission acknowledgement; no deal terms confirmed."
    elif has_money_terms:
        classification = "deal_terms"
        deal_status = "confirmed" if amount is not None else "needs_review"
        create_deal = True
        summary = "Reply appears to include pricing, a rate card/media kit, or paid placement terms."
    elif any(marker in lowered for marker in process_markers):
        classification = "needs_review"
        deal_status = "needs_review"
        create_deal = True
        summary = "Reply appears to provide a possible paid placement process but needs review."
    else:
        classification = "ignore"
        deal_status = "archived"
        create_deal = False
        summary = "No clear paid placement terms found."

    return ReplyExtraction(
        classification=classification,
        deal_status=deal_status,
        placement_type=placement_type,
        price_amount=amount,
        price_currency=currency_for_price_note(price_note),
        price_notes=price_notes_summary(raw_top_text, price_note or (f"Detected price: {amount:g}" if amount is not None else ""), price_options),
        link_insertion_cost_amount=link_amount,
        link_insertion_cost_currency=link_currency or currency_for_price_note(link_note),
        link_insertion_notes=link_note,
        writing_requirements=writing_requirements_summary(raw_top_text) or sentence_with_any(text, ["word count", "article", "content", "guidelines", "submission", "editorial"]),
        link_requirements=link_requirements_summary(raw_top_text) or sentence_with_any(text, ["link", "dofollow", "nofollow", "anchor"]),
        turnaround_time=turnaround_summary(raw_top_text) or sentence_with_any(text, ["turnaround", "business day", "week", "publish"]),
        publisher_url=publisher_url,
        reply_summary=summary,
        create_deal=create_deal,
        price_options=price_options,
    )
