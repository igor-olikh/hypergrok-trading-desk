"""Interactive Brokers Client Portal client and a fixture client. Stdlib only.

The read-only Grok connector is not a write path. Orders go through `place_order` on this
client, and only `ibkr_execute.send_once` is allowed to call it after an approval.
Account ids are kept on the client and are not part of the dicts callers print.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

# Client Portal snapshot field ids. A missing field stays unavailable.
SNAPSHOT_FIELDS = "31,70,71,82,83,84,85,86,87,88,6509,7295,7296"
FIELD_MAP = {
    "31": "last",
    "70": "high",
    "71": "low",
    "82": "change",
    "83": "change_pct",
    "84": "bid",
    "85": "ask_size",
    "86": "ask",
    "87": "volume",
    "88": "bid_size",
    "7295": "prev_close",
    "7296": "open",
}
MARKET_DATA_CODE = {
    "R": "REALTIME",
    "D": "DELAYED",
    "Z": "FROZEN",
    "Y": "FROZEN_DELAYED",
    "N": "REJECT",
    "P": "DELAYED",
}


class IbkrError(Exception):
    pass


ACCOUNT_ID_RE = re.compile(r"\b(?:DU|U|DF|F)\d{6,9}\b")
SECRET_PAIR_RE = re.compile(r"(?i)(token|secret|password|cookie|session|authorization)([\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+")
ERROR_TEXT_LIMIT = 200


def broker_error_text(raw) -> str:
    """The broker's own error text, shortened, with account ids and secret-looking pairs removed."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("error", "message", "msg", "detail"):
            if payload.get(key):
                text = str(payload[key])
                break
    text = " ".join(text.split())
    text = SECRET_PAIR_RE.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
    text = ACCOUNT_ID_RE.sub("[account]", text)
    return text[:ERROR_TEXT_LIMIT]


def account_kind(account_id: str) -> str:
    if account_id.startswith("DU"):
        return "paper"
    if account_id.startswith("U"):
        return "live"
    return "unknown"


def ssl_context_for(base_url: str):
    host = urllib.parse.urlsplit(base_url).hostname or ""
    if host in ("localhost", "127.0.0.1"):
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def _amount(summary: dict, *keys):
    for key in keys:
        raw = summary.get(key)
        if raw is None:
            raw = summary.get(key.lower())
        if isinstance(raw, dict) and "amount" in raw:
            raw = raw["amount"]
        if raw in (None, ""):
            continue
        try:
            number = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if number.is_finite():
            return number
    return None


def normalize_summary(summary: dict, kind: str) -> dict:
    if not isinstance(summary, dict):
        raise IbkrError("account summary is not an object")
    leverage = _amount(summary, "leverage", "Leverage")
    remaining = summary.get("day_trades_remaining", summary.get("daytradesremaining"))
    if isinstance(remaining, dict):
        remaining = remaining.get("amount")
    try:
        remaining_out = None if remaining in (None, "") else int(str(remaining).split(".")[0])
    except ValueError:
        remaining_out = None
    return {
        "account_kind": kind,
        "net_liquidation": _amount(summary, "netliquidation", "NetLiquidation"),
        "buying_power": _amount(summary, "buyingpower", "BuyingPower"),
        "available_funds": _amount(summary, "availablefunds", "AvailableFunds"),
        "leverage": leverage,
        "day_trades_remaining": remaining_out,
        "market_data": summary.get("market_data") or "unavailable",
    }


def parse_search(payload, symbol: str) -> dict:
    rows = payload if isinstance(payload, list) else []
    want = symbol.upper()
    chosen = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        sections = row.get("sections") or []
        types = {str(item.get("secType", "")).upper() for item in sections if isinstance(item, dict)}
        if types and "STK" not in types and str(row.get("secType", "")).upper() != "STK":
            continue
        name = str(row.get("symbol") or row.get("ticker") or "").upper()
        if name == want:
            chosen = row
            break
        if chosen is None:
            chosen = row
    if not chosen or chosen.get("conid") in (None, ""):
        raise IbkrError(f"{want} did not resolve to a stock conid")
    return {
        "symbol": want,
        "conid": int(str(chosen["conid"]).split("@")[0]),
        "exchange": chosen.get("exchange") or "SMART",
        "currency": chosen.get("currency") or "USD",
    }


