# IBKR Port Task Spec — HyperGrok desk → Interactive Brokers (US stocks)

## Кратко (для Игоря)
Задача для агента-программиста: перенести торговый деск HyperGrok с Hyperliquid (крипто-фьючерсы) на Interactive Brokers (акции США).
Архитектура та же: 7 ботов-ролей, жёсткие риск-правила, **каждая сделка — только после твоего явного подтверждения**.
По умолчанию — только paper-аккаунт; live — только через явный флаг. Автоторговли нет.
Репозиторий публичный: **никаких личных данных** (счета, ключи, балансы, позиции, сделки) — только заглушки и моки.
Работа разбита на 7 фаз с проверяемым результатом и чек-листом «готово».

---

## 0. HARD RULE — NO PERSONAL DATA IN THIS REPO (public fork)

This fork is **PUBLIC**. Violating this rule is a blocking failure for every phase.

Never commit, in code, docs, tests, fixtures, comments, commit messages, logs or screenshots:
- IBKR account numbers (e.g. `U1234567`/`DU…`), usernames, user/consumer IDs
- API keys, tokens, session cookies, passwords, private keys, OAuth secrets
- balances, equity, buying power, positions, orders, fills, trades, P&L, amounts tied to a real account
- emails, real names, phone numbers, addresses
- logs, screenshots, exports or recordings containing real data

Required practices:
- Config only via environment variables / Grok Bot secret store. Ship `.env.example` with placeholders only (`IBKR_ACCOUNT_ID=DU0000000`, `IBKR_GATEWAY_URL=https://localhost:5000`).
- `.gitignore` must cover: `.env`, `.env.*` (except `.env.example`), `logs/`, `data/`, `reports/`, `*.log`, `trading-desk/`, `*.sqlite`, screenshots dirs.
- Tests and docs use **fake/mock data only** (obviously fake account `DU0000000`, round fake prices).
- Run a secret scan (e.g. `gitleaks detect` / `gitleaks protect --staged`) before every commit/push; add it to `scripts/check.sh` and `.github/workflows/ci.yml`.
- Runtime state (limits file, journal, tickets) lives outside the repo (e.g. `/workspace/trading-desk/`), never committed.

## 1. Goal

Keep the HyperGrok architecture — seven role bots (`agents/desk-lead.md`, `market-analyst.md`, `research-analyst.md`, `strategist.md`, `risk-manager.md`, `execution-trader.md`, `trade-reviewer.md`), hard risk rules, ticket flow
`idea -> evidence -> risk sign-off -> your approval -> one send -> reconciliation -> review` — but trade **US stocks/ETFs via Interactive Brokers**.

Non-negotiables:
- Every order requires explicit human approval by ticket id (`approve IB-YYYYMMDD-NN`). No standing approvals for anything that opens/increases exposure.
- Default = IBKR **paper** account. Live only with explicit opt-in flag (`IBKR_LIVE=1` + `--live`) AND a separate user confirmation.
- Never auto-trade. Only the Execution Trader holds order-capable credentials.
- Do not change behaviour silently: keep existing hooks/approval gates (`hooks/exchange_write_gate.py`) and extend them to IBKR writes.

## 2. Data layer: Hyperliquid → IBKR

Choose one backend, behind an interface `scripts/ibkr_client.py` (`get_quote`, `get_depth`, `get_bars`, `get_account_summary`, `get_positions`, `place_order`, `cancel_order`, `get_orders`), with a fixture/mock implementation for tests:
- **IBKR Client Portal Web API** (REST via local gateway; `/iserver/marketdata/snapshot`, `/iserver/marketdata/history`, `/portfolio/{acct}/summary`, `/portfolio/{acct}/positions/0`, `/iserver/account/{acct}/orders`), or
- **TWS API / `ib_insync` (ib_async)** via TWS/IB Gateway.
- Note: Grok Bot also has a **read-only IBKR MCP connector** (snapshots, price history, positions, balances). Bots may use it for reads; it must never be the write path.

