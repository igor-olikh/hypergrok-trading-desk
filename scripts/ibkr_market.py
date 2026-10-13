"""US stock session clock and Opening Bell snapshot. Stdlib only. No account data."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# NYSE/Nasdaq cash session, 2026. Columbus Day and Veterans Day are regular stock sessions.
HOLIDAYS_2026 = {
    datetime(2026, 1, 1).date(),
    datetime(2026, 1, 19).date(),
    datetime(2026, 2, 16).date(),
    datetime(2026, 4, 3).date(),
    datetime(2026, 5, 25).date(),
    datetime(2026, 6, 19).date(),
    datetime(2026, 7, 3).date(),
    datetime(2026, 9, 7).date(),
    datetime(2026, 11, 26).date(),
    datetime(2026, 12, 25).date(),
}
HALF_DAYS_2026 = {
    datetime(2026, 11, 27).date(),
    datetime(2026, 12, 24).date(),
}

PRE_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
HALF_CLOSE = time(13, 0)
POST_CLOSE = time(20, 0)


def session_state(moment: datetime) -> str:
    """pre, regular, post, or closed. `moment` may be any timezone."""
    local = moment.astimezone(EASTERN)
    day = local.date()
    if local.weekday() >= 5 or day in HOLIDAYS_2026:
        return "closed"
    clock = local.time()
    close = HALF_CLOSE if day in HALF_DAYS_2026 else REGULAR_CLOSE
    if PRE_OPEN <= clock < REGULAR_OPEN:
        return "pre"
    if REGULAR_OPEN <= clock < close:
        return "regular"
    if close <= clock < POST_CLOSE:
        return "post"
    return "closed"


def _decimal(value, field: str):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} is not finite")
    return number


def _price(value, field: str):
    """A missing or zero quote is unavailable. Zero is not a stock price here."""
    number = _decimal(value, field)
    if number is None or number == 0:
        return None
    if number < 0:
        raise ValueError(f"{field} must be positive when present")
    return number


def build_stock_snapshot(quote: dict, now: datetime | None = None) -> dict:
    if not isinstance(quote, dict):
        raise ValueError("quote must be an object")
    symbol = str(quote.get("symbol") or "").upper()
    if not symbol:
        raise ValueError("quote is missing symbol")
    last = _price(quote.get("last"), "last")
    if last is None:
        raise ValueError(f"{symbol} last price is unavailable")
    previous = _price(quote.get("prev_close"), "prev_close")
    change = _decimal(quote.get("change"), "change") if quote.get("change") not in (None, "") else None
    change_pct = _decimal(quote.get("change_pct"), "change_pct") if quote.get("change_pct") not in (None, "") else None
    if change_pct is None and previous not in (None, 0):
        change_pct = (last / previous - 1) * Decimal(100)
    if change is None and previous is not None:
        change = last - previous
    observed = quote.get("observed_at")
    if not observed:
        moment = now or datetime.now(timezone.utc)
        observed = moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        session_at = moment
    else:
        session_at = now or datetime.now(timezone.utc)
    halted = quote.get("halted")
    if halted is not None and not isinstance(halted, bool):
        raise ValueError("halted must be true, false, or absent")
    depth = quote.get("depth", "unavailable")
    if depth in (0, "0", None):
        depth = "unavailable"
    bid = _price(quote.get("bid"), "bid")
    ask = _price(quote.get("ask"), "ask")
    volume = quote.get("volume")
    if volume in ("", None):
        volume_out = None
    else:
        volume_out = str(_decimal(volume, "volume"))
    return {
        "schema_version": 2,
        "mode": "read-only",
        "venue": "ibkr",
        "network": quote.get("network") or "gateway",
        "source": {
            "endpoint": "ibkr_client",
            "observed_at": observed,
        },
        "symbol": symbol,
        "conid": quote.get("conid"),
        "exchange": quote.get("exchange") or "",
        "currency": quote.get("currency") or "USD",
        "session": session_state(session_at),
        "halted": halted,
        "market_data": quote.get("market_data") or "unavailable",
        "prices": {
            "last": str(last),
            "bid": None if bid is None else str(bid),
            "ask": None if ask is None else str(ask),
            "previous_close": None if previous is None else str(previous),
            "change": None if change is None else str(change),
            "change_pct": None if change_pct is None else str(change_pct),
        },
        "activity": {"volume": volume_out},
        "book": {"depth": depth},
        "safety": "No order created or sent. Missing fields are unavailable, not zero.",
    }


def render_stock(snapshot: dict) -> str:
    price = snapshot["prices"]
    halted = snapshot["halted"]
    halt_text = "unavailable" if halted is None else ("HALTED" if halted else "not halted")
    bid = price["bid"] or "unavailable"
    ask = price["ask"] or "unavailable"
    previous = price["previous_close"] or "unavailable"
    change = "unavailable" if price["change_pct"] is None else f"{Decimal(price['change_pct']):+,.2f}%"
    volume = snapshot["activity"]["volume"] or "unavailable"
    depth = snapshot["book"]["depth"]
    lines = [
        f"HYPERGROK OPENING BELL — {snapshot['symbol']}",
        f"READ ONLY · IBKR · {snapshot['session']} session · {snapshot['source']['observed_at']}",
        "Source: IBKR market data. No funding, no open interest, no oracle.",
        "",
        f"Last        {Decimal(price['last']):,.2f} {snapshot['currency']}",
        f"Bid / ask   {bid} / {ask}",
        f"Prev close  {previous}",
        f"Change      {change}",
        f"Volume      {volume}",
        f"Session     {snapshot['session']}",
        f"Halt        {halt_text}",
        f"Data        {snapshot['market_data']}",
        f"Depth       {depth}",
        "",
        snapshot["safety"],
        "Facts only. This snapshot is not a trading signal.",
    ]
    return "\n".join(lines)
