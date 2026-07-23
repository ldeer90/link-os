"""Deterministic, zero-credit anchor-intent classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit


COMMERCIAL_TERMS = (
    "best",
    "book",
    "booking",
    "buy",
    "cheap",
    "compare",
    "cost",
    "coupon",
    "deal",
    "discount",
    "esim",
    "hire",
    "insurance",
    "plan",
    "plans",
    "price",
    "pricing",
    "promo",
    "quote",
    "rent",
    "rental",
    "shop",
    "software",
    "subscription",
    "vpn",
)
COMMERCIAL_PATH_TERMS = (
    "book",
    "buy",
    "compare",
    "deal",
    "discount",
    "esim",
    "hire",
    "insurance",
    "plan",
    "pricing",
    "product",
    "quote",
    "rent",
    "rental",
    "shop",
    "vpn",
)
EDITORIAL_TERMS = (
    "article",
    "blog",
    "guide",
    "how to",
    "news",
    "resource",
    "review",
    "story",
    "tips",
)
GENERIC_ANCHORS = {
    "click here",
    "find out more",
    "here",
    "learn more",
    "more information",
    "read more",
    "source",
    "this page",
    "visit site",
    "website",
}


@dataclass(frozen=True)
class AnchorIntent:
    classification: str
    score: float
    signals: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor_class": self.classification,
            "anchor_commercial_score": self.score,
            "anchor_signals": list(self.signals),
        }


def _normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").lower())).strip()


def _contains_term(text: str, term: str) -> bool:
    return bool(re.search(rf"(?:^|\s){re.escape(term)}(?:$|\s)", text))


def _target_parts(target_url: str | None) -> tuple[str, str, str]:
    raw = (target_url or "").strip()
    if not raw:
        return "", "", ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    brand = re.sub(r"[^a-z0-9]+", "", host.split(".")[0])
    path = _normalized(parsed.path)
    return host, brand, path


def classify_anchor_intent(
    anchor_text: str | None,
    *,
    target_url: str | None = None,
    nofollow: bool | None = None,
    image_link: bool | None = None,
) -> AnchorIntent:
    """Classify one backlink anchor without any provider or network call."""

    raw = (anchor_text or "").strip()
    text = _normalized(raw)
    host, brand, target_path = _target_parts(target_url)
    if not text:
        return AnchorIntent("empty", 0.0, ("image_anchor",) if image_link else ("missing_anchor",))

    compact = re.sub(r"[^a-z0-9]+", "", raw.lower())
    host_compact = re.sub(r"[^a-z0-9]+", "", host)
    if raw.lower().startswith(("http://", "https://", "www.")) or (host_compact and compact == host_compact):
        return AnchorIntent("naked_url", 0.08, ("naked_url_anchor",))
    if text in GENERIC_ANCHORS:
        return AnchorIntent("generic", 0.05, ("generic_anchor",))
    if brand and compact == brand:
        return AnchorIntent("brand", 0.15, ("exact_brand_anchor",))

    commercial_matches = [term for term in COMMERCIAL_TERMS if _contains_term(text, term)]
    editorial_matches = [term for term in EDITORIAL_TERMS if _contains_term(text, term)]
    commercial_path_matches = [term for term in COMMERCIAL_PATH_TERMS if _contains_term(target_path, term)]
    signals: list[str] = []
    score = 0.0
    if commercial_matches:
        score += 0.52 + min(0.16, max(0, len(commercial_matches) - 1) * 0.04)
        signals.extend(f"commercial_anchor:{term}" for term in commercial_matches[:4])
    if commercial_path_matches:
        score += 0.16
        signals.append(f"commercial_target:{commercial_path_matches[0]}")
    if nofollow is False and commercial_matches:
        score += 0.08
        signals.append("follow_link")
    if brand and brand in compact and compact != brand and commercial_matches:
        score += 0.08
        signals.append("brand_plus_commercial_modifier")
    if editorial_matches:
        signals.append(f"editorial_anchor:{editorial_matches[0]}")

    score = round(min(1.0, score), 3)
    if score >= 0.70:
        return AnchorIntent("commercial", score, tuple(sorted(set(signals))))
    if score >= 0.48:
        return AnchorIntent("commercial_likely", score, tuple(sorted(set(signals))))
    if editorial_matches:
        return AnchorIntent("editorial", max(score, 0.2), tuple(sorted(set(signals))))
    if brand and brand in compact:
        return AnchorIntent("brand", max(score, 0.15), ("partial_brand_anchor",))
    return AnchorIntent("other", score, tuple(sorted(set(signals))) or ("unclassified_anchor",))


def aggregate_anchor_intent(evidence_rows: Iterable[Any]) -> AnchorIntent:
    """Return the strongest commercial-intent evidence for one candidate."""

    assessments = [
        classify_anchor_intent(
            getattr(row, "anchor_text", None),
            target_url=getattr(row, "target_url", None),
            nofollow=getattr(row, "nofollow", None),
            image_link=getattr(row, "image_link", None),
        )
        for row in evidence_rows
    ]
    if not assessments:
        return AnchorIntent("unknown", 0.0, ("no_backlink_evidence",))
    strongest = max(assessments, key=lambda item: item.score)
    signals = tuple(sorted({signal for item in assessments for signal in item.signals}))
    return AnchorIntent(strongest.classification, strongest.score, signals)
