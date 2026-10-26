"""Gold Session Lab v4.1: Windows MT5 DEMO executor. See README before running."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import socket
import time
from datetime import datetime, timezone
from dataclasses import asdict
from engine import SECONDS, SETUPS, indicators, signals, in_session, floor_volume, choose
from storage import BASE, Store

# GOLD_LAB_CMD_PANEL_V1
from cmd_status import StatusPanel, install_console_colors, explain_setup

# GOLD_LAB_CAPITAL_GUARD_V1
from capital_guard import CapitalGuard, EntryBlocked
LOG = logging.getLogger("goldbot")


class FatalError(RuntimeError):
    pass


class Skip(RuntimeError):
    pass


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    # GOLD_LAB_CLOCK_FIX_V1
    offset = cfg.get("broker_time_offset_hours", 0)
    if type(offset) is not int or not -12 <= offset <= 14:
        raise FatalError("broker_time_offset_hours must be a whole number from -12 to 14")
    if type(cfg.get("account_login")) is not int or cfg["account_login"] <= 0:
        raise FatalError("Set account_login to your MT5 DEMO account number in config.json")
    if not cfg.get("account_server") or cfg["account_server"].startswith("REPLACE"):
        raise FatalError("Set account_server to the exact demo server shown in MT5")
    if "XAU" not in cfg.get("symbol", "").upper() and "GOLD" not in cfg.get("symbol", "").upper():
        raise FatalError("Select your broker's gold symbol explicitly")
    for flag in ("send_demo_orders", "require_tradingview_confirmation"):
        if type(cfg.get(flag)) is not bool:
            raise FatalError(f"{flag} must be true or false")
    if not 0 < cfg["risk_fraction"] <= 0.02 or not 0 < cfg["daily_loss_fraction"] <= 0.05:
        raise FatalError("Risk must be >0 and <=2%; daily loss threshold must be >0 and <=5%")
    if not 0 < cfg["rr"] <= 5 or not 1 <= cfg["max_daily_attempts"] <= 20:
        raise FatalError("Invalid reward/risk or daily attempt limit")
    if not 5 <= cfg["max_signal_age_seconds"] <= 60:
        raise FatalError("Signal freshness must be between 5 and 60 seconds")
    for name in ("max_spread_price", "max_spread_atr", "max_entry_drift_atr", "lock_port"):
        if not math.isfinite(cfg[name]) or cfg[name] <= 0:
            raise FatalError(f"Invalid {name}")
    if not 0 <= cfg["risk_reserve_fraction"] < 1 or cfg["commission_per_lot_round_trip"] < 0:
        raise FatalError("Invalid risk reserve or commission")
    if not set(cfg["enabled_setups"]).issubset(SETUPS) or not cfg["enabled_setups"]:
        raise FatalError("Unknown or empty enabled_setups")
    if cfg["require_tradingview_confirmation"] and not cfg.get("tv_ticker"):
        raise FatalError("Set tv_ticker to your TradingView ticker including exchange prefix")
    # Reject malformed blackouts before the main loop starts.
    for item in cfg["news_blackouts_utc"]:
        start, end = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in item]
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise FatalError("News blackout must contain two timezone-aware timestamps, start < end")
    cfg.setdefault("max_open_positions", 1)
    cfg.setdefault("max_open_risk_fraction", 0.02)
    cfg.setdefault("session_end_hour", 11)
    cfg.setdefault("scalp_rr", 1.3)
    cfg.setdefault("entry_cooldown_seconds", 30)
    if type(cfg["max_open_positions"]) is not int or not 1 <= cfg["max_open_positions"] <= 2:
        raise FatalError("max_open_positions must be 1 or 2")
    if not 0 < cfg["max_open_risk_fraction"] <= 0.02:
        raise FatalError("Combined open-risk fraction must be >0 and <=2%")
    if type(cfg["session_end_hour"]) is not int or not 9 <= cfg["session_end_hour"] <= 12:
        raise FatalError("Session end hour must be between 9 and 12 local time")
    if not 1 <= cfg["scalp_rr"] <= 2 or not 30 <= cfg["entry_cooldown_seconds"] <= 3600:
        raise FatalError("Invalid scalp RR or entry cooldown")
    cfg.setdefault("session_filter_enabled", True)
    if type(cfg["session_filter_enabled"]) is not bool:
        raise FatalError("session_filter_enabled must be true or false")
    if cfg.get("capital_guard_enabled", False):
        if type(cfg.get("capital_guard_installed_at")) not in (int,float) or cfg["capital_guard_installed_at"]<=0:
            raise FatalError("Invalid capital guard installation timestamp")
    cfg.setdefault("max_lot_size", 0.01)
    cfg.setdefault("dynamic_risk_enabled", True)
    cfg.setdefault("dashboard_enabled", True)
    cfg.setdefault("dashboard_refresh_seconds", 2)
    if not math.isfinite(cfg["max_lot_size"]) or not 0 < cfg["max_lot_size"] <= 0.01:
        raise FatalError("max_lot_size must be >0 and <=0.01 for this demo profile")
    if type(cfg["dynamic_risk_enabled"]) is not bool or type(cfg["dashboard_enabled"]) is not bool:
        raise FatalError("dynamic_risk_enabled and dashboard_enabled must be true or false")
    if not 2 <= cfg["dashboard_refresh_seconds"] <= 60:
        raise FatalError("dashboard_refresh_seconds must be between 2 and 60")
    return cfg


class Bot:
    def __init__(self, api, cfg, store):
        self.api, self.cfg, self.store = api, cfg, store
        self.scope = f'{cfg["account_server"]}:{cfg["account_login"]}:{cfg["symbol"]}:{cfg["magic"]}'
        self.pending = []
        self.protection = CapitalGuard(self)
        self.status_panel = StatusPanel()
        self.last_heartbeat = 0
        self.backoff = 2
        if store.unresolved(self.scope):
            store.put(self.scope, "halt", "Unresolved send journal. Reconcile MT5 account before clearing.")

    def identity(self):
        a = self.api.account_info()
        if a is None:
            raise Skip("Account lookup failed")
        if a.login != self.cfg["account_login"] or a.server != self.cfg["account_server"]:
            raise FatalError("MT5 account/server changed; refusing this connection")
        if a.trade_mode != self.api.ACCOUNT_TRADE_MODE_DEMO:
            raise FatalError("This build only accepts DEMO accounts")
        return a

    def connect(self):
        t = self.api.terminal_info()
        if t is None or not t.connected:
            self.api.shutdown()
            kwargs = {"timeout": 10000}
            path = self.cfg.get("terminal_path")
            ok = self.api.initialize(path, **kwargs) if path else self.api.initialize(**kwargs)
            t = self.api.terminal_info()
            if not ok or t is None or not t.connected:
                raise Skip(f"MT5 disconnected: {self.api.last_error()}")
            LOG.info("MT5 reconnected; checking pinned DEMO account")
        self.identity()
        if not self.api.symbol_select(self.cfg["symbol"], True):
            raise Skip("symbol_select failed; check exact broker symbol")
        self.backoff = 2

    def daily_gate(self, account, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        key = "day:" + day
        state = self.store.get(self.scope, key)
        if state is None:
            if not math.isfinite(account.equity) or account.equity <= 0:
                raise Skip("Invalid equity")
            state = {"reference_equity": account.equity, "blocked": False}
            self.store.put(self.scope, key, state)
        if account.equity <= state["reference_equity"] * (1 - self.cfg["daily_loss_fraction"]):
            state["blocked"] = True
            self.store.put(self.scope, key, state)
        if state["blocked"]:
            raise Skip("Daily loss gate latched until next UTC day; existing SL/TP stay at broker")
        midnight = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if self.store.attempt_count(self.scope, midnight) >= self.cfg["max_daily_attempts"]:
            raise Skip("Daily order-attempt limit reached")

    # GOLD_LAB_ALL_HOURS_V1
    def session_allowed(self, now):
        return not self.cfg.get("session_filter_enabled", True) or in_session(now, self.cfg.get("session_end_hour", 11))

    def gate(self, now, check_session=True):
        try:
            self.protection.entry_gate(now)
        except EntryBlocked as exc:
            raise Skip(str(exc)) from exc
        halt = self.store.get(self.scope, "halt")
        if self.store.unresolved(self.scope):
            raise FatalError("Unresolved order-send journal; reconcile MT5 before restarting orders")
        if halt:
            raise FatalError("Execution HALTED: " + halt)
        if (BASE / "PAUSE").exists():
            raise Skip("PAUSE file present")
        if check_session and not self.session_allowed(now):
            raise Skip("Outside configured London/NY window")
        for start, end in self.cfg["news_blackouts_utc"]:
            begin = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
            finish = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
            if begin <= now <= finish:
                raise Skip("Configured news blackout")
        t = self.api.terminal_info()
        if t is None or not t.connected:
            raise Skip("Connection lost before order")
        a = self.identity()
        if not a.trade_allowed or not a.trade_expert or not t.trade_allowed or t.tradeapi_disabled:
            raise Skip(f"Trading disabled: account trade_allowed={a.trade_allowed}, trade_expert={a.trade_expert}; terminal trade_allowed={t.trade_allowed}, tradeapi_disabled={t.tradeapi_disabled}")
        self.daily_gate(a, now)
        # Include manual trades and pending orders to avoid netting/hedging collisions.
        pos = self.api.positions_get(symbol=self.cfg["symbol"])
        orders = self.api.orders_get(symbol=self.cfg["symbol"])
        if pos is None or orders is None:
            raise Skip("Position/order lookup failed; new orders blocked")
        if len(orders):
            raise Skip("Existing gold pending order")
        if any(p.magic != self.cfg["magic"] for p in pos):
            raise Skip("Existing gold position from another bot/manual trade")
        limit = self.cfg.get("max_open_positions", 1)
        # Netting accounts cannot hold independent M5/H1 positions on one symbol.
        if getattr(a, "margin_mode", 0) != 2:
            limit = 1
        if len(pos) >= limit:
            raise Skip(f"Open gold position limit reached ({limit}); netting accounts are limited to one")
        last_attempt = self.store.get(self.scope, "last_entry_attempt", 0)
        if now - last_attempt < self.cfg.get("entry_cooldown_seconds", 30):
            raise Skip("Entry cooldown active")
        return a

    def allocation(self, a, s, now):
        """Account-currency budget after existing gold stop exposure and daily headroom."""
        positions = self.api.positions_get(symbol=self.cfg["symbol"])
        if positions is None:
            raise Skip("Position risk lookup failed")
        tick = self.api.symbol_info_tick(self.cfg["symbol"])
        if tick is None:
            raise Skip("No quote for combined exposure calculation")
        quote_time = tick.time_msc / 1000 if tick.time_msc else tick.time
        quote_time -= int(self.cfg.get("broker_time_offset_hours", 0) * 3600)
        if not -2 <= now - quote_time <= 10 or not 0 < tick.bid <= tick.ask:
            raise Skip("Stale/invalid quote for combined exposure")
        open_risk = 0.0
        for p in positions:
            if p.magic != self.cfg["magic"]:
                raise Skip("Foreign gold position; exposure unknown")
            direction = 1 if p.type == self.api.ORDER_TYPE_BUY else -1
            if direction != s.direction:
                raise Skip("Opposite-direction gold position already open")
            parts = p.comment.split()
            if len(parts) < 2 or parts[0] not in ("GL2", "GL3") or parts[1] not in SECONDS:
                raise Skip("Cannot identify existing position timeframe")
            if parts[1] == s.timeframe:
                raise Skip(f"One {s.timeframe} position already open")
            mark = tick.bid if direction == 1 else tick.ask
            if p.sl <= 0 or direction * (mark - p.sl) <= 0:
                raise Skip("Existing position has missing/crossed stop; risk cannot be allocated")
            pnl = self.api.order_calc_profit(p.type, self.cfg["symbol"], p.volume, mark, p.sl)
            if pnl is None or not math.isfinite(pnl):
                raise Skip("Cannot calculate existing stop exposure")
            # Add configured round-trip commission conservatively, even if entry fees were paid.
            open_risk += max(0.0, -pnl) + p.volume * self.cfg["commission_per_lot_round_trip"]
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        state = self.store.get(self.scope, "day:" + day)
        if state is None:
            raise Skip("Daily risk reference unavailable")
        headroom = a.equity - state["reference_equity"] * (1 - self.cfg["daily_loss_fraction"])
        overall_room = min(a.equity * self.cfg.get("max_open_risk_fraction", 0.02), headroom) - open_risk
        budget = min(a.equity * self.cfg["risk_fraction"], max(0.0, overall_room))
        if budget <= 0:
            raise Skip("No remaining combined/daily stop-risk budget")
        return budget, open_risk

    def candles(self, tf, now):
        api_tf = self.api.TIMEFRAME_M5 if tf == "M5" else self.api.TIMEFRAME_H1
        # Position 1 excludes the forming candle. Validate expected closed bar too.
        rates = self.api.copy_rates_from_pos(self.cfg["symbol"], api_tf, 1, 600)
        if rates is None:
            raise Skip("Candle lookup failed: " + tf)
        d = indicators(rates)
        # Convert feed-clock bar labels to UTC BEFORE freshness/session/signal checks.
        d["time"] = d["time"] - int(self.cfg.get("broker_time_offset_hours", 0) * 3600)
        seconds = SECONDS[tf]
        expected_open = int(now // seconds) * seconds - seconds
        if int(d.time.iloc[-1]) != expected_open:
            raise Skip(f"Stale {tf}: latest UTC open={int(d.time.iloc[-1])}, expected={expected_open}; check feed clock offset/history")
        return d

    def scan(self, now):
        h1 = self.candles("H1", now)
        m5 = self.candles("M5", now)
        for tf, d in (("H1", h1), ("M5", m5)):
            bar = int(d.time.iloc[-1])
            age = now - (bar + SECONDS[tf])
            if not 0 <= age <= self.cfg["max_signal_age_seconds"]:
                continue
            key = f"{tf}:{bar}"
            if not self.store.claim_scan(self.scope, key):
                continue
            found = signals(d, h1, tf, set(self.cfg["enabled_setups"]), self.cfg.get("session_end_hour", 11), self.cfg.get("session_filter_enabled", True))
            self.pending.extend(found)
            LOG.info("Closed %s candle %s: %s", tf, datetime.fromtimestamp(bar, timezone.utc).isoformat(),
                     ", ".join(f"{s.setup}:{'BUY' if s.direction == 1 else 'SELL'}" for s in found) or ("OUTSIDE SESSION" if not self.session_allowed(bar) else "no trigger matched hourly bias and enabled rules"))

    def filling(self, info):
        # Symbol bit flags differ from ORDER_FILLING enumeration values.
        # These SYMBOL_* bit flags are not exposed by some Python MT5 builds.
        if info.filling_mode & 2:  # SYMBOL_FILLING_IOC
            return self.api.ORDER_FILLING_IOC
        if info.filling_mode & 1:  # SYMBOL_FILLING_FOK
            return self.api.ORDER_FILLING_FOK
        if info.trade_exemode != 2:  # SYMBOL_TRADE_EXECUTION_MARKET
            return self.api.ORDER_FILLING_RETURN
        raise Skip("No supported broker filling policy")

    def market_risk_factor(self, s, now):
        if not self.cfg.get("dynamic_risk_enabled", False):
            return 1.0
        d = self.candles(s.timeframe, now)
        row = d[d.time == s.bar_open]
        if len(row) != 1:
            raise Skip("Risk context does not match the signal candle")
        c = row.iloc[0]
        baseline = float(d[d.time < s.bar_open].atr.tail(50).median())
        if not math.isfinite(baseline) or baseline <= 0:
            raise Skip("No valid volatility baseline for adaptive risk")
        ratio = s.atr / baseline
        gap = abs(float(c.ema20 - c.ema50)) / s.atr
        extension = abs(float(c.close - c.ema20)) / s.atr
        if not all(math.isfinite(v) for v in (ratio, gap, extension)):
            raise Skip("Invalid market risk context")
        if ratio >= 2.0:
            raise Skip("ATR at least twice its recent median; avoid elevated-volatility entry")
        if gap < 0.25:
            raise Skip("EMA20/50 separation below 0.25 ATR; trend filter too weak")
        if extension > 1.5:
            raise Skip("Price more than 1.5 ATR from EMA20; avoid chasing")
        factor = 0.5 if ratio >= 1.5 else 1.0
        if s.setup == "sweep_rejection":
            factor *= 0.75
        LOG.info("RISK CONTEXT | %s %s | ATR/median=%.2f EMA-gap=%.2f ATR extension=%.2f ATR | budget multiplier=%.2f | max lots=%.3f",
                 s.timeframe, s.setup, ratio, gap, extension, factor, self.cfg.get("max_lot_size", .01))
        return factor

    def prepare(self, s, now):
        a = self.gate(now)
        if not 0 <= now - s.closes_at <= self.cfg["max_signal_age_seconds"]:
            raise Skip("Expired signal")
        symbol = self.cfg["symbol"]
        i, tick = self.api.symbol_info(symbol), self.api.symbol_info_tick(symbol)
        if i is None or tick is None:
            raise Skip("Missing symbol or tick")
        quote_time = tick.time_msc / 1000 if tick.time_msc else tick.time
        quote_time -= int(self.cfg.get("broker_time_offset_hours", 0) * 3600)
        if not -2 <= now - quote_time <= 10 or not 0 < tick.bid <= tick.ask:
            raise Skip("Stale/invalid quote")
        spread = tick.ask - tick.bid
        if spread > self.cfg["max_spread_price"] or spread > self.cfg["max_spread_atr"] * s.atr:
            raise Skip(f"Spread too wide: actual={spread:.4f}; absolute cap={self.cfg['max_spread_price']:.4f}; ATR cap={self.cfg['max_spread_atr'] * s.atr:.4f}")
        if s.setup == "scalp_reclaim" and spread > 0.10 * s.atr:
            raise Skip("Scalp spread exceeds 10% of ATR")
        side = self.api.ORDER_TYPE_BUY if s.direction == 1 else self.api.ORDER_TYPE_SELL
        price = tick.ask if s.direction == 1 else tick.bid
        drift_cap = min(self.cfg["max_entry_drift_atr"], 0.15) if s.setup == "scalp_reclaim" else self.cfg["max_entry_drift_atr"]
        if abs(price - s.reference) > drift_cap * s.atr:
            raise Skip(f"Price moved too far from signal close: drift={abs(price-s.reference):.4f}; cap={drift_cap * s.atr:.4f}")
        if i.trade_mode != 4:  # SYMBOL_TRADE_MODE_FULL
            raise Skip("Symbol not fully tradeable")
        step = i.trade_tick_size
        if step <= 0 or i.point <= 0:
            raise Skip("Invalid broker tick size")
        stop_units = s.stop / step
        sl = (math.floor(stop_units) if s.direction == 1 else math.ceil(stop_units)) * step
        sl = round(sl, i.digits)
        distance = s.direction * (price - sl)
        if not 0 < distance <= 4 * s.atr:
            raise Skip("Invalid/overextended stop")
        target_rr = self.cfg.get("scalp_rr", 1.3) if s.setup == "scalp_reclaim" else self.cfg["rr"]
        raw_tp = price + s.direction * target_rr * distance
        tp = round((math.ceil(raw_tp / step) if s.direction == 1 else math.floor(raw_tp / step)) * step, i.digits)
        # Broker checks buy protection relative to bid, sell protection to ask.
        basis = tick.bid if s.direction == 1 else tick.ask
        minimum_distance = max(i.trade_stops_level * i.point, step)
        if s.direction * (basis - sl) < minimum_distance or s.direction * (tp - basis) < minimum_distance:
            raise Skip("Stop/target violates broker distance")
        adverse_entry = price + s.direction * self.cfg["deviation_points"] * i.point
        profit = self.api.order_calc_profit(side, symbol, 1.0, adverse_entry, sl)
        if profit is None or not math.isfinite(profit) or profit >= 0:
            raise Skip("Cannot calculate stop risk in account currency")
        unit_risk = abs(profit) + self.cfg["commission_per_lot_round_trip"]
        budget, open_risk = self.allocation(a, s, now)
        budget *= self.market_risk_factor(s, now)
        usable = budget * (1 - self.cfg["risk_reserve_fraction"])
        max_lots = min(i.volume_max, self.cfg.get("max_lot_size", i.volume_max))
        if max_lots < i.volume_min:
            raise Skip(f"Broker minimum lot {i.volume_min} exceeds configured maximum {max_lots}; no forced entry")
        volume = floor_volume(min(usable / unit_risk, max_lots), i.volume_min, max_lots, i.volume_step)
        if volume <= 0:
            raise Skip(f"Minimum lot exceeds risk budget: minimum lots={i.volume_min}; estimated minimum loss including configured costs={i.volume_min * unit_risk:.2f}; usable budget={usable:.2f}; full risk budget={budget:.2f} (account currency)")
        margin = self.api.order_calc_margin(side, symbol, volume, price)
        if margin is None or not math.isfinite(margin) or margin < 0 or margin > a.margin_free * 0.8:
            raise Skip("Insufficient free margin or failed margin lookup")
        request = {"action": self.api.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
                   "type": side, "price": float(price), "sl": float(sl), "tp": float(tp),
                   "deviation": self.cfg["deviation_points"], "magic": self.cfg["magic"],
                   "comment": f"GL3 {s.timeframe} {s.setup}"[:31],
                   "type_time": self.api.ORDER_TIME_GTC, "type_filling": self.filling(i)}
        check = self.api.order_check(request)
        if check is None or check.retcode != 0:
            raise Skip("Broker preflight rejected: " + (str(self.api.last_error()) if check is None else str(check.comment)))
        return request, volume * unit_risk, budget

    def execute(self, s, now):
        req, estimated_risk, budget = self.prepare(s, now)
        LOG.info("%s %s %s: entry=%s SL=%s TP=%s lots=%s estimated risk=%.2f / budget=%.2f",
                 "DEMO" if self.cfg["send_demo_orders"] else "DRY RUN", s.timeframe, s.setup,
                 req["price"], req["sl"], req["tp"], req["volume"], estimated_risk, budget)
        if not self.cfg["send_demo_orders"]:
            return
        # Recheck identity, open positions and limits immediately before send.
        current = time.time()
        latest_account = self.gate(current)
        latest_budget, _ = self.allocation(latest_account, s, current)
        if estimated_risk > latest_budget:
            raise Skip("Equity changed during preflight; risk budget exceeded")
        if not 0 <= current - s.closes_at <= self.cfg["max_signal_age_seconds"]:
            raise Skip("Signal expired during preflight")
        latest = self.api.symbol_info_tick(self.cfg["symbol"])
        info = self.api.symbol_info(self.cfg["symbol"])
        if latest is None or info is None:
            raise Skip("Quote unavailable immediately before send")
        quote_time = latest.time_msc / 1000 if latest.time_msc else latest.time
        quote_time -= int(self.cfg.get("broker_time_offset_hours", 0) * 3600)
        latest_price = latest.ask if s.direction == 1 else latest.bid
        if not -2 <= current - quote_time <= 10 or abs(latest_price - req["price"]) > self.cfg["deviation_points"] * info.point:
            raise Skip("Quote changed/stale during preflight; no chase")
        if self.cfg["require_tradingview_confirmation"] and not self.store.confirmed(
                self.cfg["tv_ticker"], s, time.time(), self.cfg["max_signal_age_seconds"]):
            raise Skip("TradingView confirmation missing/expired")
        # Persist BEFORE sending. A crash/timeout may leave a broker fill: never blindly resend.
        if not self.store.reserve(self.scope, s.key, {"signal": asdict(s), "request": req}):
            raise Skip("This candle already has an order attempt")
        self.store.put(self.scope, "last_entry_attempt", current)
        try:
            result = self.api.order_send(req)
            if result is None:
                raise RuntimeError(f"No order result: {self.api.last_error()}")
            payload = {"retcode": result.retcode, "order": result.order, "deal": result.deal,
                       "volume": result.volume, "price": result.price, "comment": result.comment}
            successful = (self.api.TRADE_RETCODE_DONE, self.api.TRADE_RETCODE_DONE_PARTIAL)
            if result.retcode in successful:
                self.store.finish(self.scope, s.key, "filled", payload)
                LOG.info("Broker fill: %s (partial fills are NOT topped up)", payload)
                return
            # Only explicit rejections are safe to classify as unfilled.
            rejection_names = ("REQUOTE", "REJECT", "INVALID", "INVALID_VOLUME", "INVALID_PRICE",
                               "INVALID_STOPS", "TRADE_DISABLED", "MARKET_CLOSED", "NO_MONEY",
                               "PRICE_CHANGED", "PRICE_OFF", "INVALID_FILL", "TOO_MANY_REQUESTS",
                               "CLIENT_DISABLES_AT", "SERVER_DISABLES_AT", "LIMIT_VOLUME", "LIMIT_ORDERS")
            rejections = {getattr(self.api, "TRADE_RETCODE_" + n, -1) for n in rejection_names}
            if result.retcode in rejections:
                self.store.finish(self.scope, s.key, "rejected", payload)
                LOG.warning("Order rejected; no retry for this candle: %s", payload)
                return
            raise RuntimeError("Ambiguous broker result: " + str(payload))
        except Exception as exc:
            self.store.put(self.scope, "halt", str(exc))
            raise FatalError("Uncertain order outcome. Check MT5 positions AND history; new orders halted.") from exc

    def step(self, now):
        self.connect()
        self.protection.tick(now)
        self.status_panel.poll(self, now)
        if self.store.get(self.scope, "halt") or self.store.unresolved(self.scope):
            raise FatalError("Execution halt is active; reconcile MT5 and inspect --status before resuming")
        # Daily equity drawdown is observed even while positions exist/off-session.
        self.daily_gate(self.identity(), now)
        if self.store.get(self.scope,"risk_pause"):
            raise Skip("Emergency entry pause persists; review then python guard_control.py --resume")
        self.scan(now)
        self.pending = [s for s in self.pending if 0 <= now - s.closes_at <= self.cfg["max_signal_age_seconds"]]
        available = self.pending
        if self.cfg["require_tradingview_confirmation"]:
            available = [s for s in available if self.store.confirmed(self.cfg["tv_ticker"], s, now,
                                                                     self.cfg["max_signal_age_seconds"])]
        if available:
            self.pending = []
            if len({s.direction for s in available}) > 1:
                LOG.info("Conflicting fresh directions; batch skipped")
            else:
                by_tf = {}
                for candidate in available:
                    old = by_tf.get(candidate.timeframe)
                    if old is None or candidate.priority > old.priority:
                        by_tf[candidate.timeframe] = candidate
                for selected in sorted(by_tf.values(), key=lambda s: (s.priority, s.timeframe == "H1"), reverse=True):
                    LOG.info("SETUP SELECTED | %s %s %s | reference=%s stop=%s ATR=%s",
                             selected.timeframe, selected.setup, "BUY" if selected.direction == 1 else "SELL",
                             selected.reference, selected.stop, selected.atr)
                    LOG.info("ENTRY RULE | %s", explain_setup(selected.setup))
                    try:
                        self.execute(selected, now)
                        break
                    except Skip as exc:
                        LOG.warning("SETUP SKIPPED | %s %s | %s", selected.timeframe, selected.setup, exc)
        if now - self.last_heartbeat >= 60:
            LOG.info("Heartbeat: DEMO connected | session=%s | next M5 close UTC=%s | next H1 close UTC=%s | pending=%d",
                     "UNRESTRICTED" if not self.cfg.get("session_filter_enabled", True) else ("OPEN" if self.session_allowed(now) else "CLOSED"),
                     datetime.fromtimestamp((int(now)//300+1)*300, timezone.utc).isoformat(),
                     datetime.fromtimestamp((int(now)//3600+1)*3600, timezone.utc).isoformat(), len(self.pending))
            self.last_heartbeat = now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--status", action="store_true", help="Print persistent halt/day state without MT5")
    parser.add_argument("--clear-halt", action="store_true", help="After manually reconciling MT5 only")
    args = parser.parse_args()
    handlers = [logging.StreamHandler(), RotatingFileHandler(BASE / "bot.log", maxBytes=2_000_000, backupCount=3)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    install_console_colors()
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api = None
    try:
        cfg = load_config(args.config)
        # One runner on this Windows host; keep this port identical in every config.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        lock.bind(("127.0.0.1", cfg["lock_port"]))
        store = Store()
        scope = f'{cfg["account_server"]}:{cfg["account_login"]}:{cfg["symbol"]}:{cfg["magic"]}'
        if args.status:
            LOG.info("Execution halt=%r unresolved=%r risk pause=%r management halt=%r", store.get(scope,"halt"), store.unresolved(scope), store.get(scope,"risk_pause"), store.get(scope,"management_halt"))
            return
        if args.clear_halt:
            for (key,) in store.unresolved(scope):
                store.finish(scope, key, "manually_reconciled", {"note": "Operator checked MT5 account and history"})
            store.put(scope, "halt", None)
            LOG.info("Halt cleared; restart bot after account reconciliation. This command sends no orders.")
            return
        import MetaTrader5 as api
        runner = Bot(api, cfg, store)
        LOG.info("CAPITAL GUARD enabled=%s | staged exits on new bot positions | news +/-30min | volatility halt requires manual resume", cfg.get("capital_guard_enabled",False))
        LOG.info("Starting Gold Lab v4.1 in %s mode", "DEMO ORDER" if cfg["send_demo_orders"] else "DRY RUN")
        LOG.info("Feed timestamp correction: subtract %s hours to obtain UTC", cfg.get("broker_time_offset_hours", 0))
        LOG.info("Scan M5 + H1 | risk %.2f%% | max open %s | combined cap %.2f%% | local sessions 08:00-%02d:00 | scalp=%s",
                 cfg["risk_fraction"]*100, cfg["max_open_positions"], cfg["max_open_risk_fraction"]*100,
                 cfg["session_end_hour"], "scalp_reclaim" in cfg["enabled_setups"])
        LOG.info("Entry-hour filter: %s. Broker availability, fresh quotes, setup and risk checks still apply.",
                 "ENABLED" if cfg.get("session_filter_enabled", True) else "DISABLED (all broker trading hours)")
        while True:
            try:
                runner.step(time.time())
                time.sleep(2)
            except Skip as exc:
                now = time.time()
                if now - runner.last_heartbeat >= 60:
                    LOG.warning("Waiting: %s", exc)
                    runner.last_heartbeat = now
                delay = runner.backoff
                runner.backoff = min(delay * 2, 30)
                time.sleep(delay)
            except FatalError:
                raise
            except Exception:
                LOG.exception("Unexpected error; no blind order retry. Resuming checks in 5 seconds")
                time.sleep(5)
    except KeyboardInterrupt:
        LOG.info("Stopped by operator; existing broker SL/TP remain")
    except Exception:
        LOG.exception("Bot stopped")
        raise SystemExit(1)
    finally:
        from dashboard import close_dashboard
        close_dashboard()
        if api is not None:
            api.shutdown()
        lock.close()


if __name__ == "__main__":
    main()
