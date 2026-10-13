#!/usr/bin/env python3
"""Ask before an IBKR order write. Stdlib only.

This file is separate from the Hyperliquid gate. It has an opinion only when
DESK_VENUE=ibkr. Otherwise it stays silent. It never approves a send.
"""

import json
import os
import re
import sys

IBKR_WRITE_RE = re.compile(
    r"\bplace_order\s*\(|\bcancel_order\s*\(|/iserver/account/[^\s\"']+/orders\b|/iserver/reply/",
    re.IGNORECASE,
)
SCRIPT_RE = re.compile(r"[\w./~-]+\.(?:py|ts|mts|cts|js|mjs|cjs)\b")
MAX_SCRIPT_BYTES = 2_000_000


def enabled(env=None) -> bool:
    source = os.environ if env is None else env
    return str(source.get("DESK_VENUE", "")).strip().lower() == "ibkr"


def find_write(text):
    match = IBKR_WRITE_RE.search(text)
    if match:
        return match.group(0).strip()
    return None


def named_scripts(command, cwd):
    for token in SCRIPT_RE.findall(command):
        path = os.path.expanduser(token)
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= MAX_SCRIPT_BYTES:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    yield token, handle.read()
        except OSError:
            continue


def decide(event, env=None):
    if not enabled(env):
        return None
    if event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command:
        return None
    found = find_write(command)
    where = "in the command"
    if not found:
        cwd = event.get("cwd") or os.getcwd()
        for token, text in named_scripts(command, cwd):
            found = find_write(text)
            if found:
                where = f"in {token}"
                break
    if not found:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"HyperGrok: this looks like an IBKR order write ({found} {where}). "
                "Approve only if it is the single send for a ticket with a Risk PASS "
                "and your approval by that ticket id."
            ),
        }
    }


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 0
    if not isinstance(event, dict):
        return 0
    decision = decide(event)
    if decision:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
