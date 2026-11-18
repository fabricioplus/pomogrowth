"""Sonidos del temporizador: los WAV propios, el servicio y dónde suenan.

Cubre los 12 WAV de assets/sounds/timer (formato, duración, pico, bordes en
cero, que son los que genera scripts/build_sounds.py y que el spec los
empaqueta), services/sound_service con QSoundEffect y winsound simulados
(desactivado, volumen, estilo, caché por estilo, carga en curso, vuelta a
winsound si falta el archivo, falla o se llama fuera del hilo de la GUI), la
sección «Sonidos» de Ajustes (guarda y «Probar» usa lo elegido sin guardar)
y los puntos del controlador: fin de pomodoro (también con la mini ventana o
la principal minimizada), fin de descanso y fin de sesión Libre, una sola
vez, y nada al reiniciar, saltar el descanso o salir.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_timer_sounds.py -q
Ningún test reproduce audio: conftest silencia play/preview y aquí el
servicio real solo ve dobles de QSoundEffect y de winsound. Datos sintéticos
en un directorio temporal — nunca los reales.
"""
import importlib.util
import json
import os
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.data_handler import DataHandler  # noqa: E402
from services import sound_service  # noqa: E402
from ui.theme import fonts  # noqa: E402

# Las funciones reales: conftest las sustituye en cada test (autouse)
REAL_PLAY = sound_service.play
REAL_PREVIEW = sound_service.preview

STYLES = ('campana', 'marimba', 'suave')
EVENTS = ('start', 'stop', 'complete', 'break')
DURATION_RANGES = {'start': (0.1, 0.4), 'stop': (0.1, 0.4),
                   'complete': (1.5, 2.5), 'break': (1.2, 2.0)}
TIMER_DIR = V9 / 'assets' / 'sounds' / 'timer'

ID1 = 1764282734316
T0 = 1_800_000_000.0


def _read_wav(path: Path):
    with wave.open(str(path), 'rb') as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    return params, np.frombuffer(frames, dtype='<i2').astype(np.float64) / 32768.0


# ================================================================ los WAV
@pytest.mark.parametrize('style', STYLES)
@pytest.mark.parametrize('event', EVENTS)
def test_wav_valido(style, event):
    path = TIMER_DIR / style / f'{event}.wav'
    assert path.is_file()
    params, x = _read_wav(path)
    assert (params.nchannels, params.sampwidth, params.framerate) == (1, 2, 44100)
    assert params.comptype == 'NONE'
    lo, hi = DURATION_RANGES[event]
    assert lo <= params.nframes / params.framerate <= hi
    peak_db = 20 * np.log10(np.abs(x).max())
    assert -12.0 <= peak_db <= -1.0  # se oye, sin saturar
    # Sin clics: empieza desde cero (ataque ≥ 5 ms) y acaba en cero (fundido)
    assert abs(x[0]) < 1e-3 and np.abs(x[:10]).max() < 0.01
    assert abs(x[-1]) < 1e-3 and np.abs(x[-10:]).max() < 0.01


def test_wav_tamano_total_y_eventos_distintos():
    files = sorted(TIMER_DIR.rglob('*.wav'))
    assert len(files) == 12
    assert sum(f.stat().st_size for f in files) < 1.5 * 1024 * 1024
    for style in STYLES:
        datas = [_read_wav(TIMER_DIR / style / f'{e}.wav')[1] for e in EVENTS]
        assert len({d.tobytes() for d in datas}) == 4


def test_wav_son_los_que_genera_el_script():
    """Determinista: el script (semilla fija) da exactamente los versionados."""
    spec = importlib.util.spec_from_file_location('build_sounds',
                                                  V9 / 'scripts' / 'build_sounds.py')
    build_sounds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_sounds)
    for (style, event), x in build_sounds.build().items():
        pcm = np.clip(np.round(x * 32767), -32768, 32767).astype('<i2')
        with wave.open(str(TIMER_DIR / style / f'{event}.wav'), 'rb') as w:
            assert w.readframes(w.getnframes()) == pcm.tobytes(), (style, event)


