# Run this desk with Interactive Brokers

This is the stock path. The original `README.md` stays the Hyperliquid desk and is not the setup for US stocks.

The stock commands live in their own files. They do nothing until `DESK_VENUE=ibkr`. They talk to the Interactive Brokers Client Portal Gateway on this computer. They do not read a `.env` file by themselves. They do not send an order unless you later approve one ticket by its exact id.

Use a paper login until you mean to use a live account. A paper account id starts with `DU`. A live account id starts with `U`. The examples below use the fake id `DU0000000`.

## 1. Get the fork

The stock files are on this fork, not on the original Hyperliquid repository.

```bash
git clone https://github.com/igor-olikh/hypergrok-trading-desk.git
cd hypergrok-trading-desk
python3 --version
```

Python 3 is enough. There is no package to install for these commands.

## 2. Turn the stock tools on

In the same terminal:

```bash
export DESK_VENUE=ibkr
```

Close the terminal and this switch is gone. Set it again next time.

Check that the desk files are intact before you involve the broker:

```bash
python3 scripts/ibkr_doctor.py --offline --json
```

You want a row named `ibkr gateway` with status `SKIP`. That means the broker was not contacted. Fix any `FAIL` row in that output before going on.

## 3. Start the gateway

The scripts use the Client Portal Gateway, the small local program from Interactive Brokers. They do not use the TWS socket API.

1. Install Java if `java -version` fails. Use the Java link on the [Client Portal quick start](https://www.interactivebrokers.com/docs/web-api/api/web-api/quick-start).
2. Download the gateway from that same page and unzip it.
3. In a second terminal, from the unzipped `clientportal.gw` folder:

```bash
bin/run.sh root/conf.yaml
```

On Windows the same step is `bin\run.bat root\conf.yaml`. Leave that window open.

4. Open `https://localhost:5000` in a browser. The certificate warning is normal for this local program. Continue.
5. Log in with the paper username. Log out of TWS, the phone app, and the website first if the gateway will not accept the login.
6. Wait until the page says the client login succeeded.

On a Mac, port 5000 is often taken by AirPlay. If the gateway cannot start, open `root/conf.yaml` in the gateway folder and change `listenPort` from `5000` to `5001`. Save, start the gateway again, and use `https://localhost:5001` in the browser. Interactive Brokers documents this on [How to modify the Client Portal Gateway port](https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/how-to-modify-the-client-portal-gateway-port).

## 4. Point the desk at the gateway

Back in the desk terminal:

```bash
export DESK_VENUE=ibkr
export IBKR_GATEWAY_URL=https://localhost:5000
export IBKR_ACCOUNT_ID=DU0000000
```

Use your own paper id in place of `DU0000000`. If you changed the port, change the URL too.

You can skip `IBKR_ACCOUNT_ID` only when the gateway returns exactly one account. If it returns more than one, the read stops and asks you to set the id.

Do not put these values in a `.env` file inside this repository. The original ignore list does not hide that file, and `bash scripts/check_ibkr.sh` refuses to pass while `.env` is here. Keep a private copy outside the repo if you want the lines saved, then load it into the terminal:

```bash
set -a
source "$HOME/.hypergrok/ibkr.env"
set +a
```

Placeholders you may copy are in `.env.example`.

## 5. Read a price

```bash
python3 scripts/ibkr_opening_bell.py --symbol SPY --json
```

A good read prints a snapshot and exits 0. `depth` is `unavailable` when the book is not subscribed. That is not a zero book.

If the gateway is down or the login has expired, the command exits 1 and says the source is unavailable. Log in again in the browser. The gateway login does not last forever.

Then:

```bash
python3 scripts/ibkr_doctor.py --json
```

A live gateway check should not print the account id. `FAIL` on `ibkr gateway` means the gateway did not answer. The detail stays `unavailable` on purpose.

Daily bars, written outside the repo:

```bash
python3 scripts/ibkr_research.py --symbol SPY --output "$HOME/.hypergrok/spy-bars" --json
```

US stocks have no funding history. `--with-funding` is refused.

## 6. Send only when you mean it

Reading does not send. A send is a separate command, one ticket, one time. The same ticket id is refused the second time.

The journal is outside the repo, in `~/.hypergrok/trading-desk`, unless you set `DESK_STATE_DIR`.

This desk sends one long limit buy plus a sell stop of the same size. The approval text must be exactly `approve IB-YYYYMMDD-NN` for that ticket. A paper ticket also requires a paper account. Do not pass `--live` for paper.

A live send needs all three, and a live account: `IBKR_LIVE=1`, `--live`, and the words `confirm live`. Leave those off.

Build the ticket yourself from numbers you accept. The file below is a fake shape, not an order to use:

```json
{
  "id": "IB-20261013-01",
  "mode": "paper",
  "account_kind": "paper",
  "symbol": "SPY",
  "conid": 0,
  "side": "BUY",
  "qty": 1,
  "order_type": "LMT",
  "limit": "100.00",
  "tif": "DAY",
  "stop": "95.00",
  "stop_qty": 1,
  "session": "regular",
  "halted": false,
  "last": "100.00"
}
```

Replace `conid`, prices, size, and the id. `conid` comes from the quote you just read. Then:

```bash
python3 scripts/ibkr_execute.py --ticket-file "$HOME/.hypergrok/ticket.json" --approve "approve IB-20261013-01"
```

The risk rules can still refuse the send. The ceilings are in the next section. A refusal prints `Not sent:` and does not call the broker.

## Ceilings

Your own limits may only be stricter. The code is `scripts/ibkr_risk.py`.

| Ceiling | Value |
| --- | --- |
| max risk per trade | 1% of equity |
| max single position | 20% of equity |
| max total open risk | 5% of equity |
| max sector concentration | 35% of equity |
| daily loss stop | -3% of start-of-day equity, then no new risk |
| leverage | 1x cash unless you explicitly enable margin |
| protective stop | required, on the broker, GTC, bracketed with the entry |
| standing approval to open or add | never |

Shares are whole numbers, minimum 1. Outside regular hours, only an explicit outside-hours limit is allowed. A halt blocks the send. Shorts stay off unless you allow them.

## If you change the repo

```bash
bash scripts/check.sh
bash scripts/check_ibkr.sh
```

The second command needs `gitleaks` on the machine. It also fails if `.env` is inside the repo.

`hooks/ibkr_write_gate.py` asks before a command that looks like an order, and only when `DESK_VENUE=ibkr`. The published plugin still loads the original Hyperliquid hook. The send script is what blocks an order that lacks the exact approval line.
