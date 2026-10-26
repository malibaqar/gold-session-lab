"""Read-only terminal status. No orders, stop changes or strategy decisions."""
from datetime import datetime, timezone, timedelta
import logging
import math
import os
import sys

RULES = {
 'session_pullback':'EMA20 pullback followed by a six-candle range breakout, aligned with hourly bias',
 'breakout_retest':'Prior 20-candle breakout retested and rejected in the hourly trend direction',
 'sweep_rejection':'Prior 12-candle extreme swept, then rejected in the hourly trend direction',
 'inside_breakout':'Inside candle followed by a close beyond its mother candle, with trend alignment',
 'impulse':'Directional candle body exceeds 1.2 ATR and agrees with hourly bias',
 'ema_reclaim':'EMA20 reclaimed after a pullback, with EMA slope, body and hourly trend filters',
 'scalp_reclaim':'M5 EMA20 touch, then close beyond the previous candle, with slope/body/ATR filters',
 'connectivity_test':'Manual one-shot connectivity test; not a strategy signal',
}

def clean(value):
    return ''.join(c for c in str(value) if c.isprintable())[:180]

def explain_setup(name):
    return RULES.get(name, 'See the selected setup and execution-check log')

def money(value, signed=False):
    try:
        value=float(value)
        if not math.isfinite(value):return 'N/A'
        return f'{value:+,.2f}' if signed else f'{value:,.2f}'
    except (TypeError,ValueError):return 'N/A'

def lot_text(value):
    return f'{value:.8f}'.rstrip('0').rstrip('.')

class ColorFormatter(logging.Formatter):
    def format(self, record):
        text=super().format(record)
        if 'STATUS PANEL' in text: color='96'
        elif 'Broker fill:' in text: color='92'
        elif record.levelno >= logging.ERROR: color='91'
        elif record.levelno >= logging.WARNING or 'SKIPPED' in text:color='93'
        elif 'SETUP SELECTED' in text or 'ENTRY RULE' in text: color='95'
        else:return text
        return '\033['+color+'m'+text+'\033[0m'

def install_console_colors():
    if not sys.stderr.isatty():return
    if os.name=='nt':
        try:
            import ctypes
            kernel=ctypes.windll.kernel32
            handle=kernel.GetStdHandle(-12)
            mode=ctypes.c_ulong()
            if not kernel.GetConsoleMode(handle,ctypes.byref(mode)):return
            if not kernel.SetConsoleMode(handle,mode.value|4):return
        except Exception:return
    for h in logging.getLogger().handlers:
        if isinstance(h,logging.StreamHandler) and not isinstance(h,logging.FileHandler):
            h.setFormatter(ColorFormatter('%(asctime)s %(levelname)s %(message)s'))

