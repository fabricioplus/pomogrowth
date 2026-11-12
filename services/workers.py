"""Puente de threading: ejecutar funciones bloqueantes fuera del hilo de UI.

run_async(fn, on_done, on_error) — reemplazo 1:1 del patrón v8
`threading.Thread + root.after(0, callback)`. Las señales cross-thread de Qt
llegan encoladas al hilo de UI automáticamente.

run_in_thread(fn, on_done, on_error) — igual, pero en un hilo daemon propio:
para esperas de red largas (la música) que no deben ocupar el QThreadPool
global (lo comparten Asana y el resto) ni retener el cierre de la app (al
salir se espera a todo ese pool; a un hilo daemon, no).
"""
import threading
import time
import traceback
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Reporter(QObject):
    finished = Signal(object)
    failed = Signal(object)


def _execute(fn: Callable, reporter: _Reporter) -> None:
    try:
        result = fn()
    except Exception as e:
        traceback.print_exc()
        reporter.failed.emit(e)
    else:
        reporter.finished.emit(result)


class _Job(QRunnable):
    def __init__(self, fn: Callable, reporter: _Reporter):
        super().__init__()
        self._fn = fn
        self._reporter = reporter

    def run(self) -> None:
        _execute(self._fn, self._reporter)


# Mantener vivos los reporters mientras el job corre (sin esto, el GC de
# Python puede destruir el QObject antes de que emita)
_pending = set()


def _reporter_for(on_done: Optional[Callable], on_error: Optional[Callable]) -> _Reporter:
    reporter = _Reporter()
    _pending.add(reporter)

    def _cleanup(*_):
        _pending.discard(reporter)

    if on_done:
        reporter.finished.connect(on_done)
    if on_error:
        reporter.failed.connect(on_error)
    reporter.finished.connect(_cleanup)
    reporter.failed.connect(_cleanup)
    return reporter


def run_async(fn: Callable, on_done: Optional[Callable] = None,
              on_error: Optional[Callable] = None) -> None:
    QThreadPool.globalInstance().start(_Job(fn, _reporter_for(on_done, on_error)))


_threads = set()
_threads_lock = threading.Lock()


def run_in_thread(fn: Callable, on_done: Optional[Callable] = None,
                  on_error: Optional[Callable] = None) -> None:
    reporter = _reporter_for(on_done, on_error)

    def target() -> None:
        try:
            _execute(fn, reporter)
        finally:
            with _threads_lock:
                _threads.discard(threading.current_thread())

    thread = threading.Thread(target=target, name="pomogrowth-red", daemon=True)
    with _threads_lock:
        _threads.add(thread)
    thread.start()


def wait_threads(timeout: float) -> bool:
    """Espera a que acaben los hilos de run_in_thread (lo usan los tests).
    True si no queda ninguno."""
    end = time.monotonic() + timeout
    while True:
        with _threads_lock:
            alive = [t for t in _threads if t.is_alive()]
        if not alive:
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        alive[0].join(min(left, 0.1))
