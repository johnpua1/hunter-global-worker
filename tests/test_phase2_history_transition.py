import copy
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
import phase2_runtime as phase2
import runner
from phase2_pack import pack, unpack
from test_foundation import row
from test_us_phase2_resume import committed_drive, CP, RANK, DATE


def event(sid='US-000780', day='2026-08-25'):
    return dict(event_id=sid+':'+day, security_id=sid, report_date=day,
                ticker='BNS', session='BMO', reaction_status='COMPLETE',
                eps_actual=1.0, revisions=[], source='NASDAQ')


class HistoryTransitionTests(unittest.TestCase):
    def test_existing_excluded_issuer_is_preserved_through_result_and_receipt_commit(self):
        drive=committed_drive(); old=event(); sid='US-000001'
        for filename,data in [('SECTOR_MAP', {'rows':[{'security_id':sid}]}),
                              ('EARNINGS_CALENDAR', {'market':'US','events':[]}),
                              ('EARNINGS_HISTORY', pack({'market':'US','events':[old]}))]:
            drive.data['US/PHASE2/'+filename+'.json']=runner.compact(data)
        rank=drive.data[RANK]
        with patch('phase2_runtime.current_universe',return_value=[
                {'security_id':sid,'ticker':'TEST','listing_status':'ACTIVE'},
                {'security_id':'US-000780','ticker':'BNS','listing_status':'EXCLUDED_NON_COMMON'}]), \
             patch('phase2_runtime.us_calendar',return_value={'market':'US','events':[],'source_errors':[]}):
            runner.finish_phase2_daily(drive,'US',expected_date=DATE)
        saved=unpack(drive.json('US/PHASE2/EARNINGS_HISTORY.json'))['events']
        self.assertEqual([e['event_id'] for e in saved],[old['event_id']])
        self.assertEqual(saved[0]['reaction_status'],'COMPLETE')
        self.assertEqual(drive.json(CP)['phase2_completed_date'],DATE)
        self.assertEqual(drive.data[RANK],rank)
        self.assertEqual(drive.writes,['US/PHASE2/EARNINGS_HISTORY.json',
                                     'US/PHASE2/EARNINGS_CALENDAR.json',CP])

    def test_new_inactive_event_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'NEW_INACTIVE_EVENT'):
            phase2.validate_history_transition('US',set(),{'events':[event()]},{})

    def test_removed_or_duplicate_history_is_rejected(self):
        e=event()
        for events,reason in [([], 'EVENT_REMOVED'),([e,e], 'DUPLICATE_EVENT')]:
            with self.assertRaisesRegex(RuntimeError,reason):
                phase2.validate_history_transition('US',{e['event_id']},{'events':events},{})

    def test_wrong_identity_or_market_is_rejected(self):
        for change in ({'security_id':'HK-000780'},{'report_date':'2026-08-26'}):
            e=event();e.update(change)
            with self.assertRaisesRegex(RuntimeError,'IDENTITY_MISMATCH'):
                phase2.validate_history_transition('US',{e['event_id']},{'events':[e]},{})

    def test_new_active_event_is_allowed(self):
        e=event('US-000001')
        phase2.validate_history_transition('US',set(),{'events':[e]},{e['security_id']:{}})

    def test_price_missing_recalculates_when_repaired_bars_arrive(self):
        sessions=[(dt.date(2026,8,1)+dt.timedelta(days=i)).isoformat() for i in range(28)]
        e=event('US-000001',sessions[20]);e['reaction_status']='PRICE_MISSING'
        sid=e['security_id']; prices={sid:{d:row(d,100 if i<20 else 110,sid) for i,d in enumerate(sessions)}}
        with patch('phase2_runtime.compose_prices',return_value=(prices,sessions)) as compose:
            result=phase2.calculate_reactions(None,'US',{'events':[e]})
        compose.assert_called_once()
        self.assertEqual(result['events'][0]['reaction_status'],'COMPLETE')
        self.assertAlmostEqual(e['day5_pct'],.1)
        self.assertEqual(e['day1_volume_ratio'],1.0)

    def test_missing_bars_do_not_become_success(self):
        e=event();e['reaction_status']='PRICE_MISSING'
        with patch('phase2_runtime.compose_prices',return_value=({e['security_id']:{}},['2026-08-24','2026-08-25'])):
            phase2.calculate_reactions(None,'US',{'events':[e]})
        self.assertEqual(e['reaction_status'],'PRICE_MISSING')
        self.assertIsNone(e['day1_pct'])

    def test_complete_and_revised_events_are_not_recomputed(self):
        first=event();second=event(day='2026-08-26')
        second.update(reaction_status='PRICE_MISSING',event_status='REVISED')
        with patch('phase2_runtime.compose_prices') as compose:
            phase2.calculate_reactions(None,'US',{'events':[first,second]})
        compose.assert_not_called()
