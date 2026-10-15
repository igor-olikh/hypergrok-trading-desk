"""Shared read-only command diagnostics and explicit network selection."""

import argparse
import json
import math
import re
from urllib.parse import urlsplit

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"
SCHEMA_VERSION = 1


def emit_error(code, message):
    print(json.dumps({"schema_version": SCHEMA_VERSION, "mode": "read-only",
                      "error": {"code": code, "message": message}}, sort_keys=True))


class ReadOnlyParser(argparse.ArgumentParser):
    def __init__(self, *args, machine_readable=False, **kwargs):
        self.machine_readable = machine_readable
        super().__init__(*args, **kwargs)

    def error(self, message):
        if self.machine_readable:
            emit_error("invalid_arguments", message)
            self.exit(2)
        super().error(message)


def add_network_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--base-url", help="explicit public API origin; custom origins are labelled custom")
    group.add_argument("--testnet", action="store_true", help="use the Hyperliquid testnet public API")
    parser.add_argument("--timeout", type=float, default=15.0, help="finite positive request timeout in seconds")
    parser.add_argument("--schema", action="store_true", help="describe the JSON output and exit without network access")


def network_settings(args, parser):
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    base = (args.base_url or (TESTNET if args.testnet else MAINNET)).rstrip("/")
    try:
        url = urlsplit(base)
        valid = (url.scheme in ("https", "http") and bool(url.hostname)
                 and url.username is None and url.password is None
                 and not url.path and not url.query and not url.fragment)
        url.port  # Validate an explicitly supplied port.
    except ValueError:
        valid = False
    if not valid:
        parser.error("base-url must be an HTTP(S) origin without credentials, path, query or fragment")
    network = "mainnet" if base == MAINNET else "testnet" if base == TESTNET else "custom"
    return base, network


def market_selection(parser, coin, dex):
    """Validate a perp coin and optional HIP-3 DEX; a prefixed coin selects its DEX."""
    coin = coin.strip()
    if not coin or not re.fullmatch(r"[A-Za-z0-9@_-]+(?::[A-Za-z0-9@_-]+)?", coin):
        parser.error("coin contains unsupported characters")
    prefix = coin.split(":", 1)[0] if ":" in coin else None
    selected = dex or prefix
    if selected and not re.fullmatch(r"[a-z][a-z0-9_-]*", selected):
        parser.error("dex must be a lowercase DEX name")
    if dex and prefix and dex != prefix:
        parser.error("coin prefix and dex must match")
    return coin, selected


def describe(command, success):
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "mode": "read-only",
        "network_access": "POST /info only; none for --schema or offline fixtures",
        "exit_codes": {"0": "success (inspect completeness, WARN and SKIP fields)",
                       "1": "unavailable data or failed checks", "2": "invalid arguments"},
        "success": success,
        "error": {"schema_version": 1, "mode": "read-only",
                  "error": {"code": "invalid_arguments | source_unavailable", "message": "string"}},
    }
