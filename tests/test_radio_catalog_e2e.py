"""Radios de serie de punta a punta: sembrar no conecta y, al pulsar
reproducir, suenan como cualquier radio.

Un servidor local (127.0.0.1, puerto efímero) hace de emisora (la radio ICY
de tests/test_music_service.py) y apunta cada petición que recibe: mientras
nadie pulsa reproducir, su registro tiene que seguir vacío. El catálogo se
sustituye por uno que apunta a ese servidor (el real nunca se toca) y el
audio va silenciado.

No se comprueba el título de la canción: cómo llega (el proxy de radios, en
la misma conexión) no es cosa de la siembra.
"""
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QThreadPool  # noqa: E402

from core import radio_catalog as rc  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from services import workers  # noqa: E402
from tests.test_music_service import WAIT, _Handler  # noqa: E402
from tests.test_ui_smoke import _make_data  # noqa: E402
from ui.theme import fonts  # noqa: E402


class _Registro(_Handler):
    """La emisora de test_music_service, que además apunta quién llama."""

    def do_GET(self):  # noqa: N802 - nombre de la API de http.server
        with self.server.lock:
            self.server.requests.append(
                (self.path, (self.headers.get("User-Agent") or '').split('/')[0]))
        if self.path.split('?', 1)[0] == '/llena':
            # Un Icecast con el cupo de oyentes lleno: 403 con una página
            self._send(b"<html><body>Too many listeners</body></html>", "text/html", 403)
            return
        super().do_GET()


@pytest.fixture
def emisora():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Registro)
    srv.daemon_threads = True
    srv.stopping = threading.Event()
    srv.lock = threading.Lock()
    srv.requests = []
    srv.drop_count = 0
    srv.ranges = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.stopping.set()
    srv.shutdown()
    srv.server_close()
    thread.join(5)


@pytest.fixture
def sembrar_locales(emisora, monkeypatch):
    """La siembra encendida con dos radios de serie en el servidor local."""
    catalog = (
        rc.DefaultRadio('local-uno', "Lofi de serie · Uno", f"{emisora.base}/live?r=1",
                        'audio/mpeg', 128),
        rc.DefaultRadio('local-dos', "Lofi de serie · Dos", f"{emisora.base}/live?r=2",
                        'audio/mpeg', 128),
    )
    monkeypatch.setattr(DataHandler, "SEED_DEFAULT_RADIOS", True)
    monkeypatch.setattr(rc, "DEFAULT_RADIOS", catalog)
    return catalog


def _requests(emisora):
    with emisora.lock:
        return list(emisora.requests)


def test_sembrar_no_conecta_y_reproducir_toca_la_primera(qtbot, tmp_path, emisora,
                                                         sembrar_locales):
    from services.music_service import MusicService
    first = sembrar_locales[0]
    lofi = tmp_path / "lofi"          # Carpeta lofi vacía: play() va a las de serie
    lofi.mkdir()
    dh = DataHandler(tmp_path / "data.json")          # usuario nuevo
    svc = MusicService(None, data_handler=dh, lofi_dir=lofi,
                       reconnect_delays=(200, 400), buffer_steps=(1, 2))
    svc.set_muted(True)
    try:
        assert [x['name'] for x in svc.lists()] == ["Carpeta lofi", rc.SEED_LIST_NAME]
        items = svc.items(rc.SEED_LIST_ID)
        assert [i['title'] for i in items] == [r.title for r in sembrar_locales]
        assert all(i['live'] is True for i in items)
        assert svc.current() is None and not svc.is_playing    # nada suena solo
        qtbot.wait(300)
        assert _requests(emisora) == []                         # sembrar es solo datos
        svc.play()                                              # sin nada elegido
        cur = svc.current()
        assert cur['list_id'] == rc.SEED_LIST_ID and cur['index'] == 0
        assert cur['title'] == first.title
        assert cur['live'] is True                              # desde el principio
        assert cur['codec'] == 'MP3'
        qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
        assert svc.current()['live'] is True
        # Solo ahora se conecta, y solo a la primera radio
        assert _requests(emisora) and {p for p, _ua in _requests(emisora)} == {"/live?r=1"}
    finally:
        svc.shutdown()
        QThreadPool.globalInstance().waitForDone(15000)
        workers.wait_threads(15)
        qtbot.wait(50)
        svc.deleteLater()


