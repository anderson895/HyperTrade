---
name: hypertrade
description: Use when building, extending, or debugging HyperTrade — a desktop BTC-USD trading bot for Hyperliquid perps. Covers app requirements, Paper vs Live modes, timeframes, risk/leverage sizing, news blackout, bot on/off lifecycle, logging, and Hyperliquid API/SDK integration rules.
---

# HyperTrade — BTC/USD Trading Bot on Hyperliquid

Desktop trading bot, same shape as **PolyTrade Pro** (`../PolyTradeMonitoring`), but instead of
Polymarket binary Up/Down contracts this trades **traditional directional positions** — the
**BTC-USD perpetual on [hyperliquid.xyz](https://app.hyperliquid.xyz)**, with real entry price,
stop loss, take profit, and leverage.

**Why Hyperliquid:** no DNS-over-HTTPS resolver and no VPN needed. PolyTrade required a custom DoH
resolver because the local ISP poisons `*.polymarket.com`; `api.hyperliquid.xyz` resolves normally,
so that whole subsystem is dropped.

---

## Requirements

Each row is a hard requirement from the original spec. Anything marked *(proposed)* is a default I
chose to fill a gap — confirm before building on it.

| # | Requirement | Concrete spec |
|---|---|---|
| 1 | Trading mode | **Paper** (simulated) or **Live** (real Hyperliquid account). Selected in Settings, switchable only while the bot is **stopped**. |
| 2 | Timeframe | `5m`, `15m`, `30m`, `1h`, `4h`, `1d`, `1w` — all natively supported by Hyperliquid candles. One active timeframe at a time. |
| 3 | Risk per trade | Amount in **USDC** the user is willing to lose if the stop loss hits. Drives position size (see [Sizing](#risk-leverage-and-sizing)). |
| 4 | Leverage | `1x`, `2x`, `5x`, … up to the asset's `maxLeverage` read from the Hyperliquid `meta` response. Never hardcode the cap. |
| 5 | News events | No new entries during a news blackout window. |
| 6 | Paper starting balance | User-configurable starting equity for Paper mode, with a **Reset paper account** action. |
| 7 | Bot on/off | Single START / STOP control; the app runs and shows data even when the bot is off. |
| 8 | Logs | Timestamped, leveled, persisted, viewable in-app, and exportable. |

### Non-goals

- No altcoins in v1 — **BTC only**. Keep the asset a constant so multi-asset support is a later change, not a rewrite.
- No withdrawals or transfers from the app. Ever.
- No martingale / averaging down / grid recovery.

---

## Settings

Everything below is editable in the UI and persisted (SQLite for plain settings, Windows Credential
Manager for secrets — same split as PolyTrade).

**Account**
- Trading mode — `Paper` / `Live`
- Network — `Mainnet` / `Testnet` (testnet uses faucet USDC; use it for the first Live-path test)
- Main wallet address (`0x…`) — this is `account_address`, the wallet that holds the USDC
- API wallet private key — see [Keys](#keys-and-security)

**Trading**
- Timeframe — the 7 values above
- Risk per trade (USDC) — default small, e.g. `5`
- Leverage — dropdown, clamped to `maxLeverage`
- Margin mode — `Cross` / `Isolated` *(proposed: default Isolated — one bad trade cannot pull the whole account into liquidation)*
- Max concurrent positions *(proposed: 1)*
- Daily loss limit (USDC) *(proposed)* — bot auto-stops for the day when hit

**Paper**
- Starting balance (USDC)
- Reset paper account (clears simulated equity, positions, and trade history)

**Safety**
- News blackout on/off + minutes before/after
- Economic Data Day manual block (carried over from PolyTrade)

---

## Paper vs Live

Both modes go through **one interface** (`Broker` / `ExecutionClient`) with two implementations.
Strategy code must not know which one it is talking to — that is what makes Paper results meaningful.

| | Paper | Live |
|---|---|---|
| Price feed | Real Hyperliquid feed | Real Hyperliquid feed |
| Orders | Simulated locally | Signed and sent to `/exchange` |
| Equity | Local ledger, starts at *Paper starting balance* | `clearinghouseState` from the API |
| Fills | Simulated against the live L2 book | Real |

**Paper fills must not be free.** A paper engine that fills every order at the mid price at no cost
shows a profitable strategy that loses money live. The implemented model (`src/broker/paper.py`):

| Event | Fills at | Cost |
|---|---|---|
| Entry | reference price crossed by `slippage` | taker fee |
| Manual close | reference price crossed by `slippage` | taker fee |
| Stop loss | **the current mark**, not the stop price | taker fee |
| Take profit | **exactly the target**, no bonus for overshooting | taker fee |

The stop rule is the one that matters: filling at the mark means a gap through the stop costs what
a gap really costs, so a paper run cannot quietly assume its worst trades were cheap. The caller
supplies the reference price, which should be the top of the book on the side being crossed when
that is available and the mid otherwise.

**Not modelled: liquidation.** Sizing already refuses any trade whose stop sits past the estimated
liquidation price, so in a continuous move the stop always comes first. A gap past both would be
recorded as a larger-than-possible loss rather than as a liquidation.

**Live mode must fail closed.** If key loading, agent approval, or the first balance read fails:
show the red error banner, fall back to Paper, and do not place anything.

---

## Risk, leverage, and sizing

Risk-per-trade and leverage are **two different controls** and are routinely confused. In HyperTrade:

- **Risk per trade** determines *position size*, via the stop distance.
- **Leverage** only caps *how much notional the margin allows*. It is a constraint, not a sizing input.

```
size_btc   = risk_usdc / abs(entry_price - stop_price)
notional   = size_btc * entry_price
max_notional = equity * leverage
```

Then:
1. Round `size_btc` **down** to the asset's `szDecimals`.
2. If `notional > max_notional` → the trade needs more margin than the leverage setting allows.
   **Reject the trade and log why.** Do not silently size down, and never raise leverage to fit.
3. If the rounded size is `0` → risk per trade is too small for the stop distance. Skip and log.
4. Verify the stop sits **inside** the liquidation price. If leverage is high enough that
   liquidation comes before the stop, the stop is decorative — reject the trade.

Every rejection is a log line the user can read, not a silent `return`.

---

## Strategy

**Decided 2026-08-15: trend-following (EMA crossover + ATR stop).** Chosen over mean reversion
because leverage punishes the mean-reversion failure mode hardest — fading a move that keeps going
is how a 5x position gets liquidated, whereas a trend system's losses are capped by a stop that sits
close to entry.

Strategies are loaded through a `Strategy` interface and a registry, so adding mean reversion or
breakout later is a new file, not a refactor.

**Signal — `src/strategy/trend_following.py`**
- Fast EMA (21) crossing the slow EMA (55) at **candle close**, on the selected timeframe.
- Slope filter: the slow EMA must be moving in the trade's direction. This is what keeps the bot
  out of a sideways market, where crossovers fire constantly and each one is a small loss.
- Stop: `2.0 × ATR(14)` beyond the entry.
- Take profit: `2.0 R` — twice the stop distance. The system can therefore be profitable while
  losing more trades than it wins.
- Same parameters across all seven timeframes; the ATR scales the stop to each one automatically.
- Two optional chop filters (slow-EMA slope, and an EMA-200 trend filter) exist but are **off by
  default** — see the evidence below.

## Backtest evidence — 2026-08-15

First run of `tools/run_backtest.py` on real Hyperliquid BTC candles. Expectancy is R per trade,
net of 4.5bps round-trip fees, with no chop filter.

| Timeframe | Candles | History | Trades | Win rate | Expectancy |
|---|---|---|---|---|---|
| 5 mins | 5,001 | 18 days | 86 | 26.7% | **−0.905R** |
| 15 mins | 5,001 | 52 days | 70 | 28.6% | **−0.395R** |
| 30 mins | 5,001 | 105 days | 64 | 40.6% | +0.071R |
| 1 hour | 5,000 | 7 months | 64 | 35.9% | +0.002R |
| 4 hours | 5,000 | 2.3 years | 68 | 41.2% | **+0.198R** |
| Daily | 2,188 | 6 years | 31 | 32.3% | −0.045R |
| Weekly | 364 | 7 years | 2 | — | sample too small |

**Read this before trusting any of it.** Hyperliquid serves at most ~5000 candles per interval, so
the fast timeframes cover only weeks — 5m is 18 days, which is one market regime, not a sample.
Each row rests on 31–86 trades, at which point differences under about 0.2R are noise. And the
backtester assumes stops fill exactly at the stop price, which real fills do not. Every number
above is optimistic.

**What it does support:**

1. **5m and 15m lose money with this strategy.** −0.9R and −0.4R per trade are not marginal
   results — fees and noise dominate at that speed. The UI still offers them because the spec asks
   for all seven, but it must warn the user before starting on either.
2. **4h is the strongest of the seven** and has the most history behind it. It is the default.
3. **No chop filter is enabled.** Each helps on some timeframes and hurts on others, and the
   best-looking configuration (EMA-200 + slope, +0.96R on 4h) rests on 9 trades. That is
   overfitting, not an edge.

Re-run the tool after any strategy change, and before enabling a filter.

The framework below is fixed regardless of which signal is used:

- **One position at a time**, one entry per candle close.
- **Evaluate on candle close only** — not on every tick. Intra-candle evaluation makes backtests
  unreproducible.
- Every entry ships with a **stop loss and a take profit** computed *before* the order is sent.
- Exits: take profit, stop loss, or timed exit after N candles *(proposed)*.
- Both exits are placed as **reduce-only trigger orders** immediately after the entry fills, so an
  app crash cannot leave a naked position.

*(Proposed starting point — confirm)*: carry over PolyTrade's mean-reversion thesis, adapted to
directional trading — enter against a stretch of X ATR from a moving-average anchor on the selected
timeframe, with a higher-timeframe trend filter to avoid catching a knife during momentum
expansion. Stop beyond the extreme of the signal candle, take profit back at the anchor.

Whatever is chosen, it must be **backtestable on `candleSnapshot` history before a single live order
is placed.**

---

## News-event blackout

Purpose: don't hold leverage through CPI, FOMC, NFP. On Polymarket a bad entry cost the premium; on
a leveraged perp it can liquidate the account.

- Blackout window: `T-30min` to `T+15min` around a high-impact event *(proposed, configurable)*.
- Inside the window: **no new entries**. Existing positions are held with their stops intact
  *(proposed — the alternative is flat-before-news; ask the user which they want)*.
- v1: manual toggle + a user-editable event list, same as PolyTrade's Economic Data Day block.
- v2: economic calendar feed. Treat a feed failure as **"blackout active"**, not "no events" —
  fail safe, not open.
- The UI shows the next event and a countdown when a blackout is pending.

---

## Bot lifecycle

`START` → validate settings → connect feed → (Live: verify balance and leverage) → subscribe to
candles → evaluate on each close.

`STOP` → stop evaluating new entries. **Decided 2026-08-15: the open position is kept**, along with
its resting stop-loss and take-profit orders, and the log says so explicitly. Pressing STOP means
"stop trading", not "market-close my position at whatever the book offers". A separate **Close
position** action exists for that intent.

**Restart recovery:** on launch, read `clearinghouseState` (Live) or the local ledger (Paper) and
adopt any existing position, including its resting orders. PolyTrade already does this — same idea.

---

## Logs

- Levels: `DEBUG` / `INFO` / `WARN` / `ERROR`, timestamped, filterable in-app.
- To SQLite **and** a rotating `data/app.log`, so the user can send the file when something breaks.
- Log every decision, not just actions: signal fired, trade rejected and why, blackout skip,
  size rounded to zero, reconnects.
- **Never log the private key, the agent key, or any full signature.** Redact addresses to
  `0x1234…abcd` in the UI log view.

---

## Hyperliquid integration

**Endpoints** — mainnet `https://api.hyperliquid.xyz`, testnet `https://api.hyperliquid-testnet.xyz`.
- `POST /info` — market and account reads
- `POST /exchange` — signed actions (orders, cancels, leverage)
- `wss://api.hyperliquid.xyz/ws` — subscriptions

**SDK** — [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
(official). `Info` for reads, `Exchange` for signed actions, `constants.MAINNET_API_URL` /
`TESTNET_API_URL`, signing via an `eth_account` `LocalAccount`.

**Reads**
- `meta` / `metaAndAssetCtxs` → `szDecimals` and `maxLeverage` for BTC. Fetch at startup; do not
  hardcode either.
- `candleSnapshot` → history. Intervals `1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M` cover all 7
  app timeframes. **Max 5000 candles per request** — page backwards for longer backtests.
- `clearinghouseState` → positions, margin, withdrawable balance.
- WS `candle` (live candle updates, the strategy's clock), `l2Book` (paper fill simulation),
  `userEvents` + `orderUpdates` (fills and order state).

**Orders**
- Limit with TIF `Gtc` / `Ioc` / `Alo`. A "market" order is an `Ioc` limit priced through the book
  by a slippage allowance — there is no separate market type.
- TP/SL are **trigger orders**, placed `reduce_only=True`.
- Price formatting is strict: max **5 significant figures**, and at most `6 - szDecimals` decimals
  for perps. Sizes round to `szDecimals`. Wrong precision = rejected order; centralize this in one
  formatting helper and unit-test it.
- `update_leverage(leverage, "BTC", is_cross)` before the first order, and whenever the setting
  changes.

**Reliability** — WS auto-reconnect with backoff; re-read state after every reconnect rather than
trusting cached position data. Respect rate limits; don't poll `/info` in a tight loop when a WS
subscription gives the same data.

---

## Keys and security

Use a **Hyperliquid API wallet (agent)**, not the main wallet key. `approve_agent()` returns a
dedicated key that **can trade but cannot withdraw**. The main wallet address goes in
`account_address`; only the agent key signs. Worst case, a leaked agent key cannot drain the account.

- Store the agent key in **Windows Credential Manager** (`keyring`), never in a file, `.env`, or the
  SQLite DB.
- `data/`, `*.log`, and any local secrets file belong in `.gitignore` from the first commit.
- Test order on **testnet** first, then mainnet at **5–10 USDC risk and 1x–2x leverage** until a
  full entry→exit cycle reconciles against the Hyperliquid UI.

---

## Suggested stack

Reuse PolyTrade's stack — it already solves the desktop-trading-app problems on this machine:
Python 3.13 · PySide6 + qasync · SQLite · pyqtgraph / finplot for charts · httpx · websockets ·
keyring · truststore · PyInstaller. **Drop** the DoH resolver and the Coinbase fallback — Hyperliquid
is the single source for both price and execution, so the feed and the fills come from the same book.

---

## Definition of done

1. App runs with the bot **off** and still shows price, chart, and connection status.
2. Every one of the 7 timeframes loads history and receives live candle updates.
3. Paper mode: a full entry→SL and entry→TP cycle, equity moving correctly from the configured
   starting balance, fees and slippage applied.
4. Sizing verified by unit test: `risk_usdc` lost (± fees) when the stop hits, at 1x and at 5x.
5. A trade that exceeds `equity * leverage` is **rejected with a log line**, not silently resized.
6. Blackout window blocks entries and says so in the log.
7. Live mode on **testnet**: order placed, filled, TP/SL resting on the book, position visible in
   the Hyperliquid UI, PnL reconciles with the app.
8. Kill the app mid-position, relaunch → position and resting orders are adopted, not duplicated.
9. Live failure falls back to Paper with a visible banner.
10. No secret appears in `data/app.log`.

---

## Decisions

- **2026-08-15 — Entry signal:** trend-following (EMA 21/55 crossover, ATR stop, 2R target).
- **2026-08-15 — STOP with an open position:** keep the position and its resting exits.

## Open questions

1. **Perp or spot?** This assumes the BTC **perp** (that is where leverage exists). Confirm.
2. **News window** — block entries only, or also flatten before the event?
3. **Backtesting UI** — is a backtest screen in scope for v1, or a dev-only script?

---

*Source notes: `summary.txt` in this folder. Reference implementation: `../PolyTradeMonitoring`
(`README.md`, `DevelopmentPlan.md`, `details.txt`, `step.txt`).*
