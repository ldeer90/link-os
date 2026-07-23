from __future__ import annotations

import unittest

from deal_tracker.crawler import CrawlConfig, FetchResult, crawl_domain, extract_public_email_evidence, registrable_domain


class PublicCrawlerTests(unittest.TestCase):
    def test_registrable_domain_accepts_all_public_tlds_and_rejects_ips(self) -> None:
        self.assertEqual(registrable_domain("https://www.Example.co.uk/contact"), "example.co.uk")
        self.assertEqual(registrable_domain("example.com.au"), "example.com.au")
        self.assertEqual(registrable_domain("127.0.0.1"), "")
        self.assertEqual(registrable_domain("localhost"), "")

    def test_extracts_only_visible_or_mailto_evidence_and_ranks_editorial(self) -> None:
        html = """
        <html><body>
          <p>Editorial pitches: editor@example.com</p>
          <a href="mailto:advertising@example.com">Advertising team</a>
          <script>hidden@example.com</script>
        </body></html>
        """
        rows = extract_public_email_evidence(html, "https://example.com/contact")
        self.assertEqual([row.email for row in rows], ["editor@example.com", "advertising@example.com"])
        self.assertTrue(all(row.source_url for row in rows))
        self.assertNotIn("hidden@example.com", {row.email for row in rows})

    def test_crawl_respects_robots_and_keeps_best_three_without_guesses(self) -> None:
        pages = {
            "https://example.com/": "<a href='/team'>Team</a><a href='/private'>Private</a>",
            "https://example.com/contact": "Contact hello@example.com and editor@example.com and sales@example.com and person@example.com",
            "https://example.com/team": "Partnerships: partners@example.com",
        }

        def fetch(url: str, _timeout: float, _max_bytes: int) -> FetchResult:
            if url not in pages:
                return FetchResult(url, url, 404, "text/html", "")
            return FetchResult(url, url, 200, "text/html; charset=utf-8", pages[url])

        progress: list[dict[str, object]] = []
        result = crawl_domain(
            "example.com",
            config=CrawlConfig(max_pages=12, host_delay_seconds=0, resolve_public_dns=False),
            fetcher=fetch,
            allowed_by_robots=lambda url: not url.endswith("/advertise"),
            sleeper=lambda _seconds: None,
            on_progress=progress.append,
        )
        self.assertEqual(result.status, "email_found")
        self.assertEqual(len(result.contacts), 3)
        self.assertEqual(result.contacts[0].email, "editor@example.com")
        self.assertTrue(any(attempt.status == "robots_denied" for attempt in result.attempts))
        self.assertFalse(any("private" in attempt.url for attempt in result.attempts))
        self.assertTrue(any(event["event_type"] == "page_started" for event in progress))
        self.assertTrue(any(event["event_type"] == "page_crawled" for event in progress))
        email_event = next(event for event in progress if event["event_type"] == "email_found")
        self.assertGreaterEqual(int(email_event["emails_found"]), 1)
        self.assertTrue(all(event.get("url") for event in progress))

    def test_crawl_skips_malformed_ipv6_links_without_failing_domain(self) -> None:
        pages = {
            "https://example.com/": """
                <a href="http://[not-an-ipv6-address]/broken">Broken theme link</a>
                <a href="/contact">Contact</a>
            """,
            "https://example.com/contact": "Editorial pitches: editor@example.com",
        }

        def fetch(url: str, _timeout: float, _max_bytes: int) -> FetchResult:
            if url not in pages:
                return FetchResult(url, url, 404, "text/html", "")
            return FetchResult(url, url, 200, "text/html; charset=utf-8", pages[url])

        result = crawl_domain(
            "example.com",
            config=CrawlConfig(max_pages=12, host_delay_seconds=0, resolve_public_dns=False),
            fetcher=fetch,
            allowed_by_robots=lambda _url: True,
            sleeper=lambda _seconds: None,
        )

        self.assertEqual(result.status, "email_found")
        self.assertEqual([contact.email for contact in result.contacts], ["editor@example.com"])


if __name__ == "__main__":
    unittest.main()
