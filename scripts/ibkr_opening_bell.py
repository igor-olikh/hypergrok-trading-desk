#!/usr/bin/env python3
"""US stock snapshot. Runs only when DESK_VENUE=ibkr. Reads. Does not send."""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import DecimalException

from cli_contract import emit_error
from ibkr_client import IbkrError
from ibkr_market import build_stock_snapshot, render_stock
from ibkr_venue import OFF, enabled

SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,7}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY", help="US stock or ETF ticker")
    parser.add_argument("--fixture-quote", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not enabled():
        message = OFF
        if args.json:
            emit_error("invalid_arguments", message)
        else:
            print(message, file=sys.stderr)
        return 2
    symbol = (args.symbol or "").strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        parser.error("symbol must be a US ticker")
    try:
        if args.fixture_quote:
            with open(args.fixture_quote, encoding="utf-8") as handle:
                quote = json.load(handle)
            quote["symbol"] = symbol
            quote["network"] = "fixture"
        else:
            from ibkr_client import client_from_env
            quote = client_from_env(args.timeout).get_quote(symbol)
        snapshot = build_stock_snapshot(quote)
    except (IbkrError, OSError, ValueError, KeyError, TypeError, DecimalException, OverflowError, json.JSONDecodeError) as exc:
        if args.json:
            emit_error("source_unavailable", str(exc))
        else:
            print(f"Opening Bell unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(snapshot, indent=2, sort_keys=True) if args.json else render_stock(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