def parse_snapshot(payload) -> dict | None:
    rows = payload if isinstance(payload, list) else []
    if not rows or not isinstance(rows[0], dict):
        raise IbkrError("snapshot is empty")
    row = rows[0]
    interesting = [key for key in row if key not in ("conid", "conidEx")]
    if not interesting:
        return None
    quote = {}
    for field, name in FIELD_MAP.items():
        if field in row and row[field] not in (None, ""):
            quote[name] = row[field]
    code = str(row.get("6509") or "")
    if code:
        quote["market_data"] = MARKET_DATA_CODE.get(code[:1], "unavailable")
    if row.get("halted") is True or str(row.get("halted")).lower() == "true":
        quote["halted"] = True
    elif "halted" in row:
        quote["halted"] = False
    return quote


def parse_bars(payload, adjusted: bool) -> list[dict]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise IbkrError("history did not return bars")
    bars = []
    for row in rows:
        if not isinstance(row, dict):
            raise IbkrError("bar is not an object")
        bars.append({
            "t": row.get("t"),
            "open": row.get("o"),
            "high": row.get("h"),
            "low": row.get("l"),
            "close": row.get("c"),
            "volume": row.get("v"),
            "adjusted": adjusted,
        })
    return bars


def gateway_report(auth: dict, summary: dict, portal=None) -> list[dict]:
    """Doctor rows. Details name the account kind, never the account id.

    `portal` is None when the portal login was not checked. Otherwise it is True or False for
    GET /sso/validate. A good portal login with a closed trading session is a WARN, not a FAIL:
    the session is opened by POST /iserver/auth/ssodh/init on the first quote.
    """
    rows = []
    authenticated = isinstance(auth, dict) and auth.get("authenticated") is True and auth.get("connected") is True
    if portal is not None:
        rows.append({
            "status": "PASS" if portal else "FAIL",
            "name": "ibkr portal login",
            "detail": "portal login is valid" if portal else "portal login is not valid; log in again in the browser",
        })
    if authenticated:
        rows.append({"status": "PASS", "name": "ibkr auth", "detail": "gateway session is authenticated"})
    elif portal:
        rows.append({
            "status": "WARN",
            "name": "ibkr auth",
            "detail": "portal login is fine but the trading session is not open yet; a quote read opens it",
        })
    else:
        rows.append({"status": "FAIL", "name": "ibkr auth", "detail": "gateway session is not authenticated"})
    kind = (summary or {}).get("account_kind") or "unknown"
    if kind == "paper":
        rows.append({"status": "PASS", "name": "ibkr account", "detail": "paper account"})
    elif kind == "live":
        rows.append({"status": "WARN", "name": "ibkr account", "detail": "live account; sends stay off without the live flags"})
    else:
        rows.append({"status": "FAIL", "name": "ibkr account", "detail": "account kind is unknown"})
    market_data = (summary or {}).get("market_data") or "unavailable"
    if market_data == "REJECT":
        rows.append({"status": "FAIL", "name": "ibkr market data", "detail": "market data permission rejected"})
    elif market_data == "unavailable":
        rows.append({"status": "WARN", "name": "ibkr market data", "detail": "market data permission unavailable"})
    else:
        rows.append({"status": "PASS", "name": "ibkr market data", "detail": market_data})
    return rows


