"""Live read-only Rich dashboard, with plain-log fallback. Never calls MT5."""
from collections import deque
from datetime import datetime
import logging
import re
import sys
import time
from zoneinfo import ZoneInfo

_ACTIVE=None
_FALLBACK_AT=0


def profit_style(value):
    return 'bold red' if '-' in value else 'bold green' if '+' in value else 'white'

class EventHandler(logging.Handler):
    def __init__(self,display):super().__init__(logging.INFO);self.display=display
    def emit(self,record):
        try:
            message=record.getMessage().replace('\n',' ')[:280]
            self.display.events.append((datetime.fromtimestamp(record.created,ZoneInfo(self.display.cfg.get('display_timezone','Asia/Karachi'))).strftime('%H:%M:%S'),record.levelno,message))
        except Exception:pass

class Display:
    def __init__(self,snapshot,runner,now,console=None,start=True):
        from rich.console import Console
        from rich.live import Live
        self.console=console or Console(stderr=True)
        self.snapshot=snapshot;self.snapshot_at=now;self.cfg=runner.cfg
        self.events=deque(maxlen=8)
        self.root=logging.getLogger();self.saved=[]
        self.handler=EventHandler(self)
        self.live=Live(console=self.console,get_renderable=self.build,screen=True,refresh_per_second=2,
                       redirect_stdout=False,redirect_stderr=False)
        self.started=False
        if start:
            for h in list(self.root.handlers):
                if isinstance(h,logging.StreamHandler) and not isinstance(h,logging.FileHandler):
                    self.saved.append(h);self.root.removeHandler(h)
            self.root.addHandler(self.handler)
            try:self.live.start();self.started=True
            except Exception:
                self.root.removeHandler(self.handler)
                for h in self.saved:self.root.addHandler(h)
                raise

    def build(self):
        from rich import box
        from rich.align import Align
        from rich.columns import Columns
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        compact=self.console.size.height<38
        lines=self.snapshot.splitlines()
        pick=lambda prefix:next((line for line in lines if line.startswith(prefix)),'Unavailable')
        age=max(0,time.time()-self.snapshot_at)
        fresh=age<=10
        title=Text('GOLD SESSION LAB  v4.1',style='bold gold1')
        title.append('   DEMO',style='bold black on bright_cyan')
        title.append('   '+datetime.now(ZoneInfo(self.cfg.get('display_timezone','Asia/Karachi'))).strftime('%H:%M:%S %Z'),style='white')
        title.append(f'   snapshot {age:.0f}s old',style='green' if fresh else 'bold red')
        if not fresh:title.append('  STALE / WAITING FOR CONNECTION',style='bold red')
        header=Panel(Align.center(Group(title,Text(pick('MODE:'),style='cyan'))),border_style='gold1',box=box.ROUNDED)
        totals=pick('OPEN BOT TRADES:').split(' | ')
        booked=pick('TODAY UTC booked bot P/L:').split(' | ')[0].replace('TODAY UTC booked bot P/L: ','')
        cards=[]
        for name,value,style in [('POSITIONS / LOTS',(totals[0].replace('OPEN BOT TRADES: ','')+' open | '+totals[1].replace('TOTAL LOTS: ','')+' lots') if len(totals)>1 else 'N/A','cyan'),
                                 ('FLOATING P/L',totals[2].replace('FLOATING P/L: ','') if len(totals)>2 else 'N/A','green'),
                                 ('TODAY BOOKED P/L',booked,'green')]:
            cards.append(Panel(Text(value,style=profit_style(value) if 'P/L' in name else 'bold cyan'),title=name,
                               border_style=style,expand=True))
        account_prefixes=['ACCOUNT balance:','ENTRY LIMITS:'] if compact else ['ACCOUNT balance:','ENTRY LIMITS:','ENABLED SETUPS:']
        account=Group(*[Text(pick(prefix),style='white') for prefix in account_prefixes])
        guard_lines=[pick('TODAY UTC order attempts:'),pick('CAPITAL GUARD:'),pick('NEWS FILTER:')]
        guards=Group(*[Text(line,style='bold yellow' if any(s in line for s in ['HALT','BLOCKED','PAUSE','review','unavailable']) else 'white') for line in guard_lines])
        top=Table.grid(expand=True,padding=(0,1));top.add_column(ratio=1);top.add_column(ratio=1)
        top.add_row(Panel(account,title='ACCOUNT & RISK',border_style='blue',expand=True),
                    Panel(guards,title='ENTRY GUARDS',border_style='yellow',expand=True))
        card_row=Table.grid(expand=True,padding=(0,1))
        for _ in range(3):card_row.add_column(ratio=1)
        card_row.add_row(*cards)
        table=Table(box=box.SIMPLE_HEAVY,expand=True,header_style='bold cyan')
        for name in ['Ticket','Side','TF','Setup','Lots','Floating P/L']:table.add_column(name,no_wrap=name!='Setup')
        details=[]
        for i,line in enumerate(lines):
            match=re.match(r'TICKET (\d+) \| (BUY|SELL|\?) (\S+) (.*?) \| LOTS (\S+) \| P/L (.*)',line)
            if match:
                ticket,side,tf,setup,lots,pnl=match.groups()
                table.add_row(ticket,Text(side,style='bold green' if side=='BUY' else 'bold red'),tf,
                              Text(setup),lots,Text(pnl,style=profit_style(pnl)))
                if i+1<len(lines):details.append(Text('#'+ticket+' '+lines[i+1].strip(),style='white'))
                if not compact and i+2<len(lines):details.append(Text(lines[i+2].strip(),style='dim'))
        if not table.row_count:table.add_row('--','--','--','Waiting for a qualifying setup','0','--')
        for line in lines:
            if line.startswith('  Ticket '):details.append(Text(line.strip(),style='yellow' if 'full exit at TP1' in line else 'cyan'))
            if line.startswith('OTHER GOLD'):details.append(Text(line,style='yellow'))
        positions=Panel(Group(table,*details),title='OPEN TRADES & EXIT PLAN',border_style='cyan')
        events=Table.grid(expand=True);events.add_column(width=9);events.add_column()
        for stamp,level,msg in list(self.events)[-(3 if compact else 8):]:
            style='red' if level>=logging.ERROR else 'yellow' if level>=logging.WARNING else 'green' if 'FILLED' in msg or 'Broker fill' in msg else 'magenta' if 'SELECTED' in msg or 'ENTRY RULE' in msg else 'white'
            events.add_row(Text(stamp,style='dim'),Text(msg,style=style,no_wrap=compact,overflow='ellipsis'))
        if not self.events:events.add_row('--','Waiting for the next scan or execution event.')
        footer=Text('Ctrl+C stops runner | All details: bot.log | Floating includes swap, excludes entry commission | Booked P/L/history: up to 60s lag',style='dim')
        return Group(header,card_row,top,positions,
                     Panel(events,title='RECENT DECISIONS & EXECUTION',border_style='magenta'),*([footer] if not compact else []))

    def update(self,snapshot,runner,now):
        self.snapshot=snapshot;self.snapshot_at=now;self.cfg=runner.cfg
        if self.started:self.live.refresh()

    def close(self):
        if self.started:self.live.stop()
        if self.handler in self.root.handlers:self.root.removeHandler(self.handler)
        for h in self.saved:
            if h not in self.root.handlers:self.root.addHandler(h)
        for stamp,level,msg in list(self.events)[-4:]:self.console.print(stamp+' '+msg,markup=False)
        self.console.print('Dashboard stopped. Full diagnostic log: bot.log',style='cyan')


def show_dashboard(snapshot,runner,now):
    global _ACTIVE,_FALLBACK_AT
    try:
        if not sys.stderr.isatty():raise RuntimeError('Not an interactive terminal')
        if _ACTIVE is None:_ACTIVE=Display(snapshot,runner,now)
        else:_ACTIVE.update(snapshot,runner,now)
    except Exception as exc:
        if _ACTIVE is not None:
            try:_ACTIVE.close()
            except Exception:pass
            _ACTIVE=None
        if now-_FALLBACK_AT>=15:
            logging.getLogger('goldbot').info('Dashboard plain-text fallback (%s)\n%s',exc,snapshot)
            _FALLBACK_AT=now


def close_dashboard():
    global _ACTIVE
    if _ACTIVE is not None:
        try:_ACTIVE.close()
        finally:_ACTIVE=None
