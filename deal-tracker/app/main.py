from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from deal_tracker.auth import (
    SESSION_COOKIE,
    VISIBILITY_TIERS,
    authenticate,
    create_session,
    create_user,
    current_user,
    delete_session,
    ensure_bootstrap_admin,
    list_users,
    update_user,
)
from deal_tracker.classifier import DEAL_STATUSES, PLACEMENT_TYPES
from deal_tracker.db import init_db
from deal_tracker.deals import (
    create_agency_enquiry,
    dashboard_stats,
    get_catalogue_deal,
    get_deal,
    html_escape,
    list_agency_enquiries,
    list_catalogue_deals,
    list_deals,
    list_publisher_entities,
    list_sync_runs,
    merge_publisher_entities,
    price_band_for,
    publisher_entity_detail,
    recent_reply_sync,
    scorecard_data,
    update_deal,
    update_publisher_entity,
)
from deal_tracker.fx import aud_estimate_label, convert_from_aud, rates_to_aud
from deal_tracker.sync_state import run_sync_recorded, sync_due, sync_interval_days


app = FastAPI(title="Guest Post Deal Tracker")
_sync_lock = asyncio.Lock()


def money(amount: object, currency: object = "AUD") -> str:
    if amount is None or amount == "":
        return ""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    return f"{currency or 'AUD'} {value:g}"


def money_with_aud(amount: object, currency: object = "AUD") -> str:
    original = money(amount, currency)
    if not original:
        return ""
    estimate = aud_estimate_label(amount, currency)
    return f"{original}<br><span class=\"muted\">{html_escape(estimate)}</span>" if estimate else original


def money_with_cross_currency(amount: object, currency: object, compare_currency: object) -> str:
    original = money(amount, currency)
    if not original:
        return ""
    code = str(currency or "AUD").upper()
    compare = str(compare_currency or "AUD").upper()
    if code == compare:
        return original
    if code == "AUD":
        converted = convert_from_aud(amount, compare)
        if converted is None:
            return original
        primary = f"{compare} {converted:,.2f}"
        return f"{html_escape(primary)}<br><span class=\"muted\">{html_escape(original)}</span>"
    else:
        estimate = aud_estimate_label(amount, code)
        return f"{html_escape(original)}<br><span class=\"muted\">{html_escape(estimate)}</span>" if estimate else html_escape(original)


def aud_value_label(amount: object, currency: object = "AUD") -> str:
    if amount is None or amount == "":
        return ""
    code = str(currency or "AUD").upper()
    if code == "AUD":
        return money(amount, "AUD")
    estimate = aud_estimate_label(amount, code)
    return estimate or "AUD estimate unavailable"


def aud_money(amount: object) -> str:
    if amount is None or amount == "":
        return ""
    try:
        return f"AUD {float(amount):,.2f}"
    except (TypeError, ValueError):
        return ""