def test_spec_empaqueta_los_sonidos(tmp_path):
    from tests.test_installer import _run_spec
    datas = _run_spec(tmp_path)['Analysis']['datas']
    assert ('assets/sounds/timer', 'assets/sounds/timer') in datas
    assert sound_service.SOUNDS_DIR == V9 / 'assets' / 'sounds' / 'timer'


# ================================================================ servicio
class FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self):
        for slot in list(self._slots):
            slot()


class FakeEffect:
    """Doble de QSoundEffect: registra volumen y reproducciones."""
    initial_status = None  # lo fija el fixture

    def __init__(self, path: Path):
        from PySide6.QtMultimedia import QSoundEffect
        self.path = Path(path)
        self._status = FakeEffect.initial_status or QSoundEffect.Status.Ready
        self.volume = None
        self.plays = 0
        self.statusChanged = FakeSignal()

    def status(self):
        return self._status

    def set_status(self, status):
        self._status = status
        self.statusChanged.emit()

    def setVolume(self, v):
        self.volume = v

    def play(self):
        self.plays += 1


@pytest.fixture
def svc(qapp, monkeypatch):
    """El servicio real, con QSoundEffect y winsound simulados."""
    monkeypatch.setattr(sound_service, 'play', REAL_PLAY)
    monkeypatch.setattr(sound_service, 'preview', REAL_PREVIEW)
    monkeypatch.setattr(sound_service, '_config', {
        'enabled': True, 'volume': 70, 'style': 'campana'})
    sound_service._reset_cache()
    created = []
    winsound_calls = []
    FakeEffect.initial_status = None

    def new_effect(path, parent):
        assert parent is qapp  # colgado de la QApplication, en su hilo
        effect = FakeEffect(path)
        created.append(effect)
        return effect
    monkeypatch.setattr(sound_service, '_new_effect', new_effect)
    monkeypatch.setattr(sound_service, '_play_winsound', winsound_calls.append)
    ns = type('Svc', (), {})()
    ns.created, ns.winsound = created, winsound_calls

    def played():
        return [(e.path.parent.name, e.path.stem) for e in created for _ in range(e.plays)]
    ns.played = played
    yield ns
    sound_service._reset_cache()
    FakeEffect.initial_status = None


def test_play_con_el_estilo_y_el_volumen(svc):
    sound_service.configure(volume=50, style='marimba')
    sound_service.play('complete')
    assert svc.played() == [('marimba', 'complete')]
    # Precarga del estilo entero (los 4 eventos), en caché
    assert sorted(e.path.stem for e in svc.created) == sorted(EVENTS)
    played = [e for e in svc.created if e.plays][0]
    assert played.volume == pytest.approx(0.25)  # curva cuadrática
    sound_service.play('break')
    sound_service.play('complete')
    assert len(svc.created) == 4
    assert svc.played().count(('marimba', 'complete')) == 2
    assert svc.winsound == []


def test_play_desactivado_o_a_volumen_cero_no_suena(svc):
    sound_service.configure(enabled=False)
    for kind in EVENTS:
        sound_service.play(kind)
    sound_service.configure(enabled=True, volume=0)
    sound_service.play('complete')
    assert svc.created == [] and svc.winsound == []


def test_estilo_windows_usa_winsound(svc):
    sound_service.configure(style='windows')
    sound_service.play('break')
    assert svc.winsound == ['break'] and svc.created == []


def test_vuelve_a_winsound_si_falta_el_archivo(svc, monkeypatch, tmp_path):
    monkeypatch.setattr(sound_service, 'SOUNDS_DIR', tmp_path / 'no_hay')
    sound_service.play('complete')
    assert svc.winsound == ['complete'] and svc.created == []


def test_vuelve_a_winsound_si_qsoundeffect_falla(svc, monkeypatch):
    from PySide6.QtMultimedia import QSoundEffect
    FakeEffect.initial_status = QSoundEffect.Status.Error
    sound_service.play('complete')
    assert svc.winsound == ['complete'] and svc.played() == []

    def broken(path, parent):
        raise RuntimeError("sin backend de audio")
    sound_service._reset_cache()
    monkeypatch.setattr(sound_service, '_new_effect', broken)
    sound_service.play('break')
    assert svc.winsound == ['complete', 'break']


