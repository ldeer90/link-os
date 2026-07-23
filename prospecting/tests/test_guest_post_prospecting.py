from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from guest_post_prospecting.apify_google import apify_actor_input, apify_items_to_rows, ApifyGoogleHarvestConfig
from guest_post_prospecting.classifier import classify_niche, classify_opportunity, quality_tier
from guest_post_prospecting.db import SCHEMA
import guest_post_prospecting.deals as deals_module
from guest_post_prospecting.deals import classify_reply, inbound_replies, save_reply_and_maybe_deal, update_deal
from guest_post_prospecting.email_discovery import classify_email_type, email_allowed, emails_from_html, emails_from_mailto, normalize_email
from guest_post_prospecting.instantly_campaigns import (
    CampaignBuildConfig,
    campaign_payload,
    dedupe_rows_by_email,
    lead_payload,
    selected_contact_rows,
)
from guest_post_prospecting.missing_email_discovery import (
    brand_search_queries,
    known_path_urls,
    parse_robots_sitemaps,
    parse_sitemap_urls,
    role_pattern_candidates,
    select_missing_rows,
    should_scrape_public_result,
    sort_candidates,
    public_email_allowed,
)
from guest_post_prospecting.paid_link_acceptance import score_paid_link_acceptance, score_rows
from guest_post_prospecting.search import parse_search_results
from guest_post_prospecting.search_harvest import (
    HARVEST_FIELDS,
    append_rows,
    operator_type,
    query_family,
    read_harvest_csv,
)
from guest_post_prospecting.search_email_scrape import candidate_urls_from_page, harvest_domain_rows
from guest_post_prospecting.universe import (
    common_crawl_patterns,
    generate_search_queries,
    outward_links_from_page,
    parse_cdxj_lines,
)
import guest_post_prospecting.universe as universe_module
from guest_post_prospecting.utils import clean_domain, is_au_domain, is_australian_candidate_domain, write_csv


