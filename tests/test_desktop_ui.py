import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest

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


if __name__ == '__main__': unittest.main()
