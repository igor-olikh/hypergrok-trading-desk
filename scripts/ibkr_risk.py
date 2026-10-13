"""Stock risk ceilings, share sizing, PDT and cash-settlement checks. Stdlib only.

Figures passed in are already read from the broker. This module does not call the network
and does not print account ids.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation

MAX_RISK = Decimal("0.01")
MAX_POSITION = Decimal("0.20")
MAX_OPEN_RISK = Decimal("0.05")
MAX_SECTOR = Decimal("0.35")
DAILY_LOSS = Decimal("-0.03")
PDT_EQUITY = Decimal("25000")


def _money(value, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} is not finite")
    return number


def tick_size(price: Decimal) -> Decimal:
    price = _money(price, "price")
    if price <= 0:
        raise ValueError("price must be positive")
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def round_limit(price, side: str) -> Decimal:
    """Buy limits round down to the tick. Sell limits round up. Never through the tick the wrong way."""
    price = _money(price, "price")
    if price <= 0:
        raise ValueError("price must be positive")
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    tick = tick_size(price)
    rounding = ROUND_DOWN if side == "BUY" else ROUND_UP
    rounded = (price / tick).to_integral_value(rounding=rounding) * tick
    if rounded <= 0:
        raise ValueError("price rounds to zero")
    return rounded


def whole_shares(quantity) -> int:
    number = _money(quantity, "quantity")
    if number <= 0:
        raise ValueError("quantity must be positive")
    shares = int(number.to_integral_value(rounding=ROUND_DOWN))
    if shares < 1:
        raise ValueError("quantity rounds below 1 share")
    return shares


def size_shares(equity, entry, stop, slippage, commission_per_share, risk_pct=MAX_RISK,
                max_position_pct=MAX_POSITION, buying_power=None, sector_room=None) -> dict:
    """Long-only share count from the stressed stop. Returns a rejection instead of rounding up."""
    equity = _money(equity, "equity")
    entry = _money(entry, "entry")
    stop = _money(stop, "stop")
    slippage = _money(slippage, "slippage")
    commission = _money(commission_per_share, "commission")
    risk_pct = _money(risk_pct, "risk_pct")
    max_position_pct = _money(max_position_pct, "max_position_pct")
    if equity <= 0 or entry <= 0 or risk_pct <= 0 or max_position_pct <= 0:
        raise ValueError("equity, entry and percents must be positive")
    if risk_pct > MAX_RISK or max_position_pct > MAX_POSITION:
        return {"ok": False, "reason": "limits file exceeds desk ceiling"}
    if stop <= 0 or stop >= entry:
        return {"ok": False, "reason": "stop must be below entry for a long"}
    if slippage < 0 or commission < 0:
        return {"ok": False, "reason": "slippage and commission must be nonnegative"}
    distance = (entry - stop) + slippage + commission
    if distance <= 0:
        return {"ok": False, "reason": "stressed distance must be positive"}
    budget = equity * risk_pct
    shares = int((budget / distance).to_integral_value(rounding=ROUND_DOWN))
    cap = int((equity * max_position_pct / entry).to_integral_value(rounding=ROUND_DOWN))
    shares = min(shares, cap)
    if buying_power is not None:
        power = _money(buying_power, "buying_power")
        if power < 0:
            power = Decimal(0)
        shares = min(shares, int((power / entry).to_integral_value(rounding=ROUND_DOWN)))
    if sector_room is not None:
        room = _money(sector_room, "sector_room")
        if room < 0:
            room = Decimal(0)
        shares = min(shares, int((room / entry).to_integral_value(rounding=ROUND_DOWN)))
    if shares < 1:
        return {"ok": False, "reason": "risk budget is below 1 share after caps"}
    notional = shares * entry
    risk = shares * distance
    return {
        "ok": True,
        "shares": shares,
        "notional": notional,
        "risk": risk,
        "risk_pct_of_equity": risk / equity,
        "stressed_distance": distance,
    }


def _weight(value, equity: Decimal) -> Decimal:
    return _money(value, "market_value") / equity


def review_new_risk(state: dict, order: dict) -> dict:
    """Decide whether a new opening order is allowed. Empty reasons means PASS."""
    reasons = []
    equity = _money(state.get("equity"), "equity")
    if equity <= 0:
        return {"decision": "REJECT", "reasons": ["equity is unavailable"]}
    side = order.get("side")
    if side not in ("BUY", "SELL"):
        return {"decision": "REJECT", "reasons": ["side must be BUY or SELL"]}
    if side == "SELL" and not order.get("short_ok"):
        reasons.append("short selling is off")
    if order.get("halted") is not False:
        reasons.append("halt status is not a clear not-halted")
    session = order.get("session")
    if session != "regular":
        if not (order.get("outside_rth") and order.get("order_type") == "LMT" and session in ("pre", "post")):
            reasons.append("outside regular hours needs an explicit outside-hours limit")
    leverage = _money(state.get("leverage", "0"), "leverage")
    if leverage > 1 and not state.get("margin_enabled"):
        reasons.append("leverage is above the 1x cash ceiling")
    buying_power = state.get("buying_power")
    if side == "BUY" and buying_power is not None and _money(buying_power, "buying_power") <= 0:
        reasons.append("buying power is zero")
    start = state.get("start_of_day_equity")
    if start is not None:
        start = _money(start, "start_of_day_equity")
        if start > 0 and (equity / start - 1) <= DAILY_LOSS:
            reasons.append("daily loss stop is hit")
    account_type = state.get("account_type") or "cash"
    if (account_type == "margin" and equity < PDT_EQUITY and order.get("is_day_trade")
            and int(state.get("day_trades_remaining") or 0) <= 0):
        reasons.append("pattern day trader limit is used up")
    if account_type == "cash" and side == "BUY":
        settled = _money(state.get("settled_cash", "0"), "settled_cash")
        notional = _money(order.get("notional", "0"), "notional")
        if notional > settled:
            reasons.append("cash account would spend unsettled money")
    for position in state.get("positions") or []:
        weight = _weight(position.get("market_value", "0"), equity)
        if weight > MAX_POSITION:
            reasons.append(f"{position.get('symbol') or 'a position'} is over 20% of equity")
    sectors: dict[str, Decimal] = {}
    for position in state.get("positions") or []:
        name = position.get("sector") or ""
        if not name:
            continue
        sectors[name] = sectors.get(name, Decimal(0)) + _money(position.get("market_value", "0"), "market_value")
    order_sector = order.get("sector") or ""
    order_notional = _money(order.get("notional", "0"), "notional") if order.get("notional") not in (None, "") else Decimal(0)
    if order_sector:
        sectors[order_sector] = sectors.get(order_sector, Decimal(0)) + order_notional
    for name, total in sectors.items():
        if total / equity > MAX_SECTOR:
            reasons.append(f"sector {name} is over 35% of equity")
    open_risk = _money(state.get("open_risk", "0"), "open_risk")
    added = _money(order.get("risk", "0"), "risk") if order.get("risk") not in (None, "") else Decimal(0)
    if side == "BUY" and (open_risk + added) / equity > MAX_OPEN_RISK:
        reasons.append("total open risk would pass 5% of equity")
    if side == "BUY" and added / equity > MAX_RISK:
        reasons.append("this trade risks more than 1% of equity")
    if not order.get("stop"):
        reasons.append("protective stop is missing")
    return {"decision": "REJECT" if reasons else "PASS", "reasons": reasons}