def test_cargando_suena_al_quedar_listo_una_vez(svc):
    from PySide6.QtMultimedia import QSoundEffect
    FakeEffect.initial_status = QSoundEffect.Status.Loading
    sound_service.play('complete')
    assert svc.played() == []
    effect = next(e for e in svc.created if e.path.stem == 'complete')
    effect.set_status(QSoundEffect.Status.Ready)
    assert svc.played() == [('campana', 'complete')]
    effect.set_status(QSoundEffect.Status.Ready)  # otra señal: no repite
    assert svc.played() == [('campana', 'complete')]
    # Otro que acaba en error mientras esperaba: winsound
    other = next(e for e in svc.created if e.path.stem == 'break')
    sound_service.play('break')
    other.set_status(QSoundEffect.Status.Error)
    assert svc.winsound == ['break']


def test_fuera_del_hilo_de_la_gui_no_toca_qsoundeffect(svc):
    t = threading.Thread(target=sound_service.play, args=('complete',))
    t.start()
    t.join(5)
    assert svc.created == [] and svc.winsound == ['complete']


def test_preview_ignora_el_interruptor_y_usa_lo_elegido(svc):
    sound_service.configure(enabled=False, volume=10, style='campana')
    sound_service.preview('suave', 'complete', 100)
    assert svc.played() == [('suave', 'complete')]
    assert [e for e in svc.created if e.plays][0].volume == pytest.approx(1.0)
    sound_service.preview('windows', 'complete', 40)
    assert svc.winsound == ['complete']
    sound_service.preview('suave', 'complete', 0)
    assert len(svc.played()) == 1


def test_configure_normaliza(svc):
    sound_service.configure_from_settings({'timer_sounds_enabled': 0,
                                           'timer_sound_volume': '250',
                                           'timer_sound_style': 'gong'})
    assert sound_service.config() == {'enabled': False, 'volume': 100, 'style': 'campana'}
    sound_service.configure_from_settings({})  # datos viejos: por defecto
    assert sound_service.config() == {'enabled': True, 'volume': 70, 'style': 'campana'}
    sound_service.configure(volume='nada')
    assert sound_service.config()['volume'] == 70
    sound_service.play('raro')  # evento desconocido: el de fin de pomodoro
    assert svc.played() == [('campana', 'complete')]


def test_winsound_con_el_alias_de_siempre(monkeypatch):
    calls = []
    fake = type(sys)('winsound')
    fake.SND_ALIAS, fake.SND_ASYNC = 0x10000, 0x1
    fake.PlaySound = lambda alias, flags: calls.append((alias, flags))
    monkeypatch.setitem(sys.modules, 'winsound', fake)
    monkeypatch.setattr(sys, 'platform', 'win32')
    sound_service._play_winsound('break')
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [('SystemQuestion', 0x10001)]


# ================================================================ ajustes
def _make_data(tmp_path, settings=None) -> Path:
    data = {'tasks': [{'id': ID1, 'name': 'Informe', 'completed': False, 'tags': [],
                       'source': 'local', 'asana_id': None, 'due': None}],
            'deleted_tasks': [], 'time_chunks': [], 'history': [], 'ui_snapshot': {},
            'settings': {'pomodoro_min': 1, 'break_min': 1, **(settings or {})}}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_datos_viejos_reciben_los_ajustes_por_defecto(tmp_path):
    s = DataHandler(_make_data(tmp_path)).load_settings()
    assert (s['timer_sounds_enabled'], s['timer_sound_volume'],
            s['timer_sound_style']) == (True, 70, 'campana')
    path = _make_data(tmp_path, {'timer_sounds_enabled': False, 'timer_sound_volume': 30,
                                 'timer_sound_style': 'suave'})
    s = DataHandler(path).load_settings()
    assert (s['timer_sounds_enabled'], s['timer_sound_volume'],
            s['timer_sound_style']) == (False, 30, 'suave')


