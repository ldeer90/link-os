from __future__ import annotations

from types import SimpleNamespace

from deal_tracker.anchor_intent import aggregate_anchor_intent, classify_anchor_intent


def test_classifies_commercial_anchor_with_target_and_follow_evidence() -> None:
    assessment = classify_anchor_intent(
        "travel eSIM",
        target_url="https://competitor.example/esim/australia",
        nofollow=False,
    )

    assert assessment.classification == "commercial"
    assert assessment.score >= 0.7
    assert "commercial_anchor:esim" in assessment.signals
    assert "commercial_target:esim" in assessment.signals
    assert "follow_link" in assessment.signals


def test_classifies_brand_generic_naked_and_editorial_anchors() -> None:
    assert classify_anchor_intent("NordVPN", target_url="https://nordvpn.com/").classification == "brand"
    assert classify_anchor_intent("click here").classification == "generic"
    assert classify_anchor_intent("https://example.com", target_url="https://example.com").classification == "naked_url"
    assert classify_anchor_intent("Bali travel guide").classification == "editorial"


def test_aggregate_uses_strongest_commercial_evidence() -> None:
    evidence = [
        SimpleNamespace(anchor_text="read more", target_url="https://example.com/blog", nofollow=True, image_link=False),
        SimpleNamespace(anchor_text="compare travel insurance", target_url="https://example.com/insurance", nofollow=False, image_link=False),
    ]

    assessment = aggregate_anchor_intent(evidence)

    assert assessment.classification == "commercial"
    assert assessment.score >= 0.7
    assert "generic_anchor" in assessment.signals
    assert "commercial_anchor:insurance" in assessment.signals
