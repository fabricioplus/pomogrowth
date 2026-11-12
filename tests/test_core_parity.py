"""Paridad del core v8 ↔ v9: la extracción no debe cambiar ningún resultado.

Ejecuta cada lado en un proceso separado (v8 y v9 tienen paquetes 'core'/'utils'
con el mismo nombre) y compara. También: round-trip de DataHandler sin pérdida
y sanidad de PomodoroModel.

Uso: python tests/test_core_parity.py   (desde la raíz del repositorio)
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

V9 = Path(__file__).resolve().parent.parent
V8 = V9.parent / "pomogrowth_v8_ultra"
DATA = V9 / "pomogrowth_data.json"
FAILS = []


def run_snippet(cwd: Path, snippet: str) -> dict:
    r = subprocess.run([sys.executable, "-c", snippet], cwd=str(cwd),
                       capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"snippet en {cwd.name} falló:\n{r.stderr[-2000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


COMMON = f'''
import json, sys
chunks = json.load(open(r"{DATA}", encoding="utf-8"))["time_chunks"]
sc = StatsCalculator(chunks)
labels, focus, breaks = sc.get_chart_data(7)
print(json.dumps({{"summary": sc.get_summary(), "labels": list(labels),
                   "focus": list(focus), "breaks": list(breaks)}}))
'''

v8_out = run_snippet(V8, "from app_ultra import StatsCalculator\n" + COMMON)
v9_out = run_snippet(V9, "from core.stats_calculator import StatsCalculator\n" + COMMON)

if v8_out == v9_out:
    print(f"[OK] StatsCalculator identico v8/v9 (total={v8_out['summary']['total_time']:.0f}s, "
          f"racha={v8_out['summary']['streak']}, pomodoros={v8_out['summary']['pomodoros_total']})")
else:
    FAILS.append("StatsCalculator difiere")
    for k in v8_out:
        if v8_out[k] != v9_out[k]:
            print(f"[FALLO] campo {k}: v8={v8_out[k]} v9={v9_out[k]}")

# --- Round-trip de DataHandler (sobre copia temporal, nunca los datos reales) ---
rt = f'''
import json, shutil, sys, tempfile
from pathlib import Path
from core.data_handler import DataHandler
tmp = Path(tempfile.mkdtemp()) / "data.json"
shutil.copy2(r"{DATA}", tmp)
original = json.load(open(tmp, encoding="utf-8"))
dh = DataHandler(tmp)
dh._save()
dh2 = DataHandler(tmp)
ok_tasks = len(dh2.data["tasks"]) == len(original["tasks"])
ok_chunks = dh2.data["time_chunks"] == original["time_chunks"]
ok_keys = all(k in dh2.data for k in ("tasks","deleted_tasks","time_chunks","history","ui_snapshot","settings"))
# Presentes y numéricas — no compara con los defaults: el usuario puede
# haberlas personalizado (p. ej. pomodoro de 35 min) y siguen siendo válidas
ok_durations = (isinstance(dh2.data["settings"].get("pomodoro_min"), (int, float))
                and isinstance(dh2.data["settings"].get("pomodoros_per_cycle"), (int, float)))
ok_settings = all(dh2.data["settings"].get(k) == v for k, v in original["settings"].items())
print(json.dumps({{"tasks": ok_tasks, "chunks": ok_chunks, "keys": ok_keys,
                   "durations_v9": ok_durations, "settings": ok_settings,
                   "n_tasks": len(dh2.data["tasks"])}}))
'''
rt_out = run_snippet(V9, rt)
if all(v for k, v in rt_out.items() if k != "n_tasks"):
    print(f"[OK] DataHandler round-trip sin pérdida ({rt_out['n_tasks']} tareas, "
          f"settings intactos, duraciones v9 presentes)")
else:
    FAILS.append(f"DataHandler round-trip: {rt_out}")
    print(f"[FALLO] round-trip: {rt_out}")

# --- Sanidad de PomodoroModel ---
pm = '''
import json, time
from core.pomodoro_model import PomodoroModel
m = PomodoroModel()
d0 = (m.pomodoro_duration, m.break_duration, m.long_break_duration, m.pomodoros_per_cycle)
m.set_durations(30, 6, 20, 3)
d1 = (m.pomodoro_duration, m.break_duration, m.long_break_duration, m.pomodoros_per_cycle)
m.reset()  # como hace el controller: set_durations no toca remaining_time
m.start(); time.sleep(0.15); e = m.pause()
print(json.dumps({"defaults": d0, "set": d1, "elapsed_ok": 0.1 < e < 1.0,
                  "remaining_ok": abs(m.remaining_time - (30*60 - e)) < 0.01}))
'''
pm_out = run_snippet(V9, pm)
if (pm_out["defaults"] == [25*60, 5*60, 15*60, 4] and pm_out["set"] == [30*60, 6*60, 20*60, 3]
        and pm_out["elapsed_ok"] and pm_out["remaining_ok"]):
    print("[OK] PomodoroModel: defaults, set_durations, start/pause y remaining correctos")
else:
    FAILS.append(f"PomodoroModel: {pm_out}")
    print(f"[FALLO] PomodoroModel: {pm_out}")

print()
if FAILS:
    print("PARIDAD: FALLO ->", "; ".join(FAILS)); sys.exit(1)
print("PARIDAD COMPLETA: v9 core == v8 core")
