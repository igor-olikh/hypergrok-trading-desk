#!/usr/bin/env python3
"""Save IBKR daily bars. Runs only when DESK_VENUE=ibkr. No funding series."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from ibkr_venue import OFF, enabled

SCHEMA_VERSION = 1


def write_stock_bundle(directory: Path, symbol: str, bars: list[dict]) -> dict:
    """Daily bars. No funding. Every bar must say it is adjusted."""
    if not bars or any(row.get("adjusted") is not True for row in bars):
        raise ValueError("stock bars must be present and marked adjusted")
    directory.mkdir(parents=True, exist_ok=False)
    fieldnames = ["t", "open", "high", "low", "close", "volume"]
    with (directory / "candles.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(bars)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "venue": "ibkr",
        "symbol": symbol.upper(),
        "adjusted": True,
        "status": "complete",
        "limitations": "Daily bars are treated as split-adjusted. There is no funding series.",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fixture-bars", help=argparse.SUPPRESS)
    parser.add_argument("--with-funding", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not enabled():
        print(OFF, file=sys.stderr)
        return 2
    if args.with_funding:
        parser.error("US stocks have no funding history")
    output = Path(args.output)
    try:
        if args.fixture_bars:
            bars = json.loads(Path(args.fixture_bars).read_text(encoding="utf-8"))
        else:
            from ibkr_client import client_from_env
            bars = client_from_env(args.timeout).get_bars(args.symbol)
        manifest = write_stock_bundle(output, args.symbol, bars)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Dataset output unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2, sort_keys=True) if args.json else f"Dataset complete: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
