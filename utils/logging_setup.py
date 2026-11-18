"""Logging a archivo con captura de stdout/stderr y excepciones no manejadas.

Portado de app_ultra._setup_logging (v8).

Diagnóstico de cierres y cuelgues (lo que el log normal no ve):
- Cierres nativos (un fallo dentro de Qt o FFmpeg tumba el proceso sin pasar
  por Python): faulthandler escribe las pilas de todos los hilos en
  logs/pomogrowth_fallos.log. Al arrancar se apunta una línea con la versión,
  la hora y el pid, para saber de qué sesión es lo que venga detrás; pasado
  de ~1 MB, el archivo se renombra a .1 y se empieza otro.
- Cuelgues del hilo de la interfaz: start_ui_watchdog() re-arma cada segundo
  faulthandler.dump_traceback_later. Si el hilo de la interfaz se queda más
  de ~5 s sin volver al bucle de eventos, el temporizador nativo vuelca las
  pilas en ese mismo archivo y, cuando el hilo se recupera, una línea en el
  log de la sesión lo dice. Mientras todo va bien no escribe nada.
scrub() quita de un texto lo que no debe acabar en un log (enlaces, rutas y
títulos entre comillas angulares).
"""
import faulthandler
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

FAULT_LOG_NAME = "pomogrowth_fallos.log"
FAULT_LOG_MAX_BYTES = 1_000_000
UI_BLOCK_TIMEOUT_S = 5.0
UI_WATCHDOG_INTERVAL_MS = 1000
_fault_file = None  # abierto toda la sesión: faulthandler escribe en su descriptor


def setup_logging(version: str = "") -> Optional[str]:
    """`version` (Config.APP_VERSION) va en la primera línea del registro."""
    try:
        base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent.parent
        log_dir = base / "logs"
        log_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"pomogrowth_{ts}.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.FileHandler(log_file, encoding="utf-8")]
        )

        class _StreamToLogger:
            def __init__(self, level):
                self.level = level

            def write(self, buf):
                for line in (buf or '').rstrip().splitlines():
                    logging.log(self.level, line)

            def flush(self):
                pass

        sys.stdout = _StreamToLogger(logging.INFO)
        sys.stderr = _StreamToLogger(logging.ERROR)

        def _handle_exception(exc_type, exc_value, exc_traceback):
            if issubclass(exc_type, KeyboardInterrupt):
                return
            logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        sys.excepthook = _handle_exception
        logging.info("PomoGrowth %s iniciado", version or "(sin versión)")
        enable_fault_log(log_dir, version)
        return str(log_file)
    except Exception:
        return None


def enable_fault_log(log_dir: Path, version: str = ""):
    """Activa faulthandler sobre logs/pomogrowth_fallos.log (ver el docstring
    del módulo). Devuelve el archivo abierto, o None si no se pudo (la app
    sigue igual, sin ese diagnóstico)."""
    global _fault_file
    path = Path(log_dir) / FAULT_LOG_NAME
    try:
        if path.exists() and path.stat().st_size > FAULT_LOG_MAX_BYTES:
            path.replace(path.with_name(FAULT_LOG_NAME + ".1"))
    except OSError:
        pass
    try:
        handle = open(path, "a", encoding="utf-8", errors="replace")
        handle.write(f"--- PomoGrowth {version or '(sin versión)'} · "
                     f"{datetime.now():%Y-%m-%d %H:%M:%S} · pid {os.getpid()} ---\n")
        handle.flush()  # faulthandler escribe directo al descriptor, sin este búfer
        faulthandler.enable(file=handle, all_threads=True)
    except (OSError, ValueError, RuntimeError):
        return None
    _fault_file = handle
    return handle


def fault_log_file():
    """El archivo de fallos de la sesión (None sin setup_logging o si falló)."""
    return _fault_file


class _UiWatchdog:
    """Ver start_ui_watchdog. `clock` se puede cambiar en los tests."""

    def __init__(self, handle, timeout_s: float, interval_ms: int, parent=None):
        from PySide6.QtCore import QTimer
        self.handle = handle
        self.timeout_s = float(timeout_s)
        self.clock = time.monotonic
        self._last = None
        self._size = self._file_size()
        self._timer = QTimer(parent)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._beat)

    def _file_size(self) -> int:
        try:
            return os.fstat(self.handle.fileno()).st_size
        except (OSError, ValueError):
            return 0

    def start(self) -> None:
        self._last = self.clock()
        self._arm()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        faulthandler.cancel_dump_traceback_later()

    def _arm(self) -> None:
        try:
            faulthandler.dump_traceback_later(self.timeout_s, repeat=False,
                                              file=self.handle, exit=False)
        except (OSError, ValueError, RuntimeError):
            self._timer.stop()  # sin archivo usable no hay vigía

    def _beat(self) -> None:
        now = self.clock()
        gap = now - (self._last if self._last is not None else now)
        self._last = now
        size = self._file_size()
        if size > self._size:
            # El temporizador nativo saltó: el hilo estuvo bloqueado y sus
            # pilas ya están en el archivo de fallos
            logging.warning("El hilo de la interfaz estuvo bloqueado ~%.1f s; "
                            "sus pilas están en %s", gap, FAULT_LOG_NAME)
        self._size = size
        self._arm()


def start_ui_watchdog(handle, parent=None, timeout_s: float = UI_BLOCK_TIMEOUT_S,
                      interval_ms: int = UI_WATCHDOG_INTERVAL_MS):
    """Arranca el vigía del hilo de la interfaz sobre el archivo `handle`
    (fault_log_file()). Devuelve el vigía (con stop(): llamarlo al salir,
    para que una salida lenta no cuente como bloqueo) o None sin archivo."""
    if handle is None:
        return None
    watchdog = _UiWatchdog(handle, timeout_s, interval_ms, parent)
    watchdog.start()
    return watchdog


_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\)[^\s«»\"']*")
_TITLE = re.compile(r"«[^»]*»")


def scrub(text, limit: int = 200) -> str:
    """Texto apto para el log: sin enlaces (pueden llevar claves), sin rutas
    (llevan el nombre de usuario) y sin títulos «…» (nombres de pistas o
    tareas), y como mucho `limit` caracteres."""
    text = _URL.sub("<enlace>", str(text or ""))
    text = _PATH.sub("<ruta>", text)
    text = _TITLE.sub("«…»", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
