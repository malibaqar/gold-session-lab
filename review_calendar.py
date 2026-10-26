"""Explicitly record a human review of the saved official event dates. No date fetching."""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import shutil
import socket

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--confirm-official-dates-reviewed',action='store_true')
    a=p.parse_args();root=Path(__file__).resolve().parent
    path=root/'news_calendar.json';data=json.loads(path.read_text(encoding='utf-8-sig'))
    if not a.confirm_official_dates_reviewed:
        print(data['coverage_note'])
        print('Review/update events against their official source URLs. This script does NOT fetch updates.')
        for url in sorted({e['source'] for e in data['events']}):print(url)
        print('After checking dates, times and coverage: python review_calendar.py --confirm-official-dates-reviewed')
    else:
        cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'));lock=socket.socket()
        try:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):lock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            lock.bind(('127.0.0.1',cfg['lock_port']))
            now=datetime.now(timezone.utc);end=datetime.fromisoformat(data['coverage_end_utc'].replace('Z','+00:00'))
            if now>=end:raise RuntimeError('Extend calendar coverage and events before approving it')
            shutil.copy2(path,root/('news_calendar_backup_'+now.strftime('%Y%m%d_%H%M%S')+'.json'))
            data['verified_on']=now.date().isoformat()
            data['review_by_utc']=min(now+timedelta(days=7),end).isoformat()
            path.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
            print('Recorded your review. Next review deadline:',data['review_by_utc'])
        finally:lock.close()
