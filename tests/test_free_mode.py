"""Modo Libre: un contador sin límite para enfocarse el tiempo que se quiera.

Cubre el modelo (acumulado entre pausas, sin tope, cuenta hacia arriba), la
suspensión a mitad de una sesión libre, el registro (chunks con mode 'free'
que cuentan como tiempo enfocado pero no como pomodoros), las recompensas
proporcionales con su umbral de 5 min, el selector «Pomodoro | Libre», el
botón «Terminar sesión» y la persistencia del último modo elegido.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_free_mode.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.data_handler import DataHandler  # noqa: E402
from core.pomodoro_model import PomodoroModel  # noqa: E402
from core.rewards import RewardsSystem  # noqa: E402
from core.stats_extended import StatsCalculator  # noqa: E402
from ui.theme import fonts  # noqa: E402

ID1 = 1764282734316
ID_ASANA = 1764282734999
POMODORO_MIN = 35
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


def _make_data(tmp_path, settings=None) -> Path:
    data = {'tasks': [{'id': ID1, 'name': 'Informe', 'completed': False, 'tags': [],
                       'source': 'local', 'asana_id': None, 'due': None},
                      {'id': ID_ASANA, 'name': 'Revisión', 'completed': False,
                       'tags': [], 'source': 'asana', 'asana_id': '120000000000001',
                       'due': None}],
            'deleted_tasks': [], 'time_chunks': [], 'history': [], 'ui_snapshot': {},
            'settings': {'pomodoro_min': POMODORO_MIN, **(settings or {})}}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _build(qtbot, path):
    dh = DataHandler(path)
    # Un test nunca sale a la red: la cola hacia Asana no envía nada
    dh.send_asana_ops = lambda ops, *a, **k: [
        {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
         'detail': '', 'offline': False} for op in ops]
    from ui.windows.main_window import MainWindow
    from ui.controllers.app_controller import AppController
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    controller = AppController(win, dh)
    win.show()
    qtbot.waitExposed(win)
    return controller


@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    controller = _build(qtbot, _make_data(tmp_path))
    yield controller
    # Drenar los singleShot diferidos (sonido 100 ms, aviso 400 ms)
    qtbot.wait(500)


@pytest.fixture
def toasts(ctrl, monkeypatch):
    """Registra los toasts (sin pintarlos)."""
    from ui.widgets import toast
    seen = []
    for name in ('show_success', 'show_info', 'show_warning'):
        monkeypatch.setattr(toast, name,
                            lambda parent, msg, duration=3000, _n=name: seen.append((_n, msg)))
    ctrl.tray.showMessage = lambda title, msg, *a: seen.append(('tray', msg))
    return seen


def _work(ctrl, clock, seconds: float, step: float = 30.0) -> None:
    """Avanza el reloj con el bucle de la UI latiendo (cada `step` s)."""
    done = 0.0
    while done < seconds:
        delta = min(step, seconds - done)
        clock.advance(delta)
        done += delta
        ctrl._tick()


def _focus_chunks(ctrl):
    return [c for c in ctrl.data_handler.get_time_chunks() if c.get('type') == 'pomodoro']


def _go_free(ctrl, task_id=ID1):
    ctrl.select_task(task_id)
    ctrl.timer_panel.kind_buttons['free'].click()
    assert ctrl.model.is_free


# ---------------------------------------------------------------- modelo
def _free_model(now: float = T0) -> PomodoroModel:
    m = PomodoroModel()
    m.set_durations(POMODORO_MIN, 5, 15, 4)
    m.reset()
    assert m.set_kind('free')
    m.start(now=now)
    return m


def test_modelo_libre_acumula_entre_pausas():
    m = _free_model()
    for t in range(30, 601, 30):
        m.heartbeat(T0 + t)
    assert m.pause(now=T0 + 600) == pytest.approx(600)
    assert m.get_display_time(now=T0 + 900) == pytest.approx(600)  # en pausa no corre
    assert m.is_session_in_progress()
    m.start(now=T0 + 1000)
    for t in range(1030, 1301, 30):
        m.heartbeat(T0 + t)
    assert m.get_display_time(now=T0 + 1300) == pytest.approx(900)
    assert m.pause(now=T0 + 1300) == pytest.approx(300)
    assert m.free_elapsed == pytest.approx(900)


def test_modelo_libre_sin_tope():
    """Tres horas seguidas: el tramo no se recorta a remaining_time."""
    m = _free_model()
    for t in range(30, 3 * 3600 + 1, 30):
        assert m.heartbeat(T0 + t) == 0.0
    assert m.pause(now=T0 + 3 * 3600) == pytest.approx(3 * 3600)
    assert m.remaining_time == POMODORO_MIN * 60  # la cuenta atrás no se toca


def test_modelo_libre_cuenta_hacia_arriba():
    m = _free_model()
    shown = []
    for t in range(0, 601, 30):  # latiendo sin huecos (no hay suspensión)
        m.heartbeat(T0 + t)
        shown.append(m.get_display_time(now=T0 + t))
    assert shown == sorted(shown) and shown[0] == 0 and shown[-1] == pytest.approx(600)


def test_modelo_libre_suspension_corta_en_el_ultimo_latido():
    m = _free_model()
    for t in range(30, 50 * 60 + 1, 30):
        m.heartbeat(T0 + t)
    wake = T0 + 50 * 60 + 4 * 3600
    assert m.heartbeat(wake) == pytest.approx(4 * 3600)
    assert m.get_display_time(now=wake) == pytest.approx(50 * 60)
    assert m.pause(now=wake) == pytest.approx(50 * 60)
    assert m.last_segment_end == pytest.approx(T0 + 50 * 60)


def test_modelo_cambio_de_modo_solo_sin_sesion_a_medias():
    m = PomodoroModel()
    m.start(now=T0)
    assert not m.set_kind('free')            # corriendo
    m.pause(now=T0 + 60)
    assert not m.set_kind('free')            # pomodoro pausado a mitad
    m.reset()
    m.is_break_time = True
    assert not m.set_kind('free')            # en descanso
    m.reset()
    assert m.set_kind('free') and m.is_free
    m.start(now=T0)
    m.pause(now=T0 + 60)
    assert not m.set_kind('pomodoro')        # sesión libre a medias
    assert m.stop_free() == pytest.approx(60) and m.free_elapsed == 0
    assert m.set_kind('pomodoro') and not m.is_free
    assert not m.set_kind('otro')


def test_modelo_corte_de_tramo_sin_parar():
    m = _free_model()
    m.heartbeat(T0 + 50)
    assert m.cut_free_segment(T0 + 40) == pytest.approx(40)
    assert m.is_running and m.start_timestamp == T0 + 40
    assert m.get_display_time(now=T0 + 50) == pytest.approx(50)
    assert m.pause(now=T0 + 50) == pytest.approx(10)
    assert m.free_elapsed == pytest.approx(50)


def test_modelo_pomodoro_sigue_igual():
    m = PomodoroModel()
    m.set_durations(POMODORO_MIN, 5, 15, 4)
    m.reset()
    assert m.timer_kind == 'pomodoro' and not m.is_free
    assert not hasattr(m, 'is_pomodoro_mode')  # vestigio de v8 retirado
    m.start(now=T0)
    m.heartbeat(T0 + 30)
    assert m.get_display_time(now=T0 + 30) == pytest.approx(POMODORO_MIN * 60 - 30)
    m.remaining_time = 100
    for t in range(40, 151, 10):
        m.heartbeat(T0 + t)
    assert m.pause(now=T0 + 150) == pytest.approx(100)  # el tope del Pomodoro sigue


# ---------------------------------------------------------------- recompensas
def test_recompensa_proporcional_y_umbral(tmp_path):
    r = RewardsSystem(tmp_path / "progress.json")
    before = (r.progress.experience, r.progress.coins, r.progress.total_time_minutes)
    info = r.add_free_session(4)
    assert info['xp_gained'] == 0 and info['coins_gained'] == 0
    assert (r.progress.experience, r.progress.coins,
            r.progress.total_time_minutes) == before
    assert r.progress.streak_days == 0

    info = r.add_free_session(47)
    assert info['xp_gained'] == 47 and info['coins_gained'] == 9
    assert r.progress.experience == 47
    assert r.progress.coins == 9
    assert r.progress.total_time_minutes == 47
    assert r.progress.total_pomodoros == 0  # no cuenta como pomodoro
    assert r.progress.streak_days == 1
    assert r.progress.last_pomodoro_date == datetime.now().strftime('%Y-%m-%d')

    info = r.add_free_session(5)
    assert info['xp_gained'] == 5 and info['coins_gained'] == 1


# ---------------------------------------------------------------- estadísticas
def test_estadisticas_libre_es_tiempo_pero_no_pomodoro():
    now = datetime.now().replace(microsecond=0)
    stamp = (now - timedelta(minutes=1)).isoformat()
    chunks = [
        {'duration': 25 * 60, 'task_id': ID1, 'timestamp': stamp, 'type': 'pomodoro'},
        {'duration': 90 * 60, 'task_id': ID1, 'timestamp': stamp, 'type': 'pomodoro',
         'mode': 'free'},
        {'duration': 30 * 60, 'task_id': ID1, 'timestamp': stamp, 'type': 'pomodoro',
         'mode': 'free'},
        {'duration': 5 * 60, 'task_id': ID1, 'timestamp': stamp, 'type': 'break'},
    ]
    calc = StatsCalculator(chunks)
    s = calc.get_summary()
    assert s['today_time'] == pytest.approx(145 * 60)   # el tiempo libre cuenta
    assert s['total_time'] == pytest.approx(145 * 60)
    assert s['pomodoros_hoy'] == 1                      # pero no son pomodoros
    assert s['pomodoros_total'] == 1
    assert calc.get_summary(period_days=7)['pomodoros_total'] == 1
    assert calc.get_session_durations(0) == [pytest.approx(25)]
    assert calc.get_success_counts(25, 0) == {'success': 1, 'fail': 0}
    assert s['streak'] == 1


# ---------------------------------------------------------------- app completa
def test_selector_cambia_a_libre_y_lo_recuerda(ctrl, qtbot, tmp_path):
    tp = ctrl.timer_panel
    assert ctrl.model.timer_kind == 'pomodoro'
    assert tp.kind_buttons['pomodoro'].isChecked()
    assert tp.mode_label.text() == "Enfoque"
    tp.kind_buttons['free'].click()
    assert ctrl.model.is_free
    assert tp.kind_buttons['free'].isChecked()
    assert tp.mode_label.text() == "Libre"
    assert tp.time_label.text() == "00:00"
    assert not tp.cycle_dots.isVisibleTo(tp)            # sin puntos del ciclo
    assert not tp.skip_btn.isVisibleTo(tp)              # ni «saltar»
    assert tp.reset_btn.toolTip() == "Terminar sesión"
    assert tp.reset_btn._icon_name == "flag"
    assert ctrl.data_handler.load_settings()['timer_kind'] == 'free'
    # Persistido en el JSON: otra instancia arranca en Libre
    saved = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert saved['settings']['timer_kind'] == 'free'
    other = _build(qtbot, tmp_path / "data.json")
    assert other.model.is_free
    assert other.timer_panel.kind_buttons['free'].isChecked()
    assert other.timer_panel.mode_label.text() == "Libre"
    # Y de vuelta al Pomodoro, todo como antes
    tp.kind_buttons['pomodoro'].click()
    assert not ctrl.model.is_free
    assert tp.mode_label.text() == "Enfoque"
    assert tp.cycle_dots.isVisibleTo(tp)
    assert tp.reset_btn.toolTip() == "Reiniciar"
    assert tp.time_label.text() == f"{POMODORO_MIN}:00"
    assert ctrl.data_handler.load_settings()['timer_kind'] == 'pomodoro'


def test_timer_kind_por_defecto_en_datos_viejos(tmp_path):
    dh = DataHandler(_make_data(tmp_path))
    assert dh.load_settings()['timer_kind'] == 'pomodoro'


def test_selector_bloqueado_con_sesion_a_medias(ctrl, clock):
    tp = ctrl.timer_panel
    ctrl.select_task(ID1)
    assert tp.kind_buttons['free'].isEnabled()
    ctrl.toggle_timer()                                  # pomodoro en marcha
    assert not tp.kind_buttons['free'].isEnabled()
    _work(ctrl, clock, 60)
    ctrl.toggle_timer()                                  # pausado a mitad
    assert not tp.kind_buttons['free'].isEnabled()
    ctrl.set_timer_kind('free')                          # ni por código
    assert not ctrl.model.is_free and tp.kind_buttons['pomodoro'].isChecked()
    ctrl.reset_timer()
    assert tp.kind_buttons['free'].isEnabled()
    # En descanso tampoco
    ctrl.model.is_break_time = True
    ctrl.model.last_pomodoro_task_id = ID1
    ctrl.toggle_timer()
    assert not tp.kind_buttons['free'].isEnabled()
    ctrl.skip_break()
    assert tp.kind_buttons['free'].isEnabled()
    # Y en el modo Libre, con la sesión a medias
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    ctrl.toggle_timer()
    assert not tp.kind_buttons['pomodoro'].isEnabled()
    ctrl.reset_timer()                                   # «Terminar sesión»
    assert tp.kind_buttons['pomodoro'].isEnabled()


def test_libre_exige_tarea_activa(ctrl, monkeypatch):
    from ui.dialogs import branded_dialogs
    asked = []
    monkeypatch.setattr(branded_dialogs, "info", lambda *a, **k: asked.append(a))
    ctrl.timer_panel.kind_buttons['free'].click()
    ctrl.toggle_timer()
    assert asked and not ctrl.model.is_running


def test_libre_cuenta_hacia_arriba_sin_tope(ctrl, clock):
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 10 * 60)
    assert ctrl.timer_panel.time_label.text() == "10:00"
    _work(ctrl, clock, 60 * 60 + 30)                     # más allá de los 35 min
    assert ctrl.model.is_running and not ctrl.model.is_break_time
    assert ctrl.timer_panel.time_label.text() == "1:10:30"
    assert ctrl.model.pomodoro_cycle_count == 0
    # «Enfocado hoy» suma el tramo en vivo
    assert ctrl.timer_panel.total_today.text() == "01:10:30"
    # Anillo: una vuelta por hora (en pausa, sin animación de por medio)
    ctrl.toggle_timer()
    ctrl._update_timer_display()
    assert ctrl.timer_panel.ring.get_fraction() == pytest.approx((10 * 60 + 30) / 3600,
                                                                 abs=0.01)


def test_pausas_registran_chunks_libres(ctrl, clock, toasts):
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 12 * 60)
    ctrl.toggle_timer()                                  # pausa
    chunks = _focus_chunks(ctrl)
    assert len(chunks) == 1
    assert chunks[0]['mode'] == 'free' and chunks[0]['type'] == 'pomodoro'
    assert chunks[0]['duration'] == pytest.approx(12 * 60, abs=1)
    assert ctrl.timer_panel.time_label.text() == "12:00"  # sobrevive a la pausa
    clock.advance(300)                                   # en pausa no corre
    ctrl._tick()
    assert ctrl.timer_panel.time_label.text() == "12:00"
    ctrl.toggle_timer()
    _work(ctrl, clock, 8 * 60)
    assert ctrl.timer_panel.time_label.text() == "20:00"


def test_terminar_sesion_da_xp_proporcional(ctrl, qtbot, clock, toasts):
    _go_free(ctrl)
    before = ctrl.rewards.get_stats()
    ctrl.toggle_timer()
    _work(ctrl, clock, 12 * 60)
    ctrl.toggle_timer()
    ctrl.toggle_timer()
    _work(ctrl, clock, 13 * 60)
    ctrl.timer_panel.reset_btn.click()                   # «Terminar sesión»
    qtbot.wait(500)
    assert not ctrl.model.is_running
    assert ctrl.model.free_elapsed == 0
    assert ctrl.timer_panel.time_label.text() == "00:00"
    assert ctrl.model.is_free                            # sigue en Libre
    chunks = _focus_chunks(ctrl)
    assert [c.get('mode') for c in chunks] == ['free', 'free']
    assert sum(c['duration'] for c in chunks) == pytest.approx(25 * 60, abs=1)
    after = ctrl.rewards.get_stats()
    assert after['xp'] - before['xp'] == 25
    assert after['coins'] - before['coins'] == 5
    assert after['total_time_minutes'] - before['total_time_minutes'] == 25
    assert after['total_pomodoros'] == before['total_pomodoros']
    assert after['streak_days'] >= 1
    assert ctrl.model.pomodoro_cycle_count == 0
    assert any(kind == 'show_success' and 'Sesión libre de 25 min' in msg
               and '+25 XP' in msg for kind, msg in toasts)
    # Ni comentario en Asana (tarea local) ni nada en la cola
    assert not ctrl.data_handler.has_pending_asana_ops()


def test_mascota_no_habla_de_pomodoros_en_libre(ctrl, qtbot, clock, toasts):
    from services import pet_service
    for species in ('pet_cat', 'pet_dog', 'pet_rat'):
        phrases = pet_service._LOCAL_PHRASES['free_session'][species]
        assert phrases
        for text in phrases:
            assert 'pomodoro' not in text.lower() and 'minuto' not in text.lower()
            assert not any(ch.isdigit() for ch in text)
    events = []
    ctrl._pet_react = lambda event, reward_info=None: events.append(event)
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 7 * 60)
    ctrl.timer_panel.reset_btn.click()                   # «Terminar sesión»
    qtbot.wait(500)
    assert events == ['free_session']


def test_rotulo_de_la_racha_habla_de_tiempo_enfocado(ctrl, qtbot):
    """La racha cuenta también los días con solo sesiones Libres."""
    from PySide6.QtWidgets import QLabel
    from ui.dialogs.stats_dialog import StatsDialog
    dlg = StatsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
    assert any(t.startswith("Días seguidos con tiempo enfocado") for t in texts)
    assert not any("al menos un pomodoro" in t for t in texts)
    dlg.done(0)


def test_sesion_corta_solo_registra_el_tiempo(ctrl, qtbot, clock, toasts):
    _go_free(ctrl)
    before = ctrl.rewards.get_stats()
    ctrl.toggle_timer()
    _work(ctrl, clock, 4 * 60 + 50)
    ctrl.reset_timer()
    qtbot.wait(500)
    chunks = _focus_chunks(ctrl)
    assert len(chunks) == 1 and chunks[0]['mode'] == 'free'
    assert ctrl.rewards.get_stats() == before            # sin XP, monedas ni racha
    assert [k for k, _m in toasts] == ['show_info']      # aviso neutro
    assert '04:50' in toasts[0][1]
    # Terminar sin nada que terminar no hace nada
    ctrl.reset_timer()
    assert len(toasts) == 1


def test_comentario_en_asana_al_terminar(ctrl, qtbot, clock, toasts, monkeypatch):
    monkeypatch.setattr(ctrl, "_asana_sync_allowed", lambda: True)
    _go_free(ctrl, ID_ASANA)
    ctrl.toggle_timer()
    _work(ctrl, clock, 30 * 60)
    ctrl.reset_timer()
    qtbot.wait(500)
    outbox = ctrl.data_handler.data.get('asana_outbox') or []
    comments = [op for op in outbox if op.get('type') == 'comment']
    assert len(comments) == 1
    assert comments[0]['gid'] == '120000000000001'
    assert comments[0]['text'].startswith('⏱️ Sesión libre: 30 min (terminada el ')


def test_sin_sync_no_se_encola_comentario(ctrl, qtbot, clock, toasts):
    _go_free(ctrl, ID_ASANA)
    ctrl.toggle_timer()
    _work(ctrl, clock, 30 * 60)
    ctrl.reset_timer()
    qtbot.wait(500)
    assert not ctrl.data_handler.has_pending_asana_ops()


def test_completar_la_tarea_cierra_la_sesion_libre(ctrl, qtbot, clock, toasts):
    _go_free(ctrl)
    before = ctrl.rewards.get_stats()
    ctrl.toggle_timer()
    _work(ctrl, clock, 10 * 60)
    ctrl.toggle_active_task_completion()
    qtbot.wait(500)
    assert not ctrl.model.is_running and ctrl.model.free_elapsed == 0
    assert ctrl.model.active_task_id is None
    assert ctrl.rewards.get_stats()['xp'] - before['xp'] == 10
    assert ctrl.model.is_free


def test_suspension_a_mitad_en_modo_libre(ctrl, qtbot, clock, toasts):
    """20 min de trabajo, 1 h suspendido, despertar: pausa en el último latido,
    sin tope al reanudar y sin completar nada."""
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 20 * 60)
    clock.advance(3600)        # equipo suspendido: el bucle no late
    qtbot.wait(400)            # al despertar late el QTimer real
    assert ctrl.model.is_running is False
    assert ctrl.timer_panel._is_running is False
    chunks = _focus_chunks(ctrl)
    assert len(chunks) == 1 and chunks[0]['mode'] == 'free'
    assert chunks[0]['duration'] == pytest.approx(20 * 60, abs=1)
    assert chunks[0]['timestamp'] == datetime.fromtimestamp(T0 + 20 * 60).isoformat()
    tray = [m for k, m in toasts if k == 'tray']
    assert len(tray) == 1 and 'sesión libre' in tray[0] and 'suspendido' in tray[0]
    assert ctrl.model.free_elapsed == pytest.approx(20 * 60, abs=1)
    assert ctrl.timer_panel.time_label.text() == "20:00"

    # Reanudar mucho más allá de la duración del pomodoro: sin tope
    ctrl.toggle_timer()
    _work(ctrl, clock, 60 * 60)
    assert ctrl.model.is_running and not ctrl.model.is_break_time
    ctrl.toggle_timer()
    total = sum(c['duration'] for c in _focus_chunks(ctrl))
    assert total == pytest.approx(80 * 60, abs=2)
    assert ctrl.model.pomodoro_cycle_count == 0


def test_sesion_que_cruza_la_medianoche_se_parte(ctrl, clock, toasts):
    midnight = datetime(2027, 1, 16).timestamp()
    clock.now = midnight - 10 * 60
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 25 * 60)
    ctrl.toggle_timer()
    chunks = _focus_chunks(ctrl)
    assert len(chunks) == 2 and all(c['mode'] == 'free' for c in chunks)
    first, second = chunks
    assert first['duration'] == pytest.approx(10 * 60, abs=1)
    assert datetime.fromisoformat(first['timestamp']).date() == datetime(2027, 1, 15).date()
    assert second['duration'] == pytest.approx(15 * 60, abs=1)
    assert datetime.fromisoformat(second['timestamp']).date() == datetime(2027, 1, 16).date()
    assert ctrl.model.free_elapsed == pytest.approx(25 * 60, abs=1)  # una sola sesión
    assert ctrl.timer_panel.time_label.text() == "25:00"


def test_apply_settings_no_pisa_el_modo_libre(ctrl, clock):
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 3 * 60)
    ctrl.toggle_timer()
    settings = dict(ctrl.data_handler.load_settings(), pomodoro_min=50)
    ctrl.apply_settings(settings)
    assert ctrl.model.is_free
    assert ctrl.model.free_elapsed == pytest.approx(3 * 60, abs=1)
    assert ctrl.timer_panel.time_label.text() == "03:00"
    ctrl.reset_timer()
    ctrl.set_timer_kind('pomodoro')
    assert ctrl.timer_panel.time_label.text() == "50:00"


def test_aviso_de_salida_habla_de_sesion_libre(ctrl, clock, monkeypatch):
    from ui.dialogs import branded_dialogs
    messages = []

    class FakeDialog:
        def __init__(self, *a, **k):
            pass

        def add_message(self, text):
            messages.append(text)

        def add_buttons(self, _buttons):
            class _B:
                def setDefault(self, _v):
                    pass
            return _B(), _B()

        def exec(self):
            return 0

        def deleteLater(self):
            pass

    monkeypatch.setattr(branded_dialogs, "BrandedDialog", FakeDialog)
    _go_free(ctrl)
    ctrl.toggle_timer()
    assert ctrl._ask_quit() is False
    assert "una sesión libre" in messages[-1]


def test_mini_ventana_muestra_libre(ctrl, clock):
    _go_free(ctrl)
    ctrl.toggle_timer()
    _work(ctrl, clock, 65 * 60)
    ctrl.show_compact()
    cw = ctrl.compact_window
    assert cw.mode_label.text() == "Libre"
    assert cw.time_label.text() == "1:05:00"
    ctrl.show_normal()


def test_pomodoro_sigue_sin_mode(ctrl, qtbot, clock):
    """Los tramos del Pomodoro no llevan 'mode' y se completa como siempre."""
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, POMODORO_MIN * 60)
    qtbot.wait(600)
    chunks = _focus_chunks(ctrl)
    assert len(chunks) == 1 and 'mode' not in chunks[0]
    assert ctrl.model.is_break_time and ctrl.model.pomodoro_cycle_count == 1
    assert ctrl.rewards.get_stats()['total_pomodoros'] == 1
