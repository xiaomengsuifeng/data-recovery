import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recovery_core.common import RecoveryError, read_json, write_json
from recovery_core.control import TaskControl, task_scope
from recovery_core.service import recover, scan
from test_core import FakeBackend, body
from test_desktop_core import boot


class RecoveryFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / 'source.img'
        self.image.write_bytes(boot() + bytes(512 * 63))
        self.session, self.destination = self.root / 'session', self.root / 'recovered'
        self.backend = FakeBackend(body('/a.txt') + body('/b.txt', inode='66-128-1'),
                                   {'65-128-1': b'hello', '66-128-1': b'world'})
        self.report = scan(self.image, self.session, backend=self.backend)

    def test_full_target_does_not_extract_and_keeps_failure_report(self):
        with patch('shutil.disk_usage', return_value=SimpleNamespace(free=0)), \
                patch.object(self.backend, 'extract') as extraction:
            result = recover(self.session, self.destination, backend=self.backend)
        extraction.assert_not_called()
        self.assertEqual(result['failed_count'], 2)
        self.assertEqual(read_json(self.destination / 'recovery.json'), result)
        self.assertIn('空间不足', result['results'][0]['warnings'][-1])

    def test_target_space_query_failure_preserves_processed_state(self):
        with patch('shutil.disk_usage', side_effect=OSError('target unavailable')):
            result = recover(self.session, self.destination, backend=self.backend)
        self.assertEqual(result['processed_count'], 1)
        self.assertEqual(result['failed_count'], 1)
        self.assertIn('剩余空间', result['results'][0]['warnings'][-1])
        self.assertEqual(read_json(self.destination / 'recovery.json'), result)

    def test_report_falls_back_to_session_when_target_cannot_save_it(self):
        def save(path, data):
            if path.parent == self.destination:
                raise OSError('target full')
            return write_json(path, data)
        with patch('recovery_core.service.write_json', side_effect=save):
            result = recover(self.session, self.destination, backend=self.backend)
        fallback = Path(result['report_path'])
        self.assertEqual(fallback.parent, self.session)
        self.assertEqual(read_json(fallback), result)
        self.assertEqual(result['exported_unverified_count'], 2)
        self.assertEqual(Path(result['destination']), self.destination)
        self.assertEqual((Path(result['destination']) / result['results'][0]['saved_path']).read_bytes(), b'hello')

    def test_both_report_locations_failing_is_not_silent_success(self):
        with patch('recovery_core.service.write_json', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(RecoveryError, '报告无法写入'):
                recover(self.session, self.destination, backend=self.backend)

    def test_live_disconnect_during_export_keeps_partial_bytes_and_stops_next_file(self):
        report = copy.deepcopy(self.report)
        report['source']['kind'] = 'windows_volume'
        (self.session / 'session.json').write_text(json.dumps(report), encoding='utf-8')
        def disconnected(*args):
            args[-2].write(b'he')
            raise OSError('device disconnected')
        with patch('recovery_core.service.check_identity',
                   side_effect=[report['source'], RecoveryError('源盘已断开'), RecoveryError('源盘已断开')]), \
                patch('recovery_core.service.ensure_safe_locations'), \
                patch('recovery_core.service.read_volume_boot'), \
                patch.object(self.backend, 'extract', side_effect=disconnected) as extraction:
            result = recover(self.session, self.destination, backend=self.backend)
        self.assertEqual(extraction.call_count, 1)
        self.assertEqual((result['status'], result['source_verification']), ('source_changed', 'changed_or_unreadable'))
        self.assertEqual(result['source_error'], '源盘已断开')
        self.assertEqual(result['processed_count'], 1)
        self.assertEqual(result['partial_count'], 1)
        self.assertEqual((self.destination / result['results'][0]['saved_path']).read_bytes(), b'he')

    def test_cancel_before_skipped_files_does_not_traverse_whole_selection(self):
        control = TaskControl()
        def created(path):
            from recovery_core.common import new_directory
            directory = new_directory(path)
            control.cancelled.set()
            return directory
        with patch('recovery_core.service.new_directory', side_effect=created), task_scope(control):
            result = recover(self.session, self.destination, backend=self.backend, max_file_bytes=1)
        self.assertEqual((result['status'], result['processed_count']), ('cancelled', 0))
        self.assertEqual(read_json(self.destination / 'recovery.json'), result)

    def test_target_disappearing_during_cancellation_does_not_discard_report(self):
        original = Path.is_file
        def unavailable(path):
            if path.is_relative_to(self.destination / 'files'):
                raise OSError('target disconnected')
            return original(path)
        with patch.object(self.backend, 'extract', side_effect=KeyboardInterrupt), \
                patch.object(Path, 'is_file', unavailable):
            result = recover(self.session, self.destination, backend=self.backend)
        self.assertEqual((result['status'], result['processed_count']), ('cancelled', 1))
        self.assertIn('取消后无法检查', result['results'][0]['warnings'][-1])
        self.assertEqual(read_json(self.destination / 'recovery.json'), result)


if __name__ == '__main__':
    unittest.main()
