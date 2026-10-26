"""One-shot MT5 DEMO connectivity test. Preview by default; --send-demo sends once."""
import argparse
import logging
import socket
import time
from bot import Bot, Skip, load_config
from engine import Signal
from storage import BASE, Store


class TestBot(Bot):
    def gate(self, now, check_session=True):
        # Connectivity test is independent of strategy sessions, not risk controls.
        account = super().gate(now, check_session=False)
        positions = self.api.positions_get(symbol=self.cfg['symbol'])
        if positions is None or len(positions):
            raise Skip('Connectivity test requires NO open gold positions')
        return account

    def prepare(self, signal, now):
        request, risk, budget = super().prepare(signal, now)
        minimum = self.api.symbol_info(self.cfg['symbol']).volume_min
        risk *= minimum / request['volume']
        request['volume'] = float(minimum)
        check = self.api.order_check(request)
        if check is None or check.retcode != 0:
            raise Skip('Minimum-lot broker preflight failed: ' + str(check))
        return request, risk, budget


def make_signal(api, cfg, now, side, distance):
    tick = api.symbol_info_tick(cfg['symbol'])
    if tick is None:
        raise Skip('No gold quote')
    direction = 1 if side == 'buy' else -1
    price = tick.ask if direction == 1 else tick.bid
    return Signal('M5', int(now)-300, direction, 'connectivity_test',
                  price, price-direction*distance, max(distance, 1.0), 0)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--send-demo', action='store_true', help='Send ONE minimum-lot demo market order; leaves it open with SL/TP')
    p.add_argument('--side', choices=['buy', 'sell'], default='buy')
    p.add_argument('--stop-distance', type=float, default=5.0, help='Gold price distance to stop, default 5.00; not account-currency loss')
    args = p.parse_args()
    if not 0 < args.stop_distance <= 50:
        p.error('stop-distance must be >0 and <=50')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config(BASE / 'config.json')
    cfg['send_demo_orders'] = args.send_demo
    cfg['require_tradingview_confirmation'] = False
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api = None
    store = None
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        lock.bind(('127.0.0.1', cfg['lock_port']))
        import MetaTrader5 as api
        store = Store()
        runner = TestBot(api, cfg, store)
        runner.connect()
        now = time.time()
        signal = make_signal(api, cfg, now, args.side, args.stop_distance)
        logging.info('Connectivity test: %s; strategy session filter bypassed; all account, quote and risk checks active',
                     'SEND ONE DEMO ORDER' if args.send_demo else 'PREVIEW ONLY')
        runner.execute(signal, now)
        if args.send_demo:
            logging.info('Inspect broker fill/rejection above and MT5 Trade/History tabs. Any filled position STAYS OPEN with SL/TP. Close it manually before restarting the strategy bot.')
        else:
            logging.info('Preview sent nothing. To attempt one demo order: python sample_demo_trade.py --send-demo')
    except OSError as exc:
        logging.error('Cannot start. Stop the normal bot first (Ctrl+C). Details: %s', exc)
        raise SystemExit(1)
    except Exception as exc:
        logging.error('Test stopped: %s. No automatic retry. Inspect MT5 positions AND history if send outcome is uncertain.', exc)
        raise SystemExit(1)
    finally:
        if api is not None:
            api.shutdown()
        if store is not None:
            store.db.close()
        lock.close()


if __name__ == '__main__':
    main()