def pct_label(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return ""


def fx_badge() -> str:
    payload = rates_to_aud()
    rates = payload.get("rates") or {}
    date_bits = payload.get("dates") or {}
    usd_rate = rates.get("USD")
    if not usd_rate:
        return '<span class="muted">AUD conversion unavailable</span>'
    date = date_bits.get("USD") or str(payload.get("fetched_at") or "")[:10]
    return f'<span class="pill">FX: 1 USD ~= AUD {float(usd_rate):.4f}</span> <span class="muted">Frankfurter, {html_escape(date)}</span>'


def option_tags(values: list[str], selected: str, labels: dict[str, str] | None = None) -> str:
    labels = labels or {}
    return "".join(
        f'<option value="{html_escape(value)}" {"selected" if value == selected else ""}>{html_escape(labels.get(value, value or "Any"))}</option>'
        for value in values
    )


def page(title: str, body: str, user: dict | None = None) -> HTMLResponse:
    role = user.get("role") if user else ""
    nav = ""
    if user:
        nav = (
            '<a href="/admin">Admin</a><a href="/admin/scorecard">Scorecard</a><a href="/admin/deals">Deals</a><a href="/admin/agencies">Agencies</a><a href="/admin/sync">Sync</a>'
            if role == "admin"
            else '<a href="/catalogue">Catalogue</a>'
        )
        nav += '<a href="/logout">Logout</a>'
    else:
        nav = '<a href="/login">Login</a>'
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{ color-scheme: light; font-family: "Geist", "Satoshi", "Segoe UI", system-ui, sans-serif; }}
    body {{ margin: 0; background: #f7f8fa; color: #202124; }}
    a {{ color: #285d5f; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    header {{ background: #fbfbfc; border-bottom: 1px solid #d8dde3; padding: 16px 22px; display: flex; align-items: center; justify-content: space-between; gap: 16px; position: sticky; top: 0; }}
    main {{ max-width: 1380px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; line-height: 1.1; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d8dde3; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e7eaee; text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ background: #edf1f3; font-size: 12px; text-transform: uppercase; color: #53606f; }}
    input, select, textarea {{ width: 100%; box-sizing: border-box; border: 1px solid #c8ced6; border-radius: 6px; padding: 8px 10px; font: inherit; background: #fff; }}
    textarea {{ min-height: 94px; }}
    button, .button {{ border: 0; border-radius: 6px; padding: 9px 13px; background: #223234; color: #fff; font-weight: 650; cursor: pointer; display: inline-block; }}
    button:active, .button:active {{ transform: translateY(1px); }}
    .brand {{ font-weight: 760; letter-spacing: 0; }}
    .nav {{ display: flex; gap: 14px; align-items: center; font-size: 14px; }}
    .panel {{ background: #fff; border: 1px solid #d8dde3; border-radius: 8px; padding: 16px; margin-bottom: 18px; }}
    .muted {{ color: #667085; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .metrics {{ display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr 1fr; gap: 12px; margin: 18px 0; }}
    .metric {{ background: #fff; border-top: 3px solid #789397; padding: 14px; }}
    .metric strong {{ display: block; font-size: 24px; font-family: "Geist Mono", ui-monospace, SFMono-Regular, monospace; }}
    .filters {{ display: grid; grid-template-columns: 1fr 1fr 1fr 1fr 1fr auto; gap: 10px; align-items: end; }}
    .pill {{ display: inline-block; border-radius: 999px; background: #e9f0ef; color: #2b5153; padding: 3px 8px; font-size: 12px; }}
    .danger {{ background: #f7e7e5; color: #883d35; }}
    .success {{ background: #e5f0e9; color: #315c42; }}
    .evidence {{ white-space: pre-wrap; background: #202124; color: #f7f8fa; border-radius: 8px; padding: 14px; overflow: auto; }}
    .split {{ display: grid; grid-template-columns: 1.8fr 1fr; gap: 16px; align-items: start; }}
    .catalogue {{ display: grid; grid-template-columns: 1.7fr 1fr 1fr 1fr 1fr auto; gap: 0; }}
    .notice {{ border-left: 4px solid #789397; background: #eef4f3; padding: 12px 14px; margin-bottom: 16px; }}
    @media (max-width: 900px) {{ main {{ padding: 16px; }} .grid, .metrics, .filters, .split {{ grid-template-columns: 1fr; }} table {{ display: block; overflow-x: auto; }} header {{ align-items: flex-start; flex-direction: column; }} }}
  </style>
</head>
<body>
  <header><div class="brand">Guest Post Deal Tracker</div><nav class="nav">{nav}</nav></header>
  <main>{body}</main>
</body>
</html>"""
    )


async def parse_form(request: Request) -> dict[str, str]:
    parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def require_admin(request: Request) -> dict | RedirectResponse:
    user = current_user(request)
    if not user:
        return login_redirect()
    if user.get("role") != "admin":
        return RedirectResponse("/catalogue", status_code=303)
    return user


def require_agency_or_admin(request: Request) -> dict | RedirectResponse:
    user = current_user(request)
    return user if user else login_redirect()


async def maybe_run_background_sync(trigger_type: str) -> None:
    if _sync_lock.locked():
        return
    async with _sync_lock:
        await asyncio.to_thread(run_sync_recorded, trigger_type)


async def sync_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        if sync_due():
            await maybe_run_background_sync("scheduled")


@app.on_event("startup")
async def startup() -> None:
    init_db()
    ensure_bootstrap_admin()
    if sync_due():
        asyncio.create_task(maybe_run_background_sync("startup"))
    asyncio.create_task(sync_loop())


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/admin" if user.get("role") == "admin" else "/catalogue", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "") -> HTMLResponse:
    user = current_user(request)
    if user:
        return page("Already logged in", '<div class="panel"><p>You are already logged in.</p><p><a class="button" href="/">Continue</a></p></div>', user)
    message = '<div class="notice danger">Login failed. Check the email, password, and account status.</div>' if error else ""
    return page(
        "Login",
        f"""{message}<form class="panel" method="post" action="/login" style="max-width: 440px;">
  <h1>Sign in</h1>
  <p class="muted">Use the admin account or an agency account created by Laurence.</p>
  <p><label>Email<br><input name="email" type="email" autocomplete="email" required></label></p>
  <p><label>Password<br><input name="password" type="password" autocomplete="current-password" required></label></p>
  <button type="submit">Login</button>
</form>""",
        None,
    )


@app.post("/login")
async def login_action(request: Request) -> RedirectResponse:
    form = await parse_form(request)
    user = authenticate(form.get("email", ""), form.get("password", ""))
    if not user:
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/admin" if user.get("role") == "admin" else "/catalogue", status_code=303)
    response.set_cookie(SESSION_COOKIE, create_session(int(user["id"])), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return response


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/sync")
def legacy_sync_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/sync", status_code=303)


@app.post("/sync")
def legacy_sync_post_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/sync", status_code=303)


@app.get("/deals/{deal_id}")
def legacy_deal_redirect(deal_id: int) -> RedirectResponse:
    return RedirectResponse(f"/admin/deals/{deal_id}", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    stats = dashboard_stats()
    sync_runs = list_sync_runs(5)
    enquiries = list_agency_enquiries(8)
    sync_rows = "".join(
        f"<tr><td>{html_escape(row.get('started_at'))}</td><td>{html_escape(row.get('trigger_type'))}</td><td>{html_escape(row.get('status'))}</td><td>{row.get('inbound_reply_count')}</td><td>{row.get('new_deal_count')}</td><td>{html_escape(row.get('error_message'))}</td></tr>"
        for row in sync_runs
    )
    enquiry_rows = "".join(
        f"<tr><td>{html_escape(row.get('created_at'))}</td><td>{html_escape(row.get('agency_email'))}</td><td><a href='/admin/deals/{row.get('deal_id')}'>{html_escape(row.get('root_domain'))}</a></td><td>{html_escape(row.get('message'))}</td></tr>"
        for row in enquiries
    )
    return page(
        "Admin",
        f"""<h1>Admin dashboard</h1>
<p class="muted">Private operating view for reply sync, review, pricing, and agency catalogue control.</p>
<section class="metrics">
  <div class="metric"><span class="muted">Deals</span><strong>{stats['total_deals']}</strong></div>
  <div class="metric"><span class="muted">Needs review</span><strong>{stats['needs_review']}</strong></div>
  <div class="metric"><span class="muted">Listed</span><strong>{stats['listed']}</strong></div>
  <div class="metric"><span class="muted">Unpriced</span><strong>{stats['missing_reseller_price']}</strong></div>
  <div class="metric"><span class="muted">Missing DT</span><strong>{stats['missing_domain_trust']}</strong></div>
</section>
<div class="split">
  <section class="panel">
    <h2>Recent syncs</h2>
    <p class="muted">Automatic Instantly sync checks every {sync_interval_days()} days while the app is running.</p>
    <p><a class="button" href="/admin/sync">Open sync controls</a></p>
    <table><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Replies</th><th>New deals</th><th>Error</th></tr></thead><tbody>{sync_rows or '<tr><td colspan="6" class="muted">No sync runs recorded yet.</td></tr>'}</tbody></table>
  </section>
  <section class="panel">
    <h2>Agency enquiries</h2>
    <table><thead><tr><th>When</th><th>Agency</th><th>Deal</th><th>Message</th></tr></thead><tbody>{enquiry_rows or '<tr><td colspan="4" class="muted">No enquiries yet.</td></tr>'}</tbody></table>
  </section>
</div>""",
        user,
    )


def count_table(rows: list[dict], label_header: str = "Bucket") -> str:
    body = "".join(f"<tr><td>{html_escape(row.get('label'))}</td><td>{row.get('count')}</td></tr>" for row in rows)
    return f"<table><thead><tr><th>{html_escape(label_header)}</th><th>Domains</th></tr></thead><tbody>{body or '<tr><td colspan=\"2\" class=\"muted\">No data.</td></tr>'}</tbody></table>"


def deal_flag_rows(rows: list[dict], *, show_margin: bool = False) -> str:
    body = ""
    for row in rows:
        margin = f"<td>{aud_money(row.get('margin_aud'))}</td>" if show_margin else ""
        body += (
            f"<tr><td><a href='/admin/deals/{row.get('id')}'>{html_escape(row.get('root_domain'))}</a>"
            f"<br><span class='muted'>{html_escape(row.get('site_name'))}</span></td>"
            f"<td>{money_with_aud(row.get('publisher_cost_amount'), row.get('publisher_cost_currency'))}</td>"
            f"<td>{money_with_aud(row.get('reseller_price_amount'), row.get('reseller_price_currency'))}</td>"
            f"{margin}<td>{html_escape(row.get('domain_trust'))}</td></tr>"
        )
    columns = "<th>Domain</th><th>Publisher cost</th><th>Reseller</th>" + ("<th>Margin</th>" if show_margin else "") + "<th>DT</th>"
    colspan = 5 if show_margin else 4
    return f"<table><thead><tr>{columns}</tr></thead><tbody>{body or f'<tr><td colspan=\"{colspan}\" class=\"muted\">No rows.</td></tr>'}</tbody></table>"


@app.get("/admin/scorecard", response_class=HTMLResponse)
def admin_scorecard(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    data = scorecard_data()
    totals = data["totals"]
    entity_rows = "".join(
        f"""<tr>
  <td><a href="/admin/publishers/{entity['id']}">{html_escape(entity.get('name'))}</a><br><span class="muted">{html_escape(entity.get('primary_email') or entity.get('primary_domain'))}</span></td>
  <td>{entity.get('domain_count')}</td>
  <td>{aud_money(entity.get('avg_publisher_cost_aud'))}</td>
  <td>{aud_money(entity.get('avg_reseller_price_aud'))}</td>
  <td>{aud_money(entity.get('avg_margin_aud'))}<br><span class="muted">{pct_label(entity.get('margin_pct'))}</span></td>
  <td>{html_escape(entity.get('lowest_domain'))}</td>
  <td>{html_escape(entity.get('highest_domain'))}</td>
  <td>{entity.get('missing_domain_trust')}</td>
  <td>{html_escape(entity.get('restrictions'))}</td>
</tr>"""
        for entity in data["entities"]
    )
    concentration_rows = "".join(
        f"<tr><td><a href='/admin/publishers/{row.get('id')}'>{html_escape(row.get('name'))}</a></td><td>{row.get('domain_count')}</td><td>{html_escape(row.get('primary_email') or row.get('primary_domain'))}</td></tr>"
        for row in data["flags"]["high_concentration"]
    )
    recent_rows = "".join(
        f"<tr><td><a href='/admin/deals/{row.get('id')}'>{html_escape(row.get('root_domain'))}</a></td><td>{html_escape(row.get('contact_email'))}</td><td>{html_escape(row.get('updated_at'))}</td></tr>"
        for row in data["flags"]["recent_needs_review"]
    )
    link_rows = "".join(
        f"<tr><td><a href='/admin/deals/{row.get('id')}'>{html_escape(row.get('root_domain'))}</a></td><td>{money_with_aud(row.get('link_insertion_cost_amount'), row.get('link_insertion_cost_currency'))}</td><td>{money_with_aud(row.get('link_insertion_reseller_price_amount'), row.get('link_insertion_reseller_price_currency'))}</td></tr>"
        for row in data["flags"]["link_insertions"]
    )
    return page(
        "Scorecard",
        f"""<h1>Publisher inventory scorecard</h1>
<p class="muted">Internal commercial health report. Costs, margins, and publisher entities are hidden from agency catalogue users.</p>
<p>{fx_badge()}</p>
<section class="metrics">
  <div class="metric"><span class="muted">Total domains</span><strong>{totals['total_domains']}</strong></div>
  <div class="metric"><span class="muted">Listed domains</span><strong>{totals['listed_domains']}</strong></div>
  <div class="metric"><span class="muted">Managing entities</span><strong>{totals['managing_entities']}</strong></div>
  <div class="metric"><span class="muted">Priced domains</span><strong>{totals['priced_domains']}</strong></div>
  <div class="metric"><span class="muted">Needs review</span><strong>{totals['needs_review']}</strong></div>
  <div class="metric"><span class="muted">Avg cost</span><strong>{aud_money(totals['avg_publisher_cost_aud']) or 'Unset'}</strong></div>
  <div class="metric"><span class="muted">Avg reseller</span><strong>{aud_money(totals['avg_reseller_price_aud']) or 'Unset'}</strong></div>
  <div class="metric"><span class="muted">Avg margin</span><strong>{aud_money(totals['avg_margin_aud']) or 'Unset'}</strong></div>
  <div class="metric"><span class="muted">Margin %</span><strong>{pct_label(totals['margin_pct']) or 'Unset'}</strong></div>
  <div class="metric"><span class="muted">Missing DT</span><strong>{totals['missing_domain_trust']}</strong></div>
</section>
<section class="panel">
  <h2>Managing publisher entities</h2>
  <table><thead><tr><th>Entity</th><th>Domains</th><th>Avg cost</th><th>Avg reseller</th><th>Margin</th><th>Lowest</th><th>Highest</th><th>Missing DT</th><th>Flags</th></tr></thead><tbody>{entity_rows or '<tr><td colspan="9" class="muted">No publisher entities yet.</td></tr>'}</tbody></table>
</section>
<div class="split">
  <section class="panel"><h2>Breakdown by TLD</h2>{count_table(data['breakdowns']['tld'], 'TLD')}</section>
  <section class="panel"><h2>Breakdown by price band</h2>{count_table(data['breakdowns']['price_band'], 'Band')}</section>
</div>
<div class="split">
  <section class="panel"><h2>Breakdown by placement</h2>{count_table(data['breakdowns']['placement_type'], 'Placement')}</section>
  <section class="panel"><h2>Breakdown by Domain Trust</h2>{count_table(data['breakdowns']['domain_trust'], 'DT bucket')}</section>
</div>
<section class="panel"><h2>Breakdown by industry</h2>{count_table(data['breakdowns']['industry'], 'Industry')}</section>
<div class="split">
  <section class="panel"><h2>Cheapest viable placements</h2>{deal_flag_rows(data['flags']['cheapest'])}</section>
  <section class="panel"><h2>Highest margin placements</h2>{deal_flag_rows(data['flags']['highest_margin'], show_margin=True)}</section>
</div>
<div class="split">
  <section class="panel"><h2>Low or negative margin</h2>{deal_flag_rows(data['flags']['low_margin'], show_margin=True)}</section>
  <section class="panel"><h2>Missing reseller price</h2>{deal_flag_rows(data['flags']['missing_reseller_price'])}</section>
</div>
<div class="split">
  <section class="panel"><h2>Missing Domain Trust</h2>{deal_flag_rows(data['flags']['missing_domain_trust'])}</section>
  <section class="panel"><h2>Link insertion pricing</h2><table><thead><tr><th>Domain</th><th>Publisher cost</th><th>Reseller</th></tr></thead><tbody>{link_rows or '<tr><td colspan="3" class="muted">No link insertion pricing yet.</td></tr>'}</tbody></table></section>
</div>
<div class="split">
  <section class="panel"><h2>Concentration risk</h2><table><thead><tr><th>Entity</th><th>Domains</th><th>Contact</th></tr></thead><tbody>{concentration_rows or '<tr><td colspan="3" class="muted">No high-concentration entities.</td></tr>'}</tbody></table></section>
  <section class="panel"><h2>Recent needs review</h2><table><thead><tr><th>Domain</th><th>Contact</th><th>Updated</th></tr></thead><tbody>{recent_rows or '<tr><td colspan="3" class="muted">No review backlog.</td></tr>'}</tbody></table></section>
</div>""",
        user,
    )


@app.get("/admin/deals", response_class=HTMLResponse)
def admin_deals(request: Request, domain: str = "", admin_review_status: str = "", listed: str = "", needs_reseller_price: str = "", needs_domain_trust: str = ""):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    filters = {
        "domain": domain,
        "admin_review_status": admin_review_status,
        "listed": listed,
        "needs_reseller_price": needs_reseller_price,
        "needs_domain_trust": needs_domain_trust,
    }
    deals = list_deals(filters)
    review_options = option_tags(["", "needs_review", "approved", "rejected", "archived"], admin_review_status, {"": "Any review status"})
    listed_options = option_tags(["", "yes", "no"], listed, {"": "Any listing", "yes": "Listed", "no": "Unlisted"})
    reseller_options = option_tags(["", "yes"], needs_reseller_price, {"": "Any reseller price", "yes": "Missing reseller price"})
    dt_options = option_tags(["", "yes"], needs_domain_trust, {"": "Any Domain Trust", "yes": "Missing Domain Trust"})
    rows = []
    for deal in deals:
        listed_pill = '<span class="pill success">listed</span>' if deal.get("is_listed") else '<span class="pill">unlisted</span>'
        rows.append(
            f"""<tr>
  <td><input type="checkbox" name="deal_ids" value="{deal['id']}"></td>
  <td><a href="/admin/deals/{deal['id']}">{html_escape(deal.get('root_domain'))}</a><br><span class="muted">{html_escape(deal.get('site_name'))}</span></td>
  <td>{html_escape(deal.get('industry')) or '<span class="muted">Unset</span>'}</td>
  <td>{money_with_aud(deal.get('publisher_cost_amount'), deal.get('publisher_cost_currency'))}</td>
  <td>{money_with_cross_currency(deal.get('reseller_price_amount'), deal.get('reseller_price_currency'), deal.get('publisher_cost_currency'))}<br><span class="muted">{html_escape(deal.get('price_band'))}</span></td>
  <td>{money_with_aud(deal.get('link_insertion_cost_amount'), deal.get('link_insertion_cost_currency'))}</td>
  <td>{money_with_cross_currency(deal.get('link_insertion_reseller_price_amount'), deal.get('link_insertion_reseller_price_currency'), deal.get('link_insertion_cost_currency'))}</td>
  <td>{html_escape(deal.get('domain_trust'))}</td>
  <td>{listed_pill}<br>{html_escape(deal.get('admin_review_status'))}</td>
  <td>{html_escape(deal.get('visibility_min_tier'))}</td>
</tr>"""
        )
    return page(
        "Admin Deals",
        f"""<section class="panel">
  <h1>Deals</h1>
  <p>{fx_badge()}</p>
  <form class="filters" method="get" action="/admin/deals">
    <label>Domain / site<br><input name="domain" value="{html_escape(domain)}"></label>
    <label>Review<br><select name="admin_review_status">{review_options}</select></label>
    <label>Listing<br><select name="listed">{listed_options}</select></label>
    <label>Price<br><select name="needs_reseller_price">{reseller_options}</select></label>
    <label>Domain Trust<br><select name="needs_domain_trust">{dt_options}</select></label>
    <button type="submit">Filter</button>
  </form>
  <p class="muted">{len(deals)} deals shown</p>
</section>
<form method="post" action="/admin/deals/bulk">
  <section class="panel">
    <label>Bulk action<br><select name="action"><option value="approve_list">Approve and list</option><option value="unlist">Unlist</option><option value="needs_review">Mark needs review</option></select></label>
    <p><button type="submit">Apply to selected</button></p>
  </section>
  <table><thead><tr><th></th><th>Domain</th><th>Industry</th><th>Publisher cost</th><th>Reseller</th><th>Link insert cost</th><th>Link insert reseller</th><th>DT</th><th>Status</th><th>Min tier</th></tr></thead><tbody>{''.join(rows) if rows else '<tr><td colspan="10" class="muted">No deals match the current filters.</td></tr>'}</tbody></table>
</form>""",
        user,
    )


@app.post("/admin/deals/bulk")
async def admin_deals_bulk(request: Request) -> RedirectResponse:
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    action = form.get("action", "")
    raw_ids = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True).get("deal_ids", [])
    for raw_id in raw_ids:
        try:
            deal_id = int(raw_id)
        except ValueError:
            continue
        if action == "approve_list":
            update_deal(deal_id, {"admin_review_status": "approved", "is_listed": "1"})
        elif action == "unlist":
            update_deal(deal_id, {"is_listed": "0"})
        elif action == "needs_review":
            update_deal(deal_id, {"admin_review_status": "needs_review"})
    return RedirectResponse("/admin/deals", status_code=303)


@app.get("/admin/deals/{deal_id}", response_class=HTMLResponse)
def admin_deal_detail(request: Request, deal_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    deal = get_deal(deal_id)
    if not deal:
        return page("Deal not found", '<section class="panel">Deal not found.</section>', user)
    status_options = option_tags(sorted(DEAL_STATUSES), deal.get("deal_status") or "needs_review")
    placement_options = option_tags(sorted(PLACEMENT_TYPES), deal.get("placement_type") or "other")
    visibility_options = option_tags(sorted(VISIBILITY_TIERS), deal.get("visibility_min_tier") or "basic")
    review_options = option_tags(["needs_review", "approved", "rejected", "archived"], deal.get("admin_review_status") or "needs_review")
    entity_options = "".join(
        f'<option value="{entity["id"]}" {"selected" if entity["id"] == deal.get("publisher_entity_id") else ""}>{html_escape(entity.get("name"))} ({entity.get("listed_count")}/{entity.get("domain_count")})</option>'
        for entity in list_publisher_entities()
    )
    listed_checked = "checked" if deal.get("is_listed") else ""
    link_insertion_cost = "" if deal.get("link_insertion_cost_amount") is None else f"{deal.get('link_insertion_cost_amount'):g}"
    link_insertion_reseller = "" if deal.get("link_insertion_reseller_price_amount") is None else f"{deal.get('link_insertion_reseller_price_amount'):g}"
    publisher_aud_estimate = aud_value_label(deal.get("publisher_cost_amount"), deal.get("publisher_cost_currency"))
    reseller_aud_estimate = aud_value_label(deal.get("reseller_price_amount"), deal.get("reseller_price_currency"))
    link_cost_aud_estimate = aud_value_label(deal.get("link_insertion_cost_amount"), deal.get("link_insertion_cost_currency"))
    link_reseller_aud_estimate = aud_value_label(deal.get("link_insertion_reseller_price_amount"), deal.get("link_insertion_reseller_price_currency"))
    return page(
        deal.get("root_domain") or "Deal",
        f"""<form method="post" action="/admin/deals/{deal_id}" class="panel">
  <h1>{html_escape(deal.get('root_domain'))}</h1>
  <p class="muted">{html_escape(deal.get('contact_email'))} · Instantly thread {html_escape(deal.get('instantly_thread_id'))}</p>
  <p>{fx_badge()}</p>
  <div class="grid">
    <label>Deal status<br><select name="deal_status">{status_options}</select></label>
    <label>Admin review<br><select name="admin_review_status">{review_options}</select></label>
    <label>Placement type<br><select name="placement_type">{placement_options}</select></label>
    <label>Industry<br><input name="industry" value="{html_escape(deal.get('industry'))}"></label>
    <label>TLD<br><input name="tld" value="{html_escape(deal.get('tld'))}"></label>
    <label>Domain Trust<br><input name="domain_trust" value="{html_escape(deal.get('domain_trust'))}"></label>
    <label>Publisher cost<br><input name="publisher_cost_amount" value="{html_escape('' if deal.get('publisher_cost_amount') is None else f'{deal.get('publisher_cost_amount'):g}')}"></label>
    <label>Publisher currency<br><input name="publisher_cost_currency" value="{html_escape(deal.get('publisher_cost_currency') or 'AUD')}"></label>
    <p><strong>Publisher cost display</strong><br>{html_escape(money(deal.get('publisher_cost_amount'), deal.get('publisher_cost_currency')) or 'Unset')}<br><span class="muted">{html_escape(publisher_aud_estimate or 'AUD unset')}</span></p>
    <label>Reseller price<br><input name="reseller_price_amount" value="{html_escape('' if deal.get('reseller_price_amount') is None else f'{deal.get('reseller_price_amount'):g}')}"></label>
    <label>Reseller currency<br><input name="reseller_price_currency" value="{html_escape(deal.get('reseller_price_currency') or 'AUD')}"></label>
    <p><strong>Reseller price display</strong><br>{html_escape(money(deal.get('reseller_price_amount'), deal.get('reseller_price_currency')) or 'Unset')}<br><span class="muted">{html_escape(reseller_aud_estimate or 'AUD unset')}</span></p>
    <label>Link insertion publisher cost<br><input name="link_insertion_cost_amount" value="{html_escape(link_insertion_cost)}"></label>
    <label>Link insertion publisher currency<br><input name="link_insertion_cost_currency" value="{html_escape(deal.get('link_insertion_cost_currency') or 'AUD')}"></label>
    <p><strong>Link insertion cost display</strong><br>{html_escape(money(deal.get('link_insertion_cost_amount'), deal.get('link_insertion_cost_currency')) or 'Unset')}<br><span class="muted">{html_escape(link_cost_aud_estimate or 'AUD unset')}</span></p>
    <label>Link insertion reseller price<br><input name="link_insertion_reseller_price_amount" value="{html_escape(link_insertion_reseller)}"></label>
    <label>Link insertion reseller currency<br><input name="link_insertion_reseller_price_currency" value="{html_escape(deal.get('link_insertion_reseller_price_currency') or 'AUD')}"></label>
    <p><strong>Link insertion reseller display</strong><br>{html_escape(money(deal.get('link_insertion_reseller_price_amount'), deal.get('link_insertion_reseller_price_currency')) or 'Unset')}<br><span class="muted">{html_escape(link_reseller_aud_estimate or 'AUD unset')}</span></p>
    <label>Minimum agency tier<br><select name="visibility_min_tier">{visibility_options}</select></label>
    <label>Publisher URL<br><input name="publisher_url" value="{html_escape(deal.get('publisher_url'))}"></label>
    <label>Managing entity<br><select name="publisher_entity_id">{entity_options}</select></label>
  </div>
  <input type="hidden" name="is_listed" value="0">
  <p><label><input style="width:auto" type="checkbox" name="is_listed" value="1" {listed_checked}> Listed in agency catalogue</label></p>
  <p><label>Quality notes<br><textarea name="quality_notes">{html_escape(deal.get('quality_notes'))}</textarea></label></p>
  <p><label>Price notes<br><textarea name="price_notes">{html_escape(deal.get('price_notes'))}</textarea></label></p>
  <p><label>Link insertion notes<br><textarea name="link_insertion_notes">{html_escape(deal.get('link_insertion_notes'))}</textarea></label></p>
  <p><label>Writing requirements<br><textarea name="writing_requirements">{html_escape(deal.get('writing_requirements'))}</textarea></label></p>
  <p><label>Link requirements<br><textarea name="link_requirements">{html_escape(deal.get('link_requirements'))}</textarea></label></p>
  <p><label>Turnaround time<br><input name="turnaround_time" value="{html_escape(deal.get('turnaround_time'))}"></label></p>
  <p><label>Additional private notes<br><textarea name="additional_notes">{html_escape(deal.get('additional_notes'))}</textarea></label></p>
  <button type="submit">Save deal</button>
</form>
<section class="panel"><h2>Original reply evidence</h2><div class="evidence">{html_escape(deal.get('reply_evidence_text'))}</div></section>""",
        user,
    )


@app.post("/admin/deals/{deal_id}")
async def admin_deal_update(request: Request, deal_id: int) -> RedirectResponse:
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    update_deal(deal_id, form)
    return RedirectResponse(f"/admin/deals/{deal_id}", status_code=303)


@app.get("/admin/publishers/{entity_id}", response_class=HTMLResponse)
def admin_publisher_entity(request: Request, entity_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    detail = publisher_entity_detail(entity_id)
    if not detail:
        return page("Publisher not found", '<section class="panel">Publisher entity not found.</section>', user)
    entity = detail["entity"]
    summary = detail["summary"]
    entities = [row for row in list_publisher_entities() if int(row.get("id")) != entity_id]
    merge_options = "".join(f'<option value="{row["id"]}">{html_escape(row.get("name"))}</option>' for row in entities)
    deal_rows = ""
    entity_selects = {row["id"]: row for row in list_publisher_entities()}
    for deal in detail["deals"]:
        options = "".join(
            f'<option value="{row_id}" {"selected" if row_id == deal.get("publisher_entity_id") else ""}>{html_escape(row.get("name"))}</option>'
            for row_id, row in entity_selects.items()
        )
        deal_rows += f"""<tr>
  <td><a href="/admin/deals/{deal['id']}">{html_escape(deal.get('root_domain'))}</a><br><span class="muted">{html_escape(deal.get('site_name'))}</span></td>
  <td>{money_with_aud(deal.get('publisher_cost_amount'), deal.get('publisher_cost_currency'))}</td>
  <td>{money_with_aud(deal.get('reseller_price_amount'), deal.get('reseller_price_currency'))}</td>
  <td>{html_escape(deal.get('domain_trust'))}</td>
  <td>{'listed' if deal.get('is_listed') else 'unlisted'}<br>{html_escape(deal.get('admin_review_status'))}</td>
  <td><form method="post" action="/admin/deals/{deal['id']}/entity"><select name="publisher_entity_id">{options}</select><button type="submit">Move</button></form></td>
</tr>"""
    return page(
        entity.get("name") or "Publisher",
        f"""<section class="panel">
  <h1>{html_escape(entity.get('name'))}</h1>
  <p class="muted">{html_escape(entity.get('primary_email') or entity.get('primary_domain'))}</p>
  <div class="metrics">
    <div class="metric"><span class="muted">Domains</span><strong>{summary['domain_count']}</strong></div>
    <div class="metric"><span class="muted">Avg cost</span><strong>{aud_money(summary['avg_publisher_cost_aud']) or 'Unset'}</strong></div>
    <div class="metric"><span class="muted">Avg reseller</span><strong>{aud_money(summary['avg_reseller_price_aud']) or 'Unset'}</strong></div>
    <div class="metric"><span class="muted">Avg margin</span><strong>{aud_money(summary['avg_margin_aud']) or 'Unset'}</strong></div>
    <div class="metric"><span class="muted">Margin %</span><strong>{pct_label(summary['margin_pct']) or 'Unset'}</strong></div>
  </div>
</section>
<div class="split">
  <form class="panel" method="post" action="/admin/publishers/{entity_id}">
    <h2>Edit entity</h2>
    <p><label>Name<br><input name="name" value="{html_escape(entity.get('name'))}"></label></p>
    <p><label>Primary email<br><input name="primary_email" value="{html_escape(entity.get('primary_email'))}"></label></p>
    <p><label>Primary domain<br><input name="primary_domain" value="{html_escape(entity.get('primary_domain'))}"></label></p>
    <p><label>Notes<br><textarea name="notes">{html_escape(entity.get('notes'))}</textarea></label></p>
    <button type="submit">Save entity</button>
  </form>
  <form class="panel" method="post" action="/admin/publishers/{entity_id}/merge">
    <h2>Merge entity</h2>
    <p class="muted">Moves all domains from this entity into the selected target and removes this entity.</p>
    <p><label>Target entity<br><select name="target_entity_id">{merge_options}</select></label></p>
    <button type="submit">Merge into target</button>
  </form>
</div>
<section class="panel">
  <h2>Domains managed by this entity</h2>
  <table><thead><tr><th>Domain</th><th>Publisher cost</th><th>Reseller</th><th>DT</th><th>Status</th><th>Move</th></tr></thead><tbody>{deal_rows or '<tr><td colspan="6" class="muted">No domains assigned.</td></tr>'}</tbody></table>
</section>""",
        user,
    )


@app.post("/admin/publishers/{entity_id}")
async def admin_publisher_entity_update(request: Request, entity_id: int) -> RedirectResponse:
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    update_publisher_entity(entity_id, form)
    return RedirectResponse(f"/admin/publishers/{entity_id}", status_code=303)


@app.post("/admin/publishers/{entity_id}/merge")
async def admin_publisher_entity_merge(request: Request, entity_id: int) -> RedirectResponse:
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    try:
        target_id = int(form.get("target_entity_id", "0"))
    except ValueError:
        target_id = 0
    if target_id:
        merge_publisher_entities(entity_id, target_id)
        return RedirectResponse(f"/admin/publishers/{target_id}", status_code=303)
    return RedirectResponse(f"/admin/publishers/{entity_id}", status_code=303)


@app.post("/admin/deals/{deal_id}/entity")
async def admin_deal_entity_update(request: Request, deal_id: int) -> RedirectResponse:
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    update_deal(deal_id, {"publisher_entity_id": form.get("publisher_entity_id", "")})
    return RedirectResponse(f"/admin/deals/{deal_id}", status_code=303)


@app.get("/admin/agencies", response_class=HTMLResponse)
def admin_agencies(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    agencies = list_users("agency")
    rows = "".join(
        f"""<tr><form method="post" action="/admin/agencies/{agency['id']}">
  <td><input name="email" value="{html_escape(agency.get('email'))}"></td>
  <td><select name="visibility_tier">{option_tags(sorted(VISIBILITY_TIERS), agency.get('visibility_tier') or 'basic')}</select></td>
  <td><input type="hidden" name="is_active" value="0"><label><input style="width:auto" type="checkbox" name="is_active" value="1" {'checked' if agency.get('is_active') else ''}> Active</label></td>
  <td><input name="password" type="password" placeholder="New password optional"></td>
  <td><button type="submit">Save</button></td>
</form></tr>"""
        for agency in agencies
    )
    return page(
        "Agencies",
        f"""<section class="panel">
  <h1>Agency accounts</h1>
  <form class="grid" method="post" action="/admin/agencies">
    <label>Email<br><input name="email" type="email" required></label>
    <label>Password<br><input name="password" type="password" required></label>
    <label>Visibility tier<br><select name="visibility_tier">{option_tags(sorted(VISIBILITY_TIERS), 'basic')}</select></label>
    <label>Active<br><select name="is_active"><option value="1">Active</option><option value="0">Inactive</option></select></label>
    <p><button type="submit">Create agency</button></p>
  </form>
</section>
<table><thead><tr><th>Email</th><th>Tier</th><th>Status</th><th>Password reset</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="5" class="muted">No agency accounts yet.</td></tr>'}</tbody></table>""",
        user,
    )


@app.post("/admin/agencies")
async def admin_agency_create(request: Request) -> RedirectResponse:
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    create_user(form.get("email", ""), form.get("password", ""), "agency", form.get("visibility_tier", "basic"), form.get("is_active") == "1")
    return RedirectResponse("/admin/agencies", status_code=303)


@app.post("/admin/agencies/{agency_id}")
async def admin_agency_update(request: Request, agency_id: int) -> RedirectResponse:
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    update_user(agency_id, form)
    return RedirectResponse("/admin/agencies", status_code=303)


@app.get("/admin/sync", response_class=HTMLResponse)
def admin_sync_page(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    sync_rows = "".join(
        f"<tr><td>{html_escape(row.get('started_at'))}</td><td>{html_escape(row.get('trigger_type'))}</td><td>{html_escape(row.get('status'))}</td><td>{row.get('inbound_reply_count')}</td><td>{row.get('new_deal_count')}</td><td>{html_escape(row.get('error_message'))}</td></tr>"
        for row in list_sync_runs(30)
    )
    reply_rows = "".join(
        f"<tr><td>{html_escape(row.get('received_at'))}</td><td>{html_escape(row.get('lead_email'))}</td><td>{html_escape(row.get('classification'))}</td><td>{html_escape(row.get('subject'))}</td></tr>"
        for row in recent_reply_sync()
    )
    return page(
        "Sync",
        f"""<section class="panel">
  <h1>Instantly sync</h1>
  <p class="muted">Automatic sync runs every {sync_interval_days()} days while this app is open. Manual sync is still available here.</p>
  <form method="post" action="/admin/sync"><button type="submit">Sync now</button></form>
</section>
<section class="panel"><h2>Sync history</h2><table><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Replies</th><th>New deals</th><th>Error</th></tr></thead><tbody>{sync_rows or '<tr><td colspan="6" class="muted">No sync runs yet.</td></tr>'}</tbody></table></section>
<section class="panel"><h2>Recent replies</h2><table><thead><tr><th>Received</th><th>Lead</th><th>Classification</th><th>Subject</th></tr></thead><tbody>{reply_rows or '<tr><td colspan="4" class="muted">No synced replies yet.</td></tr>'}</tbody></table></section>""",
        user,
    )


@app.post("/admin/sync", response_class=HTMLResponse)
async def admin_run_sync(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    result = await asyncio.to_thread(run_sync_recorded, "manual")
    return page("Sync complete", f"""<section class="panel"><h1>Sync complete</h1><pre>{html_escape(result)}</pre><p><a class="button" href="/admin/sync">Back to sync</a></p></section>""", user)


def agency_domain_label(deal: dict, user: dict) -> str:
    tier = user.get("visibility_tier")
    if tier == "basic":
        return f"{deal.get('tld') or 'publisher'} placement"
    return str(deal.get("root_domain") or deal.get("site_name") or "Publisher")


@app.get("/catalogue", response_class=HTMLResponse)
def catalogue(request: Request, q: str = "", industry: str = "", tld: str = ""):
    user = require_agency_or_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    if user.get("role") == "admin":
        return RedirectResponse("/admin/deals", status_code=303)
    deals = list_catalogue_deals(user, {"q": q, "industry": industry, "tld": tld})
    rows = []
    for deal in deals:
        exact_price = money(deal.get("reseller_price_amount"), deal.get("reseller_price_currency")) if user.get("visibility_tier") in {"trusted", "full"} else deal.get("price_band")
        rows.append(
            f"""<tr>
  <td><a href="/catalogue/{deal['id']}">{html_escape(agency_domain_label(deal, user))}</a></td>
  <td>{html_escape(deal.get('industry'))}</td>
  <td>{html_escape(deal.get('tld'))}</td>
  <td>{html_escape(deal.get('domain_trust'))}</td>
  <td>{html_escape(exact_price)}</td>
  <td><span class="pill">{html_escape(deal.get('placement_type'))}</span></td>
</tr>"""
        )
    return page(
        "Catalogue",
        f"""<section class="panel">
  <h1>Publisher catalogue</h1>
  <p class="muted">Your account tier is {html_escape(user.get('visibility_tier'))}. Catalogue fields are filtered for your access level.</p>
  <form class="filters" method="get" action="/catalogue">
    <label>Search<br><input name="q" value="{html_escape(q)}"></label>
    <label>Industry<br><input name="industry" value="{html_escape(industry)}"></label>
    <label>TLD<br><input name="tld" value="{html_escape(tld)}"></label>
    <span></span><span></span>
    <button type="submit">Filter</button>
  </form>
</section>
<table><thead><tr><th>Publisher</th><th>Industry</th><th>TLD</th><th>Domain Trust</th><th>Price</th><th>Type</th></tr></thead><tbody>{''.join(rows) if rows else '<tr><td colspan="6" class="muted">No listed placements match your filters yet.</td></tr>'}</tbody></table>""",
        user,
    )


@app.get("/catalogue/{deal_id}", response_class=HTMLResponse)
def catalogue_detail(request: Request, deal_id: int, requested: str = ""):
    user = require_agency_or_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    if user.get("role") == "admin":
        return RedirectResponse(f"/admin/deals/{deal_id}", status_code=303)
    deal = get_catalogue_deal(deal_id, user)
    if not deal:
        return page("Not available", '<section class="panel">This placement is not available for your account.</section>', user)
    tier = user.get("visibility_tier")
    publisher = agency_domain_label(deal, user)
    price = money_with_aud(deal.get("reseller_price_amount"), deal.get("reseller_price_currency")) if tier in {"trusted", "full"} else deal.get("price_band")
    link_insertion_price = (
        money_with_aud(deal.get("link_insertion_reseller_price_amount"), deal.get("link_insertion_reseller_price_currency"))
        if tier in {"trusted", "full"} and deal.get("link_insertion_reseller_price_amount") is not None
        else ""
    )
    full_fields = ""
    if tier == "full":
        full_fields = f"""<section class="panel"><h2>Requirements</h2><p>{html_escape(deal.get('writing_requirements'))}</p><p>{html_escape(deal.get('link_requirements'))}</p><p>{html_escape(deal.get('link_insertion_notes'))}</p><p class="muted">{html_escape(deal.get('turnaround_time'))}</p><p>{html_escape(deal.get('quality_notes'))}</p></section>"""
    elif tier == "trusted":
        full_fields = f"""<section class="panel"><h2>Requirements summary</h2><p>{html_escape((deal.get('writing_requirements') or deal.get('link_requirements') or '')[:360])}</p></section>"""
    notice = '<div class="notice success">Request received. Laurence will follow up manually.</div>' if requested else ""
    return page(
        publisher,
        f"""{notice}<section class="panel">
  <h1>{html_escape(publisher)}</h1>
  <div class="grid">
    <p><strong>Industry</strong><br>{html_escape(deal.get('industry'))}</p>
    <p><strong>TLD</strong><br>{html_escape(deal.get('tld'))}</p>
    <p><strong>Domain Trust</strong><br>{html_escape(deal.get('domain_trust'))}</p>
    <p><strong>Price</strong><br>{price}</p>
    <p><strong>Link insertion</strong><br>{link_insertion_price or 'Ask'}</p>
    <p><strong>Placement type</strong><br>{html_escape(deal.get('placement_type'))}</p>
    <p><strong>Price band</strong><br>{html_escape(deal.get('price_band'))}</p>
  </div>
</section>
{full_fields}
<form class="panel" method="post" action="/request-info">
  <h2>Ask Laurence about this placement</h2>
  <input type="hidden" name="deal_id" value="{deal_id}">
  <p><label>Message<br><textarea name="message" placeholder="Client niche, target URL, article status, or questions"></textarea></label></p>
  <button type="submit">Request info</button>
</form>""",
        user,
    )


@app.post("/request-info")
async def request_info(request: Request) -> RedirectResponse:
    user = require_agency_or_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await parse_form(request)
    try:
        deal_id = int(form.get("deal_id", "0"))
    except ValueError:
        return RedirectResponse("/catalogue", status_code=303)
    if get_catalogue_deal(deal_id, user):
        create_agency_enquiry(int(user["id"]), deal_id, form.get("message", ""))
    return RedirectResponse(f"/catalogue/{deal_id}?requested=1", status_code=303)
