"""Tiny trend rider for the make.fun bars-replay/v1 harness.

One program, standard library only. It speaks the harness line protocol on
stdin/stdout: the harness sends one JSON line at a time -- a hello first,
then one batch per hourly bar, then done -- and this bot answers every batch
with exactly one reply line echoing the batch seq and carrying its orders.

Strategy (per symbol, long only, no leverage, no shorts):
  * EMA(8) over closes vs EMA(21): the trend filter. A symbol is only
    bought when its slow average is RISING (slow now above slow 6 bars
    ago), so a dead-cat bounce inside a downtrend never triggers an entry.
  * Enter on the fast average crossing above the slow one, with a 3-bar
    entry window so a fill the market refuses (a gap past the slippage
    cap) is retried while the signal still holds. The 6-bar return must
    also be positive: buy strength, not noise.
  * Exit on a hard stop (-8% of cost), or when the slow average turns
    down while the trade holds no profit. A winner rides through the
    pullback instead of being sold low and bought back higher.
  * Size: one equal slice per symbol (equity / 7), never over the 25%
    weight cap, never over the cash on hand. Sells go first in a reply,
    then buys; a sell's proceeds never fund a buy of the same reply.
  * Slippage cap 300 bps: hourly gaps past 1% are common on these coins,
    and a 100 bps cap refused too many fills on fast moves.

Everything is deterministic: plain float math over the closes in arrival
order, no clock, no environment, no randomness, so two runs of the same
bars give byte-identical replies.
"""

import json
import sys

PROTOCOL = "agent-fun/bars-replay/v1"
FAST_SPAN = 8
SLOW_SPAN = 21
SLOW_TREND_BARS = 6
ROC_BARS = 6
ENTRY_WINDOW = 3
HARD_STOP_NUM = 92          # exit when value <= 92% of cost
MAX_SLIPPAGE_BPS = 300      # 3% past the close the order was decided on
BPS = 10000


class Series:
    """One symbol's bar history and indicators, updated bar by bar."""

    def __init__(self):
        self.closes = []
        self.fast = None      # EMA(8) of closes, None until seeded
        self.slow = None      # EMA(21) of closes, None until seeded
        self.slow_hist = []   # slow values, one per bar once seeded
        self._fast_warm = []
        self._slow_warm = []
        self.n_bars = 0
        self.cross_bar = None  # bar index of the latest fast-over-slow cross

    def push(self, bar):
        close = float(int(bar["c"]))
        self.closes.append(close)
        self.n_bars += 1
        prev_gap = None
        if self.fast is not None and self.slow is not None:
            prev_gap = self.fast - self.slow
        self._step_ema(self._fast_warm, FAST_SPAN, close, "fast")
        self._step_ema(self._slow_warm, SLOW_SPAN, close, "slow")
        if self.slow is not None:
            self.slow_hist.append(self.slow)
        if (self.fast is not None and self.slow is not None
                and prev_gap is not None):
            gap = self.fast - self.slow
            if gap > 0 and prev_gap <= 0:
                self.cross_bar = self.n_bars
        return close

    def _step_ema(self, warm, span, close, which):
        value = self.fast if which == "fast" else self.slow
        if value is not None:
            value += (2.0 / (span + 1)) * (close - value)
        else:
            warm.append(close)
            if len(warm) == span:
                value = sum(warm) / span
        if which == "fast":
            self.fast = value
        else:
            self.slow = value

    def roc(self):
        """Return over the last ROC_BARS bars, None without the history."""
        if len(self.closes) <= ROC_BARS:
            return None
        old = self.closes[-1 - ROC_BARS]
        return (self.closes[-1] - old) / old

    def slow_rising(self):
        """True when the slow average is above where it was SLOW_TREND_BARS ago."""
        if len(self.slow_hist) <= SLOW_TREND_BARS:
            return False
        return self.slow_hist[-1] > self.slow_hist[-1 - SLOW_TREND_BARS]

    def slow_falling(self):
        """True when the slow average is below where it was SLOW_TREND_BARS ago."""
        if len(self.slow_hist) <= SLOW_TREND_BARS:
            return False
        return self.slow_hist[-1] < self.slow_hist[-1 - SLOW_TREND_BARS]

    def entry_open(self):
        """True on the cross bar and ENTRY_WINDOW bars after it."""
        if self.cross_bar is None:
            return False
        return 0 <= self.n_bars - self.cross_bar <= ENTRY_WINDOW