| Current file / call | Hyperliquid usage | IBKR replacement |
| --- | --- | --- |
| `scripts/opening_bell.py` `post_info`, `DEFAULT_BASE_URL` | `/info` POST to api.hyperliquid.xyz | `ibkr_client` (gateway URL from env) |
| `opening_bell.py` `fetch_snapshot`, `metadata_rows`, `perp_context`, `resolve_coin` | `metaAndAssetCtxs` (mark/oracle/funding/OI/volume) | contract lookup by symbol (conid) + market data snapshot (last, bid/ask, volume, prev close, change); drop funding/OI/oracle |
| `opening_bell.py` `request_book`, `fetch_books`, `read_levels`, `depth_within`, `reach_bps`, `page_views` (`nSigFigs` paging) | `l2Book` | top-of-book always; L2 depth only if market-depth subscription exists, else report `depth: unavailable` (keep the "unavailable ≠ zero" contract) |
| `opening_bell.py` `build_snapshot`, `render`, `--schema` | JSON contract v1 | new contract `schema_version: 2` with session status (pre/regular/post/closed), halt flag; `--coin` → `--symbol` |
| `scripts/research_dataset.py` (`--with-funding`) | candles + funding history | IBKR historical bars (respect pacing limits); remove funding; add corporate-action adjusted flag |
| `scripts/desk_doctor.py` `check_public_api` | pings HL `/info` | checks gateway reachability, auth status, paper vs live account type, market-data permissions |
| `skills/desk-risk-limits` equity basis | `clearinghouseState.accountValue` | IBKR `NetLiquidation` (account summary), read live, timestamped |
| `skills/hyperliquid-*` (setup, market-data, account, orders, positions, websocket, advanced, api-reference) | HL docs | new `skills/ibkr-*` equivalents (setup, market-data, account, orders, positions, streaming, api-reference) |
| `scripts/test_order_rounding.py` | szDecimals/tick rounding | share quantities (integer; fractional only if enabled) + tick size $0.01 (≥$1) / $0.0001 (<$1) |
| `hooks/exchange_write_gate.py` | detects HL exchange writes | also detect IBKR order endpoints / `placeOrder` calls |
| `template/grok-bot.json`, `plugin.json`, `.claude-plugin/`, `.cursor-plugin/`, `.grok-plugin/`, `package.json` | HL skill lists | updated skill/agent inventories (`scripts/check_manifests.py` must pass) |

## 3. Market model changes

- **Sessions**: NYSE/Nasdaq regular 09:30–16:00 ET, pre-market 04:00–09:30, after-hours 16:00–20:00; holidays/half days. Orders outside RTH need explicit `outsideRth` and limit orders only. Opening Bell must show session state.
- **No funding, no open interest, no oracle/mark** — remove from snapshot, backtests and briefs.
- **No leverage by default**: assume cash account; margin only if user explicitly sets it in limits file.
- **Units**: shares (integer), min 1 share; notional = qty × price.
- **PDT rule**: margin accounts < $25k limited to 3 day trades / 5 business days — Risk Manager must count and block.
- **Cash settlement (T+1)**: avoid good-faith violations in cash accounts (no buying power from unsettled proceeds).
- **Corporate actions**: dividends, splits, earnings dates — Research Analyst flags; backtests use adjusted data; stops reviewed after splits.
- **Symbology**: ticker + exchange + currency → IBKR `conid`; resolve and store conid on ticket; handle class shares (BRK.B).
- **Halts / LULD / gaps**: stop orders can gap through; size on gap-stressed stop; detect halted status and refuse sends.
- Short selling: disabled by default (borrow availability, fees) unless enabled in limits.

## 4. Risk rules (rewrite `skills/desk-risk-limits/SKILL.md`)

Desk ceilings (user file may only be stricter):
| Ceiling | Value |
| --- | --- |
| max risk per trade (stop loss) | 1% of equity |
| max single position | 20% of equity |
| max total open risk | 5% of equity |
| max sector concentration | 35% of equity per GICS sector |
| daily loss stop | -3% of start-of-day NetLiquidation → no new risk |
| leverage | 1x (cash) unless explicitly enabled; never > broker limit |
| protective stop on every entry | mandatory (GTC stop, on broker side) |
| standing approval for opening/increasing exposure | never |

Sizing: `shares = floor(risk_budget / (entry - stop + slippage + commission/share))`, slippage stressed for gaps; then cap by max position %, buying power and sector cap. Limits file example uses placeholders only.

## 5. Execution

- Bots prepare a ticket (`skills/desk-trade-lifecycle`), e.g.:
  `TICKET IB-20261013-01 | paper | AAPL (conid ...) BUY 10 sh LMT 100.00 DAY | stop SELL 10 STP 95.00 GTC (bracket) | risk $50 = 0.5% of equity | approve with: "approve IB-20261013-01"`
