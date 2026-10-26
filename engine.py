"""Closed-candle rules. No MT5 dependency; scores are priorities, not win probabilities."""
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import numpy as np
import pandas as pd

SECONDS = {"M5": 300, "H1": 3600}
SETUPS = {"session_pullback", "breakout_retest", "sweep_rejection", "inside_breakout", "impulse", "ema_reclaim", "scalp_reclaim"}


@dataclass(frozen=True)
class Signal:
    timeframe: str
    bar_open: int
    direction: int
    setup: str
    reference: float
    stop: float
    atr: float
    priority: int

    @property
    def key(self):
        # At most one attempted trade per timeframe candle, irrespective of setup.
        return "CONNECTIVITY_TEST_ONCE" if self.setup == "connectivity_test" else f"{self.timeframe}:{self.bar_open}"

    @property
    def closes_at(self):
        return self.bar_open + SECONDS[self.timeframe]


def in_session(timestamp, end_hour=11):
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    for zone in ("Europe/London", "America/New_York"):
        local = dt.astimezone(ZoneInfo(zone))
        if local.weekday() < 5 and 8 <= local.hour < end_hour:
            return True
    return False


def indicators(rates):
    d = pd.DataFrame(rates).copy()
    required = {"time", "open", "high", "low", "close"}
    if not required.issubset(d) or len(d) < 250:
        raise ValueError("Need at least 250 OHLC candles")
    values = d[list(required)].to_numpy(dtype=float)
    if not np.isfinite(values).all() or not d.time.is_monotonic_increasing or d.time.duplicated().any():
        raise ValueError("Invalid/unsorted candles")
    if ((d.high < d[["open", "close", "low"]].max(axis=1)) |
            (d.low > d[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Invalid OHLC range")
    for span in (20, 50):
        d[f"ema{span}"] = d.close.ewm(span=span, adjust=False).mean()
    previous = d.close.shift(1)
    tr = pd.concat([d.high - d.low, (d.high - previous).abs(),
                    (d.low - previous).abs()], axis=1).max(axis=1).to_numpy()
    # Wilder RMA, seeded with SMA to match ta.atr's convention (after warm-up).
    atr = np.full(len(d), np.nan)
    atr[13] = tr[:14].mean()
    for i in range(14, len(d)):
        atr[i] = (atr[i - 1] * 13 + tr[i]) / 14
    d["atr"] = atr
    return d


# GOLD_LAB_ALL_HOURS_V1
def signals(d, hourly, timeframe, enabled, session_end_hour=11, session_filter_enabled=True):
    c, p = d.iloc[-1], d.iloc[-2]
    if timeframe not in SECONDS or (session_filter_enabled and not in_session(int(c.time), session_end_hour)):
        return []
    a = float(c.atr)
    if not math.isfinite(a) or a <= 0:
        return []
    if timeframe == "M5":
        # Pine v1.1 uses the last H1 completed at the START of the M5 bar.
        eligible = hourly[hourly.time + 3600 <= c.time]
        if eligible.empty:
            return []
        h = eligible.iloc[-1]
        if c.time - (h.time + 3600) >= 3600:
            return []
    else:
        h = c
    bias = 1 if h.close > h.ema20 > h.ema50 else -1 if h.close < h.ema20 < h.ema50 else 0
    if not bias:
        return []
    prior = d.iloc[-7:-1]
    lo6, hi6 = float(d.low.iloc[-6:].min()), float(d.high.iloc[-6:].max())
    bullish, bearish = c.close > c.open, c.close < c.open
    candidates = []

    def add(name, direction, stop, priority):
        if name not in enabled or direction != bias:
            return
        distance = direction * (c.close - stop)
        if not 0 < distance <= 4 * a:
            return
        candidates.append(Signal(timeframe, int(c.time), direction, name,
                                 float(c.close), float(stop), a, priority))

    # 1. Port of Gold Session Lab v1.1: touch EMA20 then break 6-bar range.
    touches = d.iloc[-5:-1]
    if c.ema20 > c.ema50 and bullish and c.close > prior.high.max() and (touches.low - touches.ema20).min() <= 0:
        add("session_pullback", 1, c.close - max(c.close - lo6, 1.5 * a), 50)
    if c.ema20 < c.ema50 and bearish and c.close < prior.low.min() and (touches.high - touches.ema20).max() >= 0:
        add("session_pullback", -1, c.close + max(hi6 - c.close, 1.5 * a), 50)

    # 2. Prior candle breaks 20-bar range; current candle retests and rejects it.
    base = d.iloc[-22:-2]
    upper, lower = float(base.high.max()), float(base.low.min())
    if p.close > upper and c.low <= upper + 0.15 * a and c.close > upper and bullish:
        add("breakout_retest", 1, min(c.low, p.low) - 0.25 * a, 40)
    if p.close < lower and c.high >= lower - 0.15 * a and c.close < lower and bearish:
        add("breakout_retest", -1, max(c.high, p.high) + 0.25 * a, 40)

    # 3. Sweep the previous 12 extremes and close back inside, WITH hourly bias.
    sweep = d.iloc[-13:-1]
    bottom, top = float(sweep.low.min()), float(sweep.high.max())
    body = abs(c.close - c.open)
    if bullish and c.low < bottom and c.close > bottom and min(c.open, c.close) - c.low >= max(body, 0.25 * a):
        add("sweep_rejection", 1, c.low - 0.25 * a, 30)
    if bearish and c.high > top and c.close < top and c.high - max(c.open, c.close) >= max(body, 0.25 * a):
        add("sweep_rejection", -1, c.high + 0.25 * a, 30)

    # 4. Previous candle is inside its mother candle; current CLOSE breaks mother.
    mother = d.iloc[-3]
    inside = p.high < mother.high and p.low > mother.low
    if inside and bullish and c.close > mother.high and c.ema20 > c.ema50:
        add("inside_breakout", 1, min(p.low, c.low) - 0.25 * a, 20)
    if inside and bearish and c.close < mother.low and c.ema20 < c.ema50:
        add("inside_breakout", -1, max(p.high, c.high) + 0.25 * a, 20)

    # 5. Original impulse rule retained, with closed trend data and Wilder ATR.
    if body > 1.2 * a:
        if bullish:
            add("impulse", 1, c.low - 0.5 * a, 10)
        if bearish:
            add("impulse", -1, c.high + 0.5 * a, 10)
    # Medium continuation: reclaim EMA20 after a pullback, without requiring
    # the stronger six-bar breakout. Hourly bias, EMA slope and body still apply.
    if c.ema20 > c.ema50 and c.ema20 > p.ema20 and bullish and p.close <= p.ema20 and c.close > c.ema20 and body >= 0.3 * a:
        add("ema_reclaim", 1, c.close - max(c.close - min(c.low, p.low) + 0.2 * a, 1.2 * a), 25)
    if c.ema20 < c.ema50 and c.ema20 < p.ema20 and bearish and p.close >= p.ema20 and c.close < c.ema20 and body >= 0.3 * a:
        add("ema_reclaim", -1, c.close + max(max(c.high, p.high) - c.close + 0.2 * a, 1.2 * a), 25)
    # M5-only scalp: previous candle touches EMA20; current candle rejects and
    # closes beyond that candle's high/low. Shorter target, never a forced entry.
    if timeframe == "M5":
        if c.ema20 > c.ema50 and c.ema20 > p.ema20 and p.low <= p.ema20 and bullish and c.close > p.high and 0.25 * a <= body <= 1.2 * a:
            stop = c.close - max(c.close - min(c.low, p.low) + 0.2 * a, a)
            if c.close - stop <= 2.5 * a:
                add("scalp_reclaim", 1, stop, 15)
        if c.ema20 < c.ema50 and c.ema20 < p.ema20 and p.high >= p.ema20 and bearish and c.close < p.low and 0.25 * a <= body <= 1.2 * a:
            stop = c.close + max(max(c.high, p.high) - c.close + 0.2 * a, a)
            if stop - c.close <= 2.5 * a:
                add("scalp_reclaim", -1, stop, 15)
    return candidates


def floor_volume(raw, minimum, maximum, step):
    # Broker min is an origin, step is an increment. Never force a tiny size up.
    if not all(math.isfinite(v) and v > 0 for v in (raw, minimum, maximum, step)):
        return 0.0
    if raw < minimum or maximum < minimum:
        return 0.0
    n = math.floor((min(raw, maximum) - minimum) / step + 1e-9)
    result = round(minimum + n * step, 8)
    # Rounding allowance is only numerical noise, never a whole lot increment.
    return result if result <= raw + 1e-9 else 0.0


def choose(candidates):
    if not candidates:
        return None
    # Conflicting simultaneous signals cause a skip. H1 wins only a priority tie.
    if len({s.direction for s in candidates}) > 1:
        return None
    return max(candidates, key=lambda s: (s.priority, s.timeframe == "H1", s.bar_open))
