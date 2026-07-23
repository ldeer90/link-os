import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { App } from "../App";

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));
}

function requestDetails(input: RequestInfo | URL, init?: RequestInit) {
  return { url: typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url, method: init?.method ?? "GET", body: init?.body };
}

function renderRoute(route: string) {
  return render(<MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><App /></MemoryRouter>);
}

const healthySystem = {
  status: "healthy",
  database: "healthy",
  worker: "healthy",
  outreach_paused: false,
  blocking_reasons: [],
  senders: [{ id: "sender-1", email: "outreach@example.test", status: "healthy", healthy: true, daily_limit: 30, sent_today: 7 }],
};

describe("LINK OS console", () => {
  it("separates historical memberships from confirmed outreach evidence", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = requestDetails(input, init);
      if (url.endsWith("/api/v1/health")) return json({ ...healthySystem, outreach_paused: true });
      if (url.endsWith("/api/v1/overview")) {
        return json({
          totals: { domains: 17483, link_building_domains: 4413, replies: 63 },
          pipeline: { imported: 171, contacted: 804, offer_review: 42, historical_membership_unverified: 14309 },
          historical_funnel: { entered_system: 4413, scrape_attempted: 2304, contact_identity_captured: 2790, public_contact_captured: 1002, verified_contact: 0, instantly_uploaded: 2096, sent_confirmed: 239, replied: 50, offer_recorded: 143, listed: 101 },
          current_state: { imported: 171, queued: 0, scraping: 0, email_found: 955, no_email: 724, failed: 369, verified: 0, suppressed: 0, outreach_ready: 0, campaign_queued: 0, uploaded: 0, contacted: 2042, replied: 8, offer_review: 42, offer_approved: 0, listed: 101, lost: 1 },
          senders: [],
          recent_replies: [],
          failures: [],
          queue: { pending: 0, active: 0, completed_last_hour: 0, failed_last_hour: 0 },
          outreach_evidence: {
            scope: "all",
            total_domains: 17483,
            historical_memberships: 2096,
            contact_confirmed: 239,
            membership_only: 1857,
            evidence_backed_engaged: 955,
            reply_domains: 50,
            offer_domains: 143,
            listed_domains: 101,
            protected_domains: 2096,
            link_building_campaigns: 13,
            workspace_campaigns: 41,
            excluded_unrelated_campaign_domains: 13070,
            tiers: {},
          },
          australian_outreach_evidence: {
            scope: "com.au",
            total_domains: 11278,
            historical_memberships: 11016,
            contact_confirmed: 644,
            membership_only: 10369,
            evidence_backed_engaged: 706,
            reply_domains: 35,
            offer_domains: 70,
            listed_domains: 50,
            protected_domains: 11016,
            tiers: {},
          },
        });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/");

    expect(await screen.findByRole("heading", { name: "Outreach evidence" })).toBeInTheDocument();
    expect(screen.getByText("Instantly leads uploaded").nextElementSibling).toHaveTextContent("2,096");
    expect(screen.getAllByText("Sent confirmed")[0].nextElementSibling).toHaveTextContent("239");
    expect(screen.getAllByText("Uploaded — no send record")[0].nextElementSibling).toHaveTextContent("1,857");
    expect(screen.getByRole("link", { name: "View all sent-confirmed domains" })).toHaveAttribute("href", "/domains?evidence=sent_confirmed");
    expect(screen.getByRole("link", { name: "View uploaded domains with no send record" })).toHaveAttribute("href", "/domains?evidence=uploaded_no_send_record");
    expect(screen.getByRole("link", { name: "View Entered LINK OS domains" })).toHaveAttribute("href", "/domains?history=entered_system");
    expect(screen.getByRole("link", { name: "View Entered LINK OS domains" })).toHaveTextContent("4,413");
    expect(screen.getByRole("link", { name: "View Uploaded to Instantly domains" })).toHaveAttribute("href", "/domains?history=instantly_uploaded");
    expect(screen.getByRole("link", { name: "View Reply or deal response domains" })).toHaveAttribute("href", "/domains?history=replied");
    expect(screen.getByRole("link", { name: "View Listed in catalogue domains" })).toHaveAttribute("href", "/domains?history=listed");
    await user.click(screen.getByRole("button", { name: "Current state" }));
    expect(screen.getByRole("link", { name: "View Imported — awaiting next step domains" })).toHaveAttribute("href", "/domains?scope=link_building&status=imported");
    expect(screen.getByRole("link", { name: "View Queued for scraping domains" })).toHaveAttribute("href", "/domains?scope=link_building&status=queued");
    expect(screen.getByRole("link", { name: "View Contacted — stored lifecycle domains" })).toHaveTextContent("2,042");
    expect(screen.getAllByText("13")[0].nextElementSibling).toHaveTextContent("of 41 campaigns with leads included");
    expect(screen.getByText("13,070").nextElementSibling).toHaveTextContent("unrelated-only domains excluded");
    expect(screen.getByText("Full domain funnel")).toBeInTheDocument();
    expect(screen.getByText("Australian universe")).toBeInTheDocument();
    expect(screen.getByText("11,278")).toBeInTheDocument();
    expect(screen.getByText("10,369")).toBeInTheDocument();
  });

  it("queues pasted domains and renders durable import progress", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestDetails(input, init);
      if (request.url.endsWith("/api/v1/health")) return json(healthySystem);
      if (request.url.endsWith("/api/v1/imports") && request.method === "POST") {
        return json({ import_id: "imp-84", status: "completed", total_rows: 3, processed_rows: 3, accepted: 2, duplicates: 1, invalid: 0, suppressed: 0, previously_contacted: 0, refresh_eligible: 0 });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/intake");

    await user.type(screen.getByLabelText("Domains or URLs"), "publisher.com{enter}newsroom.org.au{enter}publisher.com");
    await user.click(screen.getByRole("button", { name: "Queue import" }));

    expect(await screen.findByText("Import imp-84")).toBeInTheDocument();
    expect(screen.getByText("3 of 3 rows processed")).toBeInTheDocument();
    expect(screen.getByText("Accepted").previousElementSibling).toHaveTextContent("2");
    const post = fetchMock.mock.calls.map(([input, init]) => requestDetails(input, init)).find((request) => request.url.endsWith("/imports") && request.method === "POST");
    expect(String(post?.body)).toContain("publisher.com");
  });

  it("sends domain search and lifecycle filters to the server", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = requestDetails(input, init);
      if (url.endsWith("/api/v1/health")) return json(healthySystem);
      if (url.includes("/api/v1/domains")) {
        const query = new URL(url, "http://local").searchParams;
        if (query.get("search") === "needle" && query.get("status") === "verified" && query.get("evidence") === "sent_confirmed") {
          return json({ items: [{ id: 9, domain: "needle.example", status: "verified", best_email: "editor@needle.example", evidence_count: 2, outreach_evidence_status: "sent_confirmed", outreach_evidence_label: "Sent confirmed", outreach_evidence_reason: "Instantly records delivery activity for this lead.", canonical_dedupe: true, duplicate_protection_reasons: ["One normalized domain record is reused across every import and discovery source."] }], total: 1 });
        }
        return json({ items: [], total: 0 });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/domains");

    await user.type(screen.getByLabelText("Search domains"), "needle");
    await user.selectOptions(screen.getByLabelText("Lifecycle status"), "verified");
    await user.selectOptions(screen.getByLabelText("Outreach evidence"), "sent_confirmed");

    expect(await screen.findByText("needle.example")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      const { url } = requestDetails(input, init);
      return url.includes("search=needle") && url.includes("status=verified") && url.includes("evidence=sent_confirmed");
    })).toBe(true));
  });

  it("loads a lifecycle funnel link as an active server-side domain filter", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = requestDetails(input, init);
      if (url.endsWith("/api/v1/health")) return json(healthySystem);
      if (url.includes("/api/v1/domains")) {
        const query = new URL(url, "http://local").searchParams;
        if (query.get("status") === "offer_review") {
          return json({ items: [{ id: 12, domain: "publisher-review.example", status: "offer_review" }], total: 1 });
        }
        return json({ items: [], total: 0 });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/domains?status=offer_review");

    expect(await screen.findByText("publisher-review.example")).toBeInTheDocument();
    expect(screen.getByLabelText("Lifecycle status")).toHaveValue("offer_review");
    expect(fetchMock.mock.calls.some(([input, init]) => requestDetails(input, init).url.includes("status=offer_review"))).toBe(true);
  });

  it("loads a historical funnel link as an active server-side domain filter", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = requestDetails(input, init);
      if (url.endsWith("/api/v1/health")) return json(healthySystem);
      if (url.includes("/api/v1/domains")) {
        const query = new URL(url, "http://local").searchParams;
        if (query.get("history") === "entered_system") {
          return json({ items: [{ id: 17, domain: "historical.example", status: "contacted" }], total: 17483 });
        }
        return json({ items: [], total: 0 });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/domains?history=entered_system");

    expect(await screen.findByText("historical.example")).toBeInTheDocument();
    expect(screen.getByLabelText("Historical milestone")).toHaveValue("entered_system");
    expect(screen.getByText("17,483 records")).toBeInTheDocument();
  });

  it("paginates historical domains, changes page size and shows canonical source", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = requestDetails(input, init);
      if (url.endsWith("/api/v1/health")) return json(healthySystem);
      if (url.includes("/api/v1/domains")) {
        return json({
          items: [{
            id: "domain-source",
            domain: "publisher.com.au",
            status: "email_found",
            source_label: "Backlink profile: salesforce.com",
            source_count: 2,
            sources: [{ id: "analysis-1", type: "backlink_profile", label: "Backlink profile: salesforce.com" }],
          }],
          total: 623,
        });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/domains?history=contact_identity_captured");

    expect(await screen.findByText("publisher.com.au")).toBeInTheDocument();
    expect(screen.getByText("Backlink profile: salesforce.com")).toBeInTheDocument();
    expect(screen.getByText("+1 more source")).toBeInTheDocument();
    expect(screen.getByText(/Page 1 of 7/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Rows per page"), "25");
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      const query = new URL(requestDetails(input, init).url, "http://local").searchParams;
      return query.get("page_size") === "25" && query.get("offset") === "0";
    })).toBe(true));

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      const query = new URL(requestDetails(input, init).url, "http://local").searchParams;
      return query.get("page_size") === "25" && query.get("offset") === "25";
    })).toBe(true));
    expect(screen.getByText(/Page 2 of 25/)).toBeInTheDocument();
  });

  it("shows global scrape totals and maps the Active filter to leased jobs", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = requestDetails(input, init);
      if (url.endsWith("/api/v1/health")) return json({ ...healthySystem, outreach_paused: true });
      if (url.includes("/api/v1/scraping/activity")) {
        return json({
          active: [{ id: "attempt-live", domain_id: "domain-live", domain: "live-publisher.example", status: "running", attempt_number: 1, started_at: new Date().toISOString(), elapsed_seconds: 9, pages_requested: 0, pages_crawled: 0, emails_found: 0, job_status: "leased", job_attempts: 1, job_max_attempts: 3 }],
          recent: [{ id: "attempt-done", domain_id: "domain-done", domain: "finished-publisher.example", status: "succeeded", attempt_number: 1, started_at: new Date(Date.now() - 12_000).toISOString(), completed_at: new Date().toISOString(), elapsed_seconds: 12, pages_requested: 3, pages_crawled: 3, emails_found: 1, job_status: "succeeded", job_attempts: 1, job_max_attempts: 3 }],
          generated_at: new Date().toISOString(),
        });
      }
      if (url.includes("/api/v1/jobs")) {
        const status = new URL(url, "http://local").searchParams.get("status");
        const summary = { queued: 1611, active: 1, completed: 15, failed: 4, cancelled: 0 };
        if (status === "active") {
          return json({ items: [{ id: "job-active", domain: "publisher.example", status: "leased", attempts: 1, max_attempts: 3 }], total: 1, summary });
        }
        return json({ items: [{ id: "job-queued", domain: "waiting.example", status: "queued", attempts: 0, max_attempts: 3 }], total: 1627, summary });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/scraping");

    expect(await screen.findByText("waiting.example")).toBeInTheDocument();
    expect(screen.getByText("live-publisher.example")).toBeInTheDocument();
    expect(screen.getByText("finished-publisher.example")).toBeInTheDocument();
    expect(screen.getByText("Jobs in filter").nextElementSibling).toHaveTextContent("1,627");
    expect(screen.getByText("Backlog").nextElementSibling).toHaveTextContent("1,611");
    const activeMetric = screen.getAllByText("Active").find((element) => element.tagName === "P");
    expect(activeMetric?.nextElementSibling).toHaveTextContent("1");

    act(() => {
      window.dispatchEvent(new CustomEvent("linkos:scrape", { detail: {
        id: "audit-live-page",
        action: "page_started",
        created_at: new Date().toISOString(),
        after: {
          attempt_id: "attempt-live",
          domain: "live-publisher.example",
          event_type: "page_started",
          current_url: "https://live-publisher.example/contact",
          pages_attempted: 1,
          pages_crawled: 0,
          max_pages: 20,
          occurred_at: new Date().toISOString(),
        },
      } }));
    });
    expect(await screen.findByText("Opening page 2 of 20")).toBeInTheDocument();
    expect(screen.getByText("https://live-publisher.example/contact")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter scraping jobs"), "active");
    expect(await screen.findByText("publisher.example")).toBeInTheDocument();
    expect(screen.queryByText("waiting.example")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input, init]) => requestDetails(input, init).url.includes("status=active"))).toBe(true);
  });

  it("pauses outreach from campaigns and updates the global safety banner", async () => {
    let paused = false;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestDetails(input, init);
      if (request.url.endsWith("/api/v1/health")) return json({ ...healthySystem, outreach_paused: paused });
      if (request.url.endsWith("/api/v1/campaigns")) return json({ items: [] });
      if (request.url.endsWith("/api/v1/outreach/pause") && request.method === "POST") {
        paused = true;
        return json({ ...healthySystem, outreach_paused: true, pause_reason: "Emergency pause from Campaigns console" });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/campaigns");

    await user.click(await screen.findByRole("button", { name: "Emergency pause" }));
    expect(await screen.findByText(/Outreach is paused\. Scraping, reply sync and review remain available/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input, init]) => {
      const request = requestDetails(input, init);
      return request.url.endsWith("/outreach/pause") && request.method === "POST";
    })).toBe(true);
  });

  it("requires confirmation before requesting a safe outreach resume", async () => {
    let paused = true;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestDetails(input, init);
      if (request.url.endsWith("/api/v1/health")) return json({ ...healthySystem, outreach_paused: paused, pause_reason: "Pilot held" });
      if (request.url.endsWith("/api/v1/campaigns")) return json({ items: [] });
      if (request.url.endsWith("/api/v1/outreach/resume") && request.method === "POST") {
        paused = false;
        return json({ ...healthySystem, outreach_paused: false });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/campaigns");

    await user.click(await screen.findByRole("button", { name: "Review resume" }));
    expect(screen.getByRole("dialog", { name: "Resume managed outreach?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm resume" }));

    await waitFor(() => expect(screen.queryByText(/Outreach is paused\. Scraping, reply sync and review remain available/)).not.toBeInTheDocument());
    const resumeCall = fetchMock.mock.calls.map(([input, init]) => requestDetails(input, init)).find((request) => request.url.endsWith("/outreach/resume") && request.method === "POST");
    expect(String(resumeCall?.body)).toContain('"confirm":true');
  });

  it("estimates and prepares the segmented 75-contact campaign set", async () => {
    const senders = [1, 2, 3].map((id) => ({ id: `sender-${id}`, email: `sender-${id}@example.test`, status: "healthy", healthy: true, daily_limit: 30, sent_today: 0 }));
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestDetails(input, init);
      if (request.url.endsWith("/api/v1/health")) return json({ ...healthySystem, senders });
      if (request.url.endsWith("/api/v1/campaigns")) return json({ items: [] });
      if (request.url.endsWith("/api/v1/campaign-sets/estimate") && request.method === "POST") return json({ allowed: true, available: { au_publishers: 34, travel_global: 144 }, healthy_unassigned_senders: 3 });
      if (request.url.endsWith("/api/v1/campaign-sets") && request.method === "POST") return json({ id: "launch-1", job_ids: ["a", "b", "c"] });
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/campaigns");

    await user.click(await screen.findByRole("button", { name: "Check availability" }));
    expect(await screen.findByText("34 / 25")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Prepare three paused campaigns" }));
    expect(screen.getByRole("dialog", { name: "Prepare 75 unverified contacts?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm 75-contact preparation" }));
    expect(await screen.findByText(/Three paused campaigns queued as 3 durable jobs/)).toBeInTheDocument();
  });

  it("reviews and privately approves an unambiguous offer", async () => {
    let offerStatus = "review";
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestDetails(input, init);
      if (request.url.endsWith("/api/v1/health")) return json(healthySystem);
      if (request.url.includes("/api/v1/replies")) return json({ items: [] });
      if (request.url.includes("/api/v1/offers") && request.method === "GET") return json({ items: [{ id: 44, domain: "publisher.news", publisher_email: "editor@publisher.news", placement_type: "guest_post", original_amount: 180, original_currency: "USD", reseller_price_aud: 380, confidence: 0.96, status: offerStatus, evidence: "Guest posts are USD 180." }] });
      if (request.url.endsWith("/api/v1/offers/44") && request.method === "PATCH") {
        offerStatus = "approved";
        return json({ id: 44, status: offerStatus });
      }
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/replies");

    await user.click(await screen.findByRole("tab", { name: /Offers/ }));
    await user.click(screen.getByRole("button", { name: "Review" }));
    expect(screen.getByText("Private by default")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve privately" }));

    expect(await screen.findByText("Offer approved and kept private until separately listed.")).toBeInTheDocument();
    const patchCall = fetchMock.mock.calls.map(([input, init]) => requestDetails(input, init)).find((request) => request.url.endsWith("/offers/44") && request.method === "PATCH");
    expect(String(patchCall?.body)).toContain('"status":"approved"');
  });

  it("labels approved inventory as private until it is separately listed", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = requestDetails(input, init);
      if (url.endsWith("/api/v1/health")) return json(healthySystem);
      if (url.includes("/api/v1/listings")) return json({ items: [{ id: 72, domain: "quietpublisher.com", status: "private", visibility: "private", cost_aud: 247, reseller_price_aud: 420, placement_type: "guest_post", enquiries: 0 }] });
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/inventory");

    expect(await screen.findByText("quietpublisher.com")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Cost to You (AUD)" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Resell Price (Cost + 40%)" })).toBeInTheDocument();
    expect(screen.getByText("$247")).toBeInTheDocument();
    expect(screen.getByText("$420")).toBeInTheDocument();
    expect(screen.getByText("Not visible to agencies")).toBeInTheDocument();
    expect(screen.getAllByText("Private").length).toBeGreaterThan(0);
  });

  it("estimates, explicitly confirms and reviews canonical backlink discovery", async () => {
    let created = false;
    let reviewed = false;
    const run = { id: "backlink-run-1", mode: "competitor_prospecting", status: "awaiting_codex_review", client_domain: "client.com", competitor_domains: ["competitor.com"], credit_cap: 500, estimated_credits: 500, actual_credits: 3, authority_floor: 20, counters: { total_referring_domains: 1379, returned: 1, coverage_percent: 0.1, pending_review: reviewed ? 0 : 1, approved: reviewed ? 1 : 0, excluded: 0, cached: 0, imported: 0, scrape_queued: 0 }, created_at: new Date().toISOString() };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestDetails(input, init);
      if (request.url.endsWith("/api/v1/health")) return json({ ...healthySystem, outreach_paused: true });
      if (request.url.includes("/integrations/seranking/health")) return json({ configured: true, status: "ok", balance: 5000, monthly_usage: 120, monthly_cap: 10000 });
      if (request.url.endsWith("/backlink-analyses/estimate") && request.method === "POST") return json({ mode: "competitor_prospecting", client_domain: "client.com", competitor_domains: ["competitor.com"], credit_cap: 500, predicted_credits: 500, count_credit_cost: 2, maximum_paid_records: 498, authority_floor: 20, monthly_usage: 120, monthly_cap: 10000, monthly_remaining: 9880, balance: 5000, allowed: true, blocking_reasons: [], confirmation_required: true, targets: [{ domain: "competitor.com", role: "competitor", credit_cap: 500, count_credit_cost: 2, retrieval_credit_cap: 498, cache_hit: false }] });
      if (request.url.endsWith("/api/v1/backlink-analyses") && request.method === "POST") { created = true; return json({ id: "backlink-run-1", status: "queued" }, 202); }
      if (request.url.includes("/backlink-analyses/backlink-run-1/reviews") && request.method === "POST") { reviewed = true; return json({ applied: 1, approved: 1, import_id: "import-1", status: "queueing_scrape" }); }
      if (request.url.includes("/backlink-analyses/backlink-run-1/candidates")) return json({ items: reviewed ? [] : [{ id: "candidate-1", domain: "publisher.news", normalized_domain: "publisher.news", status: "awaiting_codex_review", existing_domain_id: "domain-1", occurrence_count: 1, highest_domain_inlink_rank: 52, has_dofollow: true, local_score: 0.82, commercial_anchor_class: "commercial", commercial_anchor_score: 0.84, commercial_anchor_signals: ["commercial_anchor:insurance"], reason_codes: ["editorial", "commercial_anchor_evidence"], competitor_domains: ["competitor.com"], crawl_status: "email_found", public_email_count: 2, evidence: [{ id: "evidence-1", source_url: "https://publisher.news/story", competitor_domain: "competitor.com", target_url: "https://competitor.com/insurance", page_title: "Industry story", anchor_text: "compare travel insurance", anchor_class: "commercial", anchor_commercial_score: 0.84, anchor_signals: ["commercial_anchor:insurance"], nofollow: false, domain_inlink_rank: 52 }] }], count: reviewed ? 0 : 1, run_status: "awaiting_codex_review", available_competitors: ["competitor.com"] });
      if (request.url.includes("/api/v1/backlink-analyses") && request.method === "GET") return json({ items: created ? [{ ...run, counters: { ...run.counters, pending_review: reviewed ? 0 : 1, approved: reviewed ? 1 : 0 } }] : [], count: created ? 1 : 0 });
      return json({ detail: "not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderRoute("/backlinks");

    await user.type(screen.getByLabelText("Client domain"), "client.com");
    await user.type(screen.getByLabelText(/Competitors/), "competitor.com");
    await user.click(screen.getByRole("button", { name: "Estimate credits" }));
    expect(await screen.findByText("maximum referring domains retrieved")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm maximum 500 credits & queue" }));
    expect(await screen.findByText("publisher.news")).toBeInTheDocument();
    expect(screen.getByText("1,379")).toBeInTheDocument();
    expect(screen.getByText("0.1%")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Search backlink evidence"), "publisher");
    await user.selectOptions(screen.getByLabelText("Filter by competitor"), "competitor.com");
    await user.selectOptions(screen.getByLabelText("Filter by link type"), "follow");
    await user.selectOptions(screen.getByLabelText("Filter by anchor intent"), "commercial");
    await user.type(screen.getByLabelText("Minimum authority"), "50");
    await user.selectOptions(screen.getByLabelText("Filter by public email"), "found");
    await user.selectOptions(screen.getByLabelText("Filter by crawl status"), "email_found");
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      const url = requestDetails(input, init).url;
      return url.includes("competitor=competitor.com") && url.includes("link_type=follow") && url.includes("anchor_intent=commercial") && url.includes("min_authority=50") && url.includes("email_status=found") && url.includes("crawl_status=email_found") && url.includes("search=publisher");
    })).toBe(true));
    expect(screen.getByText("2 public emails")).toBeInTheDocument();
    expect(screen.getByText("Competitor · competitor.com")).toBeInTheDocument();
    expect(screen.getByText("Commercial · 84%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(reviewed).toBe(true));
    const createCall = fetchMock.mock.calls.map(([input, init]) => requestDetails(input, init)).find((request) => request.url.endsWith("/backlink-analyses") && request.method === "POST");
    expect(String(createCall?.body)).toContain('"confirmed_credit_cap":500');
    const reviewCall = fetchMock.mock.calls.map(([input, init]) => requestDetails(input, init)).find((request) => request.url.includes("/reviews") && request.method === "POST");
    expect(String(reviewCall?.body)).toContain('"confidence":0.9');
  });

  it("shows a recoverable health error instead of a false healthy state", async () => {
    vi.stubGlobal("fetch", vi.fn(() => json({ detail: "Health readback failed" }, 503)));
    renderRoute("/settings");
    expect(await screen.findByRole("alert")).toHaveTextContent("Health readback failed");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
