from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .db import connect, init_db
from .utils import now_iso


FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v1/latest"
FX_CACHE_HOURS = 12
COMMON_CURRENCIES = {"USD", "EUR", "GBP", "NZD", "CAD", "SGD"}


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _setting(key: str) -> str:
    init_db()
    with connect() as connection:
        row = connection.execute("select value from app_settings where key=?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def _record_setting(key: str, value: str) -> None:
    init_db()
    with connect() as connection:
        connection.execute(
            """
            insert into app_settings (key, value, updated_at) values (?, ?, ?)
            on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_iso()),
        )
        connection.commit()


def _cached_rates() -> dict[str, Any]:
    raw = _setting("fx_rates_to_aud_json")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cache_is_fresh(payload: dict[str, Any]) -> bool:
    fetched_at = _parse_iso(str(payload.get("fetched_at") or ""))
    if not fetched_at:
        return False
    return fetched_at + timedelta(hours=FX_CACHE_HOURS) > datetime.now(timezone.utc)


def _fetch_rate_to_aud(currency: str) -> tuple[float | None, str]:
    if currency == "AUD":
        return 1.0, "AUD"
    params = urlencode({"base": currency, "symbols": "AUD"})
    request = Request(f"{FRANKFURTER_BASE_URL}?{params}", headers={"User-Agent": "GuestPostDealTracker/1.0"})
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rate = payload.get("rates", {}).get("AUD")
    return (float(rate), str(payload.get("date") or "")) if rate is not None else (None, "")


def rates_to_aud(refresh: bool = False) -> dict[str, Any]:
    cached = _cached_rates()
    if cached and not refresh and _cache_is_fresh(cached):
        return cached

    rates = {"AUD": 1.0}
    dates = {}
    for currency in sorted(COMMON_CURRENCIES):
        try:
            rate, date = _fetch_rate_to_aud(currency)
        except Exception:
            rate, date = None, ""
        if rate:
            rates[currency] = rate
            dates[currency] = date

    if len(rates) == 1 and cached:
        return cached

    payload = {"rates": rates, "dates": dates, "fetched_at": now_iso(), "source": "Frankfurter"}
    _record_setting("fx_rates_to_aud_json", json.dumps(payload, sort_keys=True))
    return payload


def convert_to_aud(amount: object, currency: object, refresh: bool = False) -> float | None:
    if amount is None or amount == "":
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    code = str(currency or "AUD").upper()
    if code == "AUD":
        return value
    rate = (rates_to_aud(refresh=refresh).get("rates") or {}).get(code)
    if not rate:
        return None
    return value * float(rate)


def convert_from_aud(amount: object, target_currency: object, refresh: bool = False) -> float | None:
    if amount is None or amount == "":
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    code = str(target_currency or "AUD").upper()
    if code == "AUD":
        return value
    rate = (rates_to_aud(refresh=refresh).get("rates") or {}).get(code)
    if not rate:
        return None
    return value / float(rate)


def aud_estimate_label(amount: object, currency: object) -> str:
    converted = convert_to_aud(amount, currency)
    if converted is None:
        return ""
    if str(currency or "AUD").upper() == "AUD":
        return ""
    return f"~AUD {converted:,.2f}"
