"""Capital guard: scheduled entry blackouts, volatility latch, persistent staged exits.
No news/war interpretation and no statistical win-probability estimate.
"""
from datetime import datetime,timezone
import json
import logging
import math
from pathlib import Path
from engine import floor_volume
from storage import BASE
LOG=logging.getLogger('goldbot')

class EntryBlocked(RuntimeError):pass


def utc_stamp(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('News calendar timestamps require an explicit UTC offset')
    return dt.timestamp()

def calendar_block(path,now):
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        start=utc_stamp(data['coverage_start_utc'])
        end=utc_stamp(data['coverage_end_utc'])
        review=utc_stamp(data['review_by_utc'])
        if not start<=now<end:return 'News calendar outside its covered dates; update required'
        if now>=review:return 'News calendar needs a fresh official-date review before new entries'
        events=data['events']
        if not events:return 'Empty news calendar; new entries blocked'
        for event in events:
            t=utc_stamp(event['time_utc'])
            before=event.get('minutes_before',30);after=event.get('minutes_after',30)
            if not 30<=before<=1440 or not 30<=after<=1440:return 'Invalid news-event buffer'
            if t-before*60<=now<=t+after*60:return 'NEWS PAUSE: '+event['name']+' @ '+event['time_utc']
        return None
    except Exception as exc:return 'News calendar unavailable/invalid: '+str(exc)


def partial_plan(volume,minimum,maximum,step):
    first=floor_volume(volume*.5,minimum,maximum,step)
    remaining=round(volume-first,8)
    second=floor_volume(remaining*.75,minimum,maximum,step)
    final=round(remaining-second,8)
    valid=lambda value: value>=minimum and value<=maximum and abs((value-minimum)/step-round((value-minimum)/step))<1e-6
    if not all(valid(v) for v in [first,remaining,second,final]):
        return None
    return [first,second,final]


class CapitalGuard:
    def __init__(self,runner):
        self.r=runner
        self.enabled=runner.cfg.get('capital_guard_enabled',False)
        self.samples=[]
        self.last_check=0
        self.monitor_ready=False
        self.current_shock=None
        self.news_reason=None
        self.last_message=0
        self.mscope=runner.scope+':management'
        if self.enabled and runner.store.unresolved(self.mscope):
            self.management_halt('Unresolved management send; inspect MT5 positions AND history')

    def pause(self,reason,now):
        if not self.r.store.get(self.r.scope,'risk_pause'):
            self.r.store.put(self.r.scope,'risk_pause',{'reason':reason,'at':now})
            LOG.error('EMERGENCY ENTRY HALT | %s | Manual resume required: python guard_control.py --resume',reason)

    def management_halt(self,reason):
        self.r.store.put(self.r.scope,'management_halt',reason)
        self.pause(reason,__import__('time').time())
        LOG.error('MANAGEMENT HALTED | %s | Broker SL/TP remain. Reconcile manually.',reason)

    def quote(self,now):
        t=self.r.api.symbol_info_tick(self.r.cfg['symbol'])
        if t is None:raise EntryBlocked('No quote')
        stamp=(t.time_msc/1000 if t.time_msc else t.time)-self.r.cfg.get('broker_time_offset_hours',0)*3600
        if not -2<=now-stamp<=10 or not all(math.isfinite(v) for v in (t.bid,t.ask)) or not 0<t.bid<=t.ask:
            raise EntryBlocked('Stale/invalid quote')
        return t

    def monitor(self,now,force=False):
        if not self.enabled:return
        if not force and now-self.last_check<5:return
        self.last_check=now;self.monitor_ready=False;self.current_shock=None
        try:
            tick=self.quote(now)
            d=self.r.candles('M5',now)
            atr=float(d.atr.iloc[-2]);last=d.iloc[-1];prior=d.iloc[-2]
            if not math.isfinite(atr) or atr<=0:raise EntryBlocked('No volatility baseline')
            recent=self.r.api.copy_rates_from_pos(self.r.cfg['symbol'],self.r.api.TIMEFRAME_M5,0,1)
            if recent is None or len(recent)!=1:raise EntryBlocked('No forming M5 range for shock monitor')
            bar=recent[0]
            if int(bar['time'])-self.r.cfg.get('broker_time_offset_hours',0)*3600!=int(now//300)*300:
                raise EntryBlocked('Stale forming M5 bar')
            vals=[float(bar[k]) for k in ['high','low','open']]
            if not all(math.isfinite(v) and v>0 for v in vals) or vals[0]<vals[1]:raise EntryBlocked('Invalid forming range')
            closed_range=max(float(last.high-last.low),abs(float(last.high-prior.close)),abs(float(last.low-prior.close)))
            forming_range=max(vals[0]-vals[1],abs(vals[0]-float(last.close)),abs(vals[1]-float(last.close)))
            mid=(tick.bid+tick.ask)/2
            self.samples=[(t,p) for t,p in self.samples if now-t<=60]
            move=max([abs(mid/p-1) for _,p in self.samples]+[0])
            self.samples.append((now,mid))
            if max(closed_range,forming_range)>=3*atr:
                self.current_shock=f'M5 shock range {max(closed_range,forming_range):.2f} >= 3x prior ATR {atr:.2f}'
            elif move>=.005:
                self.current_shock=f'Fast 60-second price move {move*100:.2f}% >= 0.50%'
            self.monitor_ready=True
            if self.current_shock:self.pause(self.current_shock,now)
        except Exception as exc:
            if now-self.last_message>=60:
                LOG.warning('Shock monitor unavailable; new entries blocked: %s',exc);self.last_message=now

    def entry_gate(self,now):
        if not self.enabled:return
        self.monitor(now)
        pause=self.r.store.get(self.r.scope,'risk_pause')
        if pause:raise EntryBlocked('Persistent emergency pause: '+pause['reason'])
        if self.r.store.get(self.r.scope,'management_halt') or self.r.store.unresolved(self.mscope):raise EntryBlocked('Management reconciliation required')
        if not self.monitor_ready:raise EntryBlocked('Shock monitor not ready; no new entries')
        reason=calendar_block(BASE/'news_calendar.json',now)
        if reason:raise EntryBlocked(reason)

    def send_action(self,key,request,now):
        r=self.r
        # Called only on an already identified position, independently of entry gates.
        account=r.identity();terminal=r.api.terminal_info()
        if terminal is None or not terminal.connected or not terminal.trade_allowed or terminal.tradeapi_disabled or not account.trade_allowed or not account.trade_expert:
            raise EntryBlocked('Trade permissions unavailable for management')
        self.quote(now)
        if not r.cfg['send_demo_orders']:
            LOG.info('DRY RUN MANAGEMENT | %s | %s',key,request);return False
        check=r.api.order_check(request)
        if check is None or check.retcode!=0:
            LOG.warning('MANAGEMENT DEFERRED | broker preflight: %s',getattr(check,'comment','no result'));return False
        row=r.store.db.execute('SELECT status FROM attempts WHERE scope=? AND key=?',(self.mscope,key)).fetchone()
        if row:
            if row[0]=='filled':return True
            self.management_halt('Existing unresolved/rejected management action '+key);return False
        if not r.store.reserve(self.mscope,key,{'request':request}):return False
        try:
            result=r.api.order_send(request)
            if result is None:raise RuntimeError('No broker management result')
            payload={k:getattr(result,k,None) for k in ['retcode','order','deal','volume','price','comment']}
            # A partial close fill is uncertain with respect to our stage; never top it up.
            if result.retcode!=r.api.TRADE_RETCODE_DONE or ('volume' in request and abs(float(getattr(result,'volume',0))-request['volume'])>1e-7):
                raise RuntimeError('Management did not return full DONE: '+str(payload))
            r.store.finish(self.mscope,key,'filled',payload)
            LOG.info('MANAGEMENT FILLED | %s | %s',key,payload)
            return True
        except Exception as exc:
            self.management_halt(str(exc));return False

    def close(self,p,volume,key,now):
        r=self.r
        current=r.api.positions_get(ticket=p.ticket)
        if current is None:raise EntryBlocked('Cannot revalidate position before partial close')
        if not current:return True  # broker SL/TP or operator already closed it; never open an opposite trade
        p=current[0]
        if p.magic!=r.cfg['magic'] or p.symbol!=r.cfg['symbol']:raise EntryBlocked('Position ownership changed')
        if p.volume+1e-8<volume:
            self.management_halt('Unexpected volume change before '+key);return False
        t=self.quote(now);info=r.api.symbol_info(p.symbol)
        request={'action':r.api.TRADE_ACTION_DEAL,'symbol':p.symbol,'position':p.ticket,
                 'volume':float(volume),'type':r.api.ORDER_TYPE_SELL if p.type==r.api.ORDER_TYPE_BUY else r.api.ORDER_TYPE_BUY,
                 'price':float(t.bid if p.type==r.api.ORDER_TYPE_BUY else t.ask),'deviation':r.cfg['deviation_points'],
                 'magic':r.cfg['magic'],'comment':'GC4 '+key[-20:],'type_time':r.api.ORDER_TIME_GTC,'type_filling':r.filling(info)}
        return self.send_action(key,request,now)

    def breakeven(self,p,plan,key,now):
        r=self.r;t=self.quote(now);info=r.api.symbol_info(p.symbol)
        direction=plan['direction'];sl=plan['entry']
        # Entry-price breakeven only, not a guarantee after fees/gaps.
        units=sl/info.trade_tick_size
        sl=round((math.ceil(units) if direction==1 else math.floor(units))*info.trade_tick_size,info.digits)
        if p.sl>0 and direction*(p.sl-sl)>=0:return True  # never loosen an improved stop
        mark=t.bid if direction==1 else t.ask
        minimum=max(info.trade_stops_level,getattr(info,'trade_freeze_level',0))*info.point+info.trade_tick_size
        if direction*(mark-sl)<minimum:return False
        request={'action':r.api.TRADE_ACTION_SLTP,'symbol':p.symbol,'position':p.ticket,'sl':sl,'tp':p.tp,'magic':r.cfg['magic']}
        return self.send_action(key,request,now)

    def manage(self,now):
        if not self.enabled or self.r.store.get(self.r.scope,'management_halt'):return
        r=self.r
        positions=r.api.positions_get(symbol=r.cfg['symbol'])
        if positions is None:return
        info=r.api.symbol_info(r.cfg['symbol'])
        if info is None:return
        rows=r.store.db.execute("SELECT payload FROM attempts WHERE scope=? AND status='filled' AND at>=?",
                                (r.scope,r.cfg['capital_guard_installed_at'])).fetchall()
        new_orders={json.loads(row[0]).get('order') for row in rows}
        for p in positions:
            if p.magic!=r.cfg['magic']:continue
            # Match durable post-install entry orders, not potentially shifted position timestamps.
            if not p.comment.startswith('GL3 ') or 'connectivity_test' in p.comment:continue
            pid=getattr(p,'identifier',p.ticket)
            if p.ticket not in new_orders and pid not in new_orders:
                if not new_orders:continue
                deals=r.api.history_deals_get(position=pid)
                if deals is None or not any(d.order in new_orders for d in deals):continue
            pid=getattr(p,'identifier',p.ticket);key='exit_plan:'+str(pid)
            plan=r.store.get(r.scope,key)
            direction=1 if p.type==r.api.ORDER_TYPE_BUY else -1
            if plan is None:
                if p.sl<=0 or p.tp<=0 or direction*(p.price_open-p.sl)<=0 or direction*(p.tp-p.price_open)<=0:
                    self.management_halt('Cannot initialize protected exit plan for position '+str(p.ticket));return
                legs=partial_plan(p.volume,info.volume_min,info.volume_max,info.volume_step)
                span=abs(p.tp-p.price_open)
                plan={'entry':p.price_open,'initial_volume':p.volume,'direction':direction,'targets':
                      [p.price_open+direction*span*f for f in [.5,.75,1]],'legs':legs,'stage':0,'be_done':False}
                r.store.put(r.scope,key,plan)
                LOG.info('EXIT PLAN | ticket=%s TP1=%.2f TP2=%.2f TP3=%.2f | lots=%s | %s',
                         p.ticket,*plan['targets'],p.volume,'partial exits rounded DOWN to broker step' if legs else 'minimum-lot fallback: FULL EXIT at TP1')
            if direction!=plan['direction']:
                self.management_halt('Position direction changed unexpectedly');return
            try:
                tick=self.quote(now);mark=tick.bid if direction==1 else tick.ask
                stage=plan['stage'];legs=plan['legs']
                if stage>=3:
                    if now-plan.get('finished_at',now)>30:
                        self.management_halt('Position still present after full-close acknowledgement '+str(pid))
                    continue
                expected=round(plan['initial_volume']-sum((legs or [])[:stage]),8)
                # Recover a filled close whose plan update was interrupted, using its durable journal.
                action=f'{pid}:TP{stage+1}'
                filled=r.store.db.execute('SELECT status FROM attempts WHERE scope=? AND key=?',(self.mscope,action)).fetchone()
                if legs and stage<2 and filled and filled[0]=='filled':
                    if abs(p.volume-(expected-legs[stage]))>1e-7:
                        self.management_halt('Post-fill volume needs reconciliation for '+str(pid));return
                    plan['stage']+=1;r.store.put(r.scope,key,plan);continue
                if abs(p.volume-expected)>1e-7:
                    self.management_halt('External/partial volume change needs reconciliation for '+str(pid));return
                if stage>=1 and not plan['be_done']:
                    if self.breakeven(p,plan,str(pid)+':BE',now):
                        plan['be_done']=True;r.store.put(r.scope,key,plan)
                        LOG.info('BREAKEVEN SET | ticket=%s | SL at entry (fees/slippage excluded)',p.ticket)
                    continue
                if direction*(mark-plan['targets'][stage])<0:continue
                volume=p.volume if legs is None or stage==2 else legs[stage]
                if self.close(p,volume,action,now):
                    plan['stage']=3 if legs is None else stage+1
                    if plan['stage']==3:plan['finished_at']=now
                    r.store.put(r.scope,key,plan)
                if r.store.get(r.scope,'management_halt'):return
            except Exception as exc:
                LOG.warning('Position management deferred | ticket=%s | %s',p.ticket,exc)

    def tick(self,now):
        if self.enabled:
            self.news_reason=calendar_block(BASE/'news_calendar.json',now)
            self.monitor(now)
            if not self.r.store.get(self.r.scope,'halt') and not self.r.store.unresolved(self.r.scope):
                self.manage(now)
