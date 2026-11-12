"""Regresión: suspender el equipo con el timer en marcha no debe inflar el tiempo.

Bug real (jul–oct 2026): con el PC suspendido a mitad de un pomodoro de 35 min,
al despertar el reloj de pared había saltado y se registraban tramos de 96,9 y
73,8 min (~100 min fantasma) y el pomodoro se daba por completado.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_timer_suspension.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.data_handler import DataHandler  # noqa: E402
from core.pomodoro_model import SUSPEND_GAP_S, PomodoroModel  # noqa: E402
from ui.theme import fonts  # noqa: E402

ID1 = 1764282734316
POMODORO_MIN = 35  # como en los datos reales
T0 = 1_800_000_000.0


class FakeClock:
    """Sustituye a time.time: el test decide cuándo pasa el tiempo."""

    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock(T0)
    monkeypatch.setattr(time, "time", c)
    return c


@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    data = {'tasks': [{'id': ID1, 'name': 'Informe', 'completed': False, 'tags': [],
                       'source': 'local', 'asana_id': None, 'due': None}],
            'deleted_tasks': [], 'time_chunks': [], 'history': [], 'ui_snapshot': {},
            'settings': {'pomodoro_min': POMODORO_MIN}}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    dh = DataHandler(path)
    from ui.windows.main_window import MainWindow
    from ui.controllers.app_controller import AppController
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    controller = AppController(win, dh)
    win.show()
    qtbot.waitExposed(win)
    yield controller
    # Drenar los singleShot diferidos (aviso a 400 ms) también si el test
    # falló a mitad: si no, se dispararían en el test siguiente
    qtbot.wait(500)


@pytest.fixture
def notices(ctrl, monkeypatch):
    """Registra los avisos: toasts de advertencia y notificaciones del tray."""
    from ui.widgets import toast
    seen = {'toast': [], 'tray': []}
    monkeypatch.setattr(toast, "show_warning",
                        lambda parent, msg, duration=3000: seen['toast'].append(msg))
    ctrl.tray.showMessage = lambda title, msg, *a: seen['tray'].append(msg)
    return seen


def _work(ctrl, clock, seconds: float, step: float = 30.0) -> None:
    """Avanza el reloj con el bucle de la UI latiendo (cada `step` s)."""
    done = 0.0
    while done < seconds:
        delta = min(step, seconds - done)
        clock.advance(delta)
        done += delta
        ctrl._tick()


def _chunks(ctrl, kind: str):
    return [c for c in ctrl.data_handler.get_time_chunks() if c.get('type') == kind]


# ---------------------------------------------------------------- modelo
def _model(now: float = T0) -> PomodoroModel:
    m = PomodoroModel()
    m.set_durations(POMODORO_MIN, 5, 15, 4)
    m.reset()
    m.start(now=now)
    return m


def test_modelo_corta_el_tramo_en_el_ultimo_latido():
    m = _model()
    for t in range(30, 20 * 60 + 1, 30):  # 20 min latiendo cada 30 s
        assert m.heartbeat(T0 + t) == 0.0
    wake = T0 + 20 * 60 + 3600
    assert m.heartbeat(wake) == pytest.approx(3600)  # detecta la suspensión
    assert m.pause(now=wake) == pytest.approx(20 * 60)
    assert m.remaining_time == pytest.approx(15 * 60)


def test_modelo_pausa_sin_latido_previo_tambien_corta():
    """Si al despertar lo primero es un clic en ⏸ (antes del tick), el tramo
    tampoco incluye la suspensión."""
    m = _model()
    for t in range(20, 501, 20):
        m.heartbeat(T0 + t)
    assert m.pause(now=T0 + 500 + 4000) == pytest.approx(500)


def test_modelo_display_no_salta_tras_la_suspension():
    m = _model()
    m.heartbeat(T0 + 10)
    remaining = m.get_display_time(now=T0 + 10 + 3600)
    assert remaining == pytest.approx(POMODORO_MIN * 60 - 10)
    assert m.get_current_runtime(now=T0 + 10 + 3600) == pytest.approx(10)


def test_modelo_parones_cortos_siguen_contando():
    """Un hueco por debajo del umbral (UI ocupada un momento) es trabajo."""
    m = _model()
    assert m.heartbeat(T0 + SUSPEND_GAP_S - 1) == 0.0
    assert m.pause(now=T0 + SUSPEND_GAP_S - 1) == pytest.approx(SUSPEND_GAP_S - 1)


def test_modelo_tramo_nunca_supera_lo_restante():
    m = _model()
    m.remaining_time = 100
    for t in range(10, 151, 10):  # latiendo sin huecos, pero 50 s de más
        m.heartbeat(T0 + t)
    assert m.pause(now=T0 + 150) == pytest.approx(100)
    assert m.remaining_time == pytest.approx(0)


def test_modelo_reloj_atrasado_no_resta_tiempo():
    m = _model()
    assert m.pause(now=T0 - 120) == 0.0
    assert m.remaining_time == POMODORO_MIN * 60


def test_modelo_reloj_atrasado_conserva_lo_trabajado():
    """Atrasar el reloj 25 min tras 20 min de trabajo no borra esos 20 min."""
    m = _model()
    for t in range(30, 20 * 60 + 1, 30):
        m.heartbeat(T0 + t)
    back = T0 + 20 * 60 - 25 * 60
    assert m.get_display_time(now=back) == pytest.approx(15 * 60)  # aún sin latir
    m.heartbeat(back)
    for t in range(30, 5 * 60 + 1, 30):  # 5 min más con el reloj ya atrasado
        m.heartbeat(back + t)
    assert m.pause(now=back + 5 * 60) == pytest.approx(25 * 60)


def test_modelo_fin_real_del_tramo():
    m = _model()
    for t in range(30, 10 * 60 + 1, 30):
        m.heartbeat(T0 + t)
    m.pause(now=T0 + 10 * 60 + 8 * 3600)
    assert m.last_segment_end == pytest.approx(T0 + 10 * 60)


# ---------------------------------------------------------------- app completa
def test_suspension_a_mitad_no_infla_ni_completa_el_pomodoro(ctrl, qtbot, clock, notices):
    """El caso de producción: 20 min de trabajo, 1 h suspendido, despertar."""
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 20 * 60)
    clock.advance(3600)        # equipo suspendido: el bucle no late
    qtbot.wait(400)            # al despertar late el QTimer real
    assert ctrl.model.is_running is False
    assert ctrl.timer_panel._is_running is False  # el botón vuelve a ▶
    chunks = _chunks(ctrl, 'pomodoro')
    assert len(chunks) == 1
    assert chunks[0]['duration'] == pytest.approx(20 * 60, abs=1)
    # Fechado en el corte, no al despertar (una suspensión nocturna no debe
    # pasar ese trabajo al día siguiente)
    assert chunks[0]['timestamp'] == datetime.fromtimestamp(T0 + 20 * 60).isoformat()
    # Aviso: notificación de Windows (queda en el centro de notificaciones)
    # y toast con la ventana a la vista
    assert len(notices['tray']) == 1 and 'suspendido' in notices['tray'][0]
    assert len(notices['toast']) == 1
    assert ctrl.model.pomodoro_cycle_count == 0  # no se dio por completado
    assert ctrl.model.is_break_time is False
    assert ctrl.model.remaining_time == pytest.approx(15 * 60, abs=1)

    # Reanudar y terminar: el pomodoro suma exactamente sus 35 min
    ctrl.toggle_timer()
    _work(ctrl, clock, 15 * 60)
    # El chunk final se registra con singleShot(50); esperar también al aviso
    # (400 ms) para que no se dispare en el test siguiente
    qtbot.wait(600)
    total = sum(c['duration'] for c in _chunks(ctrl, 'pomodoro'))
    assert total == pytest.approx(POMODORO_MIN * 60, abs=1)
    assert ctrl.model.pomodoro_cycle_count == 1
    assert ctrl.model.is_break_time is True


def test_suspension_en_descanso_pausa_el_descanso(ctrl, qtbot, clock):
    ctrl.select_task(ID1)
    ctrl.model.last_pomodoro_task_id = ID1
    ctrl.model.is_break_time = True
    ctrl.model.remaining_time = 5 * 60
    ctrl.toggle_timer()
    _work(ctrl, clock, 2 * 60)
    clock.advance(2 * 3600)
    qtbot.wait(400)
    assert ctrl.model.is_running is False
    breaks = _chunks(ctrl, 'break')
    assert len(breaks) == 1 and breaks[0]['duration'] == pytest.approx(2 * 60, abs=1)
    assert _chunks(ctrl, 'pomodoro') == []
    assert ctrl.model.remaining_time == pytest.approx(3 * 60, abs=1)


def test_suspension_con_la_app_en_la_bandeja(ctrl, qtbot, clock, notices):
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    ctrl.hide_to_tray()
    notices['tray'].clear()    # el propio «PomoGrowth sigue aquí»
    _work(ctrl, clock, 5 * 60)
    clock.advance(2 * 3600)
    qtbot.wait(400)
    assert ctrl.model.is_running is False
    assert len(notices['tray']) == 1 and 'suspendido' in notices['tray'][0]
    assert notices['toast'] == []  # sin ventana visible no hay toast


def test_parones_cortos_no_pausan(ctrl, qtbot, clock):
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 5 * 60, step=SUSPEND_GAP_S - 5)
    assert ctrl.model.is_running is True
    ctrl.toggle_timer()  # pausa manual
    assert _chunks(ctrl, 'pomodoro')[-1]['duration'] == pytest.approx(5 * 60, abs=1)