class GuestPostProspectingTests(unittest.TestCase):
    def test_domain_normalization(self) -> None:
        self.assertEqual(clean_domain("https://www.example.com.au/path"), "example.com.au")
        self.assertTrue(is_au_domain("example.com.au"))
        self.assertTrue(is_au_domain("example.au"))
        self.assertFalse(is_au_domain("example.com"))
        self.assertTrue(is_australian_candidate_domain("example.com", 'site:.com "write for us" Australia'))
        self.assertFalse(is_australian_candidate_domain("example.com", 'site:.com "write for us" Canada'))

    def test_manual_seed_can_explicitly_queue_a_public_non_au_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prospecting.db"

            def connect() -> sqlite3.Connection:
                connection = sqlite3.connect(path)
                connection.row_factory = sqlite3.Row
                connection.executescript(SCHEMA)
                return connection

            original_connect = universe_module.connect
            universe_module.connect = connect
            try:
                result = universe_module.import_seed_domains(
                    ["example.com"],
                    source_label="link_os_test",
                    allow_non_au=True,
                )
                self.assertGreater(result["saved_candidate_urls"], 0)
                self.assertGreater(result["queued_urls"], 0)
                with connect() as connection:
                    domains = connection.execute(
                        "select root_domain from candidate_domains where root_domain='example.com'"
                    ).fetchall()
                    queued = connection.execute(
                        "select count(*) from crawl_queue where root_domain='example.com'"
                    ).fetchone()[0]
                self.assertEqual(len(domains), 1)
                self.assertGreater(queued, 0)
            finally:
                universe_module.connect = original_connect

    def test_email_extraction_and_ranking(self) -> None:
        html = '<a href="mailto:Editor@Example.com.au">Email</a> info [at] example [dot] com.au'
        self.assertIn("editor@example.com.au", emails_from_mailto(html))
        self.assertIn("info@example.com.au", emails_from_html(html))
        self.assertEqual(classify_email_type("editor@example.com.au"), "editor")
        self.assertEqual(classify_email_type("hello@example.com.au"), "generic")
        self.assertTrue(email_allowed("advertising@example.com.au", "example.com.au"))
        self.assertFalse(email_allowed("person@gmail.com", "example.com.au"))
        self.assertFalse(email_allowed("noreply@example.com.au", "example.com.au"))
        self.assertEqual(normalize_email("asset@hero-image.png"), "")

    def test_opportunity_and_quality_classification(self) -> None:
        match = classify_opportunity("Advertise with us and request our media kit", "https://example.com.au/advertise")
        self.assertEqual(match.opportunity_type, "advertising_media_kit")
        self.assertTrue(match.has_media_kit)
        self.assertEqual(classify_niche("Australian travel magazine and tourism guide"), "travel")
        self.assertEqual(
            quality_tier(
                opportunity_type="sponsored_post",
                best_email_type="advertising",
                has_clear_opportunity_page=True,
                reject_reason="",
            ),
            "A",
        )
        self.assertEqual(
            quality_tier(
                opportunity_type="unclear_contact_only",
                best_email_type="",
                has_clear_opportunity_page=False,
                reject_reason="no_usable_business_email",
            ),
            "Reject",
        )

    def test_search_result_parser_filters_to_com_au(self) -> None:
        html = """
        <html><body>
          <a href="https://example.com.au/write-for-us">Write for us</a>
          <a href="https://example.au/write-for-us">Write for us AU</a>
          <a href="https://example.com/not-au">Not AU</a>
          <a href="https://www.bing.com/search?q=x">Bing</a>
        </body></html>
        """
        results = parse_search_results(html, "bing", 'site:.com.au "write for us"', Path("cache.html"))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].root_domain, "example.com.au")
        self.assertEqual(results[0].matched_phrase, "write for us")

    def test_csv_export_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            write_csv(path, [{"root_domain": "example.com.au", "quality_tier": "A"}], ["root_domain", "quality_tier"])
            self.assertEqual(path.read_text().splitlines()[0], "root_domain,quality_tier")

    def test_universe_query_generation_is_broad_and_deduped(self) -> None:
        queries = generate_search_queries()
        self.assertGreater(len(queries), 100)
        self.assertEqual(len(queries), len(set(queries)))
        self.assertIn('site:.com.au "write for us"', queries)
        self.assertIn('site:.com.au "media kit" travel', queries)
        self.assertIn('site:.com.au "pitch us"', queries)
        self.assertIn('site:.com.au inurl:guest-post-guidelines', queries)
        self.assertIn('site:.au "guest post"', queries)
        self.assertIn('site:.com "guest post" "Australia"', queries)

    def test_common_crawl_cdxj_parser(self) -> None:
        raw = "\n".join(
            [
                '{"url":"https://example.com.au/write-for-us","status":"200"}',
                '{"url":"https://not-au.com/write-for-us","status":"200"}',
                'not-json',
            ]
        )
        records = parse_cdxj_lines(raw, "*.com.au/write-for-us*", "CC-MAIN-TEST")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].root_domain, "example.com.au")
        self.assertEqual(records[0].matched_phrase, "write for us")

    def test_common_crawl_patterns_include_known_paths(self) -> None:
        patterns = common_crawl_patterns()
        self.assertIn("*.com.au/write-for-us*", patterns)
        self.assertIn("*.com.au/*/media-kit*", patterns)

    def test_outward_links_are_au_and_contextual(self) -> None:
        html = """
        <a href="https://partner.com.au/blog">Partner blog</a>
        <a href="https://irrelevant.com.au/shop">Shop</a>
        <a href="https://external.com/news">Not AU</a>
        <a href="/resources">Same site resources</a>
        """
        links = outward_links_from_page("https://example.com.au/write-for-us", "example.com.au", html)
        self.assertEqual(links, ["https://partner.com.au/blog"])

    def test_search_harvest_query_metadata(self) -> None:
        query = 'site:.com.au inurl:advertising "media kit" -jobs'
        self.assertEqual(query_family(query), "commercial")
        self.assertEqual(operator_type(query), "inurl:|site:|negative")

    def test_search_harvest_csv_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harvest.csv"
            row = {field: "" for field in HARVEST_FIELDS}
            row.update(
                {
                    "source_engine": "bing",
                    "query": 'site:.com.au "write for us"',
                    "result_url": "https://example.com.au/write-for-us",
                    "root_domain": "example.com.au",
                    "matched_phrase": "write for us",
                }
            )
            self.assertEqual(append_rows(path, [row]), 1)
            rows = read_harvest_csv(path)
            self.assertEqual(rows[0]["root_domain"], "example.com.au")
            self.assertEqual(rows[0]["matched_phrase"], "write for us")

    def test_email_scrape_uses_result_and_discovered_links(self) -> None:
        html = """
        <a href="/contact">Contact</a>
        <a href="/advertising/">Advertising</a>
        <a href="/random">Random</a>
        """
        urls = candidate_urls_from_page("example.com.au", "https://example.com.au/write-for-us", html, 5)
        self.assertIn("https://example.com.au/write-for-us", urls)
        self.assertIn("https://example.com.au/", urls)
        self.assertIn("https://example.com.au/contact", urls)
        self.assertIn("https://example.com.au/advertising", urls)
        self.assertNotIn("https://example.com.au/random", urls)

    def test_harvest_domain_rows_dedupes_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harvest.csv"
            rows = []
            for url in ["https://example.com.au/write-for-us", "https://example.com.au/contact"]:
                row = {field: "" for field in HARVEST_FIELDS}
                row.update({"result_url": url, "root_domain": "example.com.au", "matched_phrase": "write for us"})
                rows.append(row)
            append_rows(path, rows)
            self.assertEqual(len(harvest_domain_rows([path])), 1)

    def test_apify_google_items_flatten_to_harvest_rows(self) -> None:
        config = ApifyGoogleHarvestConfig(pages_per_query=1, country_code="au", language_code="en")
        payload = apify_actor_input(['site:.com.au intitle:"write for us"'], config)
        self.assertEqual(payload["countryCode"], "au")
        self.assertEqual(payload["queries"], 'site:.com.au intitle:"write for us"')
        rows = apify_items_to_rows(
            [
                {
                    "searchQuery": {"term": 'site:.com.au intitle:"write for us"'},
                    "organicResults": [
                        {
                            "title": "Write for us",
                            "url": "https://example.com.au/write-for-us",
                            "description": "Guest post guidelines",
                        },
                        {"title": "Ignore", "url": "https://example.com/write-for-us"},
                    ],
                }
            ],
            fallback_query="",
            query_offsets={'site:.com.au intitle:"write for us"': 0},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_engine"], "apify_google")
        self.assertEqual(rows[0]["root_domain"], "example.com.au")
        self.assertEqual(rows[0]["matched_phrase"], "write for us")

    def paid_link_row(self, **updates: str) -> dict[str, str]:
        row = {
            "root_domain": "example.com.au",
            "source_url": "https://example.com.au/write-for-us",
            "title": "Write for us",
            "matched_phrase": "write for us",
            "opportunity_type": "guest_post",
            "email": "",
            "email_type": "",
            "all_emails": "",
            "all_email_types": "",
            "all_result_urls": "https://example.com.au/write-for-us",
            "all_titles": "Write for us",
            "all_queries": 'site:.com.au "write for us"',
            "all_matched_phrases": "write for us",
            "quality_tier": "Reject",
            "reject_reason": "no_usable_business_email",
            "email_count": "0",
        }
        row.update(updates)
        return row

    def test_paid_link_scoring_keeps_pbn_marketplace_as_very_likely(self) -> None:
        row = self.paid_link_row(
            title="Buy guest post packages - DA 40 guest post",
            all_titles="Buy guest post packages - DA 40 guest post",
            all_result_urls="https://example.com.au/buy-guest-post-packages",
            all_queries='site:.com.au "buy guest post"',
            email="sales@example.com.au",
            email_type="advertising",
            all_emails="sales@example.com.au",
            all_email_types="advertising",
            quality_tier="Reject",
            reject_reason="low_quality_guest_post_marketplace_language",
        )
        score = score_paid_link_acceptance(row)
        self.assertEqual(score.paid_link_likelihood, "Very Likely")
        self.assertIn("pbn_like", score.seller_style_flags)
        self.assertIn("guest_post_marketplace", score.seller_style_flags)
        self.assertEqual(score.transaction_type, "marketplace")

    def test_paid_link_scoring_commercial_role_email_is_very_likely(self) -> None:
        row = self.paid_link_row(
            source_url="https://example.com.au/advertise-with-us",
            title="Advertise with us - Media kit",
            matched_phrase="advertise with us",
            opportunity_type="advertising_media_kit",
            email="media@example.com.au",
            email_type="media",
            all_emails="media@example.com.au",
            all_email_types="media",
            all_result_urls="https://example.com.au/advertise-with-us",
            quality_tier="A",
            reject_reason="",
        )
        score = score_paid_link_acceptance(row)
        self.assertEqual(score.paid_link_likelihood, "Very Likely")
        self.assertEqual(score.contactability, "direct_role_email")
        self.assertEqual(score.recommended_next_action, "email_pricing_request")

    def test_paid_link_scoring_write_for_us_generic_email_is_likely(self) -> None:
        row = self.paid_link_row(
            email="hello@example.com.au",
            email_type="generic",
            all_emails="hello@example.com.au",
            all_email_types="generic",
            quality_tier="B",
            reject_reason="",
        )
        score = score_paid_link_acceptance(row)
        self.assertEqual(score.paid_link_likelihood, "Likely")
        self.assertEqual(score.contactability, "generic_email")

    def test_paid_link_scoring_weak_australian_com_is_possible_or_weak(self) -> None:
        row = self.paid_link_row(
            root_domain="example.com",
            source_url="https://example.com/australia-blog",
            title="Australian travel blog contribution ideas",
            matched_phrase="",
            opportunity_type="unclear_contact_only",
            all_result_urls="https://example.com/australia-blog",
            all_titles="Australian travel blog contribution ideas",
            all_queries='site:.com "guest post" Australia',
            all_matched_phrases="",
        )
        score = score_paid_link_acceptance(row)
        self.assertIn(score.paid_link_likelihood, {"Possible", "Weak"})
        self.assertIn("global_com_noise", score.seller_style_flags)

    def test_paid_link_scoring_unrelated_result_is_out_of_scope(self) -> None:
        row = self.paid_link_row(
            root_domain="example.com.au",
            source_url="https://example.com.au/about",
            title="About our company",
            matched_phrase="",
            opportunity_type="unclear_contact_only",
            all_result_urls="https://example.com.au/about",
            all_titles="About our company",
            all_queries='site:.com.au "about"',
            all_matched_phrases="",
            reject_reason="",
        )
        score = score_paid_link_acceptance(row)
        self.assertEqual(score.paid_link_likelihood, "Out Of Scope")

    def test_paid_link_score_rows_retains_low_quality_rows(self) -> None:
        rows = score_rows(
            [
                self.paid_link_row(root_domain="marketplace.com.au", title="Buy guest post packages"),
                self.paid_link_row(root_domain="unrelated.com.au", title="About us", matched_phrase="", opportunity_type="unclear_contact_only", all_queries=""),
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertIn("paid_link_likelihood", rows[0])

    def missing_email_row(self, **updates: str) -> dict[str, str]:
        row = self.paid_link_row(
            site_name="Example Media",
            paid_link_likelihood="Possible",
            paid_link_score="35",
            transaction_type="guest_post",
            email="",
            all_emails="",
        )
        row.update(updates)
        return row

    def test_missing_email_priority_ordering(self) -> None:
        rows = [
            self.missing_email_row(root_domain="weak.com.au", paid_link_likelihood="Weak", paid_link_score="10"),
            self.missing_email_row(root_domain="likely.com.au", paid_link_likelihood="Likely", paid_link_score="45"),
            self.missing_email_row(root_domain="possible.com.au", paid_link_likelihood="Possible", paid_link_score="60"),
            self.missing_email_row(root_domain="done.com.au", paid_link_likelihood="Very Likely", email="info@done.com.au"),
            self.missing_email_row(root_domain="skip.com.au", paid_link_likelihood="Out Of Scope"),
        ]
        selected = select_missing_rows(rows)
        self.assertEqual([row["root_domain"] for row in selected], ["likely.com.au", "possible.com.au", "weak.com.au"])

    def test_missing_email_known_path_urls(self) -> None:
        urls = known_path_urls("example.com.au", "https://example.com.au/advertise")
        self.assertIn("https://example.com.au/contact", urls)
        self.assertIn("https://example.com.au/advertise-with-us", urls)
        self.assertIn("https://example.com.au/media-kit", urls)

    def test_missing_email_sitemap_and_robots_parsing(self) -> None:
        sitemap = """<urlset><url><loc>https://example.com.au/contact</loc></url></urlset>"""
        self.assertEqual(parse_sitemap_urls(sitemap), ["https://example.com.au/contact"])
        robots = "User-agent: *\nSitemap: https://example.com.au/sitemap.xml\n"
        self.assertEqual(parse_robots_sitemaps(robots), ["https://example.com.au/sitemap.xml"])

    def test_missing_email_brand_search_queries(self) -> None:
        row = self.missing_email_row(root_domain="example.com.au", site_name="Example Media")
        queries = brand_search_queries(row)
        self.assertIn("site:example.com.au email", queries)
        self.assertIn('"Example Media" email', queries)
        self.assertIn('"example.com.au" email', queries)

    def test_missing_email_public_profile_filtering(self) -> None:
        row = self.missing_email_row(root_domain="example.com.au", site_name="Example Media")
        self.assertTrue(
            should_scrape_public_result(
                row,
                "https://www.facebook.com/examplemedia/about",
                "Example Media",
                "Contact email for Example Media",
                scrape_social_profiles=True,
            )
        )
        self.assertFalse(
            should_scrape_public_result(
                row,
                "https://www.facebook.com/other/about",
                "Other Brand",
                "Contact email",
                scrape_social_profiles=True,
            )
        )

    def test_missing_email_role_pattern_candidates(self) -> None:
        row = self.missing_email_row(root_domain="example.com.au")
        candidates = role_pattern_candidates(row)
        self.assertIn("advertising@example.com.au", {candidate.email for candidate in candidates})
        self.assertTrue(all(candidate.is_role_pattern_guess for candidate in candidates))
        platform = self.missing_email_row(root_domain="facebook.com")
        self.assertEqual(role_pattern_candidates(platform), [])

    def test_missing_email_public_candidate_sorts_before_guess(self) -> None:
        row = self.missing_email_row(root_domain="example.com.au")
        public = role_pattern_candidates(row)[0]
        guessed = role_pattern_candidates(row)[1]
        public = public.__class__(
            **{
                **public.__dict__,
                "is_verified_public_email": True,
                "is_role_pattern_guess": False,
                "email_source_method": "page_scrape",
                "discovery_stage": "known_path_crawl",
            }
        )
        ordered = sort_candidates([guessed, public])
        self.assertFalse(ordered[0].is_role_pattern_guess)

    def test_missing_email_off_domain_role_email_rules(self) -> None:
        row = self.missing_email_row(root_domain="example.com.au", site_name="Example Media")
        self.assertTrue(public_email_allowed(row, "advertising@network.com.au", "https://example.com.au/advertise", "Example Media", stage="known_path_crawl"))
        self.assertFalse(public_email_allowed(row, "info@otherbrand.com.au", "https://example.com.au/about", "Example Media", stage="known_path_crawl"))

    def instantly_master_row(self, **updates: str) -> dict[str, str]:
        row = self.paid_link_row(
            root_domain="example.com.au",
            site_name="Example Media",
            paid_link_likelihood="Likely",
            paid_link_score="70",
            transaction_type="guest_post",
            email="",
            best_contact_email="editor@example.com.au",
            best_contact_email_type="editor",
            best_contact_method="page_scrape",
            best_contact_source="https://example.com.au/contact",
            best_contact_is_role_guess="no",
            why_relevant="Australian publisher with article submission evidence",
            seller_style_flags="publisher",
        )
        row.update(updates)
        return row

    def test_instantly_sequence_has_five_followups_same_subject(self) -> None:
        payload = campaign_payload(CampaignBuildConfig(campaign_name="Test Campaign", subject="Article Submission Info Request"))
        steps = payload["sequences"][0]["steps"]
        self.assertEqual(len(steps), 6)
        self.assertEqual({step["variants"][0]["subject"] for step in steps}, {"Article Submission Info Request"})
        self.assertEqual(payload["daily_max_leads"], 0)
        self.assertTrue(payload["stop_on_reply"])

    def test_instantly_contact_selection_and_email_dedupe(self) -> None:
        rows = [
            self.instantly_master_row(root_domain="weak.com.au", paid_link_likelihood="Weak", paid_link_score="20", best_contact_email="info@example.com.au"),
            self.instantly_master_row(root_domain="likely.com.au", paid_link_likelihood="Likely", paid_link_score="80", best_contact_email="info@example.com.au"),
            self.instantly_master_row(root_domain="missing.com.au", best_contact_email=""),
            self.instantly_master_row(root_domain="oos.com.au", paid_link_likelihood="Out Of Scope", best_contact_email="hello@oos.com.au"),
        ]
        selected = selected_contact_rows(rows, CampaignBuildConfig(include_out_of_scope=False))
        self.assertEqual([row["root_domain"] for row in selected], ["likely.com.au", "weak.com.au"])
        deduped, duplicates = dedupe_rows_by_email(selected)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["root_domain"], "likely.com.au")
        self.assertEqual(len(duplicates), 1)
        self.assertIn("weak.com.au", deduped[0]["additional_domains_for_email"])

    def test_instantly_lead_payload_uses_custom_variables_without_job_title_collision(self) -> None:
        lead = lead_payload(self.instantly_master_row())
        self.assertEqual(lead["email"], "editor@example.com.au")
        self.assertEqual(lead["first_name"], "there")
        self.assertIn("custom_variables", lead)
        self.assertNotIn("job_title", lead)
        self.assertNotIn("jobTitle", lead["custom_variables"])
        self.assertEqual(lead["custom_variables"]["site_name"], "Example Media")

    def test_deals_inbound_reply_filter_uses_ue_type_two(self) -> None:
        original = deals_module.list_campaign_emails
        try:
            deals_module.list_campaign_emails = lambda _campaign_id: [
                {"id": "out", "ue_type": 1},
                {"id": "in", "ue_type": 2},
            ]
            self.assertEqual([item["id"] for item in inbound_replies("campaign")], ["in"])
        finally:
            deals_module.list_campaign_emails = original

    def test_deals_mamamia_auto_reply_is_not_confirmed_deal(self) -> None:
        reply = classify_reply(
            "Hello! Thank you for sharing your submission with us at Mamamia. "
            "Due to the high volume of submissions we receive, only applicants with successful submissions will be contacted. "
            "If your submission is related to Lady Startup, please email hello@ladystartup.com.au"
        )
        self.assertEqual(reply.classification, "auto_reply")
        self.assertFalse(reply.create_deal)

    def test_deals_rejection_ignores_quoted_original_thread_terms(self) -> None:
        reply = classify_reply(
            "Hi Laurence,\n\nThis isn't one for us.\n\nRegards,\nChris\n\n"
            "-----Original Message-----\n"
            "Do you accept sponsored articles, advertorials, or paid placements?"
        )
        self.assertEqual(reply.classification, "rejected")
        self.assertFalse(reply.create_deal)

    def test_deals_price_reply_creates_confirmed_deal_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "deals.db"
            original_connect = deals_module.connect
            original_init = deals_module.init_db
            import sqlite3

            def temp_connect() -> sqlite3.Connection:
                connection = sqlite3.connect(db_path)
                connection.row_factory = sqlite3.Row
                return connection

            def temp_init() -> None:
                with temp_connect() as connection:
                    connection.executescript(SCHEMA)
                    connection.commit()

            deals_module.connect = temp_connect
            deals_module.init_db = temp_init
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
                self.assertTrue(save_reply_and_maybe_deal("campaign", email, lead))
                self.assertFalse(save_reply_and_maybe_deal("campaign", email, lead))
                with temp_connect() as connection:
                    deals = connection.execute("select * from link_deals").fetchall()
                    replies = connection.execute("select * from instantly_reply_sync").fetchall()
                self.assertEqual(len(deals), 1)
                self.assertEqual(len(replies), 1)
                self.assertEqual(deals[0]["deal_status"], "confirmed")
                self.assertEqual(deals[0]["price_amount"], 350)
                self.assertIn("dofollow", deals[0]["link_requirements"].lower())
            finally:
                deals_module.connect = original_connect
                deals_module.init_db = original_init

    def test_deals_manual_fields_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "deals.db"
            original_connect = deals_module.connect
            original_init = deals_module.init_db
            import sqlite3

            def temp_connect() -> sqlite3.Connection:
                connection = sqlite3.connect(db_path)
                connection.row_factory = sqlite3.Row
                return connection

            def temp_init() -> None:
                with temp_connect() as connection:
                    connection.executescript(SCHEMA)
                    connection.commit()

            deals_module.connect = temp_connect
            deals_module.init_db = temp_init
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
                        "target_url": "https://client.example/product",
                        "writing_requirements": "900 words",
                        "additional_notes": "Good fit",
                    },
                )
                with temp_connect() as connection:
                    row = connection.execute("select * from link_deals where id=1").fetchone()
                self.assertEqual(row["deal_status"], "confirmed")
                self.assertEqual(row["placement_type"], "sponsored_post")
                self.assertEqual(row["price_amount"], 275)
                self.assertEqual(row["domain_trust"], "42")
                self.assertEqual(row["writing_requirements"], "900 words")
                self.assertEqual(row["additional_notes"], "Good fit")
            finally:
                deals_module.connect = original_connect
                deals_module.init_db = original_init


if __name__ == "__main__":
    unittest.main()
