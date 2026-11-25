# Tiny Trend Rider — EMA 8/21 + slow-trend filter, 7 coins

[![make.fun](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![make.fun](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/GeMseN-Test/tiny-bot-easy-to-beat-20261125.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![make.fun](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/GeMseN-Test/tiny-bot-easy-to-beat-20261125/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![make.fun](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/GeMseN-Test/tiny-bot-easy-to-beat-20261125/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

Entry for the [Tiny Bot: Easy to Beat bounty on make.fun](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1) — a tiny paper-trading bot that reads hourly bars and decides when to hold crypto and when to sit in USDC, ranked by net PnL in USDC against buy-and-hold.

## Strategy

One file, `main.py`, standard library only. It speaks the `agent-fun/bars-replay/v1` line protocol: the harness sends a `hello`, then one `batch` per hourly bar, then `done`, and the bot answers every batch with exactly one reply carrying its orders.

Per symbol (FARTCOIN, JITOSOL, POPCAT, RAY, SOL, WETH, WIF), long only:

- **Trend filter — EMA(8) vs EMA(21).** A symbol is only bought when its slow average is *rising* (slow now above slow 6 bars ago), so a dead-cat bounce inside a downtrend never triggers an entry.
- **Enter on the cross.** Buy when the fast average crosses above the slow one, with a 3-bar entry window so a fill the market refuses is retried while the signal holds. The 6-bar return must also be positive — buy strength, not noise.
- **Exit on the hard stop or a dead trend.** Sell everything on a hard stop (-8% of cost), or when the slow average turns down *while the trade holds no profit*. A winner rides through the pullback instead of being sold low and bought back higher.
- **Size — one equal slice per symbol (equity / 7)**, never over the 25% weight cap, never over the cash on hand. Sells go first in a reply, then buys; a sell's proceeds never fund a buy of the same reply.
- **Slippage cap 300 bps.** Hourly gaps past 1% are common on these coins, and a 100 bps cap refused too many fills on fast moves.

Everything is deterministic — plain float math over the closes in arrival order, no clock, no environment, no randomness — so two runs of the same bars give byte-identical replies.

## Parameters

| Parameter | Value |
|---|---|
| Fast EMA | 8 bars |
| Slow EMA | 21 bars |
| Slow-trend lookback | 6 bars |
| Momentum confirmation | 6-bar return > 0 |
| Entry window after a cross | 3 bars |
| Hard stop | -8% of cost |
| Max slippage | 300 bps (3%) |
| Slice size | equity / 7, capped at 25% of equity |

## Results on the dev window (136 hourly bars, 28 Oct – 2 Nov 2026)

Graded locally with the kit (`grade-local --driver process`), run twice with identical action logs:

| Entry | Net PnL (USDC) | Trades | Max drawdown |
|---|---|---|---|
| **Tiny Trend Rider (this bot)** | **-141.34** | 22 | 2.19% |
| House ema-cross baseline | -173.04 | 28 | 2.51% |
| House momentum baseline | -268.23 | 19 | 5.66% |
| House buy-and-hold baseline | -869.34 | 7 | 10.92% |

The dev window fell the whole week (every symbol -3% to -14%), so every entry lost money — this bot lost the least by staying mostly in cash and only buying confirmed up-moves. On the mirrored (rising) window it scores +1.55 USDC while ema-cross scores -67.51. The final window (3 days, 27–30 Nov 2026) is recorded after entries close; the same rules apply there with no tuning to it.

Robustness was checked on six windows (dev, mirrored dev, both halves, and two 72-bar slices): the bot beats the house ema-cross baseline on five of six and beats buy-and-hold on every falling window, with max drawdown under 5.4% everywhere (guardrail: 25%) and at least 8 trades on every window (guardrail: 8).

## How to run

```sh
# The kit grades it on the dev window (needs node for the kit CLI):
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot . --driver process
```

The grader runs `python3 -P -s main.py`; the bot reads JSON lines on stdin and writes one reply line per batch on stdout (diagnostics go to stderr).
