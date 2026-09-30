"""Threads <-> UI thread.

- `on_ui(fn)`: run fn on the UI thread (from any thread).
- `ask_on_ui(fn)`: from a worker thread, run fn on the UI thread and wait for its
  result (dialogs a worker needs: host keys, 2FA codes, "file exists" questions).
- `Worker`: one background thread with a job queue; results come back on the UI thread.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication


class _Invoker(QObject):
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.QueuedConnection)

    @staticmethod
    def _run(fn) -> None:
        fn()


_invoker: _Invoker | None = None


def init_bridge() -> None:
    """Call once on the UI thread after QApplication exists."""
    global _invoker
    if _invoker is None:
        _invoker = _Invoker()
        _invoker.moveToThread(QApplication.instance().thread())


def _is_ui_thread() -> bool:
    app = QApplication.instance()
    return app is not None and QThread.currentThread() is app.thread()


def on_ui(fn: Callable[[], Any]) -> None:
    if _is_ui_thread():
        fn()
    else:
        _invoker.call.emit(fn)


def ask_on_ui(fn: Callable[[], Any]) -> Any:
    if _is_ui_thread():
        return fn()
    done = threading.Event()
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 (re-raised in the worker)
            box["e"] = e
        finally:
            done.set()
    _invoker.call.emit(run)
    done.wait()
    if "e" in box:
        raise box["e"]
    return box.get("v")


class Worker:
    """Runs submitted functions one after another on a private thread."""

    def __init__(self, name: str):
        self._q: queue.Queue = queue.Queue()
        self._alive = True
        self.busy = False
        threading.Thread(target=self._loop, name=name, daemon=True).start()

    def submit(self, fn: Callable[[], Any], ok: Callable[[Any], None] | None = None,
               err: Callable[[BaseException], None] | None = None) -> None:
        self._q.put((fn, ok, err))

    def stop(self) -> None:
        self._alive = False
        self._q.put(None)

    def _loop(self) -> None:
        while self._alive:
            item = self._q.get()
            if item is None:
                return
            fn, ok, err = item
            self.busy = True
            try:
                result = fn()
            except BaseException as e:  # noqa: BLE001 (delivered to the UI)
                if err:   # bind now: the loop variables change before the UI runs this
                    on_ui(lambda e=e, cb=err: cb(e))
            else:
                if ok:
                    on_ui(lambda r=result, cb=ok: cb(r))
            finally:
                self.busy = False

