import time
from PySide6.QtCore import QThread, Signal
from recovery_core.control import TaskControl, task_scope


class Worker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    changed = Signal(object)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.last_progress = 0
        self.last_phase = None
        self.control = TaskControl(progress=self.report_progress)

    def report_progress(self, value):
        now = time.monotonic()
        if value["phase"] != self.last_phase or now - self.last_progress > 0.075:
            self.last_progress, self.last_phase = now, value["phase"]
            self.changed.emit(value)

    def run(self):
        try:
            with task_scope(self.control):
                result = self.operation()
            self.result_ready.emit(result)
        except KeyboardInterrupt:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)

    def cancel(self):
        self.control.cancelled.set()
