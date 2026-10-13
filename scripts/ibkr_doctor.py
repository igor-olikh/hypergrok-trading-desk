#!/usr/bin/env python3
"""Desk check plus the IBKR gateway. Runs only when DESK_VENUE=ibkr. Read only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from ibkr_venue import OFF, enabled


def ibkr_gateway_checks(timeout: float):
    from desk_doctor import Check
    from ibkr_client import IbkrError, client_from_env, gateway_report

    try:
        client = client_from_env(timeout)
        auth = client.auth_status()
        summary = client.get_account_summary()
    except (IbkrError, OSError, ValueError, TypeError):
        return [Check("FAIL", "ibkr gateway", "unavailable")]
    return [Check(row["status"], row["name"], row["detail"]) for row in gateway_report(auth, summary)]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--desk-root", help="also validate a prepared trading-desk directory")
    parser.add_argument("--offline", action="store_true", help="skip the gateway check")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not enabled():
        print(OFF, file=sys.stderr)
        return 2
    from desk_doctor import Check, check_repository, check_workspace, render

    try:
        checks = check_repository(os.path.abspath(args.repo_root))
        if args.desk_root:
            checks.extend(check_workspace(os.path.abspath(args.desk_root)))
        if args.offline:
            checks.append(Check("SKIP", "ibkr gateway", "offline mode"))
        else:
            checks.extend(ibkr_gateway_checks(args.timeout))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Desk doctor unavailable: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"schema_version": 1, "mode": "read-only", "checks": [asdict(check) for check in checks]}, indent=2))
    else:
        print(render(checks))
    return 1 if any(check.status == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