def _build(qtbot, path):
    dh = DataHandler(path)
    dh.send_asana_ops = lambda ops, *a, **k: [
        {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
         'detail': '', 'offline': False} for op in ops]
    from ui.controllers.app_controller import AppController
    from ui.windows.main_window import MainWindow
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    controller = AppController(win, dh)
    win.show()
    qtbot.waitExposed(win)
    return controller


@pytest.fixture
def sounds(monkeypatch):
    """Registra las llamadas a sound_service.play (conftest las silencia)."""
    calls = []
    monkeypatch.setattr(sound_service, 'play', calls.append)
    monkeypatch.setattr(sound_service, '_config', dict(sound_service._config))
    return calls


@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch, sounds):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    controller = _build(qtbot, _make_data(
        tmp_path, {'timer_sounds_enabled': True, 'timer_sound_volume': 45,
                   'timer_sound_style': 'marimba'}))
    yield controller


def _drain(qtbot) -> None:
    """Deja pasar los singleShot del fin de sesión (sonido a los 100 ms,
    aviso a los 400 ms) dentro del test: pytest-qt cierra la ventana antes
    de la limpieza de los fixtures y el aviso la encontraría borrada."""
    qtbot.wait(500)


def test_controlador_aplica_la_configuracion_al_arrancar(ctrl):
    assert sound_service.config() == {'enabled': True, 'volume': 45, 'style': 'marimba'}


def test_seccion_sonidos_en_ajustes_guarda_y_aplica(ctrl, qtbot):
    from PySide6.QtWidgets import QLabel
    from ui.dialogs.settings_dialog import SettingsDialog
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    labels = [lbl.text() for lbl in dlg.tabs.widget(0).findChildren(QLabel)]
    for text in ("Sonidos", "Sonidos del temporizador", "Estilo", "Volumen",
                 "Probar el aviso"):
        assert text in labels
    assert labels.index("Ventana") < labels.index("Sonidos") < labels.index("Tareas")
    cb = dlg.cb_sound_style
    assert [cb.itemText(i) for i in range(cb.count())] == [
        "Campana", "Marimba", "Suave", "Sistema de Windows"]
    assert dlg.chk_timer_sounds.isChecked()
    assert cb.currentData() == 'marimba' and dlg.sl_sound_volume.value() == 45
    assert dlg.sound_volume_label.text() == "45 %"

    cb.setCurrentIndex(cb.findData('windows'))
    dlg.sl_sound_volume.setValue(80)
    assert dlg.sound_volume_label.text() == "80 %"
    dlg.chk_timer_sounds.setChecked(False)
    # Apagados, estilo, volumen y «Probar» se deshabilitan
    assert not cb.isEnabled() and not dlg.sl_sound_volume.isEnabled()
    assert not dlg.btn_sound_preview.isEnabled()
    dlg.accept_settings()
    s = ctrl.data_handler.load_settings()
    assert (s['timer_sounds_enabled'], s['timer_sound_style'],
            s['timer_sound_volume']) == (False, 'windows', 80)
    assert sound_service.config() == {'enabled': False, 'volume': 80, 'style': 'windows'}
    dlg.deleteLater()


def test_probar_usa_lo_elegido_sin_guardar(ctrl, qtbot, monkeypatch):
    from PySide6.QtCore import Qt
    from ui.dialogs.settings_dialog import SettingsDialog
    previews = []
    monkeypatch.setattr(sound_service, 'preview',
                        lambda style, kind='complete', volume=None:
                        previews.append((style, kind, volume)))
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    dlg.tabs.setCurrentIndex(0)
    dlg.show()
    dlg.cb_sound_style.setCurrentIndex(dlg.cb_sound_style.findData('suave'))
    dlg.sl_sound_volume.setValue(55)
    dlg.btn_sound_preview.ensurePolished()
    qtbot.mouseClick(dlg.btn_sound_preview, Qt.LeftButton)
    assert previews == [('suave', 'complete', 55)]
    # Nada guardado aún
    s = ctrl.data_handler.load_settings()
    assert (s['timer_sound_style'], s['timer_sound_volume']) == ('marimba', 45)
    dlg.reject()
    dlg.deleteLater()


# ================================================================ controlador
class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock(T0)
    monkeypatch.setattr(time, "time", c)
    return c


