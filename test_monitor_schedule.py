from datetime import datetime
import tempfile
import unittest
from pathlib import Path

from monitor_schedule import next_monitor_time, ScheduleStore


def timestamp(day, clock):
    return int(datetime.fromisoformat(f'2026-11-{day}T{clock}+08:00').timestamp())


class QuietHoursTest(unittest.TestCase):
    def test_next_run_defers_midnight_through_eight(self):
        self.assertEqual(next_monitor_time(timestamp(27, '18:00')), timestamp(28, '09:00'))
        self.assertEqual(next_monitor_time(timestamp(28, '02:59')), timestamp(28, '09:00'))
        self.assertEqual(next_monitor_time(timestamp(28, '03:15')), timestamp(28, '09:15'))
        self.assertEqual(next_monitor_time(timestamp(26, '14:46'), timestamp(28, '22:34')), timestamp(29, '09:00'))

    def test_overdue_and_failed_jobs_wait_until_nine(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ScheduleStore(Path(folder) / 'test.db')
            store.register('channels', 'video', timestamp(27, '16:00'), 'group')
            store.complete('channels', 'video', False, timestamp(27, '23:58'))
            self.assertFalse(store.claim_due(timestamp(28, '00:00')))
            self.assertFalse(store.claim_due(timestamp(28, '08:59:59')))
            self.assertEqual(len(store.claim_due(timestamp(28, '09:00'))), 1)
            self.assertFalse(store.claim_due(timestamp(28, '09:00:01')))