def _servicio_con(tmp_path, monkeypatch, catalog):
    from services.music_service import MusicService
    monkeypatch.setattr(DataHandler, "SEED_DEFAULT_RADIOS", True)
    monkeypatch.setattr(rc, "DEFAULT_RADIOS", catalog)
    lofi = tmp_path / "lofi"
    lofi.mkdir()
    dh = DataHandler(tmp_path / "data.json")
    svc = MusicService(None, data_handler=dh, lofi_dir=lofi,
                       reconnect_delays=(100, 100), buffer_steps=(1, 2))
    svc.set_muted(True)
    return svc


def _cerrar(svc, qtbot):
    svc.shutdown()
    QThreadPool.globalInstance().waitForDone(15000)
    workers.wait_threads(15)
    qtbot.wait(50)
    svc.deleteLater()


def test_una_radio_de_serie_caida_pasa_a_la_siguiente(qtbot, tmp_path, emisora,
                                                     monkeypatch):
    """live=True desde la siembra: si la primera no responde (URL caducada,
    404), no se reintenta un minuto esa misma: se avisa y suena la siguiente.
    Un directo que ya sonó y se corta sigue reconectando (test_music_service)."""
    catalog = (rc.DefaultRadio('caida', "Caída", f"{emisora.base}/caducada",
                               'audio/mpeg', 128),
               rc.DefaultRadio('viva', "Viva", f"{emisora.base}/live?r=viva",
                               'audio/mpeg', 128))
    svc = _servicio_con(tmp_path, monkeypatch, catalog)
    errors, states = [], []
    svc.error_occurred.connect(errors.append)
    svc.state_changed.connect(states.append)
    try:
        svc.play()
        assert svc.current()['title'] == "Caída" and svc.current()['live'] is True
        qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
        assert svc.current()['title'] == "Viva"
        assert errors == ["No se pudo reproducir «Caída»: paso a la siguiente"]
        assert 'reconnecting' not in states
        assert [p for p, _ua in _requests(emisora)].count("/caducada") == 1
        # La caída sigue guardada como directo: no se rebaja a pista
        assert svc.items(rc.SEED_LIST_ID)[0]['live'] is True
    finally:
        _cerrar(svc, qtbot)


def test_una_radio_de_serie_llena_pasa_a_la_siguiente(qtbot, tmp_path, emisora,
                                                     monkeypatch):
    """Un 403 de la primera (un Icecast con el cupo de oyentes lleno) es un
    rechazo, pero su URL no lleva cuenta: como una caída, una sola petición
    a ella y suena la siguiente. Antes se apagaba toda la música con el
    consejo de las radios de pago."""
    catalog = (rc.DefaultRadio('llena', "Llena", f"{emisora.base}/llena",
                               'audio/mpeg', 128),
               rc.DefaultRadio('viva', "Viva", f"{emisora.base}/live?r=viva",
                               'audio/mpeg', 128))
    svc = _servicio_con(tmp_path, monkeypatch, catalog)
    errors, states = [], []
    svc.error_occurred.connect(errors.append)
    svc.state_changed.connect(states.append)
    try:
        svc.play()
        assert svc.current()['title'] == "Llena"
        qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
        assert svc.current()['title'] == "Viva" and svc.is_playing
        assert errors == ["«Llena» rechazó la conexión (HTTP 403): paso a la siguiente"]
        assert 'reconnecting' not in states
        assert [p for p, _ua in _requests(emisora)].count("/llena") == 1
    finally:
        _cerrar(svc, qtbot)


