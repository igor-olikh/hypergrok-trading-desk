#!/usr/bin/env python3
"""IBKR venue tests. Fake accounts and prices only. No network."""

import importlib.util
import io
import json
import os
import ssl
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ibkr_market = load("ibkr_market")
ibkr_client = load("ibkr_client")
ibkr_risk = load("ibkr_risk")
ibkr_execute = load("ibkr_execute")
opening_bell = load("opening_bell")
research_dataset = load("research_dataset")
def load_hook():
    spec = importlib.util.spec_from_file_location(
        "exchange_write_gate", os.path.join(ROOT, "hooks", "exchange_write_gate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_hook()

QUOTE = {
    "symbol": "SPY",
    "conid": 100,
    "exchange": "ARCA",
    "currency": "USD",
    "last": "100.00",
    "bid": None,
    "ask": None,
    "prev_close": "99.00",
    "volume": "10",
    "halted": False,
    "market_data": "REALTIME",
    "depth": "unavailable",
    "network": "fixture",
}


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)


class SessionTest(unittest.TestCase):
    def test_regular_pre_post_and_closed(self):
        self.assertEqual(ibkr_market.session_state(at(2026, 10, 13, 2, 10)), "closed")
        self.assertEqual(ibkr_market.session_state(at(2026, 10, 13, 8, 0)), "pre")
        self.assertEqual(ibkr_market.session_state(at(2026, 10, 13, 10, 0)), "regular")
        self.assertEqual(ibkr_market.session_state(at(2026, 10, 13, 17, 0)), "post")
        self.assertEqual(ibkr_market.session_state(at(2026, 10, 13, 21, 0)), "closed")

    def test_weekend_holiday_and_half_day(self):
        self.assertEqual(ibkr_market.session_state(at(2026, 10, 17, 10, 0)), "closed")
        self.assertEqual(ibkr_market.session_state(at(2026, 1, 1, 10, 0)), "closed")
        self.assertEqual(ibkr_market.session_state(at(2026, 10, 12, 10, 0)), "regular")
        self.assertEqual(ibkr_market.session_state(at(2026, 11, 27, 10, 0)), "regular")
        self.assertEqual(ibkr_market.session_state(at(2026, 11, 27, 14, 0)), "post")


class SnapshotTest(unittest.TestCase):
    def test_missing_quote_is_unavailable_not_zero(self):
        quote = dict(QUOTE)
        quote["depth"] = 0
        quote["bid"] = "0"
        snapshot = ibkr_market.build_stock_snapshot(quote, now=at(2026, 10, 13, 10, 0))
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertIsNone(snapshot["prices"]["bid"])
        self.assertEqual(snapshot["book"]["depth"], "unavailable")
        self.assertEqual(snapshot["session"], "regular")
        self.assertFalse(snapshot["halted"])
        text = ibkr_market.render_stock(snapshot)
        self.assertIn("unavailable", text)
        self.assertIn("not halted", text)
        self.assertNotIn("funding", snapshot)
        self.assertIn("No funding", text)

    def test_opening_bell_fixture_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "quote.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(QUOTE, handle)
            with redirect_stdout(io.StringIO()) as output:
                code = opening_bell.main(["--symbol", "SPY", "--fixture-quote", path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["symbol"], "SPY")
        self.assertEqual(payload["book"]["depth"], "unavailable")


class ClientParseTest(unittest.TestCase):
    def test_localhost_skips_certificate_checks(self):
        local = ibkr_client.ssl_context_for("https://localhost:5000")
        remote = ibkr_client.ssl_context_for("https://api.example.com")
        self.assertEqual(local.verify_mode, ssl.CERT_NONE)
        self.assertNotEqual(remote.verify_mode, ssl.CERT_NONE)

    def test_search_snapshot_summary_and_bars(self):
        contract = ibkr_client.parse_search([
            {"conid": "100", "symbol": "SPY", "sections": [{"secType": "STK"}]},
        ], "spy")
        self.assertEqual(contract["conid"], 100)
        self.assertIsNone(ibkr_client.parse_snapshot([{"conid": 100}]))
        quote = ibkr_client.parse_snapshot([{"conid": 100, "31": "100.5", "84": "100.4", "86": "100.6", "6509": "Rp"}])
        self.assertEqual(quote["last"], "100.5")
        self.assertEqual(quote["market_data"], "REALTIME")
        summary = ibkr_client.normalize_summary(
            {"netliquidation": {"amount": 100000, "currency": "USD"}, "buyingpower": {"amount": 50000}},
            "paper",
        )
        self.assertEqual(summary["account_kind"], "paper")
        self.assertEqual(summary["net_liquidation"], Decimal("100000"))
        self.assertNotIn("account_id", summary)
        bars = ibkr_client.parse_bars({"data": [{"t": 1, "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}]}, True)
        self.assertTrue(bars[0]["adjusted"])

    def test_gateway_client_places_a_bracket_without_printing_the_account(self):
        captured = {}

        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def read(self):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def open_request(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = request.data
            if request.get_method() == "POST" and request.full_url.endswith("/orders"):
                return Response([{"order_id": "1", "order_status": "Submitted"}])
            return Response({"authenticated": True})

        class Opener:
            def open(self, request, timeout=None):
                return open_request(request, timeout)

        client = ibkr_client.ClientPortalClient(
            "https://localhost:5000", account_id="DU0000000", opener=Opener(),
        )
        result = client.place_order({
            "account_kind": "paper", "conid": 100, "side": "BUY", "quantity": 1,
            "order_type": "LMT", "price": "10.00", "tif": "DAY", "outside_rth": False,
            "order_ref": "IB-20261013-01",
            "stop": {"side": "SELL", "quantity": 1, "price": "9.00", "tif": "GTC"},
        })
        self.assertEqual(result["local_order_id"], "IB-20261013-01")
        self.assertNotIn("DU0000000", json.dumps(result))
        self.assertIn("/orders", captured["url"])
        body = json.loads(captured["body"])
        self.assertEqual(body["orders"][1]["parentId"], "IB-20261013-01")
        self.assertEqual(client.account_kind, "paper")

    def test_doctor_rows_hide_the_account_id(self):
        rows = ibkr_client.gateway_report(
            {"authenticated": True, "connected": True},
            {"account_kind": "paper", "market_data": "REALTIME"},
        )
        text = json.dumps(rows)
        self.assertNotIn("DU", text)
        self.assertEqual([row["status"] for row in rows], ["PASS", "PASS", "PASS"])
        live = ibkr_client.gateway_report(
            {"authenticated": False, "connected": False},
            {"account_kind": "live", "market_data": "REJECT"},
        )
        self.assertEqual([row["status"] for row in live], ["FAIL", "WARN", "FAIL"])


class RiskTest(unittest.TestCase):
    def test_share_sizing_and_ticks(self):
        sized = ibkr_risk.size_shares("10000", "100", "95", "0.05", "0.005")
        self.assertTrue(sized["ok"])
        self.assertEqual(sized["shares"], 19)
        self.assertLessEqual(sized["risk_pct_of_equity"], Decimal("0.01"))
        self.assertEqual(ibkr_risk.round_limit("100.016", "BUY"), Decimal("100.01"))
        self.assertEqual(ibkr_risk.round_limit("100.011", "SELL"), Decimal("100.02"))
        self.assertEqual(ibkr_risk.tick_size(Decimal("0.5")), Decimal("0.0001"))
        self.assertEqual(ibkr_risk.whole_shares("2.9"), 2)
        with self.assertRaises(ValueError):
            ibkr_risk.whole_shares("0.2")

    def test_ceilings_pdt_sector_and_cash(self):
        base = {
            "equity": "10000", "leverage": "1", "buying_power": "10000", "account_type": "cash",
            "settled_cash": "10000", "open_risk": "0", "positions": [], "margin_enabled": False,
        }
        order = {
            "side": "BUY", "halted": False, "session": "regular", "order_type": "LMT",
            "notional": "1000", "risk": "50", "stop": "95", "sector": "Tech", "is_day_trade": False,
        }
        self.assertEqual(ibkr_risk.review_new_risk(base, order)["decision"], "PASS")
        leveraged = dict(base, leverage="2.31", buying_power="0", account_type="margin")
        blocked = ibkr_risk.review_new_risk(leveraged, order)
        self.assertEqual(blocked["decision"], "REJECT")
        self.assertTrue(any("leverage" in reason for reason in blocked["reasons"]))
        self.assertTrue(any("buying power" in reason for reason in blocked["reasons"]))
        concentrated = dict(base, positions=[{"symbol": "BIG", "market_value": "3000", "sector": "Tech"}])
        self.assertTrue(any("20%" in reason for reason in ibkr_risk.review_new_risk(concentrated, order)["reasons"]))
        crowded = dict(base, positions=[{"symbol": "AAA", "market_value": "3000", "sector": "Tech"}])
        sector_order = dict(order, notional="600", sector="Tech")
        self.assertTrue(any("35%" in reason for reason in ibkr_risk.review_new_risk(crowded, sector_order)["reasons"]))
        pdt = dict(base, account_type="margin", equity="16000", day_trades_remaining=0)
        day_order = dict(order, is_day_trade=True)
        self.assertTrue(any("day trader" in reason for reason in ibkr_risk.review_new_risk(pdt, day_order)["reasons"]))
        pdt_ok = dict(pdt, day_trades_remaining=3)
        self.assertFalse(any("day trader" in reason for reason in ibkr_risk.review_new_risk(pdt_ok, day_order)["reasons"]))
        poor_cash = dict(base, settled_cash="100")
        self.assertTrue(any("unsettled" in reason for reason in ibkr_risk.review_new_risk(poor_cash, order)["reasons"]))
        lost = dict(base, start_of_day_equity="10000", equity="9600")
        self.assertTrue(any("daily loss" in reason for reason in ibkr_risk.review_new_risk(lost, order)["reasons"]))
        halted = dict(order, halted=True)
        self.assertTrue(any("halt" in reason for reason in ibkr_risk.review_new_risk(base, halted)["reasons"]))


def ticket(**extra):
    body = {
        "id": "IB-20261013-01", "mode": "paper", "account_kind": "paper", "symbol": "SPY",
        "conid": 100, "side": "BUY", "qty": 1, "order_type": "LMT", "limit": "100.00",
        "tif": "DAY", "stop": "95.00", "stop_qty": 1, "session": "regular", "halted": False,
        "last": "100.00", "risk_usd": "5", "risk_pct": "0.5%",
    }
    body.update(extra)
    return body


class ExecuteTest(unittest.TestCase):
    def test_unapproved_and_rejected_never_send(self):
        client = ibkr_client.MockIbkrClient()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ibkr_execute.SendRefused):
                ibkr_execute.send_once(client, ticket(), "approve IB-20261013-02", directory=directory)
            self.assertEqual(client.sent, [])
            state = {"equity": "10000", "leverage": "2", "buying_power": "0", "positions": [], "open_risk": "0"}
            with self.assertRaises(ibkr_execute.SendRefused):
                ibkr_execute.send_once(client, ticket(), "approve IB-20261013-01", directory=directory, risk_state=state)
            self.assertEqual(client.sent, [])

    def test_one_send_then_the_same_ticket_is_refused(self):
        client = ibkr_client.MockIbkrClient()
        with tempfile.TemporaryDirectory() as directory:
            first = ibkr_execute.send_once(client, ticket(), "approve IB-20261013-01", directory=directory)
            self.assertEqual(first["state"], "sent")
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0]["stop"]["price"], "95.00")
            with self.assertRaises(ibkr_execute.SendRefused):
                ibkr_execute.send_once(client, ticket(), "approve IB-20261013-01", directory=directory)
            self.assertEqual(len(client.sent), 1)

    def test_unknown_result_is_not_sent_again(self):
        class Down(ibkr_client.MockIbkrClient):
            def place_order(self, order):
                raise ibkr_client.IbkrError("timeout")

        client = Down()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ibkr_client.IbkrError):
                ibkr_execute.send_once(client, ticket(), "approve IB-20261013-01", directory=directory)
            with self.assertRaises(ibkr_execute.SendRefused):
                ibkr_execute.send_once(client, ticket(), "approve IB-20261013-01", directory=directory)

    def test_live_needs_every_flag_and_a_live_account(self):
        client = ibkr_client.MockIbkrClient(account_kind="live")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ibkr_execute.SendRefused):
                ibkr_execute.send_once(
                    client, ticket(mode="live", account_kind="live"), "approve IB-20261013-01",
                    directory=directory, live_flag=True, env={"IBKR_LIVE": "1"}, confirmation="no",
                )
            self.assertEqual(client.sent, [])
            sent = ibkr_execute.send_once(
                client, ticket(mode="live", account_kind="live"), "approve IB-20261013-01",
                directory=directory, live_flag=True, env={"IBKR_LIVE": "1"}, confirmation="confirm live",
            )
            self.assertEqual(sent["state"], "sent")

    def test_price_move_and_halt_block_before_send(self):
        client = ibkr_client.MockIbkrClient()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ibkr_execute.SendRefused):
                ibkr_execute.send_once(client, ticket(last="110"), "approve IB-20261013-01", directory=directory)
            with self.assertRaises(ibkr_execute.SendRefused):
                ibkr_execute.send_once(client, ticket(halted=True), "approve IB-20261013-01", directory=directory)
            self.assertEqual(client.sent, [])

    def test_cli_refuses_without_touching_the_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ticket.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(ticket(), handle)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = ibkr_execute.main(["--ticket-file", path, "--approve", "no", "--state-dir", directory])
        self.assertEqual(code, 2)


class DatasetTest(unittest.TestCase):
    def test_stock_bars_are_adjusted_and_have_no_funding(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "bars")
            fixture = os.path.join(directory, "fixture.json")
            with open(fixture, "w", encoding="utf-8") as handle:
                json.dump([{"t": 1, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 3, "adjusted": True}], handle)
            with redirect_stdout(io.StringIO()) as stdout:
                code = research_dataset.main([
                    "--symbol", "SPY", "--output", output, "--fixture-bars", fixture, "--json",
                ])
            self.assertEqual(code, 0)
            manifest = json.loads(stdout.getvalue())
            self.assertTrue(manifest["adjusted"])
            self.assertNotIn("funding", manifest)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                research_dataset.main(["--symbol", "SPY", "--with-funding", "--output", output + "2"])


class GateTest(unittest.TestCase):
    def test_ibkr_order_writes_ask_and_reads_do_not(self):
        ask = gate.decide({"tool_name": "Bash", "tool_input": {"command": "client.place_order(order)"}, "cwd": ROOT})
        self.assertEqual(ask["hookSpecificOutput"]["permissionDecision"], "ask")
        post = gate.decide({
            "tool_name": "Bash",
            "tool_input": {"command": "curl -X POST https://localhost:5000/v1/api/iserver/account/DU0000000/orders"},
            "cwd": ROOT,
        })
        self.assertIsNotNone(post)
        read = gate.decide({
            "tool_name": "Bash",
            "tool_input": {"command": "python3 scripts/opening_bell.py --symbol SPY --json"},
            "cwd": ROOT,
        })
        self.assertIsNone(read)


if __name__ == "__main__":
    unittest.main()
