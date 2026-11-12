"""Interfaz de la música: botón de la cabecera, diálogo de Música, mini
reproductor del timer, bandeja y «solo durante el enfoque».

Offscreen y en silencio (QAudioOutput en mute; los MP3 son tramas de
silencio a 320 kbps). La red solo sale a 127.0.0.1 (el servidor de
tests/test_music_service.py) y los datos y la carpeta lofi van a tmp.
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

from PySide6.QtCore import QPoint, QThreadPool, Qt  # noqa: E402

from core.data_handler import DataHandler  # noqa: E402
from services import workers  # noqa: E402
from core.music_library import Entry  # noqa: E402
from tests.test_music_service import WAIT, _Handler  # noqa: E402
from tests.test_ui_smoke import ID1, _make_data  # noqa: E402
from ui.theme import fonts  # noqa: E402


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.stopping = threading.Event()
    srv.lock = threading.Lock()
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
def ctrl(qtbot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    from services import music_service
    # Carpeta lofi propia del test (vacía): no depende de assets/music/lofi
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


def _open(ctrl):
    """El diálogo sin exec(): mostrado y sin bloquear el test."""
    from ui.dialogs.music_dialog import MusicDialog
    dlg = MusicDialog(ctrl.window, ctrl.music)
    dlg.show()
    return dlg


def _close(dlg):
    if dlg._overlay:
        dlg._overlay.hide()
    dlg.done(0)
    dlg.deleteLater()


def _list_with(music, *entries, name="Prueba"):
    lid = music.create_list(name)
    assert music.add_entries(lid, entries) == len(entries)
    return lid


def _titles(view):
    return [view.item(i).data(Qt.UserRole)['title'] for i in range(view.count())]


def _fake_exec(monkeypatch, seen):
    """MusicDialog.exec sin bucle modal: apunta el diálogo y lo cierra."""
    from ui.dialogs.music_dialog import MusicDialog

    def fake(self):
        seen.append(self)
        if self._overlay:
            self._overlay.hide()
        return 0
    monkeypatch.setattr(MusicDialog, "exec", fake)


def test_boton_musica_primero_en_la_cabecera(ctrl, monkeypatch, qtbot):
    tb = ctrl.window.title_bar
    assert tb._buttons[0] is tb.music_btn and tb.music_btn.isEnabled()
    assert tb.rewards_btn.toolTip() == "Recompensas"
    seen = []
    _fake_exec(monkeypatch, seen)
    qtbot.mouseClick(tb.music_btn, Qt.LeftButton)
    assert len(seen) == 1 and seen[0].music is ctrl.music
    assert ctrl._music_dialog is None   # al cerrarse se suelta la instancia


def test_recompensas_ya_sin_pestana_de_musica(ctrl):
    from ui.dialogs.rewards_dialog import RewardsDialog
    dlg = RewardsDialog(ctrl.window, ctrl)
    assert [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())] == [
        "Tienda", "Inventario", "Progreso"]
    if dlg._overlay:
        dlg._overlay.hide()
    dlg.done(0)


def test_dialogo_crear_lista_y_anadir_enlace(ctrl, server, qtbot, monkeypatch):
    from ui.dialogs import music_dialog
    dlg = _open(ctrl)
    # Sin listas propias se ve la carpeta lofi (solo lectura), con la barra del
    # enlace: lo que se pegue ahí irá a una lista que crea el servicio
    assert dlg._list_id == 'lofi' and dlg.lofi_row.isVisibleTo(dlg)
    assert dlg.composer_row.isVisibleTo(dlg) and dlg.items_stack.currentIndex() == 2
    monkeypatch.setattr(music_dialog, "ask_name", lambda *a, **k: "Radios")
    dlg.new_list_btn.click()
    info = dlg._list_info()
    assert info['name'] == "Radios" and not info['readonly']
    assert dlg.composer_row.isVisibleTo(dlg) and dlg.items_stack.currentIndex() == 1
    assert not dlg.composer.add_btn.isVisibleTo(dlg)        # vacío: solo el marcador
    dlg.link_edit.setText(f"{server.base}/live")
    assert dlg.composer.add_btn.isVisibleTo(dlg)
    with qtbot.waitSignal(ctrl.music.link_resolved, timeout=WAIT):
        qtbot.keyClick(dlg.link_edit, Qt.Key_Return)
    assert dlg.items_view.count() == 1 and dlg.items_stack.currentIndex() == 0
    row = dlg.items_view.item(0).data(Qt.UserRole)
    assert row['title'] == "Radio de prueba" and row['live'] is True
    assert "Añadida 1 radio a «Radios»" in dlg.status_label.text()
    assert dlg.link_edit.text() == ''
    # YouTube: el error del resolvedor en la línea de estado, nada añadido
    dlg.link_edit.setText("https://youtu.be/abc")
    with qtbot.waitSignal(ctrl.music.link_resolved, timeout=WAIT):
        dlg.composer.add_btn.click()
    assert dlg._status_kind == 'error' and "YouTube" in dlg.status_label.text()
    assert dlg.items_view.count() == 1 and dlg.link_edit.text() == "https://youtu.be/abc"
    _close(dlg)


def test_dialogo_quitar_reordenar_renombrar_y_eliminar(ctrl, qtbot, monkeypatch):
    from ui.dialogs import music_dialog
    music = ctrl.music
    lid = _list_with(music, *(Entry(f"http://127.0.0.1:9/{n}.mp3", title=n) for n in "ABCD"),
                     name="Mezcla")
    dlg = _open(ctrl)
    assert dlg._list_id == lid            # la primera lista propia
    view = dlg.items_view
    assert _titles(view) == list("ABCD")
    # Soltar la 1.ª fila arrastrada en la mitad baja de la 3.ª: pasa a ser la 3.ª
    # (QDrag.exec es un bucle del sistema: se simula el drop que lo cierra)
    from PySide6.QtCore import QMimeData, QPointF
    from PySide6.QtGui import QDropEvent
    target = view.visualRect(view.model().index(2, 0))
    drop = QDropEvent(QPointF(target.center().x(), target.bottom() - 2),
                      Qt.CopyAction | Qt.MoveAction, QMimeData(), Qt.LeftButton,
                      Qt.NoModifier)
    view._drag_row = 0
    view.dropEvent(drop)
    view._drag_row = -1
    assert drop.dropAction() == Qt.CopyAction          # la vista no borra la fila
    qtbot.waitUntil(lambda: _titles(view) == list("BCAD"), timeout=2000)
    assert [i['title'] for i in music.items(lid)] == list("BCAD")    # persistido
    # Supr quita la seleccionada
    view.setCurrentRow(0)
    qtbot.keyClick(view, Qt.Key_Delete)
    assert _titles(view) == list("CAD") and "Quitado «B»" in dlg.status_label.text()
    # La papelera de la fila (al pasar el ratón) también quita
    rect = dict(view.action_rects(view.visualRect(view.model().index(0, 0))))['remove']
    qtbot.mouseClick(view.viewport(), Qt.LeftButton, pos=rect.center())
    qtbot.waitUntil(lambda: _titles(view) == list("AD"), timeout=2000)
    # Renombrar
    monkeypatch.setattr(music_dialog, "ask_name", lambda *a, **k: "Nuevo nombre")
    dlg._rename_item(view.item_id(1))
    assert _titles(view) == ["A", "Nuevo nombre"]
    # Eliminar la lista pide confirmación; al cancelar no pasa nada
    monkeypatch.setattr(music_dialog.dialogs, "confirm", lambda *a, **k: False)
    dlg._delete_list(lid)
    assert music.items(lid)
    monkeypatch.setattr(music_dialog.dialogs, "confirm", lambda *a, **k: True)
    dlg._delete_list(lid)
    assert [x['id'] for x in music.lists()] == ['lofi'] and dlg._list_id == 'lofi'
    _close(dlg)


def test_soltar_una_carpeta_crea_su_lista(ctrl, tmp_path):
    folder = tmp_path / "Álbum"
    folder.mkdir()
    for name in ("uno.mp3", "dos.mp3"):
        (folder / name).write_bytes(b"\xff\xfb\xe0\x04" + bytes(1040))
    dlg = _open(ctrl)
    assert dlg._list_id == 'lofi'
    assert dlg._add_paths([str(folder)]) == 2      # lo que hace soltarla o «Carpeta…»
    assert dlg._list_info()['name'] == "Álbum" and dlg.items_view.count() == 2
    # El aviso sobrevive al cambio de lista
    assert dlg.status_row.isVisibleTo(dlg)
    assert dlg.status_label.text() == "Añadidas 2 pistas a «Álbum»"
    _close(dlg)


def test_la_fila_aprende_que_es_un_directo(ctrl, server, qtbot):
    """Una radio añadida sin saber si es directo: al sonar, su fila pasa a
    «En directo» con su bitrate (eso se guarda sin library_changed)."""
    music = ctrl.music
    lid = _list_with(music, Entry(f"{server.base}/live", title="Radio"))
    dlg = _open(ctrl)
    view = dlg.items_view
    assert view.item(0).data(Qt.UserRole)['live'] is None
    music.play(lid)
    qtbot.waitUntil(lambda: view.item(0).data(Qt.UserRole)['live'] is True, timeout=WAIT)
    qtbot.waitUntil(lambda: view.item(0).data(Qt.UserRole)['bitrate'] == 320, timeout=WAIT)
    assert "1 radio en directo" in dlg.list_meta.text()
    # En directo, «● En directo» en vez de un contador y una barra vacía
    assert dlg.now.live.text() == "En directo" and dlg.now.progress_stack.currentIndex() == 1
    music.stop()
    _close(dlg)


def test_mini_reproductor_solo_con_algo_cargado(ctrl, server, qtbot, monkeypatch):
    tp = ctrl.timer_panel
    mini = tp.mini_player
    music = ctrl.music
    ctrl.window.resize(1100, 520)   # el alto mínimo: el anillo no debe moverse
    qtbot.wait(100)

    def ring():
        g = tp.center_block.geometry()
        return g.y(), g.height()
    ring_before = ring()
    assert mini.isHidden()
    lid = _list_with(music, Entry(f"{server.base}/song.mp3", live=False, mime="audio/mpeg"))
    music.play(lid)
    assert mini.is_shown() and mini.title.full_text() == "song"
    assert not mini.next_btn.isEnabled()        # un solo elemento: «siguiente» no va a nada
    qtbot.waitUntil(lambda: music.state == 'playing', timeout=WAIT)
    qtbot.waitUntil(lambda: mini.badge.text() == "320 kbps", timeout=WAIT)
    qtbot.wait(100)
    assert ring() == ring_before                # vive en la fila de arriba: sin alto propio
    # Pausa desde el mini: sigue a la vista; parar lo oculta
    mini.play_btn.click()
    assert not music.is_playing and mini.is_shown()
    seen = []
    _fake_exec(monkeypatch, seen)
    qtbot.mouseClick(mini, Qt.LeftButton, pos=QPoint(12, mini.height() // 2))
    assert len(seen) == 1                      # clic en el título: abre Música
    music.stop()
    assert mini.isHidden()


def test_solo_durante_el_enfoque_desde_el_dialogo(ctrl, server, qtbot):
    music = ctrl.music
    lid = _list_with(music, Entry(f"{server.base}/song.mp3", live=False, mime="audio/mpeg"))
    dlg = _open(ctrl)
    dlg.now.focus_toggle.click()
    assert music.focus_only()
    dlg.play_list_btn.click()
    # Timer parado: armada pero sin sonar, y la barra lo explica
    assert music.is_playing and music.state == 'paused' and music.current()['waiting_focus']
    assert dlg.now.subtitle.full_text() == "Sonará al enfocar"
    assert dlg.lists_view.playing_list is None          # no suena: sin ecualizador
    mini = ctrl.timer_panel.mini_player
    assert mini.badge.text() == "Al enfocar" and "no sonará" in mini.play_btn.toolTip()
    ctrl.select_task(ID1)
    ctrl.toggle_timer()                         # arranca el pomodoro
    qtbot.waitUntil(lambda: music.state == 'playing', timeout=WAIT)
    ctrl.toggle_timer()                         # pausa el timer: pausa la música
    assert music.state == 'paused' and music.is_playing
    ctrl.reset_timer()
    assert music.current()['list_id'] == lid
    _close(dlg)


def test_bandeja_reproduce_y_pausa(ctrl, server, qtbot):
    action = ctrl._music_action
    ctrl._sync_tray_music()
    assert not action.isEnabled()               # nada que reproducir todavía
    _list_with(ctrl.music, Entry(f"{server.base}/song.mp3", live=False))
    ctrl._sync_tray_music()
    assert action.isEnabled() and action.text() == "Reproducir la música"
    action.trigger()
    assert ctrl.music.is_playing
    ctrl._sync_tray_music()
    assert action.text() == "Pausar la música"
    action.trigger()
    assert not ctrl.music.is_playing


def test_errores_con_el_dialogo_cerrado_avisan(ctrl, monkeypatch):
    from ui.controllers import app_controller
    shown = []
    monkeypatch.setattr(app_controller.toast, "show_warning",
                        lambda win, msg, duration=0: shown.append(msg))
    ctrl.music.error_occurred.emit("Se perdió la conexión con la emisora")
    assert shown == ["Se perdió la conexión con la emisora"]
    ctrl._music_dialog = object()               # con el diálogo abierto lo cuenta él
    ctrl.music.error_occurred.emit("otro")
    ctrl._music_dialog = None
    assert shown == ["Se perdió la conexión con la emisora"]


def test_dialogo_sigue_al_tema(ctrl):
    """Cada tema repinta el diálogo con sus tokens. Se llama a _restyle a
    mano: emitir theme_changed despertaría a ventanas ya destruidas de otros
    tests (MainWindow se conecta con una lambda al singleton)."""
    from ui.theme.tokens import THEMES, ThemeManager
    manager = ThemeManager.instance()
    original = manager.current
    dlg = _open(ctrl)
    try:
        for theme in ('theme_ocean', 'theme_matrix', 'theme_golden', 'default'):
            manager.current = THEMES[theme]
            dlg._restyle()
            t = manager.current
            assert t.bg in dlg.panel.styleSheet()           # panel hundido del tema
            assert t.surface in dlg.now.styleSheet()        # «Ahora suena»
    finally:
        manager.current = original
        _close(dlg)


def test_geometria_estrecha_de_antes_se_ensancha(qtbot):
    from PySide6.QtGui import QGuiApplication
    from ui.windows.main_window import MIN_WIDTH_COLLAPSED, MainWindow
    area = QGuiApplication.primaryScreen().availableGeometry()
    saved = {'x': area.left() + 40, 'y': area.top() + 30, 'w': 430, 'h': 540}
    win = MainWindow({'window_geometry': saved, 'task_panel_collapsed': True})
    qtbot.addWidget(win)
    assert (win.x(), win.y(), win.width()) == (saved['x'], saved['y'], MIN_WIDTH_COLLAPSED)


# ------------------------------------------------------------------ revisión
def test_en_la_lofi_se_puede_pegar_un_enlace_y_ctrl_v_lo_anade(ctrl, server, qtbot):
    """Primera vez: solo existe la lofi. Pegar (o Ctrl+V fuera del campo) crea
    la lista, como al soltar el enlace."""
    from PySide6.QtGui import QGuiApplication
    dlg = _open(ctrl)
    assert dlg._list_id == 'lofi' and dlg.composer_row.isVisibleTo(dlg)
    QGuiApplication.clipboard().setText(f"{server.base}/live")
    with qtbot.waitSignal(ctrl.music.link_resolved, timeout=WAIT):
        qtbot.keyClick(dlg.items_view, Qt.Key_V, Qt.ControlModifier)   # fuera del campo
    info = dlg._list_info()
    assert info['name'] == "Mis enlaces" and dlg.items_view.count() == 1
    assert "Añadida 1 radio a «Mis enlaces»" in dlg.status_label.text()
    _close(dlg)


def test_enter_dos_veces_no_lo_anade_dos_veces(ctrl, server, qtbot):
    music = ctrl.music
    lid = music.create_list("Radios")
    dlg = _open(ctrl)
    dlg._select_list(lid)
    results = []
    music.link_resolved.connect(lambda *args: results.append(args))
    dlg.link_edit.setText(f"{server.base}/slow.mp3")
    qtbot.keyClick(dlg.link_edit, Qt.Key_Return)
    qtbot.keyClick(dlg.link_edit, Qt.Key_Return)      # mientras «Comprobando…»
    qtbot.waitUntil(lambda: len(results) == 1, timeout=WAIT)
    qtbot.wait(300)
    assert len(results) == 1 and results[0][1:] == (1, '')
    assert dlg._status_kind == 'ok' and dlg.items_view.count() == 1
    _close(dlg)


def test_un_enlace_que_llega_tarde_no_saca_de_la_lista_abierta(ctrl, server, qtbot):
    music = ctrl.music
    first, second = music.create_list("Lista 1"), music.create_list("Lista 2")
    dlg = _open(ctrl)
    dlg._select_list(first)
    dlg.link_edit.setText(f"{server.base}/slow.mp3")
    qtbot.keyClick(dlg.link_edit, Qt.Key_Return)
    dlg._select_list(second)                       # el usuario sigue a lo suyo
    dlg.link_edit.setText("https://otra-radio.example/stream")
    with qtbot.waitSignal(music.link_resolved, timeout=WAIT):
        pass
    assert dlg._list_id == second and len(music.items(first)) == 1
    assert "a «Lista 1»" in dlg.status_label.text()
    assert dlg.link_edit.text() == "https://otra-radio.example/stream"
    # Pegado en la lofi y el usuario se va a otra lista: tampoco se le mueve
    dlg._select_list('lofi')
    dlg.link_edit.setText(f"{server.base}/slow.mp3?otra")
    qtbot.keyClick(dlg.link_edit, Qt.Key_Return)
    dlg._select_list(second)
    with qtbot.waitSignal(music.link_resolved, timeout=WAIT):
        pass
    assert dlg._list_id == second and not dlg._pending
    _close(dlg)


def test_estado_vacio_no_se_aplasta_en_pantallas_bajas(ctrl, qtbot):
    """1366×768 o 1920×1080 al 150 %: el diálogo se encoge al alto útil."""
    from PySide6.QtWidgets import QScrollArea
    lid = ctrl.music.create_list("Vacía")
    dlg = _open(ctrl)
    dlg._select_list(lid)
    assert dlg.items_stack.currentIndex() == 1
    assert isinstance(dlg.items_stack.currentWidget(), QScrollArea)
    dlg.resize(dlg.width(), 600)
    qtbot.wait(50)
    examples = dlg._examples
    assert examples.height() >= examples.minimumSizeHint().height()
    _close(dlg)


def test_listas_con_nombre_largo_tienen_tooltip(ctrl, qtbot):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QApplication, QToolTip
    names = [f"Música para estudiar por la mañana {n}" for n in (1, 2)]
    for name in names:
        ctrl.music.create_list(name)
    ctrl.music.create_list("Radios")
    dlg = _open(ctrl)
    view = dlg.lists_view
    qtbot.wait(50)
    view.viewport().repaint()
    assert {1, 2} <= view.elided_rows and 3 not in view.elided_rows   # «Radios» cabe
    rect = view.visualRect(view.model().index(2, 0))
    QApplication.sendEvent(view.viewport(), QHelpEvent(
        QEvent.ToolTip, rect.center(), view.viewport().mapToGlobal(rect.center())))
    assert QToolTip.text() == names[1]
    QToolTip.hideText()
    _close(dlg)


def test_el_error_se_lee_entero_y_sigue_al_reabrir(ctrl, qtbot, tmp_path):
    music = ctrl.music
    lid = _list_with(music, Entry(str(tmp_path / "Lluvia sobre Lima (versión extendida).mp3")))
    music.play(lid)
    qtbot.waitUntil(lambda: music.state == 'error', timeout=WAIT)
    dlg = _open(ctrl)                               # abierto después del error
    now = dlg.now
    assert now.detail.isVisibleTo(dlg) and not now.subtitle.isVisibleTo(dlg)
    assert now.detail.text() == "No se encuentra el archivo"
    _close(dlg)


def test_tab_sigue_el_orden_visual(ctrl):
    ctrl.music.create_list("Radios")
    dlg = _open(ctrl)
    chain, widget = [], dlg.lists_view
    for _ in range(200):
        chain.append(widget)
        widget = widget.nextInFocusChain()
        if widget is dlg.lists_view:
            break
    order = [dlg.lists_view, dlg.new_list_btn, dlg.shuffle_btn, dlg.play_list_btn,
             dlg.link_edit, dlg.files_btn, dlg.folder_btn, dlg.items_view, dlg.now.prev_btn,
             dlg.now.play_btn, dlg.now.volume, dlg.now.focus_toggle, dlg._close_btn]
    positions = [chain.index(w) for w in order]
    assert positions == sorted(positions)
    _close(dlg)


def test_el_bufer_se_ve_en_ahora_suena_y_en_el_mini(ctrl):
    """Tras un corte el directo vuelve con búfer: mientras precarga lo dice el
    estado («Almacenando en búfer… (5 s)») y, al sonar, «Búfer 5 s»."""
    cur = {'list_id': 'l', 'item_id': 'i', 'title': "Radio", 'subtitle': "En directo",
           'station': '', 'live': True, 'bitrate_kbps': 128, 'codec': 'MP3', 'index': 0,
           'count': 1, 'position_ms': 0, 'duration_ms': 0, 'seekable': False,
           'state': 'buffering', 'waiting_focus': False, 'error': '', 'buffer_s': 5}
    dlg = _open(ctrl)
    now, mini = dlg.now, ctrl.timer_panel.mini_player
    now.refresh(cur, True, '', True)
    mini.show_now_playing(cur, True)
    assert now.subtitle.full_text() == "Almacenando en búfer… (5 s)"
    assert now.buffer_badge.isHidden()
    assert "almacenando en búfer (5 s)" in mini.toolTip()
    playing = dict(cur, state='playing')
    now.refresh(playing, True, '', True)
    mini.show_now_playing(playing, True)
    assert now.live.text() == "En directo" and now.buffer_badge.text() == "Búfer 5 s"
    assert not now.buffer_badge.isHidden()
    assert "En directo · búfer 5 s" in mini.toolTip() and mini.badge.text() == "En directo"
    # Directo sin búfer (la primera conexión): como siempre
    direct = dict(playing, buffer_s=0)
    now.refresh(direct, True, '', True)
    mini.show_now_playing(direct, True)
    assert now.buffer_badge.isHidden() and "búfer" not in mini.toolTip()
    now.refresh(dict(cur, buffer_s=0), True, '', True)
    assert now.subtitle.full_text() == "Almacenando en búfer…"
    _close(dlg)
