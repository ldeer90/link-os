from __future__ import annotations

import unittest
from decimal import Decimal

from deal_tracker.offer_review import evaluate_offer, reseller_price_aud


class OfferReviewTests(unittest.TestCase):
    def test_unambiguous_offer_auto_approves_but_stays_a_separate_listing_decision(self) -> None:
        decision = evaluate_offer(
            "We accept sponsored guest posts for AUD 350 per article on example.com.au.",
            expected_domain="example.com.au",
        )
        self.assertTrue(decision.auto_approved)
        self.assertEqual(decision.amount, Decimal("350"))
        self.assertEqual(decision.currency, "AUD")
        self.assertEqual(decision.placement_type, "guest_post")

    def test_ambiguous_dollar_without_currency_does_not_auto_approve(self) -> None:
        decision = evaluate_offer("Guest posts are $350.", expected_domain="example.com.au")
        self.assertFalse(decision.auto_approved)
        self.assertIn("missing_or_conflicting_price", decision.review_reasons)

    def test_network_and_multi_price_reply_requires_review(self) -> None:
        decision = evaluate_offer(
            "Our sites: example.com.au AUD 100 guest post; second.com.au USD 50 guest post.",
            expected_domain="example.com.au",
        )
        self.assertFalse(decision.auto_approved)
        self.assertIn("multiple_prices", decision.review_reasons)
        self.assertIn("agency_or_network_ambiguity", decision.review_reasons)
        self.assertIn("multiple_or_conflicting_domains", decision.review_reasons)

    def test_attachment_only_requires_review(self) -> None:
        decision = evaluate_offer(
            "Please see the attached media kit for our guest post rates.",
            expected_domain="example.com.au",
            attachments=[{"name": "rates.pdf"}],
        )
        self.assertFalse(decision.auto_approved)
        self.assertIn("attachment_only_or_attachment_dependent", decision.review_reasons)

    def test_reseller_formula_uses_larger_floor_and_rounds_up_to_ten(self) -> None:
        self.assertEqual(reseller_price_aud(100, 1), Decimal("200"))
        self.assertEqual(reseller_price_aud(300, 1), Decimal("450"))
        self.assertEqual(reseller_price_aud(101, 1), Decimal("210"))


if __name__ == "__main__":
    unittest.main()
