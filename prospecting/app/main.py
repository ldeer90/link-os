from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from guest_post_prospecting.deals import (
    DEAL_STATUSES,
    PLACEMENT_TYPES,
    get_deal,
    html_escape,
    list_deals,
    recent_reply_sync,
    sync_replies,
    update_deal,
)
from guest_post_prospecting.db import init_db


app = FastAPI(title="Guest Post Deal Tracker")


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; color: #151515; background: #f6f7f8; }}
    header {{ background: #fff; border-bottom: 1px solid #d9dde3; padding: 18px 24px; display: flex; align-items: center; justify-content: space-between; }}
    main {{ max-width: 1240px; margin: 0 auto; padding: 24px; }}
    a {{ color: #0b5cad; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d9dde3; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e5e8ed; text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ background: #eef1f5; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #4b5563; }}
    input, select, textarea {{ width: 100%; box-sizing: border-box; border: 1px solid #c7ccd4; border-radius: 6px; padding: 8px 10px; font: inherit; background: #fff; }}
    textarea {{ min-height: 96px; }}
    button {{ border: 0; border-radius: 6px; padding: 9px 13px; background: #151515; color: #fff; font-weight: 650; cursor: pointer; }}
    .nav {{ display: flex; gap: 14px; }}
    .filters {{ display: grid; grid-template-columns: 1fr 1fr 1fr 2fr auto; gap: 10px; align-items: end; margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .panel {{ background: #fff; border: 1px solid #d9dde3; border-radius: 8px; padding: 16px; margin-bottom: 18px; }}
    .muted {{ color: #667085; }}
    .pill {{ display: inline-block; border-radius: 999px; background: #edf2ff; color: #243b6b; padding: 3px 8px; font-size: 12px; }}
    .evidence {{ white-space: pre-wrap; background: #111827; color: #f9fafb; border-radius: 8px; padding: 14px; overflow: auto; }}
  </style>
</head>
<body>
  <header>
    <strong>Guest Post Deal Tracker</strong>
    <nav class="nav"><a href="/">Deals</a><a href="/sync">Sync</a></nav>
  </header>
  <main>{body}</main>
</body>
</html>"""
    )


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(status: str = "", placement_type: str = "", price_present: str = "", domain: str = "") -> HTMLResponse:
    filters = {"status": status, "placement_type": placement_type, "price_present": price_present, "domain": domain}
    deals = list_deals(filters)
    rows = []
    for deal in deals:
        price = ""
        if deal.get("price_amount") is not None:
            price = f"{deal.get('price_currency') or 'AUD'} {deal.get('price_amount'):g}"
        writing = (deal.get("writing_requirements") or deal.get("reply_summary") or "")[:160]
        rows.append(
            f"""<tr>
  <td><a href="/deals/{deal['id']}">{html_escape(deal.get('site_name'))}</a><br><span class="muted">{html_escape(deal.get('contact_email'))}</span></td>
  <td>{html_escape(deal.get('root_domain'))}</td>
  <td><span class="pill">{html_escape(deal.get('placement_type'))}</span></td>
  <td>{html_escape(price)}</td>
  <td>{html_escape(deal.get('domain_trust'))}</td>
  <td>{html_escape(writing)}</td>
  <td>{html_escape(deal.get('deal_status'))}</td>
  <td>{html_escape(deal.get('updated_at'))}</td>
</tr>"""
        )
    status_options = "".join(f'<option value="{value}" {"selected" if value == status else ""}>{value or "all statuses"}</option>' for value in ["", *sorted(DEAL_STATUSES)])
    placement_options = "".join(f'<option value="{value}" {"selected" if value == placement_type else ""}>{value or "all types"}</option>' for value in ["", *sorted(PLACEMENT_TYPES)])
    price_options = "".join(f'<option value="{value}" {"selected" if value == price_present else ""}>{label}</option>' for value, label in [("", "any price"), ("yes", "price present"), ("no", "no price")])
    return page(
        "Deals",
        f"""<section class="panel">
  <form class="filters" method="get" action="/">
    <label>Status<br><select name="status">{status_options}</select></label>
    <label>Type<br><select name="placement_type">{placement_options}</select></label>
    <label>Price<br><select name="price_present">{price_options}</select></label>
    <label>Domain / email<br><input name="domain" value="{html_escape(domain)}"></label>
    <button type="submit">Filter</button>
  </form>
  <div class="muted">{len(deals)} deals shown</div>
</section>
<table>
  <thead><tr><th>Site</th><th>Domain</th><th>Type</th><th>Price</th><th>Domain Trust</th><th>Requirements</th><th>Status</th><th>Updated</th></tr></thead>
  <tbody>{''.join(rows) if rows else '<tr><td colspan="8" class="muted">No deals yet. Run sync when replies contain pricing or requirements.</td></tr>'}</tbody>
</table>""",
    )


@app.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_detail(deal_id: int) -> HTMLResponse:
    deal = get_deal(deal_id)
    if not deal:
        return page("Deal not found", "<p>Deal not found.</p>")
    status_options = "".join(f'<option value="{value}" {"selected" if value == deal.get("deal_status") else ""}>{value}</option>' for value in sorted(DEAL_STATUSES))
    placement_options = "".join(f'<option value="{value}" {"selected" if value == deal.get("placement_type") else ""}>{value}</option>' for value in sorted(PLACEMENT_TYPES))
    price_amount = "" if deal.get("price_amount") is None else f"{deal.get('price_amount'):g}"
    return page(
        deal.get("site_name") or "Deal",
        f"""<form method="post" action="/deals/{deal_id}" class="panel">
  <h1>{html_escape(deal.get('site_name'))}</h1>
  <p class="muted">{html_escape(deal.get('root_domain'))} · {html_escape(deal.get('contact_email'))}</p>
  <div class="grid">
    <label>Status<br><select name="deal_status">{status_options}</select></label>
    <label>Placement type<br><select name="placement_type">{placement_options}</select></label>
    <label>Price amount<br><input name="price_amount" value="{html_escape(price_amount)}"></label>
    <label>Currency<br><input name="price_currency" value="{html_escape(deal.get('price_currency') or 'AUD')}"></label>
    <label>Domain Trust<br><input name="domain_trust" value="{html_escape(deal.get('domain_trust'))}"></label>
    <label>Domain Trust Source<br><input name="domain_trust_source" value="{html_escape(deal.get('domain_trust_source') or 'manual_seranking_later')}"></label>
    <label>Target URL<br><input name="target_url" value="{html_escape(deal.get('target_url'))}"></label>
    <label>Publisher URL<br><input name="publisher_url" value="{html_escape(deal.get('publisher_url'))}"></label>
  </div>
  <p><label>Price notes<br><textarea name="price_notes">{html_escape(deal.get('price_notes'))}</textarea></label></p>
  <p><label>Writing requirements<br><textarea name="writing_requirements">{html_escape(deal.get('writing_requirements'))}</textarea></label></p>
  <p><label>Link requirements<br><textarea name="link_requirements">{html_escape(deal.get('link_requirements'))}</textarea></label></p>
  <p><label>Turnaround time<br><input name="turnaround_time" value="{html_escape(deal.get('turnaround_time'))}"></label></p>
  <p><label>Additional notes<br><textarea name="additional_notes">{html_escape(deal.get('additional_notes'))}</textarea></label></p>
  <button type="submit">Save Deal</button>
</form>
<section class="panel">
  <h2>Extracted Summary</h2>
  <p>{html_escape(deal.get('reply_summary'))}</p>
  <h2>Reply Evidence</h2>
  <div class="evidence">{html_escape(deal.get('reply_evidence_text'))}</div>
</section>""",
    )


@app.post("/deals/{deal_id}")
async def update_deal_route(deal_id: int, request: Request) -> RedirectResponse:
    raw = (await request.body()).decode("utf-8")
    parsed = {key: values[-1] if values else "" for key, values in parse_qs(raw, keep_blank_values=True).items()}
    update_deal(deal_id, parsed)
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@app.get("/sync", response_class=HTMLResponse)
def sync_page() -> HTMLResponse:
    replies = recent_reply_sync()
    rows = "".join(
        f"""<tr><td>{html_escape(row.get('received_at'))}</td><td>{html_escape(row.get('lead_email'))}</td><td>{html_escape(row.get('classification'))}</td><td>{html_escape(row.get('subject'))}</td></tr>"""
        for row in replies
    )
    return page(
        "Sync",
        f"""<section class="panel">
  <form method="post" action="/sync">
    <button type="submit">Sync Instantly Replies</button>
  </form>
</section>
<table>
  <thead><tr><th>Received</th><th>Lead</th><th>Classification</th><th>Subject</th></tr></thead>
  <tbody>{rows if rows else '<tr><td colspan="4" class="muted">No synced replies yet.</td></tr>'}</tbody>
</table>""",
    )


@app.post("/sync", response_class=HTMLResponse)
def run_sync() -> HTMLResponse:
    result = sync_replies()
    return page(
        "Sync complete",
        f"""<section class="panel">
  <h1>Sync complete</h1>
  <pre>{html_escape(result)}</pre>
  <p><a href="/sync">Back to sync</a> · <a href="/">View deals</a></p>
</section>""",
    )
