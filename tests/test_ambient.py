"""Sonidos de ambiente: catálogo, servicio (fundidos, relevo sin cortes,
volúmenes, «solo durante el enfoque», errores, persistencia y «Reanudar»),
el mezclador del diálogo de Música y el punto del botón Música.

Nada suena: los reproductores son falsos (tests/ambient_fakes.py; conftest
impide crear QMediaPlayer reales) y el tiempo de los fundidos lo lleva un
reloj falso que se avanza a mano. Los audios son archivos falsos en tmp
(utils.paths.resource_base apunta allí).
"""
import json
import math
import os
import sys
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402

from core import ambient_catalog as catalog  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from services.ambient_service import AmbientService  # noqa: E402
from tests.ambient_fakes import FakeVoice  # noqa: E402
from ui.theme import fonts  # noqa: E402

MISSING = 'chimenea'      # el sonido sin archivo en los tests


class Clock:
    """Reloj monótono falso (segundos)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms / 1000.0


@pytest.fixture
def sounds(tmp_path, monkeypatch):
    """Recursos en tmp con todos los audios (falsos) salvo MISSING."""
    from utils import paths
    base = tmp_path / "recursos"
    folder = base / "assets" / "sounds" / "ambient"
    folder.mkdir(parents=True)
    for sound_id in catalog.SOUND_IDS:
        if sound_id != MISSING:
            (folder / f"{sound_id}.ogg").write_bytes(b"OggS falso")
    monkeypatch.setattr(paths, "resource_base", lambda: base)
    return folder


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(qtbot, sounds, clock):
    service = AmbientService(clock=clock)
    yield service
    service._ticker.stop()


def _voices(svc, sound_id):
    return svc._loopers[sound_id].voices


def _main(svc, sound_id):
    lp = svc._loopers[sound_id]
    return lp.voices[lp.main], lp.voices[1 - lp.main]


def _step(svc, clock, ms):
    clock.advance(ms)
    svc._tick()


def _full_in(svc, clock, sound_id):
    svc.set_active(sound_id, True)
    _step(svc, clock, svc.FADE_IN_MS + 10)
    return _main(svc, sound_id)


# ================================================================ catálogo
def test_catalogo_en_orden_con_iconos_y_archivos(sounds):
    from ui.widgets.icon_button import ICONS_DIR
    assert catalog.SOUND_IDS == ('lluvia', 'tormenta', 'oceano', 'rio', 'bosque', 'chimenea',
                                 'ruido_blanco', 'ruido_rosa', 'ruido_marron')
    names = [s.name for s in catalog.SOUNDS]
    assert names == ["Lluvia", "Tormenta", "Océano", "Río", "Bosque", "Chimenea",
                     "Ruido blanco", "Ruido rosa", "Ruido marrón"]
    icons = {s.id: s.icon for s in catalog.SOUNDS}
    assert icons['lluvia'] == 'cloud-rain' and icons['tormenta'] == 'cloud-lightning'
    assert icons['oceano'] == 'waves' and icons['rio'] == 'droplets'
    assert icons['bosque'] == 'trees' and icons['chimenea'] == 'flame'
    assert {icons[s] for s in ('ruido_blanco', 'ruido_rosa', 'ruido_marron')} == {
        'audio-waveform'}
    for s in catalog.SOUNDS:
        assert (ICONS_DIR / f"{s.icon}.svg").is_file()
        assert 0 < s.volume <= 100 and s.hint
        assert s.group == (catalog.GROUP_NOISE if s.id.startswith('ruido')
                           else catalog.GROUP_NATURE)
    assert catalog.sound_path('lluvia') == sounds / "lluvia.ogg"
    assert catalog.is_available('lluvia') and not catalog.is_available(MISSING)
    assert not catalog.is_available('desconocido')
    # Los ruidos arrancan más bajos que la lluvia
    assert catalog.get('ruido_blanco').volume < catalog.get('lluvia').volume


def test_el_spec_empaqueta_los_audios_de_ambiente(tmp_path):
    from tests.test_installer import _run_spec
    datas = _run_spec(tmp_path)["Analysis"]["datas"]
    assert ('assets/sounds/ambient', 'assets/sounds/ambient') in datas
    assert (V9 / "assets" / "sounds" / "ambient").is_dir()   # PyInstaller la exige


def test_la_mezcla_guardada_se_sanea():
    mix = catalog.normalize_mix(None)
    assert mix == catalog.default_mix() and mix['last_mix'] == [] and not mix['focus_only']
    raw = {'volumes': {'lluvia': 150, 'oceano': -3, 'rio': True, 'bosque': 'x',
                       'ruido_rosa': 33.6, 'inventado': 50},
           'master': float('nan'), 'focus_only': 'sí',
           'last_mix': ['oceano', 'inventado', 'lluvia', 'lluvia', 3]}
    mix = catalog.normalize_mix(raw)
    v = mix['volumes']
    assert v['lluvia'] == 100 and v['oceano'] == 0 and v['ruido_rosa'] == 34
    assert v['rio'] == catalog.get('rio').volume and v['bosque'] == catalog.get('bosque').volume
    assert 'inventado' not in v and set(v) == set(catalog.SOUND_IDS)
    assert mix['master'] == catalog.MASTER_DEFAULT and mix['focus_only'] is False
    assert mix['last_mix'] == ['lluvia', 'oceano']        # orden del catálogo, sin repetir
    assert catalog.mix_gain(100, 100) == 1.0 and catalog.mix_gain(0, 80) == 0.0
    assert catalog.mix_gain(50, 100) == pytest.approx(0.25)
    assert catalog.mix_gain(60, 70) == pytest.approx((0.6 * 0.7) ** 2)


def test_data_handler_carga_la_mezcla_saneada(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({'tasks': [], 'settings': {
        'ambient': {'master': 300, 'last_mix': ['rio', 'nada']}}}), encoding="utf-8")
    dh = DataHandler(p)
    mix = dh.load_settings()['ambient']
    assert mix['master'] == 100 and mix['last_mix'] == ['rio']
    p.write_text(json.dumps({'tasks': [], 'settings': {}}), encoding="utf-8")
    assert DataHandler(p).load_settings()['ambient'] == catalog.default_mix()


# ================================================================ servicio
def test_activar_entra_con_fundido(svc, clock, qtbot):
    states = []
    svc.state_changed.connect(states.append)
    with qtbot.waitSignal(svc.changed):
        assert svc.toggle('lluvia') is True
    assert svc.active_ids() == ['lluvia'] and svc.is_playing() and states == ['playing']
    a, b = _voices(svc, 'lluvia')
    assert a.source.endswith("lluvia.ogg") and b.source == a.source   # los dos cargados
    assert a.state == 'playing' and b.state == 'stopped' and a.gain == 0.0
    target = svc.gain_of('lluvia')
    assert target == pytest.approx(catalog.mix_gain(catalog.get('lluvia').volume,
                                                    catalog.MASTER_DEFAULT))
    gains = []
    for _ in range(5):
        _step(svc, clock, svc.FADE_IN_MS / 5)
        gains.append(a.gain)
    assert gains == sorted(gains) and 0 < gains[0] < target
    assert gains[-1] == pytest.approx(target) and b.gain == 0.0
    # Ya entero: tick lento (solo vigila el final del archivo)
    _step(svc, clock, 10)
    assert svc._ticker.interval() == svc.STEADY_MS


def test_desactivar_sale_con_fundido_y_suelta_los_reproductores(svc, clock):
    a, b = _full_in(svc, clock, 'oceano')
    target = a.gain
    svc.toggle('oceano')
    assert svc.active_ids() == [] and svc.state() == 'off'
    assert a.state == 'playing' and not a.disposed           # aún suena: se funde
    _step(svc, clock, svc.FADE_OUT_MS / 2)
    assert 0 < a.gain < target
    _step(svc, clock, svc.FADE_OUT_MS / 2 + 10)
    assert a.disposed and b.disposed and 'oceano' not in svc._loopers
    assert a.gain == 0.0
    _step(svc, clock, 10)
    assert not svc._ticker.isActive()                         # nada suena: sin tick


def test_parar_o_volver_a_activar_a_mitad_de_un_fundido(svc, clock):
    svc.toggle('rio')
    _step(svc, clock, svc.FADE_IN_MS / 2)
    a, _ = _main(svc, 'rio')
    half = a.gain
    assert 0 < half < svc.gain_of('rio')
    svc.toggle('rio')                  # apagar a mitad de la entrada
    _step(svc, clock, 0)
    assert a.gain == pytest.approx(half)        # parte de donde iba: sin saltos
    _step(svc, clock, svc.FADE_OUT_MS / 2)
    lower = a.gain
    assert lower < half
    svc.toggle('rio')                  # y encender a mitad de la salida
    assert svc.active_ids() == ['rio'] and not a.disposed
    _step(svc, clock, 0)
    assert a.gain == pytest.approx(lower)
    _step(svc, clock, svc.FADE_IN_MS + 10)
    assert a.gain == pytest.approx(svc.gain_of('rio')) and not a.disposed


def test_relevo_con_fundido_cruzado_de_igual_potencia(svc, clock):
    a, b = _full_in(svc, clock, 'lluvia')
    target = svc.gain_of('lluvia')
    a.dur = 60000
    a.pos = 56000                       # quedan 4 s: aún no
    _step(svc, clock, 200)
    assert b.state == 'stopped' and svc._loopers['lluvia'].xfade_t0 is None
    a.pos = 57500                       # quedan 2,5 s: entra la otra desde el principio
    _step(svc, clock, 200)
    lp = svc._loopers['lluvia']
    assert b.state == 'playing' and b.pos == 0 and lp.xfade_ms == pytest.approx(2500)
    assert a.gain == pytest.approx(target) and b.gain == pytest.approx(0.0, abs=1e-9)
    assert svc._ticker.interval() == svc.TICK_MS
    for _ in range(4):
        _step(svc, clock, 500)
        # Igual potencia: la suma de cuadrados no cambia (sin bache de volumen)
        assert a.gain ** 2 + b.gain ** 2 == pytest.approx(target ** 2)
        assert 0 < a.gain < target and 0 < b.gain < target
    _step(svc, clock, 600)
    assert lp.main == 1 and a.state == 'stopped' and a.gain == 0.0
    assert b.gain == pytest.approx(target) and lp.xfade_t0 is None
    # Y vuelta a empezar: al acercarse b al final, entra a otra vez
    b.dur, b.pos = 60000, 58000
    _step(svc, clock, 200)
    assert a.state == 'playing' and a.plays == 2 and a.pos == 0


def test_archivo_corto_y_duracion_desconocida(svc, clock):
    a, b = _full_in(svc, clock, 'bosque')
    # Sin duración no hay relevo… pero si el archivo se acaba, salta al otro
    a.pos = 90000
    _step(svc, clock, 200)
    assert b.state == 'stopped'
    a.ended = True
    _step(svc, clock, 200)
    lp = svc._loopers['bosque']
    assert lp.main == 1 and b.state == 'playing' and a.state == 'stopped'
    assert b.gain == pytest.approx(svc.gain_of('bosque')) and a.gain == 0.0
    # Un archivo muy corto: el cruce dura un tercio de él
    b.dur, b.pos = 3000, 2100
    _step(svc, clock, 40)
    assert lp.xfade_t0 is not None and lp.xfade_ms == pytest.approx(900)


def test_volumen_por_sonido_por_el_general(svc, clock, qtbot):
    a, _ = _full_in(svc, clock, 'lluvia')
    c, _ = _full_in(svc, clock, 'ruido_rosa')
    svc.set_volume('lluvia', 80)
    assert a.gain == pytest.approx(catalog.mix_gain(80, catalog.MASTER_DEFAULT))
    assert c.gain == pytest.approx(   # el otro, igual
        catalog.mix_gain(catalog.get('ruido_rosa').volume, catalog.MASTER_DEFAULT))
    svc.set_master(50)
    assert a.gain == pytest.approx(catalog.mix_gain(80, 50))
    assert c.gain == pytest.approx(catalog.mix_gain(catalog.get('ruido_rosa').volume, 50))
    svc.set_volume('lluvia', 150)
    assert svc.volume('lluvia') == 100
    svc.set_master(-4)
    assert svc.master() == 0 and a.gain == 0.0 and c.gain == 0.0
    svc.set_volume('inventado', 30)                              # se ignora
    assert 'inventado' not in svc._mix['volumes']


def test_solo_durante_el_enfoque(svc, clock):
    svc.set_focus_only(True)
    assert svc.toggle('tormenta') is True
    a, b = _voices(svc, 'tormenta')
    assert svc.state() == 'waiting' and svc.sound_state('tormenta') == 'waiting'
    assert a.state == 'stopped' and a.gain == 0.0 and not svc.is_playing()
    svc.on_focus_state(True)                       # arranca un pomodoro
    assert svc.state() == 'playing' and a.state == 'playing'
    _step(svc, clock, svc.FADE_IN_MS + 10)
    assert a.gain == pytest.approx(svc.gain_of('tormenta'))
    a.pos = 30000
    svc.on_focus_state(False)                      # descanso: baja y se pausa
    assert svc.state() == 'waiting' and svc.active_ids() == ['tormenta']
    _step(svc, clock, svc.FADE_OUT_MS / 2)
    assert a.state == 'playing' and 0 < a.gain < svc.gain_of('tormenta')
    _step(svc, clock, svc.FADE_OUT_MS / 2 + 10)
    assert a.state == 'paused' and a.gain == 0.0 and not a.disposed and a.pos == 30000
    svc.on_focus_state(True)                       # sigue donde iba
    assert a.state == 'playing' and a.pos == 30000
    # Quitar la opción con el timer parado: vuelve a sonar
    svc.on_focus_state(False)
    _step(svc, clock, svc.FADE_OUT_MS + 10)
    assert a.state == 'paused'
    svc.set_focus_only(False)
    assert a.state == 'playing' and svc.state() == 'playing'
    # Sin la opción, el timer no lo para
    svc.on_focus_state(False)
    _step(svc, clock, 2000)
    assert a.state == 'playing' and a.gain == pytest.approx(svc.gain_of('tormenta'))


def test_enfoque_que_se_pausa_durante_un_relevo(svc, clock):
    svc.set_focus_only(True)
    svc.on_focus_state(True)
    a, b = _full_in(svc, clock, 'oceano')
    a.dur, a.pos = 60000, 58000
    _step(svc, clock, 40)
    assert b.state == 'playing'
    svc.on_focus_state(False)
    _step(svc, clock, svc.FADE_OUT_MS + 10)
    lp = svc._loopers['oceano']
    # El relevo se da por hecho: queda en pausa la voz nueva, la vieja parada
    assert lp.main == 1 and b.state == 'paused' and a.state == 'stopped'
    assert lp.xfade_t0 is None


def test_archivo_que_falla_se_desactiva_y_avisa(svc, clock, qtbot):
    a, _ = _full_in(svc, clock, 'lluvia')
    svc.toggle('rio')
    svc.stop_all()
    _step(svc, clock, svc.FADE_OUT_MS + 10)
    svc.resume_last()
    assert svc.last_mix() == ['lluvia', 'rio']
    a, _ = _main(svc, 'lluvia')
    a.err = "Formato no admitido"
    with qtbot.waitSignal(svc.error_occurred) as blocker:
        _step(svc, clock, 40)
    assert blocker.args == ["No se pudo reproducir «Lluvia»: Formato no admitido"]
    assert svc.active_ids() == ['rio'] and a.disposed
    assert svc.sound_state('lluvia') == 'unavailable' and not svc.available('lluvia')
    assert "Formato no admitido" in svc.unavailable_reason('lluvia')
    assert svc.last_mix() == ['rio']               # «Reanudar» no lo vuelve a intentar
    assert svc.toggle('lluvia') is False


def test_fabrica_que_falla_no_rompe_nada(sounds, clock, qtbot):
    def broken(parent):
        raise RuntimeError("sin dispositivo de audio")
    svc = AmbientService(clock=clock, voice_factory=broken)
    errors = []
    svc.error_occurred.connect(errors.append)
    assert svc.set_active('lluvia', True) is False
    assert svc.active_ids() == [] and errors and "sin dispositivo de audio" in errors[0]


def test_sonido_sin_archivo_no_esta_disponible(svc, clock):
    errors = []
    svc.error_occurred.connect(errors.append)
    assert not svc.available(MISSING) and svc.sound_state(MISSING) == 'unavailable'
    assert "falta su archivo" in svc.unavailable_reason(MISSING)
    assert svc.toggle(MISSING) is False and svc.set_active('inventado', True) is False
    assert svc.active_ids() == [] and FakeVoice.created == [] and errors == []


def test_persistencia_y_reanudar(tmp_path, sounds, clock, qtbot):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({'tasks': [], 'settings': {}}), encoding="utf-8")
    dh = DataHandler(path)
    svc = AmbientService(data_handler=dh, clock=clock)
    assert not svc.can_resume()                      # nada que reanudar aún
    svc.toggle('oceano')
    svc.toggle('lluvia')
    svc.set_volume('oceano', 25)
    svc.set_master(90)
    svc.set_focus_only(True)
    qtbot.waitUntil(lambda: not svc._save_timer.isActive(), timeout=3000)
    saved = json.loads(path.read_text(encoding="utf-8"))['settings']['ambient']
    assert saved['volumes']['oceano'] == 25 and saved['master'] == 90
    assert saved['focus_only'] is True and saved['last_mix'] == ['lluvia', 'oceano']
    svc.stop_all()
    assert svc.active_ids() == [] and svc.last_mix() == ['lluvia', 'oceano']
    assert svc.can_resume()
    svc._ticker.stop()

    # Al abrir otra vez: no suena nada solo y la mezcla espera a «Reanudar»
    FakeVoice.created.clear()
    again = AmbientService(data_handler=DataHandler(path), clock=clock)
    assert again.active_ids() == [] and again.state() == 'off' and FakeVoice.created == []
    assert again.volume('oceano') == 25 and again.master() == 90 and again.focus_only()
    assert again.can_resume()
    assert again.resume_last() == 2
    assert again.active_ids() == ['lluvia', 'oceano'] and again.state() == 'waiting'
    again._ticker.stop()


def test_reanudar_salta_lo_que_ya_no_esta(tmp_path, sounds, clock):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({'tasks': [], 'settings': {'ambient': {
        'last_mix': [MISSING, 'rio']}}}), encoding="utf-8")
    svc = AmbientService(data_handler=DataHandler(path), clock=clock)
    assert svc.can_resume() and svc.resume_last() == 1 and svc.active_ids() == ['rio']
    svc._ticker.stop()
    (sounds / "rio.ogg").unlink()
    svc2 = AmbientService(data_handler=DataHandler(path), clock=clock)
    assert not svc2.can_resume() and svc2.resume_last() == 0


def test_importar_datos_relee_la_mezcla(tmp_path, sounds, clock):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({'tasks': [], 'settings': {}}), encoding="utf-8")
    dh = DataHandler(path)
    svc = AmbientService(data_handler=dh, clock=clock)
    svc.set_master(20)
    svc._persist()
    dh.data = json.loads(json.dumps(dh.data))       # como al importar un backup
    dh.data['settings']['ambient']['master'] = 55
    assert svc.master() == 55


# ================================================================ interfaz
@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch, sounds, clock):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    from services import music_service
    monkeypatch.setattr(music_service, "MUSIC_DIR", tmp_path / "lofi")
    from tests.test_ui_smoke import _make_data
    path = _make_data(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data['settings']['ambient'] = {'last_mix': ['lluvia', 'oceano']}
    path.write_text(json.dumps(data), encoding="utf-8")
    dh = DataHandler(path)
    dh.send_asana_ops = lambda ops, *a, **k: [
        {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
         'detail': '', 'offline': False} for op in ops]
    from ui.controllers.app_controller import AppController
    from ui.windows.main_window import MainWindow
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    controller = AppController(win, dh)
    controller.music.set_muted(True)
    controller.ambient._clock = clock
    win.show()
    qtbot.waitExposed(win)
    yield controller
    controller.ambient._ticker.stop()
    controller.music.stop()


def _open(ctrl, qtbot):
    from ui.dialogs.music_dialog import MusicDialog
    dlg = MusicDialog(ctrl.window, ctrl.music, ambient=ctrl.ambient)
    dlg.show()
    qtbot.waitExposed(dlg)
    return dlg


def _close(dlg):
    if dlg._overlay:
        dlg._overlay.hide()
    dlg.done(0)
    dlg.deleteLater()


def _select_ambient(dlg):
    from ui.dialogs.ambient_mixer import AMBIENT_ID
    row = dlg.lists_view.row_of(AMBIENT_ID)
    assert row == 0                                   # arriba del todo
    dlg.lists_view.setCurrentRow(row)
    assert dlg.right_stack.currentWidget() is dlg.mixer


def test_al_arrancar_no_suena_nada(ctrl):
    amb = ctrl.ambient
    assert amb.active_ids() == [] and amb.state() == 'off' and FakeVoice.created == []
    assert amb.last_mix() == ['lluvia', 'oceano'] and amb.can_resume()
    dot = ctrl.window.title_bar.ambient_dot
    assert dot.isHidden()
    assert "Ambiente" not in ctrl.window.title_bar.music_btn.toolTip()


def test_entrada_fija_y_mezclador(ctrl, qtbot):
    from ui.dialogs.ambient_mixer import AMBIENT_ID
    ctrl.music.create_list("Radios")
    dlg = _open(ctrl, qtbot)
    view = dlg.lists_view
    rows = [view.item(i).data(Qt.UserRole) for i in range(view.count())]
    assert [r['name'] for r in rows] == ["Sonidos de ambiente", "Carpeta lofi", "Radios"]
    assert rows[0]['id'] == AMBIENT_ID and rows[0]['count'] == 0
    assert dlg.lists_count.text() == "1"               # solo cuenta las listas propias
    assert dlg.right_stack.currentWidget() is dlg.panel  # abre en la lista, como siempre
    _select_ambient(dlg)
    mixer = dlg.mixer
    assert list(mixer.cards) == list(catalog.SOUND_IDS)
    lluvia, chimenea = mixer.cards['lluvia'], mixer.cards[MISSING]
    # Sin archivo: desactivada y el tooltip lo dice
    assert not chimenea.isEnabled() and "falta su archivo" in chimenea.toolTip()
    assert lluvia.isEnabled() and not lluvia.slider.isVisible()
    assert mixer.main_btn.text() == "Reanudar" and mixer.main_btn.isEnabled()
    assert "Lluvia, Océano" in mixer.main_btn.toolTip()
    # Clic: se enciende con su deslizador
    qtbot.mouseClick(lluvia, Qt.LeftButton, pos=QPoint(24, 24))
    assert ctrl.ambient.active_ids() == ['lluvia'] and lluvia.state == 'playing'
    assert lluvia.slider.isVisible() and lluvia.slider.value() == catalog.get('lluvia').volume
    assert mixer.main_btn.text() == "Parar todo"
    assert view.item(0).data(Qt.UserRole)['count'] == 1 and view.ambient_playing
    assert mixer.meta.full_text() == "Sonando: Lluvia" and mixer._anim.isActive()
    # Su deslizador y el general mandan al servicio
    lluvia.slider.setValue(35)
    assert ctrl.ambient.volume('lluvia') == 35
    mixer.master.setValue(40)
    assert ctrl.ambient.master() == 40
    # Un clic en el deslizador no apaga la tarjeta
    qtbot.mouseClick(lluvia.slider, Qt.LeftButton)
    assert ctrl.ambient.active_ids() == ['lluvia']
    # Teclado: Enter con el foco en la tarjeta
    oceano = mixer.cards['oceano']
    oceano.setFocus(Qt.TabFocusReason)
    qtbot.keyClick(oceano, Qt.Key_Return)
    assert ctrl.ambient.active_ids() == ['lluvia', 'oceano']
    # «Parar todo» y «Reanudar»
    qtbot.mouseClick(mixer.main_btn, Qt.LeftButton)
    assert ctrl.ambient.active_ids() == [] and mixer.main_btn.text() == "Reanudar"
    assert not lluvia.slider.isVisible() and view.item(0).data(Qt.UserRole)['count'] == 0
    qtbot.mouseClick(mixer.main_btn, Qt.LeftButton)
    assert ctrl.ambient.active_ids() == ['lluvia', 'oceano']
    # «Solo durante el enfoque» desde el mezclador (el timer está parado)
    mixer.focus_toggle.click()
    assert ctrl.ambient.focus_only() and lluvia.state == 'waiting'
    assert "Sonará al enfocar: " in mixer.meta.full_text()
    assert not view.ambient_playing
    # Volver a una lista: el panel de siempre, y el ambiente sigue
    view.setCurrentRow(2)
    assert dlg.right_stack.currentWidget() is dlg.panel and dlg._list_info()['name'] == "Radios"
    assert ctrl.ambient.active_ids() == ['lluvia', 'oceano']
    _close(dlg)
    assert not mixer._anim.isActive()


def test_sin_servicio_de_ambiente_no_hay_entrada(ctrl, qtbot):
    from ui.dialogs.music_dialog import MusicDialog
    dlg = MusicDialog(ctrl.window, ctrl.music)
    dlg.show()
    names = [dlg.lists_view.item(i).data(Qt.UserRole)['name']
             for i in range(dlg.lists_view.count())]
    assert names == ["Carpeta lofi"] and dlg.mixer is None
    _close(dlg)


def test_tab_con_el_mezclador(ctrl, qtbot):
    ctrl.music.create_list("Radios")
    dlg = _open(ctrl, qtbot)
    _select_ambient(dlg)
    m = dlg.mixer
    chain, widget = [], dlg.lists_view
    for _ in range(300):
        chain.append(widget)
        widget = widget.nextInFocusChain()
        if widget is dlg.lists_view:
            break
    order = [dlg.lists_view, dlg.new_list_btn, m.main_btn, m.cards['lluvia'],
             m.cards['lluvia'].slider, m.cards['tormenta'], m.cards['ruido_marron'],
             m.master, m.focus_toggle, dlg.shuffle_btn, dlg.items_view, dlg.now.play_btn,
             dlg.now.focus_toggle, dlg._close_btn]
    positions = [chain.index(w) for w in order]
    assert positions == sorted(positions)
    _close(dlg)


def test_soltar_archivos_con_el_mezclador_a_la_vista(ctrl, qtbot, tmp_path):
    track = tmp_path / "tema.mp3"
    track.write_bytes(b"\xff\xfb" + b"\0" * 64)
    dlg = _open(ctrl, qtbot)
    _select_ambient(dlg)
    assert dlg._visible_drop_hint() is dlg.mixer_hint
    # Los sueltos van a «Mis enlaces» (no suena ninguna lista propia)
    assert dlg._drop_target_text() == ("Suelta para añadir a «Mis enlaces»"
                                       " (las carpetas y listas crean la suya)")
    assert dlg._add_paths([str(track)]) == 1
    # Como en la lofi: el servicio crea la lista y se pasa a ella
    assert dlg.right_stack.currentWidget() is dlg.panel
    assert dlg._user_list_selected() and dlg.items_view.count() == 1
    assert dlg._visible_drop_hint() is dlg.drop_hint
    _close(dlg)


def test_mezclador_al_tamano_minimo_y_en_todos_los_temas(ctrl, qtbot):
    from ui.dialogs.music_dialog import CARD_MIN
    from ui.theme.tokens import THEMES, ThemeManager
    dlg = _open(ctrl, qtbot)
    dlg._card.resize(CARD_MIN)
    dlg.resize(dlg.minimumSizeHint())
    _select_ambient(dlg)
    ctrl.ambient.toggle('lluvia')
    qtbot.wait(20)
    mixer = dlg.mixer
    assert mixer.width() >= 3 * 150                      # tres columnas caben
    for card in mixer.cards.values():
        assert card.width() >= card.minimumWidth() and card.height() == card.minimumHeight()
    # La cabecera no se monta: el botón a la derecha del título
    btn = mixer.main_btn
    assert btn.geometry().left() >= mixer.title.geometry().left()
    assert btn.geometry().right() <= mixer.contentsRect().right()
    # Cada tema, a mano (como test_music_ui): emitir theme_changed despertaría
    # a ventanas ya destruidas de otros tests
    manager = ThemeManager.instance()
    original = manager.current
    try:
        for theme in THEMES.values():
            manager.current = theme
            mixer.restyle()
            ctrl.window.title_bar._retheme()
            assert theme.bg in mixer.styleSheet() and theme.border in mixer.styleSheet()
            assert not mixer.grab().isNull()
            assert not ctrl.window.title_bar.grab().isNull()
    finally:
        manager.current = original
        mixer.restyle()
    _close(dlg)


def test_punto_del_boton_musica(ctrl, clock):
    tb = ctrl.window.title_bar
    base = tb.music_btn.toolTip()
    amb = ctrl.ambient
    amb.toggle('lluvia')
    amb.toggle('rio')
    assert tb.ambient_dot.isVisible() and tb.ambient_dot.state == 'playing'
    assert tb.music_btn.toolTip() == f"{base}\nAmbiente: Lluvia, Río"
    amb.set_focus_only(True)                         # timer parado: espera
    assert tb.ambient_dot.state == 'waiting' and tb.ambient_dot.isVisible()
    assert "Ambiente al enfocar: Lluvia, Río" in tb.music_btn.toolTip()
    amb.stop_all()
    assert tb.ambient_dot.isHidden() and tb.music_btn.toolTip() == base
    # El punto no se come el clic del botón Música
    assert tb.ambient_dot.testAttribute(Qt.WA_TransparentForMouseEvents)
    # El mini reproductor de la música no se entera
    assert ctrl.timer_panel.mini_player.isHidden()


def test_el_controlador_pasa_el_enfoque_y_lo_apaga_al_salir(ctrl, clock, monkeypatch):
    amb = ctrl.ambient
    amb.set_focus_only(True)
    amb.toggle('bosque')
    assert amb.state() == 'waiting'
    monkeypatch.setattr(ctrl.model, "is_running", True)
    monkeypatch.setattr(ctrl.model, "is_break_time", False)
    ctrl._sync_music_focus()
    assert amb.state() == 'playing'
    monkeypatch.setattr(ctrl.model, "is_break_time", True)      # descanso
    ctrl._sync_music_focus()
    assert amb.state() == 'waiting'
    monkeypatch.setattr(ctrl.model, "is_break_time", False)
    ctrl._sync_music_focus()
    a, _ = _main(amb, 'bosque')
    from ui.windows import welcome_farewell
    monkeypatch.setattr(welcome_farewell, "FarewellWindow", lambda on_done, **kw: None)
    monkeypatch.setattr(ctrl.focus_mode, "cleanup_system_blocks", lambda: False)
    monkeypatch.setattr(ctrl.model, "is_running", False)
    ctrl.quit_with_farewell()
    # Sale con su fundido (no un corte) y la mezcla queda para «Reanudar»
    assert amb.active_ids() == [] and not a.disposed and a.state == 'playing'
    _step(amb, clock, amb.FADE_OUT_MS + 10)
    assert a.disposed and amb.last_mix() == ['bosque']
    saved = ctrl.data_handler.load_settings()['ambient']
    assert saved['last_mix'] == ['bosque'] and saved['focus_only'] is True


def test_errores_del_ambiente_como_toast_con_el_dialogo_cerrado(ctrl, clock, monkeypatch):
    from ui.widgets import toast
    seen = []
    monkeypatch.setattr(toast, "show_warning", lambda win, msg, **kw: seen.append(msg))
    amb = ctrl.ambient
    amb.toggle('lluvia')
    a, _ = _main(amb, 'lluvia')
    a.err = "Archivo dañado"
    _step(amb, clock, 40)
    assert seen == ["No se pudo reproducir «Lluvia»: Archivo dañado"]
    assert ctrl.window.title_bar.ambient_dot.isHidden()


def _min_mixer(ctrl, qtbot):
    """El diálogo a su tamaño mínimo con el mezclador a la vista (el
    contenido no cabe: hay scroll)."""
    from ui.dialogs.music_dialog import CARD_MIN
    dlg = _open(ctrl, qtbot)
    dlg._card.resize(CARD_MIN)
    dlg.resize(dlg.minimumSizeHint())
    _select_ambient(dlg)
    qtbot.wait(20)
    return dlg


def test_enter_mantenido_no_alterna_sin_parar(ctrl, qtbot):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    dlg = _open(ctrl, qtbot)
    _select_ambient(dlg)
    card = dlg.mixer.cards['lluvia']
    saves = []
    real_save = ctrl.data_handler.save_settings
    ctrl.data_handler.save_settings = lambda st: (saves.append(1), real_save(st))
    for k in range(10):                              # la primera y 9 de autorrepetición
        QApplication.sendEvent(card, QKeyEvent(QEvent.KeyPress, Qt.Key_Return,
                                               Qt.NoModifier, "\r", k > 0))
    assert ctrl.ambient.active_ids() == ['lluvia'] and len(saves) == 1
    _close(dlg)


def test_rueda_sobre_un_deslizador_sin_foco_desplaza_el_mezclador(ctrl, qtbot):
    from PySide6.QtCore import QEvent, QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    dlg = _min_mixer(ctrl, qtbot)
    mixer = dlg.mixer
    ctrl.ambient.toggle('lluvia')
    slider = mixer.cards['lluvia'].slider
    bar = mixer.scroll.verticalScrollBar()
    assert bar.maximum() > 0 and slider.isVisible() and not slider.hasFocus()
    before = slider.value()

    def wheel(widget):
        pos = QPointF(widget.rect().center())
        event = QWheelEvent(pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(0, 0),
                            QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(widget, event)
        return event.isAccepted()
    # Sin foco, el deslizador no se queda la rueda: la rechaza y Qt la pasa a
    # los padres hasta el scroll (sendEvent no propaga, así que el último
    # tramo se prueba aparte, sobre el viewport)
    for _ in range(3):
        assert not wheel(slider)
    assert slider.value() == before
    assert wheel(mixer.scroll.viewport()) and bar.value() > 0
    # Con el foco puesto (Tab o clic en él), la rueda sí cambia el volumen
    slider.setFocus(Qt.TabFocusReason)
    assert wheel(slider) and slider.value() != before
    _close(dlg)


def test_la_franja_del_deslizador_no_apaga_el_sonido(ctrl, qtbot):
    from PySide6.QtCore import QPoint
    dlg = _open(ctrl, qtbot)
    _select_ambient(dlg)
    card = dlg.mixer.cards['lluvia']
    assert card.hitButton(QPoint(20, 70))                     # apagada: toda la tarjeta
    ctrl.ambient.toggle('lluvia')
    assert card.active
    below = card.slider.geometry().bottom() + 2
    assert not card.hitButton(QPoint(card.slider.geometry().center().x(), below))
    assert card.hitButton(QPoint(card.width() // 2, 20))      # la parte de arriba, sí
    qtbot.mouseClick(card, Qt.LeftButton, pos=QPoint(card.slider.x() + 4, below))
    assert ctrl.ambient.active_ids() == ['lluvia']
    _close(dlg)


def test_el_foco_con_teclado_lleva_la_tarjeta_a_la_vista(ctrl, qtbot):
    dlg = _min_mixer(ctrl, qtbot)
    mixer = dlg.mixer
    assert mixer.scroll.focusPolicy() == Qt.NoFocus           # sin parada invisible
    bar = mixer.scroll.verticalScrollBar()
    assert bar.value() == 0 and bar.maximum() > 0
    last = mixer.cards['ruido_marron']
    last.setFocus(Qt.BacktabFocusReason)                      # Shift+Tab desde el pie
    viewport = mixer.scroll.viewport()
    top = last.mapTo(viewport, last.rect().topLeft()).y()
    assert bar.value() > 0 and 0 <= top and top + last.height() <= viewport.height()
    _close(dlg)


def test_errores_del_ambiente_con_el_dialogo_abierto(ctrl, clock, qtbot, monkeypatch):
    from ui.widgets import toast
    seen = []
    monkeypatch.setattr(toast, "show_warning", lambda win, msg, **kw: seen.append((win, msg)))
    dlg = _open(ctrl, qtbot)
    ctrl._music_dialog = dlg                          # como open_music: el controlador calla
    _select_ambient(dlg)
    amb = ctrl.ambient
    amb.toggle('lluvia')
    a, _ = _main(amb, 'lluvia')
    a.err = "Archivo dañado"
    _step(amb, clock, 40)
    assert seen == [(dlg, "No se pudo reproducir «Lluvia»: Archivo dañado")]
    ctrl._music_dialog = None
    _close(dlg)
    amb.toggle('oceano')                              # cerrado: ya no escucha
    b, _ = _main(amb, 'oceano')
    b.err = "Otro"
    _step(amb, clock, 40)
    assert [w for w, _ in seen] == [dlg, ctrl._feedback_window()]


def test_pegar_un_enlace_con_el_mezclador_a_la_vista(ctrl, qtbot, monkeypatch):
    from PySide6.QtGui import QGuiApplication
    dlg = _open(ctrl, qtbot)
    _select_ambient(dlg)
    asked = []
    monkeypatch.setattr(ctrl.music, "add_link", lambda text, list_id: asked.append(
        (text, list_id)))
    QGuiApplication.clipboard().setText("https://www.youtube.com/watch?v=abc")
    dlg._paste_link()
    # Pasa a la lofi, donde se ven el campo y «Comprobando…»; decide el servicio
    assert dlg.right_stack.currentWidget() is dlg.panel
    assert asked == [("https://www.youtube.com/watch?v=abc", None)]
    assert dlg.status_row.isVisible() and dlg.status_label.text() == "Comprobando el enlace…"
    dlg._on_link_resolved('', 0, "YouTube no se puede reproducir")
    assert dlg.status_row.isVisible() and "YouTube" in dlg.status_label.text()
    _close(dlg)


def test_un_enlace_que_se_resuelve_no_saca_del_mezclador(ctrl, qtbot, monkeypatch):
    dlg = _open(ctrl, qtbot)
    monkeypatch.setattr(ctrl.music, "add_link", lambda text, list_id: None)
    dlg._add_link("https://radio.example/stream", None)      # pegado en la lofi
    _select_ambient(dlg)                                      # y se va al mezclador
    new_id = ctrl.music.create_list("Radio nueva")
    dlg._on_link_resolved(new_id, 1, '')
    assert dlg.right_stack.currentWidget() is dlg.mixer
    _close(dlg)


def test_ahora_suena_sin_musica_no_dice_nada_sonando(ctrl, qtbot):
    dlg = _open(ctrl, qtbot)
    ctrl.ambient.toggle('lluvia')
    assert ctrl.music.current() is None
    assert dlg.now.title.full_text() == "Sin música"
    _close(dlg)


def _import_like(dh, **ambient):
    """Como «Importar datos»: data_handler.data pasa a ser otro dict."""
    data = json.loads(json.dumps(dh.data))
    data['settings']['ambient'].update(ambient)
    dh.data = data


def test_importar_con_un_sonido_armado_reaplica_el_enfoque(tmp_path, sounds, clock, qtbot):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({'tasks': [], 'settings': {}}), encoding="utf-8")
    dh = DataHandler(path)
    svc = AmbientService(data_handler=dh, clock=clock)
    svc.set_focus_only(True)
    svc.toggle('lluvia')                              # armado: el timer está parado
    a, _ = _main(svc, 'lluvia')
    assert svc.state() == 'waiting' and a.state == 'stopped'
    _import_like(dh, focus_only=False)                # un backup sin la opción
    assert svc.focus_only() is False
    qtbot.wait(20)                                    # el reajuste va fuera del getter
    _step(svc, clock, svc.FADE_IN_MS + 10)
    assert svc.state() == 'playing' and a.state == 'playing'
    assert a.gain == pytest.approx(svc.gain_of('lluvia'))
    # Y al revés: sonando, un backup con la opción lo pausa (timer parado)
    _import_like(dh, focus_only=True)
    assert svc.focus_only() is True
    qtbot.wait(20)
    _step(svc, clock, svc.FADE_OUT_MS + 10)
    assert svc.state() == 'waiting' and a.state == 'paused'
    svc._ticker.stop()


def test_volumenes_infinitos_en_el_json_no_tumban_el_arranque(tmp_path):
    assert catalog.clamp_volume(float('inf'), 40) == 40
    assert catalog.clamp_volume(float('-inf'), 40) == 40
    assert catalog.clamp_volume(10 ** 400, 40) == 100
    path = tmp_path / "data.json"
    path.write_text('{"tasks": [], "settings": {"ambient": {"master": 1e999,'
                    ' "volumes": {"lluvia": -Infinity, "oceano": 55}}}}', encoding="utf-8")
    dh = DataHandler(path)
    assert dh.load_error is None
    mix = dh.load_settings()['ambient']
    assert mix['master'] == catalog.MASTER_DEFAULT
    assert mix['volumes']['lluvia'] == catalog.get('lluvia').volume
    assert mix['volumes']['oceano'] == 55


def test_errores_del_reproductor_en_espanol():
    """El texto de Qt llega en inglés («Could not open file»): al usuario,
    el propio según el tipo de error."""
    from types import SimpleNamespace
    from PySide6.QtMultimedia import QMediaPlayer
    from services import ambient_service
    E = QMediaPlayer.Error
    assert ambient_service.error_text(E.ResourceError) == "No se pudo leer el archivo"
    assert ambient_service.error_text(E.FormatError) == "Formato de audio no compatible"
    assert ambient_service.error_text(E.NetworkError) == ambient_service.DEFAULT_ERROR_TEXT
    voice = SimpleNamespace(_error='')
    ambient_service._QtVoice._on_error(voice, E.ResourceError, "Could not open file")
    assert voice._error == "No se pudo leer el archivo"
    ambient_service._QtVoice._on_error(voice, E.NoError, "")
    assert voice._error == "No se pudo leer el archivo"


def test_salir_sin_despedida_espera_al_fundido_del_ambiente(ctrl, clock, qtbot, monkeypatch):
    """«Reiniciar como administrador» sale sin despedida: el ambiente no se
    corta en seco, se espera a su fundido."""
    from PySide6.QtWidgets import QApplication
    quits = []
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: quits.append(1))
    monkeypatch.setattr(ctrl.focus_mode, "cleanup_system_blocks", lambda: False)
    monkeypatch.setattr(ctrl.model, "is_running", False)
    amb = ctrl.ambient
    amb.toggle('lluvia')
    assert amb.is_playing()
    ctrl.quit_with_farewell(farewell=False)
    assert quits == []
    qtbot.waitUntil(lambda: quits == [1], timeout=amb.FADE_OUT_MS + 1000)


def test_salir_sin_despedida_ni_ambiente_sale_al_momento(ctrl, monkeypatch):
    from PySide6.QtWidgets import QApplication
    quits = []
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: quits.append(1))
    monkeypatch.setattr(ctrl.focus_mode, "cleanup_system_blocks", lambda: False)
    monkeypatch.setattr(ctrl.model, "is_running", False)
    ctrl.quit_with_farewell(farewell=False)
    assert quits == [1]


def test_ningun_test_crea_reproductores_reales():
    from services import ambient_service
    assert ambient_service.VOICE_FACTORY is FakeVoice
    assert math.isclose(catalog.mix_gain(100, 50), 0.25)