class Bot:
    """Holds the hello, the account, and one Series per symbol."""

    def __init__(self, hello):
        self.hello = hello
        self.symbols = hello["symbols"]            # universe order, kept
        self.scales = {s["symbol"]: s for s in self.symbols}
        self.quote_scale = hello["capital"]["quote_scale"]
        self.caps = hello["caps"]
        self.fees = hello["fees"]
        self.warmup_bars = hello.get("warmup_bars", 0)
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = self.cash
        self.positions = {}                        # symbol -> (qty, cost)
        self.series = {s["symbol"]: Series() for s in self.symbols}
        self.seq = -1
        self.next_id = 1
        self.trades = 0

    def warming_up(self):
        return self.warmup_bars > 0 and self.seq < self.warmup_bars

    def value(self, symbol, qty, price=None):
        """What qty is worth in quote units at price (default latest close)."""
        s = self.scales[symbol]
        if price is None:
            series = self.series[symbol]
            if not series.closes:
                return 0
            price = series.closes[-1]
        return (qty * int(price) * (10 ** self.quote_scale)
                // (10 ** (s["qty_scale"] + s["price_scale"])))

    def apply_batch(self, batch):
        self.seq = batch["seq"]
        for bar in batch["bars"]:
            series = self.series.get(bar["symbol"])
            if series is not None:
                series.push(bar)
        account = batch["account"]
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {p["symbol"]: (int(p["qty"]),
                                       int(p["cost_quote_units"]))
                          for p in account["positions"]}

    def decide(self):
        """Orders for this batch: sells first, then buys, universe order."""
        if self.warming_up():
            return []
        orders = []
        spent = 0
        for symbol in [s["symbol"] for s in self.symbols]:
            held = self.positions.get(symbol)
            if held is not None and self._should_exit(symbol, held):
                qty, _cost = held
                orders.append(self._order("sell", symbol, qty))
        for symbol in [s["symbol"] for s in self.symbols]:
            if symbol in self.positions:
                continue
            series = self.series[symbol]
            if not series.entry_open():
                continue
            if not series.slow_rising():
                continue
            r = series.roc()
            if r is None or r <= 0:
                continue
            room = (self.caps["max_weight_bps"] * self.equity // BPS
                    - self.value(symbol, 0))
            size = min(self.equity // len(self.symbols), room,
                       self.cash - spent)
            if size < int(self.fees["min_notional_quote_units"]):
                continue
            orders.append(self._order("buy", symbol, size))
            spent += size
        if len(orders) > self.caps["max_orders_per_bar"]:
            orders = orders[:self.caps["max_orders_per_bar"]]
        self.trades += len(orders)
        return orders

    def _should_exit(self, symbol, held):
        qty, cost = held
        series = self.series[symbol]
        if series.closes:
            value = self.value(symbol, qty)
            if value * 100 <= cost * HARD_STOP_NUM:
                return True
            # Slow trend turned down: exit only when the trade holds no
            # profit -- a winner rides through the pullback instead of
            # being sold low and bought back higher.
            if series.slow_falling() and value <= cost:
                return True
        return False

    def _order(self, side, symbol, amount):
        cid = "o%d" % self.next_id
        self.next_id += 1
        if side == "buy":
            return {"side": "buy", "client_id": cid, "symbol": symbol,
                    "notional_quote_units": str(amount),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS}
        return {"side": "sell", "client_id": cid, "symbol": symbol,
                "qty": str(amount),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}


def main():
    bot = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                raise SystemExit("harness speaks another protocol")
            bot = Bot(msg)
            print("hello: warmup=%d symbols=%d cash=%s" % (
                bot.warmup_bars, len(bot.symbols),
                msg["capital"]["quote_units"]), file=sys.stderr)
        elif kind == "batch":
            bot.apply_batch(msg)
            orders = bot.decide()
            reply = json.dumps({"type": "reply", "seq": msg["seq"],
                                "orders": orders},
                               separators=(",", ":"), ensure_ascii=False)
            sys.stdout.write(reply + "\n")
            sys.stdout.flush()
        elif kind == "done":
            print("done: seq=%d equity=%d trades=%d" % (
                msg["seq"], bot.equity, bot.trades), file=sys.stderr)
            return 0
        else:
            raise SystemExit("unknown message type %r" % (kind,))
    raise SystemExit("input ended before done")


if __name__ == "__main__":
    sys.exit(main())