- Execution Trader (`agents/execution-trader.md`, `skills/desk-execution-protocol`) sends once, as a bracket (parent + attached stop), with a unique `orderRef`/client id = ticket id; reconciles from IBKR order/execution records; never resends without a new approval.
- Mode default `paper`; `live` requires env flag + CLI flag + user confirmation shown on the ticket.
- Pre-send checks: market open/allowed session, not halted, price moved < max slippage, ticket not already sent.
- Keep Grok Bot "Require Approval" gate and the Claude Code hook as external enforcement.

## 6. Security

- Credentials only via env/secret store; never in repo, logs or chat.
- Read-only bots get read-only access; only Execution Trader can place orders.
- **No financial data** (balances, positions, amounts, P&L) sent to any external channel except the user's own Grok Bot chat / own Gmail. No posting to X, public groups, issues, PRs.
- Logs redact account ids and amounts by default.

## 7. Remove / disable crypto-only parts

Behind a flag `DESK_VENUE=hyperliquid|ibkr` (default `ibkr`), or remove: HIP-3 (`--dex`, `xyz:` prefixes, `resolve_coin` dex logic), `--testnet`, API wallets/USDC/onchain context, funding/OI, dead-man's switch/TWAP/spot (`skills/hyperliquid-advanced`), `hyperliquid-setup` wallet flow, `hypergrok-bootstrap` HL steps (rewrite bootstrap to: gateway check → paper account check → Opening Bell on SPY → doctor). Update README/SETUP/docs accordingly.

## 8. Testing

- Unit tests (`scripts/test_*.py`, stdlib `unittest`) with **mocked IBKR** fixtures (fake data only): snapshot parsing, session detection, halt handling, sizing, PDT counter, sector cap, rounding, write-gate detection, doctor checks.
- `scripts/check.sh` + CI green, including manifests and gitleaks.
- Integration test against IBKR **paper** account (manual, not in CI, no outputs committed).
- **~1 month of paper trading** with full ticket flow and Trade Reviewer journal (kept outside repo) before any live use.

## 9. Phases & deliverables

1. **Scaffold & hygiene** — `.env.example`, `.gitignore`, gitleaks in check/CI, `DESK_VENUE` flag. Deliverable: CI green, scan clean.
2. **IBKR client** — `scripts/ibkr_client.py` + mock + tests. Deliverable: reads quote/bars/account on paper.
3. **Opening Bell & doctor** — stocks snapshot (schema v2), doctor IBKR checks. Deliverable: `opening_bell.py --symbol SPY --json` works; fixtures fake.
4. **Research dataset** — IBKR bars, adjusted data, verify mode. Deliverable: tests pass offline.
5. **Skills & agents** — `skills/ibkr-*`, rewritten risk-limits, lifecycle, execution, monitoring, incident, strategy-lab; 7 agent files updated. Deliverable: manifests pass.
6. **Execution path (paper)** — bracket orders, reconciliation, write gate. Deliverable: approved ticket executes once on paper, rejected/unapproved never sends.
7. **Paper trading month & docs** — README/SETUP/FAQ rewritten. Deliverable: journal review, then live flag documented (not enabled).

## 10. Acceptance checklist ("done" =)

- [ ] **No personal data anywhere in repo or git history** (no account ids, usernames, keys, balances, positions, trades, P&L, emails, names, real logs/screenshots); gitleaks clean on full history; `.env.example` placeholders only; `.gitignore` covers `.env`, logs, data, reports.
- [ ] All Hyperliquid reads replaced by `ibkr_client` (or behind `DESK_VENUE=hyperliquid`).
- [ ] Opening Bell shows stock snapshot, session state, halt status; unavailable data labelled, never zero.
- [ ] Risk Manager enforces ceilings, position %, sector cap, daily loss, PDT, cash settlement.
- [ ] No order is ever sent without `approve <ticket-id>`; double-send impossible (tested).
- [ ] Default paper; live requires env flag + CLI flag + confirmation.
- [ ] Only Execution Trader can write; read-only MCP never used for orders.
- [ ] No financial data leaves user's own chat/Gmail.
- [ ] HIP-3 / testnet / wallet / funding code removed or flagged off.
- [ ] Unit tests with mocks pass; `scripts/check.sh` and CI green.
- [ ] ~1 month paper trading completed with reviewed journal (kept outside repo).
