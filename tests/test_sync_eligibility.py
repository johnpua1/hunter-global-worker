import json
import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hunter-global"))
import universe
from foundation import append_daily_date
from runner import compact
from runner import digest


class SyncEligibilityTests(unittest.TestCase):
    def test_data_repair_is_reversible_idempotent_and_preserves_other_fields(self):
        path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/repair-sync-eligibility.py'
        spec = importlib.util.spec_from_file_location('repair_sync', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        doc = {'market': 'US', 'updated_at_myt': 'old-source-time', 'securities': [
            {'security_id': 'US-1', 'name': 'Alpha Warrants', 'listing_status': 'ACTIVE',
             'security_id_origin': 'NEW_LISTING', 'other': 'preserve'},
            {'security_id': 'US-2', 'name': 'Company Common Stock', 'listing_status': 'ACTIVE'},
            {'security_id': 'US-3', 'name': 'Company Preferred Stock', 'listing_status': 'QUARANTINED_IDENTITY'}]}
        class Drive:
            def __init__(self):
                self.data = {'US/CURRENT_UNIVERSE.json': compact(doc)}
            def read(self, path):
                return self.data[path]
            def file(self, path):
                return path in self.data
            def put(self, path, data, expected_sha=None, **kwargs):
                if expected_sha is not None:
                    assert expected_sha == digest(self.data[path])
                self.data[path] = data
        drive = Drive()
        self.assertEqual(module.repair(drive)['changes'], 1)
        self.assertEqual(len(drive.data), 1)
        answer = module.repair(drive, apply=True)
        self.assertEqual(answer['new_listing_excluded'], 1)
        after = json.loads(drive.read('US/CURRENT_UNIVERSE.json'))
        self.assertEqual(after['updated_at_myt'], doc['updated_at_myt'])
        self.assertEqual(after['securities'][1:], doc['securities'][1:])
        self.assertEqual(after['securities'][0]['other'], 'preserve')
        self.assertEqual(len(after['securities']), 3)
        receipt = json.loads(next(v for k, v in drive.data.items() if '/CONTROL/' in k))
        self.assertEqual(receipt['changes'][0]['before'], {'listing_status': 'ACTIVE'})
        self.assertEqual(module.repair(drive, apply=True)['changes'], 0)

    def test_official_parser_excludes_non_common_without_excluding_substrings(self):
        header = 'Symbol|Security Name|Test Issue|ETF\n'
        body = ''.join(f'S{i}|Company {i} Common Stock|N|N\n' for i in range(1000))
        names = ['Alpha Warrants', 'Beta Preferred Stock', 'Gamma Rights',
                 'Delta Units', 'Some REIT', 'Closed End Fund',
                 'Senior Notes due 2030', 'Trust Preferred Securities',
                 'Alpha Acquisition Class A Ordinary Shares', 'Alpha SPAC']
        body += ''.join(f'BAD{i}|{name}|N|N\n' for i, name in enumerate(names))
        body += 'KEEP|Funding Corporation Common Stock|N|N\n'
        data = (header + body + 'File Creation Time: 10072026\n').encode()
        parsed = universe.parse_us({'nasdaqlisted.txt': data})
        self.assertEqual(len(parsed), 1001)
        self.assertIn('KEEP', parsed)
        self.assertFalse(any(key.startswith('BAD') for key in parsed))

    def test_weekly_list_cannot_reactivate_any_existing_gate(self):
        statuses = ['QUARANTINED_DATA_GAP', 'QUARANTINED_IDENTITY',
                    'QUARANTINED_DATA_QUALITY', 'QUARANTINED_UNCONFIRMED_SOURCE',
                    'EXCLUDED_NON_COMMON', 'EXCLUDED_SPAC', 'ACTIVE']
        rows = [{'security_id': f'US-{i:06d}', 'market': 'US', 'ticker': f'T{i}',
                 'name': f'Company {i}', 'listing_status': status,
                 'identity_review': True} for i, status in enumerate(statuses, 1)]
        doc = {'securities': rows}
        class Drive:
            def read(self, path):
                return compact(doc)
            def put(self, path, value, **kwargs):
                if path == 'US/CURRENT_UNIVERSE.json':
                    self.saved = json.loads(value)
        drive = Drive()
        observed = {x['ticker']: {'ticker': x['ticker'], 'name': x['name'],
                                'exchange': 'NASDAQ'} for x in rows}
        with patch('universe.current_universe'), \
                patch('universe.source_bytes', return_value={'nasdaqlisted.txt': b'official'}), \
                patch('universe.parse_us', return_value=observed), \
                patch('universe.append_queue'):
            universe.refresh(drive, 'US')
        self.assertEqual([x['listing_status'] for x in drive.saved['securities']], statuses)
        self.assertTrue(all(x['identity_review'] for x in drive.saved['securities']))

    def test_unknown_listing_history_is_bounded_to_base_window(self):
        security = {'security_id': 'US-009999', 'market': 'US', 'ticker': 'NEW',
                    'security_id_origin': 'NEW_LISTING', 'listing_status': 'ACTIVE'}
        class Drive:
            def put(self, *args, **kwargs):
                pass
        with patch('foundation.closed_dates_since', return_value=['2026-10-05']) as calendar, \
                patch('foundation.fetch_security', return_value=([], ['FETCH_FAILED'], [], 'NO_ROWS')) as fetch:
            result = append_daily_date(Drive(), 'US', '2026-10-05', [security],
                                       {'US-009999': None}, set(), 1,
                                       ['2024-09-27', '2026-09-25'])
        calendar.assert_called_once_with('US', '2024-09-27')
        self.assertEqual(fetch.call_args.args[1], ['2024-09-27', '2026-09-25', '2026-10-05'])
        self.assertEqual(result['status'], 'MARKET_WIDE_DATA_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
