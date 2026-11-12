"""Logging a archivo con captura de stdout/stderr y excepciones no manejadas.

Portado de app_ultra._setup_logging (v8).
"""
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


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
        return str(log_file)
    except Exception:
        return None
