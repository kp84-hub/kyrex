# Crypto paper experiment

This opt-in experiment uses live **public** Kraken USD market data and a $1,000
simulated USD balance. Owners can select BTC, ETH, SOL, XRP, ADA, DOGE, LINK and
AVAX in **Coins to watch**, or press **Watch all eight coins**, then save.
No exchange key, wallet, account, funding,
withdrawal or live order endpoint exists in this implementation. Nothing starts
until the owner creates/chooses a running Crypto Bot, selects their own model,
and presses **Start paper experiment** in Settings → Crypto paper trading.
Existing Bots and connections are not modified.

New experiments default to all eight coins. Existing BTC/ETH experiments keep
their original selection until the owner explicitly saves a broader watchlist.
Changing coins retains cash, holdings, history, the original benchmark and all
paper limits. Removed coins with holdings remain valued and sellable, but cannot
be bought again until selected. The host rejects off-watchlist buys even when
the model sees an asset solely for benchmark valuation.

## Data and model

The adapter permits only fixed-origin HTTPS GETs to Kraken's public Ticker and
OHLC endpoints, with response/time bounds and no redirects or credentials.
Ticker bid/ask retrieval time is recorded; it is not an exchange event timestamp.
Owners choose 30 closed hourly candles (about 30 hours) or 30 closed five-minute
candles (about 2.5 hours). Only completed candles are used. Missing, stale or malformed coverage and
wide/crossed spreads block the tick. The selected Bot's provider/model is resolved
through the existing owner provider boundary, without a global model fallback.

Each shared worker task makes one bounded, tools-disabled decision call using
public quotes/candles, the owner's experiment strategy and simulated positions.
The call compares all selected coins and can choose only one action per check;
the broader watchlist does not create one model call per coin. Larger prompts
can increase token costs. Bounded parallel candle reads are followed by a shared
fresh ticker snapshot. Watching more coins does not enable continuous monitoring.

### Aggressive paper preset

**Use aggressive paper preset** loads a strategy and five-minute candle setting
into the form. Press **Save paper settings** to apply them. Opening Settings or
loading the preset alone never changes a running experiment. Existing strategies
and hourly settings remain intact until saved. The preset leaves the selected
Bot, coins, check frequency and saved risk profile unchanged. Standard limits
remain $25 per order, $100 per position, $200 daily buying and a sampled $50 loss pause.

The model is asked to consider earlier momentum, breakouts and supported rebounds
without requiring the entire lookback to rise. It receives host-computed five-bar
and twelve-bar simple moving averages, the three-bar percentage change, whether
the last three closes rose and whether the last close exceeded the previous six
candle highs. These are observations, not predictions or guaranteed edges. A
model can still choose HOLD if conditions do not qualify. No trade quota or
forced entry is implemented, and an exit recommendation waits for the next
scheduled check; this is not continuous monitoring or an exchange stop order.

The prompt states costs as approximately 0.5% each side and 1% round trip before
spread. Candle duration is explicit in model input and recorded with each new
decision; historical records without that field used hourly candles. Switching
the timeframe creates a new settings revision, fencing queued/running old work,
and preserves the simulated portfolio, history and benchmark.
Mailbox, Calendar, device, account and secret data are not provided. Provider
inference charges still apply: up to 96 calls/day at 15-minute intervals, 24
hourly, or 6 every four hours. Provider cost is not subtracted from paper P/L.
The model must return a validated buy, sell or hold with a reason. Kyrex fetches
a new bid/ask snapshot after that decision and uses only host-derived prices.

### Broader paper test and outcome feedback

**Use broader paper test** loads a five-minute strategy and the broader risk
profile into the form. Press **Save paper settings** to activate it. Loading or
reading a preset never activates it, changes another owner's limits, or resets
cash, positions, history or the benchmark. The existing Bot, watchlist, provider,
schedule and inference-call count are retained. Existing experiments and older
clients without a saved profile keep standard limits. Older clients saving an
existing broader experiment without the new field preserve its saved profile.

| Saved profile | Maximum order before costs | Maximum position | New buys per local day | Sampled loss pause from $1,000 |
| --- | --- | --- | --- | --- |
| Standard | $25 | $100 | $200 | $50 |
| Broader paper test | $100 | $300 | $2,000 | $100 |

