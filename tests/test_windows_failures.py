import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from recovery_core.common import RecoveryError
from recovery_core.control import OperationCancelled, TaskControl, task_scope
from recovery_core.tsk import run_bounded
from recovery_core.windows import _query_volumes, _shell_elevate, elevate, read_volume_boot, validate_volume_rows


class WindowsFailureTests(unittest.TestCase):
    def test_query_decodes_unicode_and_enforces_output_budget(self):
        row = dict(mount='E:\\', label='测试盘', guid='volume-id', size=102400,
                   disk_numbers=[2], disk_ids=['disk-2'], sector_size=512, system=False)
        def result(command, output, **limits):
            self.assertEqual(limits, dict(limit=4 * 1024 * 1024, timeout=30))
            self.assertIn('-NonInteractive', command)
            output.write(json.dumps([row], ensure_ascii=False).encode('utf-8-sig'))
        with patch('recovery_core.windows.run_bounded', side_effect=result):
            self.assertEqual(_query_volumes('query'), [row])
        for field, value in [('mount', None), ('guid', None), ('label', 1), ('system', 'false')]:
            with self.subTest(field=field), self.assertRaises(RecoveryError):
                validate_volume_rows([dict(row, **{field: value})])

    def test_bad_query_output_and_process_failure_are_actionable(self):
        for raw in (b'not json', b'\xff', b'{}'):
            with self.subTest(raw=raw), patch('recovery_core.windows.run_bounded',
                                            side_effect=lambda cmd, output, **kw: output.write(raw)):
                with self.assertRaisesRegex(RecoveryError, '磁盘列表'):
                    _query_volumes('query')
        for error in (FileNotFoundError(), RecoveryError('timeout')):
            with patch('recovery_core.windows.run_bounded', side_effect=error):
                with self.assertRaisesRegex(RecoveryError, '连接.*Storage'):
                    _query_volumes('query')

    def test_cancelling_query_reaps_its_real_waiting_process(self):
        control = TaskControl()
        children = []
        original = subprocess.Popen
        def started(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        def query(command, output, **limits):
            timer = threading.Timer(.2, control.cancelled.set)
            timer.start()
            try:
                run_bounded([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], output, **limits)
            finally:
                timer.cancel()
                timer.join()
        started_at = time.monotonic()
        with patch('recovery_core.tsk.subprocess.Popen', side_effect=started), \
                patch('recovery_core.windows.run_bounded', side_effect=query), task_scope(control):
            with self.assertRaises(OperationCancelled):
                _query_volumes('query')
        self.assertLess(time.monotonic() - started_at, 5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    @unittest.skipUnless(os.name == 'nt', 'Windows volume error messages')
    def test_permission_and_disconnection_messages_are_distinct(self):
        for error, message in ((PermissionError('access denied'), '管理员权限'),
                               (OSError('device disconnected'), '断开')):
            with patch('builtins.open', side_effect=error), self.assertRaisesRegex(RecoveryError, message):
                read_volume_boot({'path': r'\\.\Z:'})
        with patch('builtins.open', return_value=io.BytesIO(b'')):
            with self.assertRaisesRegex(RecoveryError, 'NTFS'):
                read_volume_boot({'path': r'\\.\Z:'})

    @unittest.skipUnless(os.name == 'nt', 'Windows elevation error mapping')
    def test_elevation_cancel_failure_and_success(self):
        for code, message in ((1223, '已取消管理员授权'), (5, 'Windows 错误 5')):
            with patch('recovery_core.windows._shell_elevate', return_value=code), \
                    self.assertRaisesRegex(RecoveryError, message):
                elevate(['-B', 'test.py'])
        with patch('recovery_core.windows._shell_elevate', return_value=0):
            elevate(['-B', 'test.py'])

    @unittest.skipUnless(os.name == 'nt', 'Native ShellExecuteEx structure')
    def test_shell_execute_uses_unicode_arguments_and_preserves_last_error(self):
        import ctypes
        from types import SimpleNamespace
        captured = []
        def execute(pointer):
            info = pointer._obj
            captured.append((info.cbSize, ctypes.sizeof(info), info.lpVerb, info.lpFile, info.lpParameters, info.fMask))
            ctypes.set_last_error(1223)
            return False
        with patch('ctypes.WinDLL', return_value=SimpleNamespace(ShellExecuteExW=execute)):
            self.assertEqual(_shell_elevate(['-B', 'C:\\测试 目录\\launch.pyw']), 1223)
        size, actual_size, verb, executable, parameters, flags = captured[0]
        self.assertEqual(size, actual_size)
        self.assertEqual(verb, 'runas')
        self.assertEqual(executable, sys.executable)
        self.assertIn('"C:\\测试 目录\\launch.pyw"', parameters)
        self.assertEqual(flags, 0x500)


if __name__ == '__main__':
    unittest.main()
