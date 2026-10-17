#!/usr/bin/env python3
"""Send one approved IBKR bracket, or refuse. Stdlib only.

State (the sent-ticket journal) stays outside the repo. Nothing here prints an account id.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal

from ibkr_client import IbkrError
from ibkr_risk import review_new_risk, round_limit
from ibkr_venue import OFF, enabled

TICKET_RE = re.compile(r"^IB-\d{8}-\d{2}$")
STATE_NAME = "sent-tickets.json"


class SendRefused(Exception):
    pass


def state_dir() -> str:
    return os.environ.get("DESK_STATE_DIR", os.path.expanduser("~/.hypergrok/trading-desk"))


def _journal_path(directory: str) -> str:
    return os.path.join(directory, STATE_NAME)


def _load_journal(directory: str) -> dict:
    path = _journal_path(directory)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SendRefused("ticket journal is unreadable")
    return payload


def _save_journal(directory: str, journal: dict) -> None:
    os.makedirs(directory, exist_ok=True)
    path = _journal_path(directory)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(journal, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def format_ticket(ticket: dict) -> str:
    return (
        f"TICKET {ticket['id']} | {ticket['mode']} | {ticket['symbol']} (conid {ticket['conid']}) "
        f"{ticket['side']} {ticket['qty']} sh {ticket['order_type']} {ticket['limit']} {ticket['tif']} | "
        f"stop SELL {ticket['stop_qty']} STP {ticket['stop']} GTC (bracket) | "
        f"risk {ticket.get('risk_usd', '?')} = {ticket.get('risk_pct', '?')} of equity | "
        f'approve with: "approve {ticket["id"]}"'
    )


def _require_ticket(ticket: dict) -> None:
    if not isinstance(ticket, dict) or not TICKET_RE.fullmatch(str(ticket.get("id") or "")):
        raise SendRefused("ticket id must look like IB-YYYYMMDD-NN")
    for key in ("mode", "symbol", "conid", "side", "qty", "order_type", "limit", "tif", "stop", "stop_qty"):
        if ticket.get(key) in (None, ""):
            raise SendRefused(f"ticket is missing {key}")
    if ticket["side"] != "BUY" or ticket["order_type"] != "LMT":
        raise SendRefused("this desk sends a long limit bracket only")
    if int(ticket["qty"]) < 1 or int(ticket["stop_qty"]) != int(ticket["qty"]):
        raise SendRefused("stop size must match the entry size")


def live_allowed(mode: str, account_kind: str, live_flag: bool, env: dict, confirmation: str) -> None:
    if mode == "paper":
        if account_kind != "paper":
            raise SendRefused("paper ticket requires a paper account")
        if live_flag:
            raise SendRefused("drop --live for a paper ticket")
        return
    if mode != "live":
        raise SendRefused("mode must be paper or live")
    if account_kind == "paper":
        raise SendRefused("a paper account cannot take a live ticket")
    if env.get("IBKR_LIVE") != "1" or not live_flag or confirmation != "confirm live":
        raise SendRefused("live needs IBKR_LIVE=1, --live, and confirm live")


def send_once(client, ticket: dict, approval: str, *, directory: str, risk_state: dict | None = None,
              live_flag: bool = False, env: dict | None = None, confirmation: str = "") -> dict:
    """Place the bracket once. Any refusal happens before place_order."""
    if not enabled():
        raise SendRefused(OFF)
    _require_ticket(ticket)
    expected = f"approve {ticket['id']}"
    if approval != expected:
        raise SendRefused(f'approval must be exactly "{expected}"')
    env = os.environ if env is None else env
    account_kind = getattr(client, "account_kind", None) or ticket.get("account_kind") or "unknown"
    live_allowed(ticket["mode"], account_kind, live_flag, env, confirmation)
    if ticket.get("halted") is not False:
        raise SendRefused("halt is not clearly false")
    session = ticket.get("session")
    if session != "regular" and not (ticket.get("outside_rth") and session in ("pre", "post")):
        raise SendRefused("session does not allow this order")
    last = ticket.get("last")
    if last not in (None, ""):
        moved = abs(Decimal(str(last)) - Decimal(str(ticket["limit"]))) / Decimal(str(ticket["limit"])) * Decimal(10000)
        cap = Decimal(str(ticket.get("max_slippage_bps", "10")))
        if moved > cap:
            raise SendRefused("price moved past the ticket slippage")
    # Fail closed: no risk state, no send. The risk check is not optional.
    if not isinstance(risk_state, dict) or not risk_state:
        raise SendRefused("risk state is required; refusing to send without a risk check")
    try:
        decision = review_new_risk(risk_state, {
            "side": ticket["side"],
            "halted": ticket.get("halted"),
            "session": session,
            "outside_rth": ticket.get("outside_rth"),
            "order_type": ticket["order_type"],
            "notional": str(Decimal(str(ticket["qty"])) * Decimal(str(ticket["limit"]))),
            "risk": str(max(Decimal(str(ticket.get("risk_usd") or "0")),
                            (Decimal(str(ticket["limit"])) - Decimal(str(ticket["stop"]))) * Decimal(str(ticket["qty"])))),
            "stop": ticket["stop"],
            "sector": ticket.get("sector") or "",
            "is_day_trade": ticket.get("is_day_trade", False),
            "short_ok": False,
        })
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise SendRefused(f"risk check could not run: {exc}") from exc
    if decision["decision"] != "PASS":
        raise SendRefused("; ".join(decision["reasons"]))
    journal = _load_journal(directory)
    prior = journal.get(ticket["id"])
    if prior:
        raise SendRefused(f"ticket {ticket['id']} was already sent")
    limit = round_limit(ticket["limit"], "BUY")
    stop = round_limit(ticket["stop"], "SELL")
    if limit != Decimal(str(ticket["limit"])) or stop != Decimal(str(ticket["stop"])):
        raise SendRefused("ticket prices are not on the tick")
    order = {
        "account_kind": account_kind,
        "conid": int(ticket["conid"]),
        "side": "BUY",
        "quantity": int(ticket["qty"]),
        "order_type": "LMT",
        "price": str(limit),
        "tif": ticket["tif"],
        "outside_rth": bool(ticket.get("outside_rth")),
        "order_ref": ticket["id"],
        "stop": {"side": "SELL", "quantity": int(ticket["stop_qty"]), "price": str(stop), "tif": "GTC"},
    }
    # Record the intent before the call so a crash cannot be followed by a silent second send.
    journal[ticket["id"]] = {"state": "sending", "order_ref": ticket["id"]}
    _save_journal(directory, journal)
    try:
        result = client.place_order(order)
    except Exception:
        journal[ticket["id"]] = {"state": "unknown", "order_ref": ticket["id"]}
        _save_journal(directory, journal)
        raise
    journal[ticket["id"]] = {"state": "sent", "order_ref": ticket["id"], "broker": _public_result(result)}
    _save_journal(directory, journal)
    return {"ticket": ticket["id"], "state": "sent", "broker": _public_result(result)}


def _public_result(result) -> dict:
    if not isinstance(result, dict):
        return {"accepted": True}
    allowed = {}
    for key in ("order_id", "order_status", "local_order_id"):
        if key in result:
            allowed[key] = result[key]
    return allowed or {"accepted": True}


def risk_state_from_client(client, overrides: dict | None = None) -> dict:
    """Build the risk state from broker reads. Anything the broker cannot give is refused, not guessed.

    `overrides` is an optional file kept outside the repo (open risk, start-of-day equity,
    settled cash, account type). It cannot lower the equity the broker reported.
    """
    summary = client.get_account_summary()
    equity = summary.get("net_liquidation")
    if equity is None or Decimal(str(equity)) <= 0:
        raise SendRefused("broker equity is unavailable; refusing to send")
    state = {
        "equity": str(equity),
        "leverage": str(summary.get("leverage") if summary.get("leverage") is not None else "0"),
        "buying_power": None if summary.get("buying_power") is None else str(summary["buying_power"]),
        "settled_cash": str(summary.get("available_funds") if summary.get("available_funds") is not None else "0"),
        "day_trades_remaining": summary.get("day_trades_remaining"),
        "positions": client.get_positions(),
        "open_risk": "0",
        "account_type": "cash",
    }
    for key in ("open_risk", "start_of_day_equity", "settled_cash", "account_type", "margin_enabled"):
        if overrides and key in overrides:
            state[key] = overrides[key]
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket-file", required=True)
    parser.add_argument("--approve", default="")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--risk-overrides-file", default=None,
                        help="optional JSON kept outside the repo: open_risk, start_of_day_equity, settled_cash, account_type")
    args = parser.parse_args(argv)
    if not enabled():
        print(f"Not sent: {OFF}", file=sys.stderr)
        return 2
    try:
        with open(args.ticket_file, encoding="utf-8") as handle:
            ticket = json.load(handle)
        from ibkr_client import client_from_env
        overrides = None
        if args.risk_overrides_file:
            with open(args.risk_overrides_file, encoding="utf-8") as handle:
                overrides = json.load(handle)
            if not isinstance(overrides, dict):
                raise SendRefused("risk overrides file must be a JSON object")
        client = client_from_env()
        try:
            risk_state = risk_state_from_client(client, overrides)
        except SendRefused:
            raise
        except Exception as exc:
            raise SendRefused(f"risk state could not be read: {exc}") from exc
        outcome = send_once(
            client, ticket, args.approve, risk_state=risk_state,
            directory=args.state_dir or state_dir(),
            live_flag=args.live, confirmation=args.confirm,
        )
    except (OSError, ValueError, SendRefused, IbkrError, json.JSONDecodeError) as exc:
        print(f"Not sent: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
