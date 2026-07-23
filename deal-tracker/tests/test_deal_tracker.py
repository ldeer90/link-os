from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import deal_tracker.deals as deals_module
import deal_tracker.auth as auth_module
import app.main as app_main
from tools import discover_combined_link_prospect_contacts as contact_discovery
from tools import filter_likely_guest_post_candidates as guest_filter
from tools.import_csv_prospects_to_instantly import extract_emails
from deal_tracker.auth import SESSION_COOKIE, authenticate, create_session, create_user, current_user, hash_password, verify_password
from deal_tracker.classifier import classify_reply, extract_price_options, normalize_priced_domain
from deal_tracker.db import SCHEMA
from deal_tracker.deals import inbound_replies, list_catalogue_deals, publisher_entity_detail, save_reply_and_maybe_deal, scorecard_data, update_deal


class DealTrackerTests(unittest.TestCase):
    def patch_temp_db(self, db_path: Path):
        original_deals_connect = deals_module.connect
        original_deals_init = deals_module.init_db
        original_auth_connect = auth_module.connect
        original_auth_init = auth_module.init_db

        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc, traceback):
                result = super().__exit__(exc_type, exc, traceback)
                self.close()
                return result

        def temp_connect() -> sqlite3.Connection:
            connection = sqlite3.connect(db_path, factory=ClosingConnection)
            connection.row_factory = sqlite3.Row
            return connection

        def temp_init() -> None:
            with temp_connect() as connection:
                connection.executescript(SCHEMA)
                connection.commit()

        deals_module.connect = temp_connect
        deals_module.init_db = temp_init
        auth_module.connect = temp_connect
        auth_module.init_db = temp_init
        return temp_connect, temp_init, (original_deals_connect, original_deals_init, original_auth_connect, original_auth_init)

    def restore_temp_db(self, originals) -> None:
        original_deals_connect, original_deals_init, original_auth_connect, original_auth_init = originals
        deals_module.connect = original_deals_connect
        deals_module.init_db = original_deals_init
        auth_module.connect = original_auth_connect
        auth_module.init_db = original_auth_init

    def test_inbound_reply_filter_uses_ue_type_two(self) -> None:
        original = deals_module.list_campaign_emails
        try:
            deals_module.list_campaign_emails = lambda _campaign_id: [{"id": "out", "ue_type": 1}, {"id": "in", "ue_type": 2}]
            self.assertEqual([item["id"] for item in inbound_replies("campaign")], ["in"])
        finally:
            deals_module.list_campaign_emails = original

    def test_mamamia_auto_reply_is_not_confirmed_deal(self) -> None:
        reply = classify_reply(
            "Hello! Thank you for sharing your submission with us at Mamamia. "
            "Due to the high volume of submissions we receive, only applicants with successful submissions will be contacted. "
            "If your submission is related to Lady Startup, please email hello@ladystartup.com.au"
        )
        self.assertEqual(reply.classification, "auto_reply")
        self.assertFalse(reply.create_deal)

    def test_rejection_ignores_quoted_original_thread_terms(self) -> None:
        reply = classify_reply(
            "Hi Laurence,\n\nThis isn't one for us.\n\nRegards,\nChris\n\n"
            "-----Original Message-----\n"
            "Do you accept sponsored articles, advertorials, or paid placements?"
        )
        self.assertEqual(reply.classification, "rejected")
        self.assertFalse(reply.create_deal)

    def test_domain_price_list_with_content_restrictions_is_deal_terms(self) -> None:
        reply = classify_reply(
            "Thank you for asking about publishing do follow articles and links. "
            "We offer native advertising articles, do follow links and advertorials. "
            "Pricing: Link placements are generally $15 USD for general business links. "
            "$100 AUD TheTimes.au. $25 USD Businesses.com.au. "
            "Articles may have 2 business links. "
            "We do not accept content about essay writing, casino, gambling, lottery or personal loans."
        )
        self.assertEqual(reply.classification, "deal_terms")
        self.assertTrue(reply.create_deal)
        self.assertEqual(reply.deal_status, "confirmed")
        self.assertEqual(reply.price_amount, 15)
        self.assertEqual(reply.price_currency, "USD")

    def test_link_insertion_price_is_structured(self) -> None:
        reply = classify_reply(
            "Option 1 Article Only $110 USD. "
            "Option 3: Link Insertion $150 ($ USD). "
            "The rate for link insertion is $150 USD per article."
        )
        self.assertEqual(reply.classification, "deal_terms")
        self.assertEqual(reply.link_insertion_cost_amount, 150)
        self.assertEqual(reply.link_insertion_cost_currency, "USD")
        self.assertIn("link insertion", reply.link_insertion_notes.lower())

    def test_media_kit_only_reply_stays_manual_review(self) -> None:
        reply = classify_reply(
            "Thanks Laurence. We do have a media kit for sponsored content. "
            "Please review it here: https://docs.google.com/document/d/example and let me know."
        )
        self.assertEqual(reply.classification, "needs_review")
        self.assertFalse(reply.create_deal)

    def test_priced_domain_lines_are_extracted_from_network_reply(self) -> None:
        reply_text = """
        Popular sites

        $100 AUD TheTimes .au
        $50 USD BusinessDailyMedia .com
        $25 USD Businesses .com.au
        $20 USD The Australasian .com.au
        $15 USD GuestPosting .com.au
        $10 USD TimeMagazine .com.au

        We do not accept content about essay writing, casino, gambling, lottery or personal loans.
        """
        options = {option.root_domain: option for option in extract_price_options(reply_text)}
        self.assertEqual(options["thetimes.au"].price_amount, 100)
        self.assertEqual(options["thetimes.au"].price_currency, "AUD")
        self.assertEqual(options["businessdailymedia.com"].price_amount, 50)
        self.assertEqual(options["businesses.com.au"].price_amount, 25)
        self.assertEqual(options["theaustralasian.com.au"].price_amount, 20)
        self.assertEqual(options["guestposting.com.au"].price_amount, 15)
        self.assertEqual(options["timemagazine.com.au"].price_amount, 10)

    def test_spaced_domain_normalization(self) -> None:
        self.assertEqual(normalize_priced_domain("Women. net.au")[0], "women.net.au")
        self.assertEqual(normalize_priced_domain("agency.businesses .com.au")[0], "agency.businesses.com.au")

    def test_price_reply_creates_confirmed_deal_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "deals.db"
            temp_connect, _, originals = self.patch_temp_db(db_path)
            try:
                email = {
                    "id": "reply-1",
                    "lead": "sales@example.com.au",
                    "thread_id": "thread-1",
                    "subject": "Re: Article Submission Info Request",
                    "from_address_email": "sales@example.com.au",
                    "to_address_email_list": "laurence.d@ldsearch.com.au",
                    "timestamp_email": "2026-05-25T01:00:00Z",
                    "body": {"text": "Yes, sponsored posts are $350 AUD. Please send 800 words and one dofollow link."},
                }
                lead = {
                    "id": "lead-1",
                    "email": "sales@example.com.au",
                    "company_name": "Example Publisher",
                    "website": "https://example.com.au",
                    "payload": {"root_domain": "example.com.au", "site_name": "Example Publisher"},
                }
                first = save_reply_and_maybe_deal("campaign", email, lead)
                second = save_reply_and_maybe_deal("campaign", email, lead)
                with temp_connect() as connection:
                    deal_rows = connection.execute("select * from link_deals").fetchall()
                    reply_rows = connection.execute("select * from instantly_reply_sync").fetchall()
                self.assertEqual(first["deal_created"], 1)
                self.assertEqual(first["deal_updated"], 0)
                self.assertFalse(first["duplicate_reply"])
                self.assertTrue(second["duplicate_reply"])
                self.assertEqual(len(deal_rows), 1)
                self.assertEqual(len(reply_rows), 1)
                self.assertEqual(deal_rows[0]["deal_status"], "confirmed")
                self.assertEqual(deal_rows[0]["price_amount"], 350)
                self.assertEqual(deal_rows[0]["publisher_cost_amount"], 350)
                self.assertIsNone(deal_rows[0]["link_insertion_cost_amount"])
                self.assertEqual(deal_rows[0]["is_listed"], 0)
                self.assertEqual(deal_rows[0]["admin_review_status"], "needs_review")
                self.assertIn("dofollow", deal_rows[0]["link_requirements"].lower())
            finally:
                self.restore_temp_db(originals)

    def test_network_price_reply_creates_one_deal_per_priced_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "deals.db"
            temp_connect, _, originals = self.patch_temp_db(db_path)
            try:
                email = {
                    "id": "reply-network",
                    "lead": "editor@thetimes.com.au",
                    "thread_id": "thread-network",
                    "subject": "Re: Article Submission Info Request",
                    "from_address_email": "editor@thetimes.com.au",
                    "to_address_email_list": "laurence.d@ldsearch.com.au",
                    "timestamp_email": "2026-05-25T01:00:00Z",
                    "body": {
                        "text": """
                        We offer fast publishing.
                        $100 AUD TheTimes .au
                        $50 USD BusinessDailyMedia .com
                        $25 USD Businesses .com.au
                        Articles may have 2 business links plus up to 2 more reference links included in the price.
                        No word limit but articles must have 420 words at least.
                        Posts are NOT marked as sponsored.
                        """
                    },
                }
                lead = {
                    "id": "lead-network",
                    "email": "editor@thetimes.com.au",
                    "company_name": "The Times",
                    "website": "https://thetimes.com.au",
                    "payload": {"root_domain": "thetimes.com.au", "site_name": "The Times"},
                }
                first = save_reply_and_maybe_deal("campaign", email, lead)
                second = save_reply_and_maybe_deal("campaign", email, lead)
                with temp_connect() as connection:
                    deal_rows = connection.execute("select root_domain, price_amount, price_currency, link_requirements, writing_requirements from link_deals order by root_domain").fetchall()
                self.assertEqual(first["deal_created"], 3)
                self.assertTrue(second["duplicate_reply"])
                self.assertEqual(len(deal_rows), 3)
                self.assertEqual([row["root_domain"] for row in deal_rows], ["businessdailymedia.com", "businesses.com.au", "thetimes.au"])
                self.assertEqual(deal_rows[2]["price_amount"], 100)
                self.assertEqual(deal_rows[2]["price_currency"], "AUD")
                self.assertIn("business links", deal_rows[0]["link_requirements"].lower())
                self.assertIn("420 words", deal_rows[0]["writing_requirements"].lower())
            finally:
                self.restore_temp_db(originals)

    def test_attachment_reply_is_stored_but_not_created_as_deal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "deals.db"
            temp_connect, _, originals = self.patch_temp_db(db_path)
            try:
                email = {
                    "id": "reply-attachment",
                    "lead": "sales@example.com.au",
                    "thread_id": "thread-attachment",
                    "subject": "Re: Article Submission Info Request",
                    "from_address_email": "sales@example.com.au",
                    "to_address_email_list": "laurence.d@ldsearch.com.au",
                    "timestamp_email": "2026-05-25T01:00:00Z",
                    "attachments": [{"name": "rate-card.pdf"}],
                    "body": {"text": "Please see attached media kit for pricing and packages."},
                }
                lead = {
                    "id": "lead-attachment",
                    "email": "sales@example.com.au",
                    "company_name": "Example Publisher",
                    "website": "https://example.com.au",
                    "payload": {"root_domain": "example.com.au", "site_name": "Example Publisher"},
                }
                result = save_reply_and_maybe_deal("campaign", email, lead)
                with temp_connect() as connection:
                    deal_rows = connection.execute("select * from link_deals").fetchall()
                    reply_row = connection.execute("select raw_json from instantly_reply_sync where email_id='reply-attachment'").fetchone()
                self.assertTrue(result["stored_reply"])
                self.assertTrue(result["attachment_or_unclear"])
                self.assertEqual(result["deal_created"], 0)
                self.assertEqual(len(deal_rows), 0)
                self.assertIn("rate-card.pdf", reply_row["raw_json"])
            finally:
                self.restore_temp_db(originals)

    def test_manual_fields_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "deals.db"
            temp_connect, temp_init, originals = self.patch_temp_db(db_path)
            try:
                temp_init()
                with temp_connect() as connection:
                    connection.execute(
                        """
                        insert into link_deals (
                            root_domain, site_name, contact_email, instantly_thread_id, deal_status, placement_type,
                            price_currency, domain_trust_source, created_at, updated_at
                        ) values ('example.com.au', 'Example', 'sales@example.com.au', 'thread-edit', 'needs_review', 'guest_post', 'AUD', 'manual_seranking_later', 'now', 'now')
                        """
                    )
                    connection.commit()
                update_deal(
                    1,
                    {
                        "deal_status": "confirmed",
                        "placement_type": "sponsored_post",
                        "price_amount": "275",
                        "domain_trust": "42",
                        "writing_requirements": "900 words",
                        "reseller_price_amount": "420",
                        "visibility_min_tier": "trusted",
                        "is_listed": "1",
                        "admin_review_status": "approved",
                    },
                )
                with temp_connect() as connection:
                    row = connection.execute("select * from link_deals where id=1").fetchone()
                self.assertEqual(row["deal_status"], "confirmed")
                self.assertEqual(row["placement_type"], "sponsored_post")
                self.assertEqual(row["price_amount"], 275)
                self.assertEqual(row["domain_trust"], "42")
                self.assertEqual(row["writing_requirements"], "900 words")
                self.assertEqual(row["reseller_price_amount"], 420)
                update_deal(
                    1,
                    {
                        "link_insertion_cost_amount": "150",
                        "link_insertion_cost_currency": "USD",
                        "link_insertion_reseller_price_amount": "250",
                        "link_insertion_reseller_price_currency": "AUD",
                        "link_insertion_notes": "Requires article expert approval.",
                    },
                )
                with temp_connect() as connection:
                    row = connection.execute("select * from link_deals where id=1").fetchone()
                self.assertEqual(row["link_insertion_cost_amount"], 150)
                self.assertEqual(row["link_insertion_cost_currency"], "USD")
                self.assertEqual(row["link_insertion_reseller_price_amount"], 250)
                self.assertEqual(row["link_insertion_reseller_price_currency"], "AUD")
                self.assertIn("expert", row["link_insertion_notes"])
                self.assertEqual(row["reseller_price_amount"], 420)
                self.assertEqual(row["price_band"], "$$")
                self.assertEqual(row["visibility_min_tier"], "trusted")
                self.assertEqual(row["is_listed"], 1)
                self.assertEqual(row["admin_review_status"], "approved")
            finally:
                self.restore_temp_db(originals)

    def test_password_hash_authentication_and_session_lookup(self) -> None:
        self.assertTrue(verify_password("secret", hash_password("secret")))
        self.assertFalse(verify_password("wrong", hash_password("secret")))
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "auth.db"
            _, temp_init, originals = self.patch_temp_db(db_path)
            try:
                temp_init()
                user_id = create_user("agency@example.com", "secret", "agency", "trusted", True)
                user = authenticate("agency@example.com", "secret")
                self.assertEqual(user["id"], user_id)
                token = create_session(user_id)
                request = type("RequestLike", (), {"cookies": {SESSION_COOKIE: token}})()
                self.assertEqual(current_user(request)["email"], "agency@example.com")
            finally:
                self.restore_temp_db(originals)

    def test_agency_tier_catalogue_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalogue.db"
            temp_connect, temp_init, originals = self.patch_temp_db(db_path)
            try:
                temp_init()
                with temp_connect() as connection:
                    connection.execute(
                        """
                        insert into link_deals (
                            root_domain, site_name, contact_email, instantly_thread_id, deal_status, placement_type,
                            price_currency, domain_trust_source, created_at, updated_at, industry, tld,
                            reseller_price_amount, reseller_price_currency, price_band, visibility_min_tier,
                            is_listed, admin_review_status
                        ) values
                        ('basic.com.au', 'Basic Site', 'a@basic.com.au', 't1', 'confirmed', 'guest_post', 'AUD', 'manual', 'now', 'now', 'Lifestyle', 'com.au', 220, 'AUD', '$$', 'basic', 1, 'approved'),
                        ('trusted.com.au', 'Trusted Site', 'a@trusted.com.au', 't2', 'confirmed', 'guest_post', 'AUD', 'manual', 'now', 'now', 'Finance', 'com.au', 520, 'AUD', '$$$', 'trusted', 1, 'approved'),
                        ('draft.com.au', 'Draft Site', 'a@draft.com.au', 't3', 'confirmed', 'guest_post', 'AUD', 'manual', 'now', 'now', 'Travel', 'com.au', 120, 'AUD', '$', 'basic', 0, 'needs_review')
                        """
                    )
                    connection.commit()
                basic_rows = list_catalogue_deals({"role": "agency", "visibility_tier": "basic"}, {})
                trusted_rows = list_catalogue_deals({"role": "agency", "visibility_tier": "trusted"}, {})
                self.assertEqual([row["root_domain"] for row in basic_rows], ["basic.com.au"])
                self.assertEqual([row["root_domain"] for row in trusted_rows], ["basic.com.au", "trusted.com.au"])
            finally:
                self.restore_temp_db(originals)

    def test_agency_detail_hides_publisher_cost(self) -> None:
        original_current_user = app_main.current_user
        original_get_catalogue_deal = app_main.get_catalogue_deal
        original_aud_estimate_label = app_main.aud_estimate_label
        try:
            app_main.current_user = lambda _request: {"id": 7, "role": "agency", "visibility_tier": "trusted"}
            app_main.aud_estimate_label = lambda amount, currency: ""
            app_main.get_catalogue_deal = lambda _deal_id, _user: {
                "id": 44,
                "root_domain": "examplepublisher.com.au",
                "site_name": "Example Publisher",
                "industry": "Lifestyle",
                "tld": "com.au",
                "domain_trust": "41",
                "placement_type": "guest_post",
                "reseller_price_amount": 300,
                "reseller_price_currency": "AUD",
                "publisher_cost_amount": 17.31,
                "publisher_cost_currency": "USD",
                "link_insertion_cost_amount": 12.5,
                "link_insertion_cost_currency": "USD",
                "link_insertion_reseller_price_amount": 220,
                "link_insertion_reseller_price_currency": "AUD",
                "link_insertion_notes": "Agency visible link insertion notes.",
                "price_band": "$$",
                "writing_requirements": "700 words",
                "link_requirements": "One link",
                "turnaround_time": "Two days",
                "quality_notes": "Internal only",
            }
            request = type("RequestLike", (), {"cookies": {}})()
            html = app_main.catalogue_detail(request, 44).body.decode()
            self.assertIn("examplepublisher.com.au", html)
            self.assertIn("AUD 300", html)
            self.assertIn("AUD 220", html)
            self.assertNotIn("17.31", html)
            self.assertNotIn("12.5", html)
            self.assertNotIn("publisher_cost", html)
        finally:
            app_main.current_user = original_current_user
            app_main.get_catalogue_deal = original_get_catalogue_deal
            app_main.aud_estimate_label = original_aud_estimate_label

    def test_publisher_entity_backfill_groups_by_contact_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scorecard.db"
            temp_connect, temp_init, originals = self.patch_temp_db(db_path)
            try:
                temp_init()
                with temp_connect() as connection:
                    connection.execute(
                        """
                        insert into link_deals (
                            root_domain, site_name, contact_email, instantly_thread_id, deal_status, placement_type,
                            price_currency, publisher_cost_amount, publisher_cost_currency, reseller_price_amount,
                            reseller_price_currency, domain_trust_source, created_at, updated_at, is_listed, admin_review_status
                        ) values
                        ('siteone.com.au', 'Site One', 'editor@network.com.au', 't1', 'confirmed', 'advertorial', 'AUD', 20, 'AUD', 80, 'AUD', 'manual', 'now', 'now', 1, 'approved'),
                        ('sitetwo.com.au', 'Site Two', 'editor@network.com.au', 't2', 'confirmed', 'advertorial', 'AUD', 25, 'AUD', 85, 'AUD', 'manual', 'now', 'now', 1, 'approved'),
                        ('solo.com.au', 'Solo', 'hello@solo.com.au', 't3', 'confirmed', 'guest_post', 'AUD', 120, 'AUD', 240, 'AUD', 'manual', 'now', 'now', 1, 'approved')
                        """
                    )
                    connection.commit()
                data = scorecard_data()
                self.assertEqual(data["totals"]["total_domains"], 3)
                self.assertEqual(data["totals"]["managing_entities"], 2)
                network = next(entity for entity in data["entities"] if entity["primary_email"] == "editor@network.com.au")
                self.assertEqual(network["domain_count"], 2)
                detail = publisher_entity_detail(network["id"])
                self.assertEqual({row["root_domain"] for row in detail["deals"]}, {"siteone.com.au", "sitetwo.com.au"})
            finally:
                self.restore_temp_db(originals)

    def test_scorecard_uses_aud_normalization_for_average_and_margin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scorecard.db"
            temp_connect, temp_init, originals = self.patch_temp_db(db_path)
            original_convert = deals_module.convert_to_aud
            try:
                temp_init()
                deals_module.convert_to_aud = lambda amount, currency: float(amount) * 2 if currency == "USD" else float(amount)
                with temp_connect() as connection:
                    connection.execute(
                        """
                        insert into link_deals (
                            root_domain, site_name, contact_email, instantly_thread_id, deal_status, placement_type,
                            price_currency, publisher_cost_amount, publisher_cost_currency, reseller_price_amount,
                            reseller_price_currency, domain_trust_source, created_at, updated_at, is_listed, admin_review_status
                        ) values
                        ('usd.com', 'USD Site', 'a@usd.com', 't1', 'confirmed', 'guest_post', 'USD', 50, 'USD', 200, 'AUD', 'manual', 'now', 'now', 1, 'approved'),
                        ('aud.com.au', 'AUD Site', 'a@aud.com.au', 't2', 'confirmed', 'guest_post', 'AUD', 100, 'AUD', 180, 'AUD', 'manual', 'now', 'now', 1, 'approved')
                        """
                    )
                    connection.commit()
                totals = scorecard_data()["totals"]
                self.assertEqual(totals["avg_publisher_cost_aud"], 100)
                self.assertEqual(totals["avg_reseller_price_aud"], 190)
                self.assertEqual(totals["avg_margin_aud"], 90)
            finally:
                deals_module.convert_to_aud = original_convert
                self.restore_temp_db(originals)

    def test_scorecard_keeps_link_insertion_metrics_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scorecard.db"
            temp_connect, temp_init, originals = self.patch_temp_db(db_path)
            try:
                temp_init()
                with temp_connect() as connection:
                    connection.execute(
                        """
                        insert into link_deals (
                            root_domain, site_name, contact_email, instantly_thread_id, deal_status, placement_type,
                            price_currency, publisher_cost_amount, publisher_cost_currency, reseller_price_amount,
                            reseller_price_currency, link_insertion_cost_amount, link_insertion_cost_currency,
                            link_insertion_reseller_price_amount, link_insertion_reseller_price_currency,
                            domain_trust_source, created_at, updated_at, is_listed, admin_review_status
                        ) values
                        ('links.com.au', 'Links', 'a@links.com.au', 't1', 'confirmed', 'guest_post', 'AUD', 110, 'AUD', 220, 'AUD', 70, 'AUD', 140, 'AUD', 'manual', 'now', 'now', 1, 'approved')
                        """
                    )
                    connection.commit()
                totals = scorecard_data()["totals"]
                self.assertEqual(totals["avg_publisher_cost_aud"], 110)
                self.assertEqual(totals["avg_reseller_price_aud"], 220)
                self.assertEqual(totals["avg_link_insertion_cost_aud"], 70)
                self.assertEqual(totals["avg_link_insertion_reseller_aud"], 140)
            finally:
                self.restore_temp_db(originals)

    def test_agency_user_cannot_access_scorecard(self) -> None:
        original_current_user = app_main.current_user
        try:
            app_main.current_user = lambda _request: {"id": 7, "role": "agency", "visibility_tier": "trusted"}
            request = type("RequestLike", (), {"cookies": {}})()
            response = app_main.admin_scorecard(request)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/catalogue")
        finally:
            app_main.current_user = original_current_user

    def test_combined_contact_discovery_parses_kept_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "combined.csv"
            path.write_text(
                "root_domain,status,paid_link_likelihood_score,link_types,brands,example_source_urls,source_files\n"
                "example.com.au,kept,92,media / editorial,Brand A,https://example.com.au/article | https://example.com.au/contact,input.csv\n"
                "skip.com.au,excluded,50,general citation,Brand B,https://skip.com.au/a,input.csv\n",
                encoding="utf-8",
            )
            prospects = contact_discovery.read_combined_prospects(path)
        self.assertEqual(len(prospects), 1)
        self.assertEqual(prospects[0].root_domain, "example.com.au")
        self.assertEqual(prospects[0].paid_link_likelihood_score, 92)
        self.assertEqual(prospects[0].example_source_urls[1], "https://example.com.au/contact")

    def test_search_result_extraction_decodes_duckduckgo_uddg(self) -> None:
        body = '<a class="result__a" href="/l/?kh=-1&uddg=https%3A%2F%2Fexample.com.au%2Fcontact">Contact</a>'
        self.assertEqual(contact_discovery.extract_search_result_urls(body), ["https://example.com.au/contact"])

    def test_email_cleanup_handles_cloudflare_and_u003e_prefix(self) -> None:
        encoded = "6e070008012e0b160f031e020b400d0103400f1b"
        emails = extract_emails(f'<a data-cfemail="{encoded}"></a> u003eads@publisher.com.au')
        self.assertIn("info@example.com.au", emails)
        self.assertIn("ads@publisher.com.au", emails)
        self.assertFalse(any(email.startswith("u003e") for email in emails))

    def test_contact_discovery_recommended_actions(self) -> None:
        ready = contact_discovery.ContactHit(
            email="advertising@example.com.au",
            email_type="advertising",
            confidence=91,
            source_url="https://example.com.au/advertise",
            discovery_method="known_contact_path",
            is_off_domain_email=False,
        )
        off_domain = contact_discovery.ContactHit(
            email="media@publishernetwork.com.au",
            email_type="advertising",
            confidence=88,
            source_url="https://example.com.au/contact",
            discovery_method="known_contact_path",
            is_off_domain_email=True,
        )
        route = contact_discovery.ContactRoute("https://example.com.au/contact", "known_contact_path", "contact")
        self.assertEqual(contact_discovery.recommended_action(ready, []), "ready_for_import")
        self.assertEqual(contact_discovery.recommended_action(off_domain, []), "manual_review")
        self.assertEqual(contact_discovery.recommended_action(None, [route]), "contact_form_only")
        self.assertEqual(contact_discovery.recommended_action(None, []), "no_contact_found")

    def strict_filter_row(
        self,
        email: str,
        source_url: str,
        best_type: str = "media / editorial",
        confidence: int = 95,
        is_off_domain_email: bool = False,
    ) -> guest_filter.SourceRow:
        return guest_filter.SourceRow(
            root_domain="example.com.au",
            site_name="Example",
            email=email,
            email_type="advertising" if "advertis" in email else "generic",
            confidence=confidence,
            source_url=source_url,
            discovery_method="known_contact_path",
            is_off_domain_email=is_off_domain_email,
            best_type=best_type,
            paid_link_likelihood_score=80,
            brands="",
            example_source_urls="https://example.com.au/article",
            all_candidates="",
            recommended_action="ready_for_import",
        )

    def test_strict_filter_advertise_with_us_role_email_is_tier_one(self) -> None:
        row = self.strict_filter_row("advertising@example.com.au", "https://example.com.au/advertise-with-us")
        evidence = guest_filter.Evidence(row.source_url, "Advertise with us. Sponsored article packages and media kit available.", True)
        result = guest_filter.score_row(row, evidence)
        self.assertEqual(result.tier, "Tier 1")
        self.assertIn("commercial_placement_terms", " ".join(result.acceptance_signals))

    def test_strict_filter_editorial_guidelines_editor_email_is_kept(self) -> None:
        row = self.strict_filter_row("editor@example.com.au", "https://example.com.au/editorial-guidelines")
        evidence = guest_filter.Evidence(row.source_url, "Editorial guidelines for contributors. Submit an article for review.", True)
        result = guest_filter.score_row(row, evidence)
        self.assertIn(result.tier, {"Tier 1", "Tier 2"})

    def test_strict_filter_corporate_media_relations_without_placement_is_not_kept(self) -> None:
        row = self.strict_filter_row("media@example.com.au", "https://example.com.au/newsroom/media-releases")
        evidence = guest_filter.Evidence(row.source_url, "Media releases and investor relations. Contact our media relations team for press enquiries.", True)
        result = guest_filter.score_row(row, evidence)
        self.assertIn(result.tier, {"Manual Review", "Exclude"})

    def test_strict_filter_sales_product_page_is_excluded_without_sponsored_terms(self) -> None:
        row = self.strict_filter_row("sales@example.com.au", "https://example.com.au/products/commercial-widget", best_type="blog / guide")
        evidence = guest_filter.Evidence(row.source_url, "Buy commercial widgets from our sales team. Product details and support.", True)
        result = guest_filter.score_row(row, evidence)
        self.assertEqual(result.tier, "Exclude")

    def test_strict_filter_generic_email_on_write_for_us_page_is_kept(self) -> None:
        row = self.strict_filter_row("info@example.com.au", "https://example.com.au/write-for-us", best_type="blog / guide")
        evidence = guest_filter.Evidence(row.source_url, "Write for us. We accept guest post submissions from contributors.", True)
        result = guest_filter.score_row(row, evidence)
        self.assertIn(result.tier, {"Tier 2", "Tier 3"})


if __name__ == "__main__":
    unittest.main()