The larger budget permits reinvesting simulated sale proceeds during the day,
but cash funding still limits simultaneous exposure. Sales never replenish the
gross buying allowance. Larger exposure can magnify gains and losses; there is
no required investment percentage, trade quota or promised return. Changing a
profile creates a settings revision, fencing old queued/running work. An account
already stopped by its loss guard cannot bypass that stop by changing profiles.
New decision records save the actual risk profile and limits so later results
can be interpreted across configuration changes.

Each decision now receives a host-computed review of the last 40 saved checks:
realized sell wins/losses/breakevens, net realized results per coin, fees paid,
and at most 12 recent fills with bounded reasons and sell return percentages.
Sell profit uses differences in saved cumulative realized P/L, which already
includes the allocated entry cost/fee and the exit fee. One earlier record gives
the baseline for a truncated window, including older fills. Commit order handles
tied timestamps. Partial exits count separately; these counts are not completed
round-trip trades. Unrealized holdings remain separate in portfolio metrics.
The window can mix risk profiles and is explicitly labelled as limited evidence.

The model is instructed to reassess repeated losses and fee drag using those
actual outcomes and the current market evidence. The broader strategy allows
discretionary sizing up to the host limit and asks it to reconsider tiny-crossover
exits and weak setups that may not justify costs. This is feedback in the next
decision's context, not training model weights, an automatic strategy rewrite,
or proof that the next choice will be profitable. It adds prompt tokens but no
extra inference calls. Saved reasons remain untrusted data. Crypto Bot's saved
portfolio read exposes the same review without applying an unsaved preset.

## Simulation and limits

Prices include observed spread plus 0.1% adverse slippage; every fill assumes a
0.4% fee. These are experiment assumptions, not a quote of Kraken's fee schedule.
The model cannot change them or the limits of the owner's saved risk profile.
The simulation is long-only and cash-funded; sales without holdings,
insufficient cash and exceeded limits are recorded without a fill. Equity is an
estimated liquidation value after assumed exit costs. Realized P/L, cumulative
fees and sampled maximum decline from observed peak are separate values.

The saved daily buying limit applies to **new buy notional before costs**, rather
than combined buy/sell turnover. Sales of existing holdings remain eligible
after buying is capped and do not replenish buying capacity. Total turnover can
therefore exceed the buying limit. Every sale still obeys the saved order limit, owned quantity,
fresh-quote checks, owner/claim/revision fences, pause and loss-guard behavior.
This does not add automatic liquidation, continuous monitoring or real orders.

A sell's requested dollar amount is a maximum before fees. Inside the atomic
fill transaction, Kyrex caps its quantity to the actual holding at the fresh
execution price. This lets a cent-rounded or pre-price-change estimate close a
smaller holding without rejecting the whole sale or creating negative units.
To exit as much as possible, the model is instructed to request the saved order maximum: positions
worth less than that are closed for their actual value, including remainders
below the $1 request minimum; larger positions are reduced by at most the order limit and
need later sell decisions. Smaller requests remain partial reductions. Zero-unit
or zero-notional trades never fill. The original decision and actual fill stay
separate in the ledger. Existing failed decisions are retained, not replayed.

Daily buying, sales, total turnover and remaining buying capacity are derived
from the existing sealed fill ledger, including pre-update fills; no history or
portfolio reset is needed. Budget enforcement rereads these fills inside the
atomic commit transaction. Local midnight uses the saved summary timezone,
including 23/25-hour daylight-saving days. The UI and model input show the same
buying-only budget. Fees and no-fill decisions do not count as buy notional.

At the first valid execution snapshot the fixed benchmark buys $100 each of BTC
and ETH (including assumed purchase costs), with the remaining balance in cash.
It retains those quantities and uses the same valuation/cost rules thereafter.
This comparison does not prove alpha: the Bot's actual exposure varies, and the
experiment has no queue priority, real liquidity, partial-fill or latency model.

A sampled decline to $950 or less (standard) or $900 or less (broader) automatically pauses new checks. This is not
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