def test_con_aleatorio_dos_caidas_no_tapan_a_la_que_funciona(qtbot, tmp_path, emisora,
                                                           monkeypatch):
    """Aleatorio y repetir «todo» (lo de serie) con 2 de las 3 radios caídas.
    El usuario elige a mano la última del orden barajado; falla y, al
    rebarajar, sale [b, a, c] (una de las salidas posibles): antes volvía a
    pedir «a», ya caída, y se rendía con «ninguna pista» sin probar «c»."""
    from services import music_service as ms
    catalog = (rc.DefaultRadio('r0', "Radio 0", f"{emisora.base}/caducada?r=0",
                               'audio/mpeg', 128),
               rc.DefaultRadio('r1', "Radio 1", f"{emisora.base}/caducada?r=1",
                               'audio/mpeg', 128),
               rc.DefaultRadio('r2', "Radio 2", f"{emisora.base}/live?r=2",
                               'audio/mpeg', 128))
    svc = _servicio_con(tmp_path, monkeypatch, catalog)
    svc.ERROR_SKIP_MS = 200
    ids = {it['title']: it['id'] for it in svc.items(rc.SEED_LIST_ID)}
    a, b, c = ids["Radio 0"], ids["Radio 1"], ids["Radio 2"]
    orders = [[c, b, a], [b, a, c]]       # al empezar; al rebarajar tras fallar «a»

    def fake_shuffle(seq):
        seq[:] = orders.pop(0)
    monkeypatch.setattr(ms.random, "shuffle", fake_shuffle)
    errors = []
    svc.error_occurred.connect(errors.append)
    try:
        assert svc.repeat() == 'all'
        svc.set_shuffle(True)
        svc.play(rc.SEED_LIST_ID)
        qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
        assert svc.current()['title'] == "Radio 2"
        svc.play(rc.SEED_LIST_ID, a)                 # elige a mano «Radio 0»
        qtbot.waitUntil(lambda: svc.state == 'playing' or not svc.is_playing,
                        timeout=WAIT)
        assert svc.state == 'playing' and svc.current()['title'] == "Radio 2"
        assert errors == ["No se pudo reproducir «Radio 0»: paso a la siguiente",
                          "No se pudo reproducir «Radio 1»: paso a la siguiente"]
        paths = [p for p, _ua in _requests(emisora)]
        assert paths.count("/caducada?r=0") == 1 and paths.count("/caducada?r=1") == 1
        assert orders == []
    finally:
        _cerrar(svc, qtbot)


def test_una_radio_sola_en_su_lista_se_reintenta(qtbot, tmp_path, emisora,
                                                monkeypatch):
    """Sin otra a la que pasar, reintentar es lo único útil: la escalera de
    reconexión de siempre y, al acabarla, «Se perdió la conexión…»."""
    catalog = (rc.DefaultRadio('caida', "Caída", f"{emisora.base}/caducada",
                               'audio/mpeg', 128),)
    svc = _servicio_con(tmp_path, monkeypatch, catalog)
    errors, states = [], []
    svc.error_occurred.connect(errors.append)
    svc.state_changed.connect(states.append)
    try:
        svc.play()
        qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT)
        assert 'reconnecting' in states
        assert errors == ["Se perdió la conexión con la emisora y no se pudo recuperar"]
        assert not svc.is_playing
        assert [p for p, _ua in _requests(emisora)].count("/caducada") == 3   # 1 + 2 reintentos
    finally:
        _cerrar(svc, qtbot)


@pytest.fixture
def ctrl_sembrado(qtbot, tmp_path, monkeypatch, sembrar_locales):
    """La app entera con un usuario que ya existía (tareas, sin música) y la
    siembra encendida: así arranca quien actualiza."""
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    from services import music_service
    monkeypatch.setattr(music_service, "MUSIC_DIR", tmp_path / "lofi")
    dh = DataHandler(_make_data(tmp_path))
    dh.send_asana_ops = lambda ops, *a, **k: [
        {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
         'detail': '', 'offline': False} for op in ops]
    from ui.controllers.app_controller import AppController
    from ui.windows.main_window import MainWindow
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    controller = AppController(win, dh)
    controller.music.set_muted(True)
    win.show()
    qtbot.waitExposed(win)
    yield controller
    controller.music.stop()
    controller.music.set_muted(True)
    QThreadPool.globalInstance().waitForDone(15000)
    workers.wait_threads(15)
    qtbot.wait(50)


