from __future__ import annotations

import unittest

from deal_tracker.worker import _lead_last_contact_at

from deal_tracker.integrations.instantly import (
    HISTORICAL_LINK_BUILDING_CAMPAIGN_IDS,
    InstantlyControl,
    REQUIRED_LEGACY_CAMPAIGN_IDS,
    activation_blockers,
    analytics_blockers,
    matching_campaigns,
    campaign_is_link_building,
    has_reply_stop_instruction,
    reply_sync_campaigns,
    render_campaign_text,
    safe_campaign_payload,
    sender_is_healthy,
    verification_is_eligible,
)


class InstantlyControlTests(unittest.TestCase):
    def test_historical_membership_requires_explicit_last_contact_evidence(self) -> None:
        self.assertIsNone(_lead_last_contact_at({"status": 1}))
        self.assertIsNone(_lead_last_contact_at({"status": 3}))
        contacted_at = _lead_last_contact_at({"status": 1, "timestamp_last_contact": "2026-07-18T02:03:04Z"})
        self.assertIsNotNone(contacted_at)
        self.assertEqual(contacted_at.isoformat(), "2026-07-18T02:03:04+00:00")

    def test_only_explicit_verified_non_catchall_is_eligible(self) -> None:
        self.assertTrue(verification_is_eligible({"verification_status": "verified", "catch_all": False}))
        self.assertFalse(verification_is_eligible({"verification_status": "verified", "catch_all": True}))
        self.assertFalse(verification_is_eligible({"verification_status": "pending", "catch_all": False}))
        self.assertFalse(verification_is_eligible({"verification_status": "verified"}))

    def test_sender_health_is_fail_closed(self) -> None:
        self.assertTrue(sender_is_healthy({"status": 1, "setup_pending": False, "autofix_failed": False}))
        self.assertFalse(sender_is_healthy({"status": -1, "setup_pending": False}))
        self.assertFalse(sender_is_healthy({"status": 1}))

    def test_safe_campaign_payload_has_required_overrides(self) -> None:
        payload = safe_campaign_payload(batch_number=2, sender="sender@example.com", today="2026-07-15")
        self.assertTrue(payload["name"].startswith("LINK OS |"))
        self.assertEqual(payload["daily_limit"], 30)
        self.assertEqual(payload["daily_max_leads"], 10)
        self.assertFalse(payload["allow_risky_contacts"])
        self.assertFalse(payload["disable_bounce_protect"])
        self.assertFalse(payload["open_tracking"])
        self.assertFalse(payload["link_tracking"])
        self.assertFalse(payload["insert_unsubscribe_header"])
        self.assertTrue(has_reply_stop_instruction(payload["sequences"]))
        self.assertEqual(len(payload["sequences"][0]["steps"]), 6)
        self.assertEqual(
            [step["delay"] for step in payload["sequences"][0]["steps"]],
            [2, 3, 5, 7, 10, 14],
        )
        override = safe_campaign_payload(
            batch_number=2,
            sender="sender@example.com",
            provider_bounce_protection_enabled=False,
        )
        self.assertTrue(override["disable_bounce_protect"])

    def test_campaign_copy_renders_without_template_tokens(self) -> None:
        payload = safe_campaign_payload(batch_number=1, sender="sender@example.com")
        rendered = [
            render_campaign_text(
                step["variants"][0]["body"],
                {"firstName": "Laurence", "site_name": "Example Publisher"},
            )
            for step in payload["sequences"][0]["steps"]
        ]
        self.assertEqual(len(rendered), 6)
        self.assertTrue(rendered[0].startswith("Hi Laurence,"))
        self.assertIn("Example Publisher", rendered[0])
        self.assertTrue(all("{{" not in body for body in rendered))
        self.assertTrue(
            all(
                "\n\nRegards,\n\nLaurence\nLD Search\nldsearch.com.au\n\n"
                in body
                for body in rendered
            )
        )

    def test_test_email_uses_rendered_text_and_safe_html(self) -> None:
        calls = []

        def transport(path, **kwargs):
            calls.append((path, kwargs))
            return {"status": "success"}

        result = InstantlyControl(transport).send_test_email(
            sender="sender@example.com",
            recipient="owner@example.com",
            subject="[TEST] Article Submission Info Request",
            text="Hi Laurence,\n\nExample <Publisher>",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(calls[0][0], "/emails/test")
        body = calls[0][1]["body"]
        self.assertEqual(body["body"]["text"], "Hi Laurence,\n\nExample <Publisher>")
        self.assertIn("Example &lt;Publisher&gt;", body["body"]["html"])

    def test_paused_campaign_copy_update_requires_exact_readback(self) -> None:
        campaign = {"id": "campaign-1", "name": "Old", "status": 2, "sequences": []}

        def transport(path, **kwargs):
            if kwargs.get("method") == "PATCH":
                campaign.update(kwargs["body"])
                return campaign
            return dict(campaign)

        sequences = [{"steps": [{"delay": 3}]}]
        result = InstantlyControl(transport).update_paused_campaign_copy(
            "campaign-1",
            name="LINK OS | Public Evidence Pilot",
            sequences=sequences,
            insert_unsubscribe_header=False,
            disable_bounce_protect=True,
        )
        self.assertEqual(result["name"], "LINK OS | Public Evidence Pilot")
        self.assertEqual(result["sequences"], sequences)
        self.assertFalse(result["insert_unsubscribe_header"])
        self.assertTrue(result["disable_bounce_protect"])

    def test_metrics_thresholds_pause_at_boundary(self) -> None:
        self.assertEqual(
            analytics_blockers({"emails_sent_count": 100, "bounced_count": 3, "unsubscribed_count": 1}),
            ["rolling_bounce_rate_at_or_above_3_percent", "rolling_unsubscribe_rate_at_or_above_1_percent"],
        )

    def test_activation_requires_all_gates(self) -> None:
        blockers = activation_blockers(
            outreach_paused=True,
            account={"status": -1, "setup_pending": False},
            reserved_count=25,
            uploaded_count=24,
            pending_verification_count=1,
            sender_has_other_active_managed_campaign=True,
        )
        self.assertEqual(len(blockers), 5)

    def test_campaign_matching_preserves_required_and_name_matches(self) -> None:
        campaigns = [
            {"id": "7bc7d5e3-924d-463b-b875-8e8301be264b", "name": "unrelated renamed"},
            {"id": "custom", "name": "Guest Post Follow-up"},
            {"id": "skip", "name": "Sales"},
        ]
        self.assertEqual({row["id"] for row in matching_campaigns(campaigns)}, {"7bc7d5e3-924d-463b-b875-8e8301be264b", "custom"})
        self.assertTrue(campaign_is_link_building(campaigns[0]))
        self.assertTrue(campaign_is_link_building(campaigns[1]))
        self.assertFalse(campaign_is_link_building(campaigns[2]))
        self.assertEqual(len(HISTORICAL_LINK_BUILDING_CAMPAIGN_IDS), 13)

    def test_managed_campaigns_sync_replies_but_are_not_legacy_pause_targets(self) -> None:
        campaigns = [
            {"id": "managed", "name": "LINK OS | Verified Relaunch"},
            {"id": "legacy", "name": "Guest Post Follow-up"},
        ]
        self.assertEqual({row["id"] for row in matching_campaigns(campaigns)}, {"legacy"})
        self.assertEqual(
            {row["id"] for row in reply_sync_campaigns(campaigns)},
            {"legacy", "managed"},
        )

    def test_bulk_upload_uses_safe_flags_and_checks_count(self) -> None:
        calls = []

        def transport(path, **kwargs):
            calls.append((path, kwargs))
            return {"status": "success", "total_sent": len(kwargs["body"]["leads"])}

        result = InstantlyControl(transport).upload_leads("campaign", [{"email": f"u{i}@example.com"} for i in range(3)], chunk_size=2)
        self.assertTrue(result["matches"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][1]["body"]["verify_leads_on_import"])
        self.assertTrue(calls[0][1]["body"]["skip_if_in_workspace"])

    def test_bulk_upload_can_explicitly_skip_instantly_verification(self) -> None:
        calls = []

        def transport(path, **kwargs):
            calls.append((path, kwargs))
            return {"status": "success", "total_sent": len(kwargs["body"]["leads"])}

        result = InstantlyControl(transport).upload_leads(
            "campaign",
            [{"email": "public@example.com"}],
            verify_leads_on_import=False,
        )
        self.assertTrue(result["matches"])
        self.assertFalse(calls[0][1]["body"]["verify_leads_on_import"])

    def test_legacy_pause_guards_count_and_converts_unhealthy_to_manual_pause(self) -> None:
        campaigns = {
            campaign_id: {
                "id": campaign_id,
                "name": f"Required {index}",
                "status": -1,
            }
            for index, campaign_id in enumerate(REQUIRED_LEGACY_CAMPAIGN_IDS)
        }
        campaigns["name-match"] = {
            "id": "name-match",
            "name": "Guest Post Follow-up",
            "status": 1,
        }
        pause_calls = []

        def transport(path, **kwargs):
            if path == "/campaigns":
                return {"items": list(campaigns.values())}
            campaign_id = path.split("/")[2]
            if path.endswith("/pause"):
                pause_calls.append(campaign_id)
                campaigns[campaign_id]["status"] = 2
                return {}
            return dict(campaigns[campaign_id])

        result = InstantlyControl(transport).pause_legacy_campaigns(
            execute=True,
            expected_count=13,
        )
        self.assertEqual(result["matched"], 13)
        self.assertEqual(len(pause_calls), 13)
        self.assertTrue(all(row["paused"] for row in result["campaigns"]))


if __name__ == "__main__":
    unittest.main()