class MockIbkrClient:
    """In-memory broker for tests. `place_order` only records what it was given."""

    def __init__(self, quote=None, summary=None, positions=None, orders=None, bars=None,
                 auth=None, account_kind="paper"):
        self.quote = quote or {}
        self.summary = summary or {"account_kind": account_kind, "net_liquidation": Decimal("100000")}
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.bars = list(bars or [])
        self.auth = auth or {"authenticated": True, "connected": True}
        self.account_kind = account_kind
        self.sent = []

    def auth_status(self):
        return dict(self.auth)

    def portal_status(self):
        return bool(self.auth.get("portal", True))

    def resolve_contract(self, symbol: str):
        return {"symbol": symbol.upper(), "conid": int(self.quote.get("conid") or 1),
                "exchange": self.quote.get("exchange") or "SMART", "currency": "USD"}

    def get_quote(self, symbol: str):
        quote = dict(self.quote)
        quote["symbol"] = symbol.upper()
        quote.setdefault("depth", "unavailable")
        return quote

    def get_depth(self, symbol: str):
        return {"symbol": symbol.upper(), "depth": self.quote.get("depth", "unavailable")}

    def get_bars(self, symbol: str, period: str = "1m", bar: str = "1d", adjusted: bool = True):
        return [{"symbol": symbol.upper(), "adjusted": adjusted, **row} for row in self.bars]

    def get_account_summary(self):
        return dict(self.summary)

    def get_positions(self):
        return list(self.positions)

    def get_orders(self):
        return list(self.orders)

    def place_order(self, order: dict):
        if order.get("account_kind") not in ("paper", "live"):
            raise IbkrError("order is missing an account kind")
        self.sent.append(order)
        self.orders.append({"order_ref": order["order_ref"], "status": "Submitted"})
        return {"order_id": "1", "order_status": "Submitted", "local_order_id": order["order_ref"]}

    def cancel_order(self, order_id: str):
        self.sent.append({"cancel": order_id})
        return {"order_id": order_id, "order_status": "Cancelled"}