def _work(ctrl, clock, seconds: float, step: float = 15.0) -> None:
    done = 0.0
    while done < seconds:
        delta = min(step, seconds - done)
        clock.now += delta
        done += delta
        ctrl._tick()


def test_fin_de_pomodoro_y_de_descanso_suenan_una_vez(ctrl, clock, sounds, qtbot):
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    assert sounds == ['start']
    _work(ctrl, clock, 75)  # 1 min de pomodoro y más ticks después
    _drain(qtbot)
    assert ctrl.model.is_break_time
    assert sounds == ['start', 'complete']
    ctrl.toggle_timer()  # arrancar el descanso
    _work(ctrl, clock, 75)
    _drain(qtbot)
    assert not ctrl.model.is_break_time
    assert sounds == ['start', 'complete', 'start', 'break']


def test_pausar_suena_stop_y_reiniciar_o_saltar_no_suenan(ctrl, clock, sounds, qtbot):
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 20)
    ctrl.toggle_timer()
    assert sounds == ['start', 'stop']
    ctrl.toggle_timer()
    _work(ctrl, clock, 10)
    ctrl.reset_timer()  # reiniciar a mitad: sin sonido
    _drain(qtbot)
    assert sounds == ['start', 'stop', 'start']
    # Saltar el descanso tampoco suena
    ctrl.toggle_timer()
    _work(ctrl, clock, 75)
    _drain(qtbot)
    assert ctrl.model.is_break_time
    del sounds[:]
    ctrl.skip_break()
    _drain(qtbot)
    assert sounds == []


def test_parar_suena_como_pausar(ctrl, clock, sounds, qtbot):
    """«Parar» un pomodoro en pausa no suena (el «stop» ya sonó al pausar);
    parado en marcha (por código) suena «stop» una vez, y nunca «complete»:
    no se completó. En Libre, «Parar» es el fin de sesión de siempre."""
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 20)
    ctrl.toggle_timer()
    ctrl.timer_panel.stop_btn.click()
    _drain(qtbot)
    assert sounds == ['start', 'stop']
    ctrl.toggle_timer()
    _work(ctrl, clock, 10)
    ctrl.stop_session()
    _drain(qtbot)
    assert sounds == ['start', 'stop', 'start', 'stop']
    ctrl.timer_panel.kind_buttons['free'].click()
    assert ctrl.model.is_free
    del sounds[:]
    ctrl.toggle_timer()
    _work(ctrl, clock, 6 * 60, step=30)
    ctrl.timer_panel.stop_btn.click()
    _drain(qtbot)
    assert sounds == ['start', 'complete']


def test_fin_de_pomodoro_con_la_mini_ventana(ctrl, clock, sounds, qtbot):
    ctrl.select_task(ID1)
    ctrl.show_compact()
    assert not ctrl.window.isVisible() and ctrl.compact_window.isVisible()
    ctrl.toggle_timer()
    _work(ctrl, clock, 75)
    _drain(qtbot)
    assert sounds == ['start', 'complete']
    ctrl.show_normal()


def test_fin_de_pomodoro_con_la_principal_minimizada(ctrl, clock, sounds, qtbot):
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    ctrl.window.showMinimized()
    _work(ctrl, clock, 75)
    _drain(qtbot)
    assert sounds == ['start', 'complete']
    ctrl.window.showNormal()


def test_fin_de_sesion_libre(ctrl, clock, sounds, qtbot):
    ctrl.select_task(ID1)
    ctrl.timer_panel.kind_buttons['free'].click()
    assert ctrl.model.is_free
    ctrl.toggle_timer()
    _work(ctrl, clock, 6 * 60, step=30)
    ctrl.reset_timer()  # en Libre termina la sesión, como «Parar»
    _drain(qtbot)
    assert sounds == ['start', 'complete']
    # Una sesión corta (menos de 5 min) no suena, como antes
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    ctrl.reset_timer()
    _drain(qtbot)
    assert sounds == ['start', 'complete', 'start']
    # Al salir de la app (silent) tampoco
    ctrl.toggle_timer()
    _work(ctrl, clock, 6 * 60, step=30)
    ctrl.finish_free_session(silent=True)
    _drain(qtbot)
    assert sounds == ['start', 'complete', 'start', 'start']


