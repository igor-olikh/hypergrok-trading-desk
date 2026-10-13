# US stocks (Interactive Brokers)

The Hyperliquid files stay as they were. Stock tools are separate. They run only when `DESK_VENUE=ibkr`. Any other value, or an empty value, and each stock tool stops before it reads the broker or sends.

Do not put a real `.env` in this repository. The original ignore list is unchanged. Put the account id in the environment of the process. Placeholders are in `.env.example`.

```bash
export DESK_VENUE=ibkr
export IBKR_GATEWAY_URL=https://localhost:5000
export IBKR_ACCOUNT_ID=DU0000000

python3 scripts/ibkr_opening_bell.py --symbol SPY --json
python3 scripts/ibkr_doctor.py --offline --json
python3 scripts/ibkr_research.py --symbol SPY --output /tmp/spy-bars
python3 scripts/ibkr_execute.py --ticket-file ticket.json --approve "approve IB-YYYYMMDD-NN"
```

Paper is the default. A live send also needs `IBKR_LIVE=1`, `--live`, and the words `confirm live`. One ticket id is sent once. The same id is refused the second time.

## Ceilings

The user's own limits file may only be stricter. The code is `scripts/ibkr_risk.py`.

| Ceiling | Value |
| --- | --- |
| max risk per trade | 1% of equity |
| max single position | 20% of equity |
| max total open risk | 5% of equity |
| max sector concentration | 35% of equity |
| daily loss stop | -3% of start-of-day equity, then no new risk |
| leverage | 1x cash unless the limits file explicitly enables margin |
| protective stop | mandatory, on the broker, GTC, bracketed with the entry |
| standing approval to open or add | never |

Shares are whole numbers, minimum 1. Outside regular hours, only an explicit outside-hours limit is allowed. A halt blocks the send. Shorts stay off unless the limits file allows them.

## Order hook

`hooks/ibkr_write_gate.py` asks before a command that looks like an IBKR order, and only when `DESK_VENUE=ibkr`. The published plugin still loads the original Hyperliquid hook. That file is not edited. The send script is what blocks an order that lacks the exact approval line.

Checks for this path: `bash scripts/check_ibkr.sh`.