class ClientPortalClient:
    def __init__(self, base_url: str, account_id: str = "", timeout: float = 15, opener=None,
                 sleep=time.sleep, snapshot_attempts: int = 4, retry_delay: float = 0.5):
        self.sleep = sleep
        self.snapshot_attempts = max(1, int(snapshot_attempts))
        self.retry_delay = retry_delay
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/v1/api"):
            self.base_url += "/v1/api"
        self.account_id = account_id
        self.timeout = timeout
        self.account_kind = account_kind(account_id) if account_id else "unknown"
        if opener is None:
            context = ssl_context_for(self.base_url)
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=context),
                urllib.request.HTTPCookieProcessor(),
            )
        self.opener = opener

    def _request(self, method: str, path: str, body=None, query=None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "User-Agent": "hypergrok-ibkr/1.0"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = broker_error_text(exc.read())
            except Exception:
                detail = ""
            message = f"gateway returned HTTP {exc.code}"
            if detail:
                message += f": {detail}"
            raise IbkrError(message) from exc
        except OSError as exc:
            raise IbkrError("gateway unreachable") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IbkrError("gateway returned non-JSON") from exc

    def auth_status(self):
        """Trading session state: POST /iserver/auth/status."""
        payload = self._request("POST", "/iserver/auth/status")
        if not isinstance(payload, dict):
            raise IbkrError("auth status is not an object")
        return payload

    def portal_status(self) -> bool:
        """Portal login only (GET /sso/validate). It says nothing about the trading session."""
        payload = self._request("GET", "/sso/validate")
        if not isinstance(payload, dict):
            return False
        result = payload.get("RESULT", payload.get("result"))
        return result is True or str(result).lower() == "true"

    def open_trading_session(self):
        """Open the brokerage session, then list accounts. The broker wants both before market data."""
        self._request("POST", "/iserver/auth/ssodh/init", {"publish": True, "compete": True})
        self._request("GET", "/iserver/accounts")

    def _ensure_account(self):
        if self.account_id:
            return self.account_id
        payload = self._request("GET", "/portfolio/accounts")
        rows = payload if isinstance(payload, list) else []
        ids = []
        for row in rows:
            if isinstance(row, dict):
                found = row.get("accountId") or row.get("id")
                if found:
                    ids.append(str(found))
        if len(ids) != 1:
            raise IbkrError("set IBKR_ACCOUNT_ID; the gateway did not return one account")
        self.account_id = ids[0]
        self.account_kind = account_kind(self.account_id)
        return self.account_id

    def resolve_contract(self, symbol: str):
        payload = self._request("POST", "/iserver/secdef/search", {"symbol": symbol, "name": False, "secType": "STK"})
        return parse_search(payload, symbol)

    def get_quote(self, symbol: str):
        self.open_trading_session()
        contract = self.resolve_contract(symbol)
        query = {"conids": contract["conid"], "fields": SNAPSHOT_FIELDS}
        parsed = None
        last_error = ""
        for attempt in range(self.snapshot_attempts):
            if attempt:
                self.sleep(self.retry_delay)
            try:
                payload = self._request("GET", "/iserver/marketdata/snapshot", query=query)
                parsed = None if payload in (None, [], "") else parse_snapshot(payload)
            except IbkrError as exc:
                last_error = str(exc)
                parsed = None
                continue
            last_error = ""
            if parsed and parsed.get("last") not in (None, ""):
                break
        if not parsed or parsed.get("last") in (None, ""):
            reason = last_error or "the broker returned no last price"
            raise IbkrError(f"{symbol} quote is unavailable: {reason}")
        parsed.update(contract)
        parsed["depth"] = "unavailable"
        parsed["network"] = self.account_kind
        return parsed

    def get_depth(self, symbol: str):
        return {"symbol": symbol.upper(), "depth": "unavailable"}

    def get_bars(self, symbol: str, period: str = "1m", bar: str = "1d", adjusted: bool = True):
        contract = self.resolve_contract(symbol)
        payload = self._request("GET", "/iserver/marketdata/history", query={
            "conid": contract["conid"], "period": period, "bar": bar, "outsideRth": "false",
        })
        return parse_bars(payload, adjusted)

    def get_account_summary(self):
        account = self._ensure_account()
        payload = self._request("GET", f"/portfolio/{account}/summary")
        summary = normalize_summary(payload, self.account_kind)
        summary["market_data"] = payload.get("market_data", summary["market_data"]) if isinstance(payload, dict) else summary["market_data"]
        return summary

    def get_positions(self):
        account = self._ensure_account()
        payload = self._request("GET", f"/portfolio/{account}/positions/0")
        if not isinstance(payload, list):
            raise IbkrError("positions response is not a list")
        positions = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            positions.append({
                "symbol": row.get("contractDesc") or row.get("ticker") or "",
                "conid": row.get("conid"),
                "quantity": row.get("position"),
                "market_price": row.get("mktPrice"),
                "market_value": row.get("mktValue"),
                "sector": row.get("sector") or "",
                "asset_class": row.get("assetClass") or "",
            })
        return positions

    def get_orders(self):
        payload = self._request("GET", "/iserver/account/orders")
        if isinstance(payload, dict):
            payload = payload.get("orders")
        if not isinstance(payload, list):
            raise IbkrError("orders response is not a list")
        return payload

    def place_order(self, order: dict):
        account = self._ensure_account()
        kind = account_kind(account)
        if kind != order.get("account_kind"):
            raise IbkrError("ticket account kind does not match the gateway account")
        parent = {
            "conid": int(order["conid"]),
            "orderType": order["orderType"] if "orderType" in order else order["order_type"],
            "side": order["side"],
            "quantity": order["quantity"],
            "price": float(order["price"]),
            "tif": order["tif"],
            "outsideRTH": bool(order.get("outside_rth")),
            "cOID": order["order_ref"],
        }
        children = []
        stop = order.get("stop")
        if stop:
            children.append({
                "conid": int(order["conid"]),
                "orderType": "STP",
                "side": stop["side"],
                "quantity": stop["quantity"],
                "price": float(stop["price"]),
                "tif": stop.get("tif", "GTC"),
                "parentId": order["order_ref"],
                "cOID": order["order_ref"] + "-STOP",
            })
        payload = self._request("POST", f"/iserver/account/{account}/orders", {"orders": [parent, *children]})
        if isinstance(payload, list) and payload and isinstance(payload[0], dict) and payload[0].get("message"):
            raise IbkrError("broker asked a question; the order was not confirmed")
        return {"order_status": "Submitted", "local_order_id": order["order_ref"]}

    def cancel_order(self, order_id: str):
        account = self._ensure_account()
        return self._request("DELETE", f"/iserver/account/{account}/order/{order_id}")


def client_from_env(timeout: float = 15) -> ClientPortalClient:
    return ClientPortalClient(
        base_url=os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000"),
        account_id=os.environ.get("IBKR_ACCOUNT_ID", ""),
        timeout=timeout,
    )