def test_el_dialogo_abre_en_las_radios_de_serie(ctrl_sembrado, emisora, qtbot):
    from ui.dialogs.music_dialog import MusicDialog, item_icon, item_subtitle
    ctrl = ctrl_sembrado
    dlg = MusicDialog(ctrl.window, ctrl.music)
    dlg.show()
    try:
        assert dlg._list_id == rc.SEED_LIST_ID
        assert dlg.list_meta.text() == "2 elementos · 2 radios en directo"
        assert dlg.more_btn.isVisibleTo(dlg)        # lista normal: no es de solo lectura
        row = dlg.items_view.item(0).data(Qt.UserRole)
        assert row['title'] == "Lofi de serie · Uno"
        assert item_icon(row) == "radio"
        assert item_subtitle(row) == "En directo · 127.0.0.1 · MP3 · 128 kbps"
        ctrl._sync_tray_music()
        assert ctrl._music_action.isEnabled()       # la bandeja ya tiene qué tocar
        qtbot.wait(300)
        assert _requests(emisora) == []             # abrir Música tampoco conecta
        # «Reproducir» de la cabecera de la lista: suena la primera
        dlg.play_list_btn.click()
        cur = ctrl.music.current()
        assert cur['list_id'] == rc.SEED_LIST_ID and cur['title'] == "Lofi de serie · Uno"
        qtbot.waitUntil(lambda: ctrl.music.state == 'playing', timeout=WAIT)
        assert {p for p, _ua in _requests(emisora)} == {"/live?r=1"}
        ctrl.music.stop()
    finally:
        if dlg._overlay:
            dlg._overlay.hide()
        dlg.done(0)
        dlg.deleteLater()


def test_lo_que_pega_quien_empieza_va_a_la_lista_a_la_vista(ctrl_sembrado, emisora,
                                                            qtbot, monkeypatch):
    """El primer arranque de verdad: «Radios lofi» es la única lista propia y
    el diálogo abre en ella. Lo que se pega (o se suelta) va a la lista a la
    vista, como en cualquier lista propia, y la interfaz lo dice antes y
    después. Sin listas propias (con la siembra apagada, o tras borrar
    «Radios lofi») va a «Mis enlaces»: ver tests/test_music_ui.py::
    test_en_la_lofi_se_puede_pegar_un_enlace_y_ctrl_v_lo_anade."""
    from ui.dialogs import music_dialog
    from ui.dialogs.music_dialog import MusicDialog
    ctrl = ctrl_sembrado
    dlg = MusicDialog(ctrl.window, ctrl.music)
    dlg.show()
    try:
        assert dlg._list_id == rc.SEED_LIST_ID
        assert dlg._drop_target_text() == f"Suelta para añadir a «{rc.SEED_LIST_NAME}»"
        dlg.link_edit.setText(f"{emisora.base}/live?mia=1")
        with qtbot.waitSignal(ctrl.music.link_resolved, timeout=WAIT) as blk:
            qtbot.keyClick(dlg.link_edit, Qt.Key_Return)
        assert blk.args == [rc.SEED_LIST_ID, 1, '']
        assert dlg._list_id == rc.SEED_LIST_ID and dlg.items_view.count() == 3
        assert f"a «{rc.SEED_LIST_NAME}»" in dlg.status_label.text()
        # Borrarla para quitarse las de serie avisa de todo lo que se lleva
        asked = []
        monkeypatch.setattr(music_dialog.dialogs, "confirm",
                            lambda _parent, _title, message, **_k: asked.append(message))
        dlg._delete_list(rc.SEED_LIST_ID)
        assert asked == [f"Se quitará «{rc.SEED_LIST_NAME}» y sus 3 elementos. "
                         "Los archivos de tu equipo no se borran."]
        assert ctrl.music.items(rc.SEED_LIST_ID)        # canceló: sigue ahí
    finally:
        if dlg._overlay:
            dlg._overlay.hide()
        dlg.done(0)
        dlg.deleteLater()
