#!/usr/bin/env bash
# IBKR checks live here so the original scripts/check.sh stays untouched.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 scripts/test_ibkr.py

if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "refusing: .env is tracked"
  exit 1
fi
if [[ -e .env || -e .env.local ]]; then
  echo "refusing: a secret env file is inside the repo. Keep it outside."
  exit 1
fi
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks is not installed"
  exit 1
fi
gitleaks detect --source . --no-banner --redact --config .gitleaks.toml
