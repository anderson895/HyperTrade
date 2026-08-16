# HyperTrade

Windows desktop trading bot for the **BTC-USD perpetual on Hyperliquid**, implementing the
15-Minute Volume Rejection Strategy. Paper and Live modes, seven timeframes, risk-per-trade sizing,
native exchange stops and targets, a trailing stop, and a news blackout.

The strategy was specified by the client, not chosen here. Those source documents live in
`development_guide/`, which is deliberately kept out of this repository — every rule they set is
reproduced below, so nothing you need is behind that door.

Repository: <https://github.com/anderson895/HyperTrade>

> **Trading risk.** This software places real orders that can lose real money, and it has done both.
> Read [Does it work?](#does-it-work) before running it — the strategy as specified backtests
> **negative on every timeframe with a sample big enough to read**, including the 15 minutes it is
> named after. Every backtest figure here is optimistic on top of that, because stop fills are
> modelled at the stop price rather than where they would really fill. Paper-trade first, start
> small, and treat the numbers as an upper bound. You alone are responsible for what this does with
> your money, and for whether using Hyperliquid is lawful where you are.

## The strategy

A failed breakout, faded. Price pushes past the last 24 hours' high or low on unusual volume, gets
pushed straight back inside, and closes there — leaving a long wick where it was rejected. The wick
is the evidence: someone was there in size and lost. The bot takes the other side.

It is a **mean-reversion** system. It bets against the move that just happened, which is the
opposite instinct to a breakout system. Worth knowing before the losses start looking like bugs.

Every rule below comes from the client's specification; the periods and multiples are its author's,
reproduced rather than tuned. A strategy that does not match the specification it was written from
tells you nothing about what that specification does.

| Specification | Where it lives | Notes |
|---|---|---|
| 24-hour range: highest high / lowest low of 96 candles | `strategy/volume_rejection.py` | The signal bar is excluded from its own range — included, a new high would always be its own high and no break could ever be detected |
| Volume spike: > 2× the 20-period average | same | The 20 bars *before* the signal bar, so the spike does not inflate the average it is measured against |
| Wick ≥ 40% of the candle's total length | same | Upper wick for a short, lower for a long |
| Closes back inside the range | same | This is what makes it a *failed* breakout rather than a breakout |
| Post-Only (ALO) limit entry at the candle close | `engine.py` → `broker/live.py` | On by default |
| Cancel unfilled after 30 minutes (2 candles) | `engine.py`, `entry_expiry_candles` | Counted in candles, so it still means "two bars" if you change the timeframe |
| Risk 3% of equity, leverage hard-capped at 5x | `core/sizing.py`, `config.py` | See [the note on 3% and 5x](#what-3-and-5x-actually-risk) — they interact |
| Stop just beyond the rejection wick | `strategy/volume_rejection.py` | 0.1% past the wick, per the spec's `high * 1.001` |
| Fixed take-profit at 2R | `config.py`, `take_profit_rr` | Editable |
| Trailing stop, 0.4% behind the peak, from 1R profit | `engine.py` → `_trail_stop` | Editable, and on by default |
| News blackout ±30 min around high-impact USD events | `data/calendar.py` | Also cancels resting entries |
| Isolated margin | `config.py` | Default |

### Where this deviates from the reference script, and why

The specification ships with a working Python script. The detection logic here matches it bar for
bar —
same lookbacks, same slicing, same comparisons. Three things are done differently, each because the
script has a defect at that point:

**1. Exits are attached when the entry fills, not when it is placed.** The script calls
`place_bracket_orders` inside the `"resting"` branch, so the reduce-only stop and target are sent
against a position that does not exist yet. HyperTrade holds them until the fill is confirmed
(`_settle_pending_entry`).

**2. One resting order at a time.** The script's main loop sleeps 15 seconds and re-evaluates, and
it never checks whether it already has an order on the book — only whether it has a *position*.
Because the signal bar does not change until the candle closes, the same setup is re-detected and
re-ordered every 15 seconds, each duplicate carrying its own bracket. HyperTrade refuses a second
entry while one is pending (`engine.py`, `_no_trade_reason`).

**3. `TRAILING_ACTIVATION_RR` is honoured.** The script declares it as `1.0` and then never reads
it — `manage_active_position` trails from the first tick of profit. HyperTrade follows the written
instruction (1R), which is also the safer of the two: trailing from tick one tightens the stop into
ordinary noise.

The news filter is fetched from a different URL — the script's endpoint
(`nodedata.forexfactory.com/forex.json`) returns nothing usable; this uses ForexFactory's weekly
JSON feed. And where the script logs a calendar failure and carries on trading, this one stands
aside. See [The news blackout](#the-news-blackout).

## Does it work?

Not on the evidence available. Measured **2026-08-16** on real Hyperliquid candles, spec settings
(0.1% stop buffer, 2R target).

Fees are quoted **per side**. The backtester charges them on the entry and again on the exit, so
`4.5` is a 9bps round trip. That rate is not an assumption: real fills on the live account came
back at exactly **4.50 bps** a side with `crossed: true`. The middle column models the shipped
configuration, where `post_only_entry` rests the entry and pays the maker rate instead of crossing;
the stop always crosses, because a stop that does not cross does not get you out.

| Timeframe | Trades | Win rate | Taker both legs | Maker entry (shipped) | Fees at zero |
|---|---:|---:|---:|---:|---:|
| 5 mins | 40 | 37.5% | **−0.316R** | −0.169R | +0.125R |
| **15 mins** (spec) | 44 | 27.3% | **−0.448R** | −0.359R | −0.182R |
| 30 mins | 53 | 30.2% | **−0.287R** | −0.227R | −0.105R |
| 1 hour | 52 | 34.6% | −0.066R | **−0.031R** | +0.038R |
| 4 hours | 46 | 21.7% | **−0.399R** | — | −0.348R |
| Daily | 12 | 50.0% | +0.484R | — | +0.500R |

Read carefully:

- **Every timeframe with a readable sample loses, in every fee column.** Paying maker on the entry
  narrows the gap — 1h comes within a third of a tenth of an R of flat — but does not cross it.
- **15m loses even with fees set to zero.** That separates signal from cost, and it says the signal
  is the problem at that speed. No fee tier, rebate or maker-only fill rescues it.
- **The maker column is modelled, not observed.** It splits 6bps evenly across the two legs, and no
  maker fill has been measured on the live account yet. Treat it as the optimistic bound it is.
- **Where the edge actually goes.** At 5 mins the signal is worth +0.125R before costs, which is
  what a 37.5% win rate at 1:2 should pay. The stop sits 0.12–0.30% from entry, so a 9bps round
  trip is 15–37% of R — wins land near +1.5R instead of +2R, losses near −1.45R instead of −1R,
  and a 1:2 system behaves like a 1:1 one. The signal has an edge; the cost of taking it is larger.
- **Daily is the one positive row, on 12 trades.** Twelve trades of a 50% winner swings between 3
  and 9 wins on chance alone. It is not evidence, and the backtester marks it as such.
- **4h is worse than 5m.** A slower timeframe is not a safer one, which is why the app's advisory
  now flags 4h too.
- **Sample sizes are small everywhere.** Hyperliquid serves about 5,000 candles per interval and no
  more, so the fast timeframes cover weeks, not years. 40–55 trades is thin.
- Widening the stop buffer to 0.4–0.8% moves 15m/30m/1h to roughly break-even (−0.06R to +0.05R).
  That is six variants measured on one window, so the best of them is partly luck — a starting
  point for a test, not a result. `tools/run_backtest.py` prints the full comparison.

**The app ships the spec's settings anyway**, including 15m, and warns at every start. Overriding
the specification with a timeframe nobody asked for would be a worse failure than reporting this
honestly: the numbers are here, the choice is yours.

### What 3% and 5x actually risk

They fight each other, and the result is not 3%.

Size comes from risk and stop distance: `size = risk ÷ stop_distance`. The leverage needed follows
from the stop alone — `leverage = risk% ÷ stop%` — and equity cancels out entirely. At the spec's
3% risk and its typical 0.38% stop, that wants **7.9x**, above the 5x cap.

So the cap binds on most signals, and the position is cut to fit. Effective risk lands nearer
**0.6%** than 3%. The specification sizes exactly the same way
(`min(risk_amount / sl_dist_pct, equity * MAX_LEVERAGE)`), so this is faithful, not a workaround —
but it means the number in Settings is a ceiling, not a promise.

**The reduction is never silent.** Every clamped trade logs both figures:

```
size capped by the 5x limit: risking 0.61 USDC (0.61% of equity),
not the 3.00 (3.00%) requested - the stop is 0.38% away
```

Turn the clamp off in Settings and the trade is refused instead, and the log says why. Either is
defensible; being unaware of which one is happening is not.

## Status

**Runs as a desktop app in Paper and Live modes.** Live has been run against a real mainnet account
and reconciled against Hyperliquid's own order history: entry filled, `Stop Market` and
`Take Profit Limit` both placed, both `reduceOnlyCanceled` on a manual close.

| Component | State |
|---|---|
| Precision rules (`src/core/precision.py`) | Done, tested |
| Risk sizing (`src/core/sizing.py`) | Done, tested |
| Indicators (`src/core/indicators.py`) | Done, tested |
| Hyperliquid info/REST client (`src/data/hl_info.py`) | Done, tested against the live API |
| Strategy — volume rejection (`src/strategy/`) | Done, tested, backtested on real candles |
| Backtester (`src/backtest.py`, `tools/run_backtest.py`) | Done, tested |
| Settings + validation (`src/config.py`) | Done, tested |
| Secrets store (`src/secrets_store.py`) | Done |
| Logging with secret redaction (`src/logging_setup.py`) | Done, tested |
| Broker interface (`src/broker/base.py`) | Done |
| Paper engine (`src/broker/paper.py`) | Done, tested, survives restart |
| Trade history + statistics (`src/store.py`) | Done, tested |
| Bot engine (`src/engine.py`) | Done, tested |
| Post-only entries with expiry | Done, tested |
| Trailing stop (`engine._trail_stop`) | Done, tested |
| News blackout (`src/data/calendar.py`) | Done, tested, verified against the live feed |
| Desktop UI (`src/ui/`) | Done, smoke-tested off-screen |
| Console runner (`src/console.py`) | Done |
| Live execution (`src/broker/live.py`) | Done — run against a funded mainnet account |
| WebSocket feed | Not started — the engine polls REST |

402 tests passing (386 offline, 16 against live APIs — Hyperliquid and the calendar feed).

Verified against mainnet: `api.hyperliquid.xyz` is reachable with no VPN and no custom DNS
resolver. BTC is `szDecimals=5`, `maxLeverage=40x`, so prices take one decimal place.

## Setup

```powershell
& "C:\Program Files\Python313\python.exe" -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python **3.13** — the system `python` on this machine is 3.10 and will not work. Always call
`.\venv\Scripts\python.exe`, never a bare `python`.

## Running it

Opens in **Paper** mode. See [Going live](#going-live) before switching.

```powershell
.\run.bat                                # opens the desktop window
.\venv\Scripts\python.exe -m src.main    # the same thing
.\venv\Scripts\python.exe src\main.py    # also fine - it re-runs itself as a module
```

Any of those work, from the project root or from inside `src\`. What does **not** work is a bare
`python` — the system interpreter here is 3.10 and the project needs 3.13. Always call
`.\venv\Scripts\python.exe`, or activate the venv first.

Six pages: **Dashboard** (connection and bot status, position, balance, price chart with the
entry/stop/target levels drawn on it, recent logs), **Settings**, **Logs**, **Trades**,
**Statistics** and **About**. The bottom bar carries Market / Strategy / Timeframe / Risk /
Leverage, the START and STOP buttons, Close position, and an uptime counter.

The strategy is named on that bar because a live account was once started on the wrong one and
nothing on screen said so.

### Settings

Three cards: **Account Settings**, **Trading Configuration** and **Exit Rules**.

Risk takes a unit — `USDC` or `% of equity` — and the one you pick is the one that applies and the
one the bottom bar shows. That is not decoration: the bar once read `0.10 USDC` while the engine
was staking 0.30% of equity, because it printed a field the engine was ignoring.

**Exit Rules** holds the four parameters the specification asks to be adjustable:

| Setting | Spec constant | Default |
|---|---|---|
| Take profit | `FIXED_TP_RR` | 2.0 R |
| Trailing stop on/off | `ENABLE_TRAILING_STOP` | On |
| Trailing activation | `TRAILING_ACTIVATION_RR` | 1.0 R |
| Trailing distance | `TRAILING_DISTANCE_PCT` | 0.4% |

Stop buffer sits there too, and greys out for any strategy that does not measure its stop from a
wick.

**About** carries a live sizing preview: it sizes a trade from whatever is on the Settings form,
using the current price, your equity and the current stop estimate, and it updates as you type. It
exists because a risk and leverage pair that can never fit produces no trades at all — which looks
exactly like a market that never signals. On the shipped defaults it says

```
capped by 5x: risking 0.61 USDC, not the 3.00 asked for
```

and with the clamp switched off, where the trade would be refused outright:

```
Cannot trade: exceeds leverage cap - needs 8x, or risk of about 0.61 USDC
```

Either way you find out on the Settings screen instead of after an evening of waiting and wondering.

Settings are locked while the bot runs — changing the timeframe mid-trade would leave the strategy
reasoning about candles it never saw. The news blackout controls stay live, because that is the one
thing you may need to change mid-session, and standing aside never puts money at risk.

### The news blackout

**No new entries around high-impact US releases** — CPI, FOMC, NFP and the like. Leverage through a
data release is how accounts get liquidated: the book thins out and a stop that normally fills
within a tick fills wherever the next resting order happens to be. The strategy has no view on any
of that; it reads a candle close. So the bot stands aside.

The schedule comes from [ForexFactory](https://nfs.faireconomy.media/ff_calendar_thisweek.json)'s
weekly JSON feed — free, no API key — filtered to `country = USD` and `impact = High`. Only the
timing is used; forecast and actual values are ignored, because trading the number is a different
system to this one. The feed is cached for an hour, so a running bot fetches it 24 times a day at
most. It rate-limits: three fetches inside a few seconds earned a 429 with `Retry-After: 67`. The
cache is also written to SQLite, so a restart does not start cold — a cold start that met a 429
would otherwise stand the bot down until the next candle close.

**±30 minutes**, symmetric, per the specification. Both sides are editable in Settings.

**It fails closed.** If the calendar cannot be read at all, the bot does not trade and says so:
`no entry: economic calendar unavailable - standing aside`. Not knowing whether CPI is five minutes
away is not a reason to assume it isn't. A *refresh* that fails falls back to the cached copy — an
hour-stale calendar is still a good calendar — so only a cold start with no network and no cache
stands the bot down.

**A resting entry is cancelled when a release comes into range.** An unfilled limit order has no
claim on being left where it is, and filling into a release is the thing the blackout exists to
prevent.

**An open position is never touched by news.** Its stop and target are already with the exchange;
closing on a release would realise a loss the stop might never have taken. Only new entries are
held back.

Beside it is a manual override — `No new trades today (economic data day)` — for when you know
something the calendar does not.

### The daily loss limit

A circuit breaker, set as a **percentage of equity** and defaulting to **2%**. Once the day's
realised losses reach it, no new entries are taken until 00:00 UTC.

It is measured against equity *now*, not equity at midnight, so it tightens as the account
shrinks — the same way `risk_pct` does, and for the same reason: a percentage that compounds one
way and not the other is not a percentage.

**It is not part of the strategy specification**, and it is careful not to become one. It refuses
new entries and does nothing else: sizing, stops, targets and the signal are untouched, and an
open position keeps the exits already lodged with the exchange. Closing on a daily limit would
realise a loss the stop might never have taken.

The one thing to know is that a backtest has no such limit, so a halted day is a day the backtest
would have gone on trading. At the shipped settings the limit is roughly three losing trades, and
the strategy averages about one signal every day and a half — so it should be rare, and when it
fires something is worth looking at.

Set it to `Off` to disable it.

**STOP BOT does not close an open position.** It stops looking for entries and leaves the position
with its stop and target in place. Use **Close position** to flatten.

### The chart

**The chart runs whenever the app is open**, whether or not the bot is started. Candles load at
launch — including for a range you had selected last session — and the live price is folded into
the candle currently forming, so the chart moves every second rather than once a timeframe. Polling
continues while stopped for a second reason too: in Paper mode it is what evaluates a held
position's stop and target, so "the stop still stands" is true rather than a figure of speech.

**Candlesticks by default**, with a Line option beside them. Candles are the default deliberately —
the strategy reads wicks, and a close-only line hides exactly the data the entry and the stop are
built from.

The range selector next to it works like an exchange chart: `1s · 1H · 4H · 1D · 1W · 1M · YTD ·
All`. **Every entry is a span of history, not a candle size** — `4H` means the last four hours,
drawn with 5-minute candles, not four-hour candles. Longer spans are drawn from coarser ones (a
week of 5m bars would be 2,000 bars of mush):

| Range | Drawn with | Span |
|---|---|---|
| `1s` | live polled price | as long as the app has been open |
| `1H` | 5m × 12 | one hour |
| `4H` | 5m × 48 | four hours |
| **`1D`** | **15m × 96** | **one day — the default** |
| `1W` | 1h × 168 | one week |
| `1M` | 4h × 180 | thirty days |
| `YTD` | 1d | since January 1st |
| `All` | 1w × 400 | whatever history the exchange holds |

`1D` is the default: index 0 is the live view, which opens empty, so it cannot be. It also happens
to be exactly the strategy's field of view — 96 candles of 15 minutes is the same 24-hour range the
bot measures its high and low from. The chart title names both halves — "1D view (15 mins candles)"
— so a zoomed-in picture is never mistaken for what the bot is trading.

**No view is tied to the strategy.** The chart is display-only and never reaches the strategy; the
candles the bot decides on are set by Timeframe in Settings. There was once a `Bot` entry here and
it was removed: nothing on this chart is strategy-specific, so it only ever meant one fixed candle
size under a name that promised more. Change the timeframe to 30m and no range on this chart draws
30m candles.

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

**Entries are considered only when a candle closes** — on 15m that is four times an hour, and the
overwhelming majority of closes produce no trade. The backtest above found 44 setups in seven weeks
of 15-minute candles, so roughly one every day and a half. Long stretches with no activity are the
strategy working, not a hang. Everything is written to `data/app.log` as well as the Logs page.

## Going live

**What you need**

1. USDC in a Hyperliquid account, in the **Perps** wallet. Spot and perps are separate balances and
   only perps margins a trade — a funded spot wallet will have every trade rejected, so the session
   refuses to start and names the amount sitting in the wrong place.
2. An **API wallet (agent)** approved in the Hyperliquid app. It returns a private key that can
   place and cancel orders but **cannot withdraw**, so a leaked agent key cannot drain the account.
   The main wallet key is never entered into this app.
3. Your **main wallet address** — the one holding the USDC.

**Setting it up**

1. Settings → Trading Mode → `Live (REAL MONEY - Hyperliquid)`. Two fields appear.
2. Paste the main wallet address and the agent key. The key goes to Windows Credential Manager,
   never to a file, and is redacted from the logs. Leave it blank later to keep the stored one.
3. Set Network to **Testnet** first, or start on mainnet at a stake you are willing to lose outright.
4. Save. If anything is missing the save is refused and says which — all problems at once, not one
   per attempt.

**What the bot does in Live mode**

| | |
|---|---|
| Entry (default) | Post-only **ALO** limit at the candle close, cancelled after 2 candles if unfilled |
| Entry (post-only off) | IOC limit priced through the book by the slippage allowance — Hyperliquid has no market order |
| Stop | reduce-only **market** trigger; getting out beats getting a price |
| Target | reduce-only **limit** trigger at exactly the target, matching what the backtest assumed |
| Both exits | sent together under `positionTpsl`, so the exchange cancels one when the other fills |
| Trailing | the new stop is placed **before** the old one is cancelled — two stops for a moment costs nothing, a moment with none is when a fast move arrives |

**It fails closed.** If the key is missing, the address is wrong, or the exchange cannot be reached,
the session comes up in **Paper** with a red banner naming the reason. It will not half-configure
itself against real money.

**Before raising the risk**, check that one full entry-to-exit cycle reconciles with the Hyperliquid
UI: the same size, the same prices, the same realised PnL.

`tools/check_account.py` runs those checks from the terminal — funding, wallet split, agent
approval, and whether the current risk and leverage can actually place a trade.

Two known gaps: if the stop and target cannot be placed after an entry fills, the position is open
and unprotected — the error says so in those words, and you must close it by hand. And a position
closed on the exchange by anything this bot did not place (a manual exit, a liquidation) is recorded
as `manual close` rather than guessed at.

## Backtesting

```powershell
.\venv\Scripts\python.exe tools\run_backtest.py
.\venv\Scripts\python.exe tools\run_backtest.py --timeframes 1h 4h 1d
.\venv\Scripts\python.exe tools\run_backtest.py --fee-bps 0    # signal quality, cost removed
```

It compares six variants of the shipped strategy across the timeframes you ask for, then pools
every trade for a single expectancy. Pooled, not averaged per timeframe: a weekly run of 2 trades
once counted as much as a 30-minute run of 55, which turned a strategy that lost on every liquid
timeframe into a positive-looking average.

Rows below 25 trades are marked `<- too few to read`. Believe the marker.

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
    sizing.py    risk -> size, leverage cap or clamp, liquidation guard
    indicators.py EMA, true range, Wilder ATR, crossover detection
  data/
    hl_info.py   read-only /info client (meta, candles, mids, account state)
    calendar.py  high-impact US releases, for the news blackout
  strategy/
    base.py      Strategy interface + registry behind the Settings dropdown
    volume_rejection.py  24h range fakeout: volume spike, long wick, close back inside
  broker/
    base.py      Broker interface shared by paper and live, Fill, ManagedPosition, PendingEntry
    paper.py     simulated account: real prices, real fees, real slippage
    live.py      signed orders on Hyperliquid: ALO or IOC entry, reduce-only stop and target
  backtest.py    bar-by-bar replay, results in R multiples
  store.py       fill history and statistics behind the Trades page
  engine.py      the bot loop: candle close -> strategy -> sizing -> broker, plus trailing
  session.py     assembles settings + connection + broker + engine, for both front ends
  errors.py      the failure types the app expects, so handlers stay narrow
  ui/
    app.py       boots Qt and asyncio on one loop via qasync
    main_window.py     sidebar, page stack, timers, wiring
    dashboard_page.py  status cards, chart panel, recent logs
    settings_page.py   Account, Trading and Exit Rules
    about_page.py      how it works, and the sizing preview for the current settings
    trades_page.py / logs_page.py / stats_page.py
    bottom_bar.py      run controls and the config summary
    chrome.py    PageHeader and the top bar
    chart.py     candlesticks, close line, position levels, live ticks
    widgets.py   Card, StatCard, StatusCard, TitledCard, WheelBlocker
    alert_banner.py    dismissible error strip
    busy_overlay.py    "working..." veil over a page mid-save
    controller.py      the only place the widgets touch async code
    theme.py     dark stylesheet
  console.py     headless runner
  main.py        entry point: window by default, --console for the terminal
  config.py      AppSettings + SQLite persistence + START validation
  db.py          SQLite connection and migrations
  paths.py       data/ location (portable when frozen)
  secrets_store.py  agent key -> Windows Credential Manager
  logging_setup.py  rotating log with private-key redaction
tools/
  run_backtest.py   variant comparison on real candles
  check_account.py  pre-flight: funding, wallet split, agent approval, sizing
tests/
data/            runtime DB and logs — gitignored, never committed
```

The UI layout follows [PolyTrade Pro](https://github.com/anderson895/Poly-Trade-Monitoring)'s —
collapsible icon sidebar, page stack, bottom bar — in a teal palette instead of indigo.

## Safety notes

- The app stores a Hyperliquid **API wallet (agent) key** only. Agents can trade but cannot
  withdraw, and `usdClassTransfer` is a user-signed action an agent cannot perform either. The main
  wallet key is never entered here.
- `data/` and `venv/` are gitignored. Secrets are in Windows Credential Manager, not on disk.
- The **daily loss limit** defaults to **2% of equity** and is a percentage on purpose: a
  fixed USDC figure does not travel, and 2.00 USDC is a sensible circuit breaker on a 99 USDC
  account while halting a 1,000 USDC one after three trades. See below for what it does and
  does not do.
- Live mode should first be exercised on **testnet**, then at a stake you would not mind losing.

## Copyright

© 2026 anderson895. All rights reserved.

No licence is granted. The repository is public, so the code is readable, but nobody
else has permission to use, copy, modify or distribute it.
