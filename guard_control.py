"""Read guard status or explicitly approve entry resumption. Never sends trades."""
import argparse
import json
import socket
import time
from bot import Bot,load_config
from capital_guard import calendar_block
from storage import Store,BASE

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--resume',action='store_true',help='Explicitly approve clearing the emergency entry pause after reviewing conditions')
    p.add_argument('--ack-management',action='store_true',help='After manual reconciliation and closing ALL this bot gold positions, acknowledge management halt')
    a=p.parse_args();cfg=load_config(BASE/'config.json');lock=socket.socket(socket.AF_INET,socket.SOCK_STREAM);api=None;store=None
    try:
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):lock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        lock.bind(('127.0.0.1',cfg['lock_port']))
        import MetaTrader5 as api
        store=Store();r=Bot(api,cfg,store);r.connect();now=time.time()
        print('Entry emergency pause:',store.get(r.scope,'risk_pause'))
        print('Management halt:',store.get(r.scope,'management_halt'))
        print('Unresolved management sends:',store.unresolved(r.protection.mscope))
        print('News:',calendar_block(BASE/'news_calendar.json',now) or 'No saved-event blackout now')
        if a.ack_management:
            positions=api.positions_get(symbol=cfg['symbol'])
            if positions is None or any(p.magic==cfg['magic'] for p in positions):
                raise RuntimeError('Close ALL bot-owned gold positions manually and inspect MT5 history before acknowledgement')
            for (key,) in store.unresolved(r.protection.mscope):
                store.finish(r.protection.mscope,key,'manually_reconciled',{'note':'Operator inspected history and closed bot gold positions'})
            store.put(r.scope,'management_halt',None)
            print('Management halt acknowledged. Emergency entry pause remains; --resume is a separate approval.')
        if a.resume:
            if store.get(r.scope,'management_halt') or store.unresolved(r.protection.mscope):raise RuntimeError('Reconcile management first')
            if store.get(r.scope,'halt') or store.unresolved(r.scope):raise RuntimeError('Existing execution halt needs its original reconciliation procedure')
            r.protection.monitor(now,force=True)
            if not r.protection.monitor_ready or r.protection.current_shock:raise RuntimeError('Shock monitor not ready or shock condition still active; cannot resume')
            reason=calendar_block(BASE/'news_calendar.json',now)
            if reason:raise RuntimeError(reason)
            store.put(r.scope,'risk_pause',None)
            print('Operator approved entry resumption. Restart: python bot.py')
    finally:
        if api is not None:api.shutdown()
        if store is not None:store.db.close()
        lock.close()

if __name__=='__main__':
    try:main()
    except Exception as exc:print('CONTROL STOPPED:',exc);raise SystemExit(1)