class StatusPanel:
    def __init__(self):
        self.last=0
        self.history_at=0
        self.history_day=None
        self.history=None
        self.ownership={}
        self.last_warning=0

    def owned_deal(self,api,cfg,deal,known):
        if deal.symbol!=cfg['symbol']:return False
        if deal.magic==cfg['magic']:return True
        pid=getattr(deal,'position_id',0)
        if not pid:return False
        if pid in known:return True
        if pid not in self.ownership:
            history=api.history_deals_get(position=pid)
            if history is None:return False
            self.ownership[pid]=any(d.symbol==cfg['symbol'] and d.magic==cfg['magic'] for d in history)
        return self.ownership[pid]

    def refresh_history(self,runner,now,positions):
        day=datetime.fromtimestamp(now,timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
        if self.history_day==day and now-self.history_at<60:return
        self.history_at=now;self.history_day=day
        # Broad query then normalize each record, matching this bot's feed-clock correction.
        deals=runner.api.history_deals_get(day-timedelta(hours=14),
                    datetime.fromtimestamp(now,timezone.utc)+timedelta(hours=14))
        if deals is None:
            self.history=None;return
        offset=runner.cfg.get('broker_time_offset_hours',0)*3600
        known={getattr(p,'identifier',p.ticket) for p in positions}
        known.update(getattr(d,'position_id',0) for d in deals
                     if d.symbol==runner.cfg['symbol'] and d.magic==runner.cfg['magic'])
        own=[d for d in deals if day.timestamp()<=d.time-offset<=now
             and self.owned_deal(runner.api,runner.cfg,d,known)]
        net=sum(float(getattr(d,k,0)) for d in own for k in ('profit','commission','swap','fee'))
        exits=sum(getattr(d,'entry',-1) in (1,2,3) for d in own)
        self.history=(net,exits)

    def render(self,runner,now):
        account=runner.identity()
        positions=runner.api.positions_get(symbol=runner.cfg['symbol'])
        if positions is None:
            raise RuntimeError('MT5 position lookup failed; open count and P/L unavailable')
        owned=[p for p in positions if p.magic==runner.cfg['magic']]
        self.refresh_history(runner,now,owned)
        cur=clean(getattr(account,'currency','account currency'))
        mode='DEMO ORDERS' if runner.cfg['send_demo_orders'] else 'DRY RUN'
        hour_mode='ALL HOURS' if not runner.cfg.get('session_filter_enabled',True) else ('WINDOW OPEN' if runner.session_allowed(now) else 'WINDOW CLOSED')
        floating=sum(p.profit+getattr(p,'swap',0) for p in owned)
        lots=sum(p.volume for p in owned)
        midnight=datetime.fromtimestamp(now,timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        attempts=runner.store.attempt_count(runner.scope,midnight)
        state=runner.store.get(runner.scope,'day:'+datetime.fromtimestamp(now,timezone.utc).date().isoformat(),{})
        gate='BLOCKED: daily loss gate' if state.get('blocked') else 'subject to setup, spread, risk and broker checks'
        lines=['STATUS PANEL | '+datetime.fromtimestamp(now).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'),
               f'MODE: {mode} | ENTRY HOURS: {hour_mode} | M5 + H1',
               f'OPEN BOT TRADES: {len(owned)} | TOTAL LOTS: {lot_text(lots)} | FLOATING P/L: {money(floating,True)} {cur}',
               f'ACCOUNT balance: {money(getattr(account,"balance",None))} | equity: {money(account.equity)} | free margin: {money(account.margin_free)} {cur}',
               f'TODAY UTC order attempts: {attempts}/{runner.cfg["max_daily_attempts"]} | {gate}']
        if self.history is None:lines.append('TODAY UTC booked bot P/L: UNAVAILABLE (history lookup failed)')
        else:
            net,exits=self.history
            lines.append(f'TODAY UTC booked bot P/L: {money(net,True)} {cur} | EXIT FILLS: {exits} | history refreshed up to 60s ago')
        if len(positions)!=len(owned):lines.append(f'OTHER GOLD POSITIONS: {len(positions)-len(owned)} (excluded from bot totals; may block entries)')
        if owned:
            for p in owned:
                parts=clean(p.comment).split()
                tf=parts[1] if len(parts)>1 and parts[0] in ('GL2','GL3') else '?'
                setup=' '.join(parts[2:]) if len(parts)>2 else clean(p.comment)
                side='BUY' if p.type==0 else 'SELL' if p.type==1 else '?'
                lines.append(f'TICKET {p.ticket} | {side} {tf} {setup} | LOTS {lot_text(p.volume)} | P/L {money(p.profit+getattr(p,"swap",0),True)} {cur}')
                lines.append(f'  Entry {money(p.price_open)} | current {money(p.price_current)} | SL {money(p.sl)} | TP {money(p.tp)}')
                lines.append('  Rule: '+explain_setup(setup))
        else:lines.append('NO OPEN BOT POSITIONS. Waiting for a qualifying closed-candle setup.')
        # CAPITAL_GUARD_PANEL_V1
        if runner.cfg.get('capital_guard_enabled',False):
            pause=runner.store.get(runner.scope,'risk_pause')
            lines.append('CAPITAL GUARD: '+('ENTRY HALTED: '+pause['reason'] if pause else 'enabled; news and volatility checks apply'))
            lines.append('NEWS FILTER: '+(getattr(runner.protection,'news_reason',None) or 'no saved-event blackout now; calendar coverage still applies'))
            for pos in owned:
                plan=runner.store.get(runner.scope,'exit_plan:'+str(getattr(pos,'identifier',pos.ticket)))
                if plan:
                    lines.append(f"  Ticket {pos.ticket} TP1={plan['targets'][0]:.2f} TP2={plan['targets'][1]:.2f} TP3={plan['targets'][2]:.2f} | completed stages={plan['stage']} | breakeven={plan['be_done']} | {('partial exits' if plan['legs'] else 'full exit at TP1: lot too small')}")
                else:lines.append(f'  Ticket {pos.ticket}: original SL/TP; no new guarded exit plan')
        lines.append('Floating P/L includes swap, excludes entry commission. Booked P/L includes reported deal costs; separate unlinked charges excluded.')
        lines.append(f"ENTRY LIMITS: max lots={runner.cfg.get('max_lot_size', .01)} | base risk={runner.cfg['risk_fraction']*100:.2f}% | adaptive risk={runner.cfg.get('dynamic_risk_enabled', False)}")
        lines.append('ENABLED SETUPS: '+', '.join(runner.cfg['enabled_setups']))
        lines.append('END STATUS PANEL')
        return '\n'.join(lines)

    def poll(self,runner,now):
        if now-self.last<runner.cfg.get("dashboard_refresh_seconds", 15):return
        self.last=now
        try:
            snapshot = self.render(runner,now)
            if runner.cfg.get("dashboard_enabled", False):
                from dashboard import show_dashboard
                show_dashboard(snapshot, runner, now)
            else:
                logging.getLogger('goldbot').info('\n%s',snapshot)
        except Exception as exc:
            if now-self.last_warning>=60:
                logging.getLogger('goldbot').warning('Status panel unavailable: %s (reporting does not change trading rules)',exc)
                self.last_warning=now
