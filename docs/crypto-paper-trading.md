# Crypto paper experiment

This opt-in experiment uses live **public** Kraken BTC/USD and ETH/USD data and
a $1,000 simulated USD balance. No exchange key, wallet, account, funding,
withdrawal or live order endpoint exists in this implementation. Nothing starts
until the owner creates/chooses a running Crypto Bot, selects their own model,
and presses **Start paper experiment** in Settings → Crypto paper trading.
Existing Bots and connections are not modified.

## Data and model

The adapter permits only fixed-origin HTTPS GETs to Kraken's public Ticker and
OHLC endpoints, with response/time bounds and no redirects or credentials.
Ticker bid/ask retrieval time is recorded; it is not an exchange event timestamp.
Only closed hourly candles are used. Missing, stale or malformed coverage and
wide/crossed spreads block the tick. The selected Bot's provider/model is resolved
through the existing owner provider boundary, without a global model fallback.

Each shared worker task makes one bounded, tools-disabled decision call using
public quotes/candles, the owner's experiment strategy and simulated positions.
Mailbox, Calendar, device, account and secret data are not provided. Provider
inference charges still apply: up to 96 calls/day at 15-minute intervals, 24
hourly, or 6 every four hours. Provider cost is not subtracted from paper P/L.
The model must return a validated buy, sell or hold with a reason. Kyrex fetches
a new bid/ask snapshot after that decision and uses only host-derived prices.

## Simulation and limits

Prices include observed spread plus 0.1% adverse slippage; every fill assumes a
0.4% fee. These are experiment assumptions, not a quote of Kraken's fee schedule.
The model cannot change them or the $25 order, $100 per-position or $200 daily
turnover limits. The simulation is long-only and cash-funded; oversells,
insufficient cash and exceeded limits are recorded without a fill. Equity is an
estimated liquidation value after assumed exit costs. Realized P/L, cumulative
fees and sampled maximum decline from observed peak are separate values.

At the first valid execution snapshot the fixed benchmark buys $100 each of BTC
and ETH (including assumed purchase costs), with the remaining balance in cash.
It retains those quantities and uses the same valuation/cost rules thereafter.
This comparison does not prove alpha: the Bot's actual exposure varies, and the
experiment has no queue priority, real liquidity, partial-fill or latency model.

A sampled decline to $950 or less automatically pauses new checks. This is not
a guaranteed loss cap, and paused holdings/quotes are not continuously marked.
The owner can always pause without a working Bot or provider. Pause and strategy
changes fence previously queued/running settings revisions. This first version
does not reset or erase the experiment after a loss guard stop.

## Persistence and chat

Settings, positions, quotes, model identity, reasons and every decision/fill are
sealed in the existing task database. Scheduling submits deterministic tasks to
CloudTaskStore/TaskWorker, never a second execution queue. The ledger transaction
checks the current configuration and worker claim, cancellation and ownership;
it commits the fill and its record atomically. Repeated finalization cannot
repeat a fill. Failed ticks retain the previous completed snapshot and a visible
failure status; they never use an old quote as if it were new. History is paged
50 checks at a time, with earlier decisions available from the same settings UI.

Daily cumulative snapshots are published on the first completed check after a
local date change, into the Bot's existing ongoing conversation and notification
inbox. A report outbox in the same database retries publication independently
of simulation; source/message identities prevent duplicate delivery from
repeating a fill. Crypto Bot can read its saved experiment via `crypto_read`.
The dedicated policy grants only `crypto:read` and `crypto:paper` and denies
other operations; these grants cannot authorize real trading.

Deploy web, worker, frontend and engine together. The server needs its existing
persistent data volume and stable encryption secret, plus HTTPS egress to
`api.kraken.com` and the owner's selected model provider. No live exchange
account or funds are needed. Controlled tests use fake market/provider data.

API references:
- https://docs.kraken.com/api-reference/market-data/get-ticker-information
- https://docs.kraken.com/api-reference/market-data/get-ohlc-data
