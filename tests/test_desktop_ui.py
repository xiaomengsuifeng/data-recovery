import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from recovery_desktop.app import RecoveryWindow, STYLE
    from recovery_desktop.worker import Worker
    from recovery_core.control import checkpoint
    from recovery_core.service import scan
    from test_core import FakeBackend
    QT = True
except ImportError:
    QT = False


@unittest.skipUnless(QT, 'Install desktop dependencies for GUI tests')
class DesktopUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.window = RecoveryWindow()
        self.errors = []
        self.window.show_error = self.errors.append
        self.window.backend = lambda: FakeBackend()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        from test_desktop_core import boot
        image = self.root / 'test.img'
        image.write_bytes(boot() + bytes(512 * 63))
        self.report = scan(image, self.root / 'scan', backend=FakeBackend())
        self.window.load_report(self.root / 'scan', self.report)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.wait_task()
        self.window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def wait_task(self):
        deadline = time.monotonic() + 10
        while self.window.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertIsNone(self.window.worker)

    def test_filter_selection_and_bulk_clear(self):
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.select_visible()
        self.assertTrue(self.window.recover_button.isEnabled())
        self.window.search.setText('missing')
        self.assertEqual(self.window.proxy.rowCount(), 0)
        self.assertEqual(len(self.window.model.checked), 1)
        self.window.clear_selection()
        self.assertFalse(self.window.recover_button.isEnabled())

    def test_preview_and_export_end_to_end(self):
        self.window.table.setCurrentIndex(self.window.proxy.index(0,1))
        self.window.request_preview()
        self.wait_task()
        self.assertEqual(self.window.preview_text.toPlainText(), 'hello')
        self.window.select_visible()
        self.window.export_to(self.root, sorted(self.window.model.checked))
        self.wait_task()
        self.assertFalse(self.errors)
        self.assertEqual(self.window.pages.currentIndex(), 2)
        self.assertEqual((self.window.last_output / 'files/Documents/report.txt').read_bytes(), b'hello')
        self.assertTrue((self.window.last_output / 'recovery.json').is_file())

    def test_worker_cancel_restores_ui(self):
        def work():
            while True:
                checkpoint()
                time.sleep(.01)
        self.window.run_task('waiting', work, lambda r: None)
        self.window.cancel_task()
        self.wait_task()
        self.assertTrue(self.window.home_button.isEnabled())
        self.assertFalse(self.window.cancel_button.isEnabled())

    def test_failed_task_restores_controls_and_can_retry_preview(self):
        def denied():
            raise PermissionError('source denied')
        self.window.run_task('reading', denied, lambda r: self.fail('Unexpected success'))
        self.wait_task()
        self.assertEqual(self.errors, ['source denied'])
        self.assertTrue(self.window.home_button.isEnabled())
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 1))
        self.window.request_preview()
        self.wait_task()
        self.assertEqual(self.window.preview_text.toPlainText(), 'hello')

    def test_elevation_cancel_keeps_window_session_and_selection(self):
        from recovery_core.common import RecoveryError
        self.window.select_visible()
        session, checked = self.window.session, set(self.window.model.checked)
        self.window.workspace.setText(str(self.root))
        with patch('recovery_desktop.app.elevate', side_effect=RecoveryError('已取消管理员授权')) as launch:
            self.window.restart_admin()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.session, session)
        self.assertEqual(self.window.model.checked, checked)
        self.assertIn('已取消管理员授权', self.errors[-1])
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments[arguments.index('--session') + 1], str(session.resolve()))
        self.assertEqual(arguments[arguments.index('--workspace') + 1], str(self.root.resolve()))

    def test_partial_failure_explains_reason_and_pending_files(self):
        self.window.last_output = self.root / 'result'
        self.window.show_recovery(dict(status='completed', exported_unverified_count=0,
            partial_count=0, failed_count=1, skipped_count=0, selected_count=2, processed_count=1,
            results=[dict(status='failed', original_path='/a.txt', observed_path='/a.txt',
                          saved_path=None, warnings=['目标磁盘空间不足'])]))
        self.assertEqual(self.window.complete_title.text(), '保存未全部完成')
        self.assertIn('目标磁盘空间不足', self.window.complete_text.toPlainText())
        self.assertIn('还有 1 个', self.window.complete_text.toPlainText())

    def test_closing_during_work_cancels_and_waits_for_worker_cleanup(self):
        def work():
            while True:
                checkpoint()
                time.sleep(.01)
        self.window.run_task('reading', work, lambda result: None)
        self.window.close()
        self.wait_task()
        self.assertFalse(self.window.isVisible())

    def test_deep_png_scan_preview_save_and_reopen(self):
        from test_carving import BitmapBackend, make_image, png
        from recovery_core.common import read_json
        payload = png()
        image = make_image(self.root / 'png.img', [(1021, payload)])
        self.window.backend = lambda: BitmapBackend()
        self.window.go_home()
        self.window.source_edit.setText(str(image))
        self.window.workspace.setText(str(self.root))
        self.window.partition.clear()
        self.window.partition.addItem('Test NTFS', {'offset': 3, 'sector_size': 512})
        self.window.deep_png.setChecked(True)
        self.window.start_scan()
        self.assertFalse(self.window.deep_png.isEnabled())
        self.wait_task()
        self.assertFalse(self.errors)
        self.assertTrue(self.window.report['scan_options']['deep_png'])
        self.assertEqual(self.window.model.data(self.window.model.index(0, 5)), '深度扫描·生成名称')
        self.assertEqual(self.window.model.data(self.window.model.index(0, 4)), '原目录未知')
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 1))
        self.window.request_preview()
        self.wait_task()
        self.assertFalse(self.window.preview_image.pixmap().isNull())
        self.assertIn('原名与目录未知', self.window.preview_note.text())
        self.window.resize(1020, 720)
        self.app.processEvents()
        self.assertLess(self.window.preview_image.geometry().bottom(), self.window.preview_note.geometry().top())
        self.assertLess(self.window.preview_note.geometry().bottom(), self.window.preview_button.geometry().top())
        self.window.select_visible()
        self.window.export_to(self.root, sorted(self.window.model.checked))
        self.wait_task()
        self.assertFalse(self.errors)
        saved, = read_json(self.window.last_output / 'recovery.json')['results']
        self.assertEqual((self.window.last_output / saved['saved_path']).read_bytes(), payload)
        self.window.load_report(self.window.session, read_json(self.window.session / 'session.json'))
        self.assertEqual(self.window.proxy.rowCount(), 1)

    def test_deep_png_cannot_remain_enabled_in_live_volume_mode(self):
        self.window.choose_source = lambda: None
        self.window.deep_png.setChecked(True)
        self.window.deep_log.setChecked(True)
        self.window.mode.setCurrentIndex(1)
        self.assertFalse(self.window.deep_png.isChecked())
        self.assertFalse(self.window.deep_png.isEnabled())
        self.assertFalse(self.window.deep_log.isChecked())
        self.assertFalse(self.window.deep_log.isEnabled())
        self.window.set_busy(False)
        self.assertFalse(self.window.deep_png.isEnabled())
        self.assertFalse(self.window.deep_log.isEnabled())
        self.window.mode.setCurrentIndex(0)
        self.assertTrue(self.window.deep_png.isEnabled())
        self.assertFalse(self.window.deep_png.isChecked())
        self.assertTrue(self.window.deep_log.isEnabled())
        self.assertFalse(self.window.deep_log.isChecked())

    def test_log_fragment_scan_preview_save_and_reopen(self):
        from test_ntfs_log import sample
        from recovery_core.common import read_json
        image, backend, original, _ = sample(self.root, allocated=(80,))
        self.window.backend = lambda: backend
        self.window.go_home()
        self.window.source_edit.setText(str(image))
        self.window.workspace.setText(str(self.root))
        self.window.partition.clear()
        self.window.partition.addItem('Test NTFS', {'offset': 3, 'sector_size': 512})
        self.window.deep_log.setChecked(True)
        self.window.start_scan()
        self.assertFalse(self.window.deep_log.isEnabled())
        self.wait_task()
        self.assertFalse(self.errors)
        self.assertTrue(self.window.report['scan_options']['deep_log'])
        self.assertEqual(self.window.model.data(self.window.model.index(0, 5)), '旧日志·不完整片段')

        self.assertIn('（片段）', self.window.model.data(self.window.model.index(0, 1)))
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 1))
        self.window.request_preview()
        self.wait_task()
        self.assertIn('不完整片段', self.window.preview_note.text())
        self.assertIn('4096', self.window.preview_note.text())
        self.window.select_visible()
        self.window.export_to(self.root, sorted(self.window.model.checked))
        self.wait_task()
        self.assertFalse(self.errors)
        result = read_json(self.window.last_output / 'recovery.json')
        self.assertEqual((result['exported_unverified_count'], result['partial_count']), (0, 1))
        saved, = result['results']
        self.assertEqual((self.window.last_output / saved['saved_path']).read_bytes(), original[4096:])
        self.assertIn('.fragment-00001000.txt', saved['saved_path'])
        self.window.load_report(self.window.session, read_json(self.window.session / 'session.json'))
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.assertEqual(self.window.model.data(self.window.model.index(0, 5)), '旧日志·不完整片段')

    def test_source_controls_remain_readable_at_minimum_window_size(self):
        self.window.go_home()
        self.window.resize(1020, 720)
        self.app.processEvents()
        for control in (self.window.mode, self.window.source_edit, self.window.source_button,
                        self.window.partition, self.window.workspace_button, self.window.scan_button):
            self.assertGreaterEqual(control.height(), control.sizeHint().height())
        self.assertGreater(self.window.home_scroll.verticalScrollBar().maximum(), 0)

    def test_fragmented_jpeg_scan_preview_export_and_reopen(self):
        from test_carving import BitmapBackend, make_image
        from test_jpeg import qt_jpeg
        from recovery_core.common import read_json
        payload = qt_jpeg()
        image = make_image(self.root / 'jpeg.img', [(512, payload[:1024]), (2048, payload[1024:])])
        self.window.backend = lambda: BitmapBackend(b'\x09' + bytes(7))
        self.window.go_home()
        self.window.source_edit.setText(str(image))
        self.window.workspace.setText(str(self.root))
        self.window.partition.clear()
        self.window.partition.addItem('Test NTFS', {'offset': 3, 'sector_size': 512})
        self.window.deep_jpeg.setChecked(True)
        self.window.reassemble_jpeg.setChecked(True)
        self.window.start_scan()
        self.wait_task()
        self.assertFalse(self.errors)
        self.assertEqual(self.window.report['jpeg']['reconstructed_count'], 1)
        self.assertIn('碎片', self.window.model.data(self.window.model.index(0, 5)))
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 1))
        self.window.request_preview()
        self.wait_task()
        self.assertFalse(self.window.preview_image.pixmap().isNull())
        self.assertIn('重组', self.window.preview_note.text())
        self.window.select_visible()
        self.window.export_to(self.root, sorted(self.window.model.checked))
        self.wait_task()
        result = read_json(self.window.last_output / 'recovery.json')['results'][0]
        self.assertEqual((self.window.last_output / result['saved_path']).read_bytes(), payload)
        self.window.load_report(self.window.session, read_json(self.window.session / 'session.json'))
        self.assertEqual(self.window.proxy.rowCount(), 1)

    def test_acquisition_resume_report_and_load_image(self):
        from recovery_core.acquisition import acquire
        from recovery_core.control import OperationCancelled
        image = self.root / 'test.img'
        data = image.read_bytes()
        def interrupted(path, offset, size, timeout):
            if offset:
                raise OperationCancelled()
            return data[:size]
        report = acquire(image, self.root / 'capture', block_bytes=512, reader=interrupted)
        self.window.show_acquisition(report)
        self.assertEqual(self.window.pages.currentIndex(), 3)
        self.assertIn('pending', self.window.acquisition_text.toPlainText())
        self.window.retry_acquisition()
        self.wait_task()
        self.assertFalse(self.errors)
        self.assertEqual(self.window.last_acquisition['status'], 'completed')
        self.assertEqual(Path(self.window.last_acquisition['image']).read_bytes(), data)
        self.window.load_acquired()
        self.wait_task()
        self.assertEqual(self.window.mode.currentIndex(), 0)
        self.assertEqual(self.window.partition.count(), 1)

    def test_exfat_rejects_ntfs_log_before_starting_worker(self):
        self.window.go_home()
        self.window.workspace.setText(str(self.root))
        self.window.partition.clear()
        self.window.partition.addItem('exFAT', {'filesystem': 'exFAT'})
        self.window.deep_log.setChecked(True)
        self.window.start_scan()
        self.assertIsNone(self.window.worker)
        self.assertIn('exFAT', self.errors[0])
        self.window.home_scroll.ensureWidgetVisible(self.window.scan_button)
        self.app.processEvents()
        viewport = self.window.home_scroll.viewport()
        self.assertTrue(viewport.rect().contains(self.window.scan_button.mapTo(viewport, self.window.scan_button.rect().center())))


if __name__ == '__main__': unittest.main()
