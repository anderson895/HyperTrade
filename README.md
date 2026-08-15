# HyperTrade

[![License: MIT](https://img.shields.io/badge/License-MIT-2dd4bf.svg)](LICENSE)

Windows desktop trading bot for the **BTC-USD perpetual on Hyperliquid**. Paper and Live modes,
seven timeframes, risk-per-trade sizing in USDC, and a news blackout.

Repository: <https://github.com/anderson895/HyperTrade>

The full specification lives in [`SKILL.md`](SKILL.md). This file covers running the code.

> **Trading risk.** This software places orders that can lose money. Leverage can lose it faster
> than the market moves. The strategy backtests *negative* on the 5-minute and 15-minute
> timeframes, and every backtest figure here is optimistic because stop fills are modelled at the
> stop price rather than where they would really fill. Paper-trade first, start small, and treat
> the numbers as an upper bound. You alone are responsible for what this does with your money, and
> for whether using Hyperliquid is lawful where you are.

## Status

**Runs as a desktop app in paper mode.** No live execution yet.

| Component | State |
|---|---|
| Precision rules (`src/core/precision.py`) | Done, tested |
| Risk sizing (`src/core/sizing.py`) | Done, tested |
| Indicators (`src/core/indicators.py`) | Done, tested |
| Hyperliquid info/REST client (`src/data/hl_info.py`) | Done, tested against the live API |
| Strategy — trend following (`src/strategy/`) | Done, tested, backtested on real candles |
| Backtester (`src/backtest.py`, `tools/run_backtest.py`) | Done, tested |
| Settings + validation (`src/config.py`) | Done, tested |
| Secrets store (`src/secrets_store.py`) | Done |
| Logging with secret redaction (`src/logging_setup.py`) | Done, tested |
| Broker interface (`src/broker/base.py`) | Done |
| Paper engine (`src/broker/paper.py`) | Done, tested, survives restart |
| Trade history + statistics (`src/store.py`) | Done, tested |
| Bot engine (`src/engine.py`) | Done, tested |
| Desktop UI (`src/ui/`) | Done, smoke-tested off-screen |
| Console runner (`src/console.py`) | Done |
| Live execution | Not started |
| WebSocket feed | Not started — the engine polls REST |

160 tests passing (149 offline, 11 against the live API).

Verified against mainnet on 2026-08-15: `api.hyperliquid.xyz` is reachable with no VPN and no
custom DNS resolver. BTC is `szDecimals=5`, `maxLeverage=40x`, so prices take one decimal place.

**Backtest result, first run:** the strategy is profitable on 4h (+0.198R per trade) and roughly
break-even on 30m/1h/daily, but **loses badly on 5m (−0.905R) and 15m (−0.395R)**. Sample sizes are
small and stop fills are modelled optimistically. Full table and caveats in
[`SKILL.md`](SKILL.md#backtest-evidence--2026-08-15).

## Setup

```powershell
& "C:\Program Files\Python313\python.exe" -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python **3.13** — the system `python` on this machine is 3.10 and will not work. Always call
`.\venv\Scripts\python.exe`, never a bare `python`.

## Running it

Paper mode only — live execution is not implemented, and choosing it stops with a message rather
than pretending to trade.

```powershell
.\run.bat                                # opens the desktop window
.\venv\Scripts\python.exe -m src.main    # the same thing
.\venv\Scripts\python.exe src\main.py    # also fine - it re-runs itself as a module
```

Any of those work, from the project root or from inside `src\`. What does **not** work is a bare
`python` — the system interpreter here is 3.10 and the project needs 3.13. Always call
`.\venv\Scripts\python.exe`, or activate the venv first.

The layout follows PolyTrade Pro's — collapsible icon sidebar, page stack, and a bottom bar with
the run controls — in a teal palette instead of indigo.

Six pages: **Dashboard** (connection and bot status, position, paper balance, price chart with the
entry/stop/target levels drawn on it, recent logs), **Settings**, **Logs**, **Trades**,
**Statistics** and **About**. The bottom bar carries Market / Timeframe / Risk / Leverage, the
START and STOP buttons, Close position, and an uptime counter.

Settings are locked while the bot runs — changing the timeframe mid-trade would leave the strategy
reasoning about candles it never saw. The news blackout toggle stays live, because that is the one
switch you may need mid-session.

**STOP BOT does not close an open position.** It stops looking for entries and leaves the position
with its stop and target in place. Use **Close position** to flatten.

**The chart runs whenever the app is open**, whether or not the bot is started. Candles load at
launch — including for a range you had selected last session — and the live price is folded into
the candle currently forming, so the chart moves every second rather than once a timeframe. Polling continues while stopped for a second reason too: in
Paper mode it is what evaluates a held position's stop and target, so "the stop still stands" is
true rather than a figure of speech.

**Candlesticks by default**, with a Line option beside them. Candles are the default deliberately —
the stop is `2 x ATR`, ATR is built from the highs and lows, and a close-only line hides exactly
the data that decides where the stop goes.

The range selector next to it works like an exchange chart: `Bot · 1s · 1H · 4H · 1D · 1W · 1M ·
YTD · All`. Longer spans are drawn from coarser candles (a week of 5m bars would be 2,000 bars of
mush), and the chart title names the view so a zoomed-in picture is never mistaken for what the bot
is trading. **`Bot` is the default** and is the only view showing the timeframe the strategy
actually decides on; the others are display-only and never reach the strategy.

`1s` is the live polled price plotted tick by tick — Hyperliquid has no one-second candles, so
there is nothing to fetch and nothing to backfill. It starts empty and fills at about a point a
second while the app is open. **Expect it to look flat or stepped:** BTC's tick on Hyperliquid is
$1 and the spread is usually exactly that, so the mid only moves when the whole book shifts. A
measured 15 seconds produced two distinct prices. The y axis has a 0.1% floor so a $1 step does not
scale up into what looks like a crash.

The window opens maximised; if you un-maximise it, it stays that way next launch.

### Headless

```powershell
.\venv\Scripts\python.exe -m src.main --console            # terminal, Ctrl+C to stop
.\venv\Scripts\python.exe -m src.main --console --once     # one tick, then exit
.\venv\Scripts\python.exe -m src.main --console --timeframe 1h --risk 10 --leverage 2
.\venv\Scripts\python.exe -m src.main --console --reset-paper --balance 500
.\venv\Scripts\python.exe -m src.main --console --save     # persist these as settings
.\venv\Scripts\python.exe -m src.main -v                   # debug logging (either mode)
```

**Entries are considered only when a candle closes** — on 4h that is every four hours, and most
closes produce no trade. Long stretches with no activity are the strategy working, not a hang.
Everything is written to `data/app.log` as well as the Logs page.

## Backtesting

```powershell
.\venv\Scripts\python.exe tools\run_backtest.py
.\venv\Scripts\python.exe tools\run_backtest.py --timeframes 1h 4h 1d
```

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest -m "not network"   # offline suite
.\venv\Scripts\python.exe -m pytest -m network         # hits the live Hyperliquid API
.\venv\Scripts\python.exe -m pytest                    # everything
```

## Layout

```
src/
  core/          pure domain logic — no I/O, no SDK, no network
    models.py    Side, Timeframe, AssetMeta, Candle, Position, AccountState
    precision.py Hyperliquid price/size rounding rules
    sizing.py    risk -> size, leverage cap, liquidation guard
    indicators.py EMA, true range, Wilder ATR, crossover detection
  data/
    hl_info.py   read-only /info client (meta, candles, mids, account state)
  strategy/
    base.py      Strategy interface + registry behind the Settings dropdown
    trend_following.py  EMA crossover, ATR stop, 2R target
  broker/
    base.py      Broker interface shared by paper and live, Fill, ManagedPosition
    paper.py     simulated account: real prices, real fees, real slippage
  backtest.py    bar-by-bar replay, results in R multiples
  store.py       fill history and statistics behind the Trades page
  engine.py      the bot loop: candle close -> strategy -> sizing -> broker
  session.py     assembles settings + connection + broker + engine, for both front ends
  errors.py      the failure types the app expects, so handlers stay narrow
  ui/
    app.py       boots Qt and asyncio on one loop via qasync
    main_window.py     sidebar, page stack, timers, wiring
    dashboard_page.py  status cards, chart panel, recent logs
    settings_page.py   Bot Settings form
    trades_page.py / logs_page.py / stats_page.py / about_page.py
    bottom_bar.py      run controls and the config summary
    chart.py     candlesticks, close line, position levels, live ticks
    widgets.py   Card, StatCard, StatusCard, WheelBlocker
    alert_banner.py    dismissible error strip
    controller.py      the only place the widgets touch async code
    theme.py     dark stylesheet
  console.py     headless runner
  main.py        entry point: window by default, --console for the terminal
  config.py      AppSettings + SQLite persistence + START validation
  db.py          SQLite connection and migrations
  paths.py       data/ location (portable when frozen)
  secrets_store.py  agent key -> Windows Credential Manager
  logging_setup.py  rotating log with private-key redaction
tests/
data/            runtime DB and logs — gitignored, never committed
```

The UI layout follows [PolyTrade Pro](https://github.com/anderson895/Poly-Trade-Monitoring)'s, in a
teal palette instead of indigo.

## Safety notes

- The app stores a Hyperliquid **API wallet (agent) key** only. Agents can trade but cannot
  withdraw. The main wallet key is never entered here.
- `data/` and `venv/` are gitignored. Secrets are in Windows Credential Manager, not on disk.
- Live mode should first be exercised on **testnet**, then at 5–10 USDC risk and 1x–2x leverage.

## License

[MIT](LICENSE) © 2026 anderson895