# ------------------------------------------------------- revisión cruzada
@pytest.mark.parametrize("raw,volume", [
    ("1e999", 70), ("-1e999", 70), ("Infinity", 70), ("NaN", 70), ("1" + "0" * 400, 100),
    ('"abc"', 70), ("null", 70), ("true", 70), ("45.6", 46)])
def test_un_json_editado_a_mano_no_tumba_nada(tmp_path, raw, volume):
    """json.load acepta Infinity, 1e999 o enteros de cientos de dígitos:
    el volumen se sanea al cargar (antes: OverflowError al arrancar y al
    abrir Ajustes) y el resto de claves también."""
    path = tmp_path / "data.json"
    path.write_text('{"tasks": [], "settings": {"timer_sound_volume": %s,'
                    ' "timer_sounds_enabled": null, "timer_sound_style": 7}}' % raw,
                    encoding="utf-8")
    dh = DataHandler(path)
    assert dh.load_error is None
    s = dh.load_settings()
    assert (s['timer_sounds_enabled'], s['timer_sound_volume'],
            s['timer_sound_style']) == (True, volume, 'campana')
    assert sound_service.normalize_volume(json.loads(raw)) in (70, 100, 46)


def test_normalize_volume_con_valores_extremos():
    for value in (float('inf'), float('-inf'), float('nan'), True, None, "x"):
        assert sound_service.normalize_volume(value) == sound_service.DEFAULT_VOLUME
    assert sound_service.normalize_volume(10 ** 400) == 100
    assert sound_service.normalize_volume(-10 ** 400) == 0


def test_los_estilos_del_data_handler_son_los_del_servicio():
    from core.data_handler import TIMER_SOUND_STYLES
    assert TIMER_SOUND_STYLES == sound_service.STYLE_KEYS


def test_arranca_y_abre_ajustes_con_un_volumen_infinito(qtbot, tmp_path, monkeypatch, sounds):
    from ui.dialogs.settings_dialog import SettingsDialog
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    path = _make_data(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace(
        '"settings": {', '"settings": {"timer_sound_volume": 1e999, '), encoding="utf-8")
    controller = _build(qtbot, path)
    assert sound_service.config()['volume'] == 70
    dlg = SettingsDialog(controller.window, controller)
    qtbot.addWidget(dlg)
    assert dlg.sl_sound_volume.value() == 70
    dlg.deleteLater()


def test_estilo_windows_suena_al_volumen_del_sistema(svc):
    """winsound no admite volumen: con «Sistema de Windows» el deslizador no
    aplica (tampoco su 0 %)."""
    sound_service.preview('windows', 'complete', 0)
    sound_service.configure(style='windows', volume=0)
    sound_service.play('break')
    assert svc.winsound == ['complete', 'break'] and svc.created == []


def test_ajustes_con_estilo_windows_desactivan_el_volumen(ctrl, qtbot):
    from PySide6.QtWidgets import QLabel
    from ui.dialogs import settings_dialog as sd
    dlg = sd.SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    page = dlg.tabs.widget(0)

    def texts():
        return [lbl.text() for lbl in page.findChildren(QLabel)]
    assert dlg.sl_sound_volume.isEnabled() and sd.SOUND_VOLUME_HELP in texts()
    dlg.cb_sound_style.setCurrentIndex(dlg.cb_sound_style.findData('windows'))
    assert not dlg.sl_sound_volume.isEnabled() and not dlg.sound_volume_label.isEnabled()
    assert dlg.btn_sound_preview.isEnabled() and dlg.cb_sound_style.isEnabled()
    assert sd.SOUND_VOLUME_WINDOWS_HELP in texts() and sd.SOUND_PREVIEW_WINDOWS_HELP in texts()
    dlg.cb_sound_style.setCurrentIndex(dlg.cb_sound_style.findData('suave'))
    assert dlg.sl_sound_volume.isEnabled() and sd.SOUND_PREVIEW_HELP in texts()
    dlg.deleteLater()
