"""Cooperative task cancellation and progress, scoped to one worker thread."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from typing import Callable


class OperationCancelled(KeyboardInterrupt):
    pass


@dataclass
class TaskControl:
    cancelled: Event = field(default_factory=Event)
    progress: Callable[[dict], None] = field(default=lambda event: None)


_active = ContextVar("recovery_task", default=None)


def checkpoint():
    active = _active.get()
    if active and active.cancelled.is_set():
        raise OperationCancelled()


def progress(phase: str, completed: int = 0, total: int = 0, message: str = ""):
    checkpoint()
    active = _active.get()
    if active:
        active.progress(dict(phase=phase, completed=completed, total=total, message=message))


@contextmanager
def task_scope(control: TaskControl):
    token = _active.set(control)
    try:
        yield
    finally:
        _active.reset(token)
