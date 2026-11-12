"""Extracción ÚNICA del core de v8 (app_ultra.py) hacia módulos de v9.

Se ejecuta UNA sola vez. Después, los archivos generados son fuente normal
de v9 y se editan a mano (no regenerar: se perderían los cambios).
"""
import re
import sys
from pathlib import Path

# La v8 se buscaba junto a la carpeta del repo (como en tests/test_core_parity.py).
V9 = Path(__file__).resolve().parent.parent
V8 = V9.parent / "pomogrowth_v8_ultra" / "app_ultra.py"

src = V8.read_text(encoding="utf-8").splitlines(keepends=True)

# Localizar el inicio de cada clase (línea 'class X' a nivel de módulo)
starts = {}
for i, line in enumerate(src):
    m = re.match(r"^class (\w+)", line)
    if m:
        starts[m.group(1)] = i

def block(from_cls: str, to_cls: str) -> str:
    """Devuelve el código desde el inicio de from_cls hasta justo antes de to_cls,
    recortando comentarios/blancos finales que pertenezcan a la sección siguiente."""
    a, b = starts[from_cls], starts[to_cls]
    lines = src[a:b]
    while lines and (lines[-1].strip() == "" or lines[-1].lstrip().startswith("#") or lines[-1].startswith("# ===")):
        lines.pop()
    return "".join(lines) + "\n"

HEADER_COMMON = '''"""{doc}

Extraído de pomogrowth_v8_ultra/app_ultra.py (extract_from_v8.py, una sola vez).
A partir de aquí este archivo es fuente normal de v9.
"""
'''

modules = {
    "core/data_handler.py": (
        HEADER_COMMON.format(doc="Capa de datos: persistencia JSON atómica + API de Asana.")
        + "import time\n"
        + "import threading\n"
        + "import traceback\n"
        + "from collections import defaultdict\n"
        + "from datetime import datetime, timedelta, date\n"
        + "from pathlib import Path\n"
        + "from typing import Dict, Any, List, Optional, Tuple\n\n"
        + "import requests\n\n"
        + "from core.config import Config\n"
        + "from utils.safe_json import load_json_safe, save_json_atomic\n\n\n"
        + block("DataHandler", "PomodoroModel")
    ),
    "core/pomodoro_model.py": (
        HEADER_COMMON.format(doc="Estado y lógica del temporizador Pomodoro (sin UI).")
        + "import time\n"
        + "from collections import defaultdict\n"
        + "from typing import Dict, Any, List, Optional\n\n\n"
        + block("PomodoroModel", "MainView")
    ),
    "core/stats_calculator.py": (
        HEADER_COMMON.format(doc="Agregación de time_chunks para estadísticas (sin UI).")
        + "from collections import defaultdict\n"
        + "from datetime import datetime, timedelta\n"
        + "from typing import Dict, Any, List\n\n\n"
        + block("StatsCalculator", "AppController")
    ),
    "services/webhook_server.py": (
        HEADER_COMMON.format(doc="Servidor HTTP en thread para webhooks entrantes (n8n/Asana).")
        + "import json\n"
        + "import threading\n"
        + "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        + "from queue import Queue\n"
        + "from typing import Optional\n\n\n"
        + block("WebhookServer", "DataHandler")
    ),
}

for rel, content in modules.items():
    out = V9 / rel
    out.write_text(content, encoding="utf-8")
    print(f"{rel}: {len(content.splitlines())} líneas")

print("Clases localizadas:", {k: v + 1 for k, v in starts.items() if k in
      ('WebhookServer', 'DataHandler', 'PomodoroModel', 'MainView', 'StatsCalculator', 'AppController')})
