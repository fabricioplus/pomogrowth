"""Ventanas tras «dejó de mostrarse encima y ni minimizando ni con la
compacta funcionaba» (noviembre de 2026, con la radio online caída).

No se reprodujo una pérdida directa del «siempre visible», pero sí fallos que
impedían recuperarlo o dejaban la app bloqueada:
- la marca «Siempre visible» de la bandeja no seguía a Ajustes y su clic
  hacía lo contrario de lo que mostraba;
- cambiarla con la ventana minimizada la restauraba de golpe;
- «Mostrar PomoGrowth» con la compacta abierta dejaba las dos ventanas;
- pedir la compacta a mitad del desvanecido de minimizar dejaba la principal
  minimizada y visible;
- nada reponía el «encima de todo» nativo (WS_EX_TOPMOST) si Windows u otra
  app lo quitaba;
- una segunda instancia solo avisaba, sin traer al frente la abierta;
- el aviso de la música tapaba los controles de la compacta y se apilaba;
- modales colgados de la principal oculta o minimizada bloqueaban la
  compacta sin verse.
Y el diagnóstico para la próxima vez: faulthandler, el vigía del hilo de la
interfaz y el log de la música.

De la revisión de esos arreglos: reponer el «encima de todo» no puede dejar
la ventana encima de su propio modal; mostrar la principal con un modal
sobre la compacta trae el modal; relanzar durante la despedida arranca otra;
la administradora a mano ve el aviso; el vigía no avisa de bloqueos que no
hubo; la bienvenida espera al error de Asana del arranque; y la racha de
«paso a la siguiente» se cierra al pausar o elegir otra.

Offscreen no tiene HWND: el «encima de todo» nativo se simula (fixture
`native`), salvo los tests con ventanas Win32 reales, ocultas y sin activar.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_ventanas.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.data_handler import DataHandler  # noqa: E402
from ui.theme import fonts  # noqa: E402

ID1 = 1764282734316


def _make_data(tmp_path: Path) -> Path:
    data = {'tasks': [{'id': ID1, 'name': 'Informe', 'completed': False, 'tags': [],
                       'source': 'local', 'asana_id': None, 'due': None}],
            'deleted_tasks': [], 'time_chunks': [], 'history': [], 'ui_snapshot': {},
            'settings': {'task_sort_mode': 'manual', 'always_on_top': True}}
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    dh = DataHandler(_make_data(tmp_path))
    dh.send_asana_ops = lambda ops, *a, **k: [
        {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
         'detail': '', 'offline': False} for op in ops]
    from ui.controllers.app_controller import AppController
    from ui.windows.main_window import MainWindow
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    controller = AppController(win, dh)
    # El tick (cada 250 ms) también repone el «encima de todo»: parado, los
    # tests lo llaman cuando toca
    controller._tick_timer.stop()
    win.show()
    qtbot.waitExposed(win)
    yield controller
    if controller.compact_window is not None:
        controller.compact_window.hide()


class _Native:
    """«Encima de todo» nativo simulado: estado por HWND (por defecto, lo
    tiene) y las llamadas a set_topmost."""

    def __init__(self):
        self.top = {}
        self.calls = []

    def is_topmost(self, hwnd):
        return self.top.get(int(hwnd), True)

    def set_topmost(self, hwnd, on):
        self.calls.append((int(hwnd), bool(on)))
        self.top[int(hwnd)] = bool(on)
        return True


@pytest.fixture
def native(monkeypatch):
    from utils import win_native
    fake = _Native()
    monkeypatch.setattr(win_native, "is_topmost", fake.is_topmost)
    monkeypatch.setattr(win_native, "set_topmost", fake.set_topmost)
    return fake


def _top(w) -> bool:
    return bool(w.windowFlags() & Qt.WindowStaysOnTopHint)


def _hwnd(w) -> int:
    return int(w.winId())


def _tray_action(ctrl, text):
    return next(a for a in ctrl.tray.contextMenu().actions() if a.text() == text)


def _toasts(monkeypatch, name="show_success"):
    from ui.widgets import toast
    seen = []
    real = getattr(toast, name)

    def spy(win, msg, duration=3000):
        seen.append((win, msg))
        return real(win, msg, duration)
    monkeypatch.setattr(toast, name, spy)
    return seen


# ---------- 1. «Siempre visible» de la bandeja sincronizado con Ajustes ----------

def test_la_bandeja_sigue_a_ajustes_y_hace_lo_que_muestra(ctrl):
    action = _tray_action(ctrl, "Siempre visible")
    fired = []
    action.toggled.connect(lambda on: fired.append(('toggled', on)))
    action.triggered.connect(lambda on: fired.append(('triggered', on)))
    assert action.isChecked() and _top(ctrl.window)

    s = ctrl.data_handler.load_settings()
    s['always_on_top'] = False
    ctrl.data_handler.save_settings(s)
    ctrl.apply_settings(s)                         # «Guardar» en Ajustes
    assert not action.isChecked() and not _top(ctrl.window)
    assert fired == []                             # sin disparar su señal

    action.trigger()                               # el usuario lo quiere encendido
    assert action.isChecked() and _top(ctrl.window) and ctrl.window.isVisible()
    assert ctrl.data_handler.load_settings()['always_on_top'] is True
    action.trigger()
    assert not action.isChecked() and not _top(ctrl.window)
    assert ctrl.data_handler.load_settings()['always_on_top'] is False


def test_desde_la_bandeja_no_sale_configuracion_guardada(ctrl, monkeypatch):
    seen = _toasts(monkeypatch)
    _tray_action(ctrl, "Siempre visible").trigger()
    assert not _top(ctrl.window)
    assert not [m for _w, m in seen if m == "Configuración guardada"]
    ctrl.apply_settings(ctrl.data_handler.load_settings())   # desde Ajustes, sí
    assert [m for _w, m in seen if m == "Configuración guardada"]


# ---------- 2. Cambiarlo con la ventana minimizada ----------

def test_cambiarlo_minimizada_la_deja_minimizada(ctrl, qtbot, monkeypatch):
    """Offscreen conserva el estado minimizado aunque se llame a show(), pero
    Windows no: show() tras setWindowFlag la restauraba de golpe (comprobado
    con la plataforma real). Por eso se mira también qué se llamó."""
    win = ctrl.window
    win.showMinimized()
    qtbot.waitUntil(win.isMinimized, timeout=2000)
    called = []
    for name in ("show", "showMinimized", "showNormal"):
        real = getattr(win, name)
        monkeypatch.setattr(win, name, lambda real=real, name=name: (called.append(name),
                                                                    real()))
    action = _tray_action(ctrl, "Siempre visible")
    action.trigger()
    qtbot.wait(50)
    assert not _top(win) and win.isMinimized()
    action.trigger()
    qtbot.wait(50)
    assert _top(win) and win.isMinimized()
    assert called == ["showMinimized", "showMinimized"]


# ---------- 3. «Mostrar PomoGrowth» pasa por show_normal ----------

@pytest.mark.parametrize("how", ["accion", "doble-clic", "metodo"])
def test_mostrar_desde_la_bandeja_con_la_compacta(ctrl, qtbot, how):
    from PySide6.QtWidgets import QSystemTrayIcon
    ctrl.show_compact()
    assert ctrl.compact_window.isVisible() and not ctrl.window.isVisible()
    if how == "accion":
        _tray_action(ctrl, "Mostrar PomoGrowth").trigger()
    elif how == "doble-clic":
        ctrl.tray.activated.emit(QSystemTrayIcon.DoubleClick)
    else:
        ctrl.show_from_tray()
    assert ctrl.window.isVisible() and not ctrl.compact_window.isVisible()
    qtbot.waitUntil(lambda: ctrl.window.windowOpacity() > 0.99, timeout=2000)


# ---------- 4. La compacta a mitad del desvanecido de minimizar ----------

def test_compacta_durante_el_desvanecido_de_minimizar(ctrl, qtbot):
    win = ctrl.window
    win.fade_minimize()
    qtbot.wait(40)
    assert win.is_fading()
    ctrl.show_compact()
    qtbot.wait(500)                                # más que el desvanecido (170 ms)
    assert not win.isVisible() and not win.isMinimized()
    assert win.windowOpacity() == pytest.approx(win._base_opacity, abs=0.01)
    assert ctrl.compact_window.isVisible()
    ctrl.show_normal()                             # y se vuelve sin problemas
    qtbot.waitUntil(lambda: win.isVisible() and win.windowOpacity() > 0.99, timeout=2000)
    assert not win.isMinimized() and not ctrl.compact_window.isVisible()


def test_la_compacta_suelta_la_tarea_que_suelta_un_sync(ctrl, monkeypatch):
    """Timer parado y la compacta a la vista: un sync en segundo plano (el
    reintento, el diferido tras un envío, el del arranque) trae la tarea
    activa completada en Asana y se suelta. La compacta lo dice al momento:
    seguía con su nombre (parado, el tick no la refresca) y su ▶ contestaba
    «Sin tarea activa»."""
    from ui.dialogs import branded_dialogs as dialogs
    asked = []
    monkeypatch.setattr(dialogs, "info", lambda _parent, title, *a, **k: asked.append(title))
    ctrl.select_task(ID1)
    ctrl.show_compact()
    cw = ctrl.compact_window
    assert cw._task_name == 'Informe'
    for task in ctrl.data_handler.data['tasks']:
        task['completed'] = True                   # cerrada en Asana
    ctrl.asana.finished.emit(1)
    assert ctrl.model.active_task_id is None
    assert cw._task_name is None
    assert cw.task_label._full == "Ninguna tarea seleccionada"
    assert asked == []
    ctrl.show_normal()


def test_minimizar_sigue_minimizando(ctrl, qtbot):
    """El guardia del final del desvanecido no estorba al caso normal."""
    win = ctrl.window
    win.fade_minimize()
    qtbot.waitUntil(win.isMinimized, timeout=2000)
    qtbot.wait(200)
    assert win.windowOpacity() == pytest.approx(win._base_opacity, abs=0.01)


# ---------- 5. «Encima de todo» nativo ----------

def test_el_tick_repone_el_encima_de_todo_y_lo_apunta(ctrl, native, caplog):
    win = ctrl.window
    native.top[_hwnd(win)] = False                 # Windows u otra app se lo quitó
    with caplog.at_level(logging.INFO):
        ctrl._keep_on_top()
    assert native.calls == [(_hwnd(win), True)]
    assert [r for r in caplog.records if "principal" in r.getMessage()
            and "repuesto" in r.getMessage()]
    caplog.clear()
    with caplog.at_level(logging.INFO):
        ctrl._keep_on_top()                        # ya lo tiene: ni se toca
    assert native.calls == [(_hwnd(win), True)] and not caplog.records


def test_el_tick_lo_mira_cada_dos_segundos(ctrl, monkeypatch):
    calls = []
    monkeypatch.setattr(ctrl, "_keep_on_top", lambda: calls.append(1))
    for _ in range(ctrl.TOPMOST_CHECK_TICKS * 3):
        ctrl._tick()
    assert len(calls) == 3
    assert ctrl.TOPMOST_CHECK_TICKS * 250 == pytest.approx(2000, abs=250)


def test_no_toca_la_principal_si_no_toca(ctrl, native, qtbot):
    win = ctrl.window
    native.top[_hwnd(win)] = False
    # Sin «Siempre visible»
    s = ctrl.data_handler.load_settings()
    s['always_on_top'] = False
    ctrl.apply_settings(s)
    native.calls.clear()
    native.top[_hwnd(win)] = False
    ctrl._keep_on_top()
    assert native.calls == []
    s['always_on_top'] = True
    ctrl.apply_settings(s)
    native.calls.clear()
    native.top[_hwnd(win)] = False
    # Desvaneciéndose
    win.fade_minimize()
    ctrl._keep_on_top()
    assert native.calls == []
    # Minimizada
    qtbot.waitUntil(win.isMinimized, timeout=2000)
    ctrl._keep_on_top()
    assert native.calls == []
    # Oculta (en la bandeja)
    win.showNormal()
    qtbot.wait(300)
    native.calls.clear()
    native.top[_hwnd(win)] = False
    win.hide()
    ctrl._keep_on_top()
    assert native.calls == []


def test_la_compacta_tambien(ctrl, native, caplog):
    ctrl.show_compact()
    cw = ctrl.compact_window
    native.calls.clear()
    native.top[_hwnd(cw)] = False
    with caplog.at_level(logging.INFO):
        ctrl._keep_on_top()
    assert (_hwnd(cw), True) in native.calls
    assert [r for r in caplog.records if "compacta" in r.getMessage()]
    native.calls.clear()
    cw.hide()                                      # oculta: no se mira
    native.top[_hwnd(cw)] = False
    ctrl._keep_on_top()
    assert (_hwnd(cw), True) not in native.calls


def test_al_mostrar_y_restaurar_se_repone(ctrl, native, qtbot):
    win = ctrl.window
    # Mostrar la compacta (su showEvent)
    ctrl.show_compact()
    assert (_hwnd(ctrl.compact_window), True) in native.calls
    # Volver a la principal (fade_show), aunque ya lo tuviera
    native.calls.clear()
    ctrl.show_normal()
    assert (_hwnd(win), True) in native.calls
    # Salir de minimizada (barra de tareas, Alt+Tab)
    qtbot.waitUntil(lambda: win.windowOpacity() > 0.99, timeout=2000)
    win.showMinimized()
    qtbot.waitUntil(win.isMinimized, timeout=2000)
    native.calls.clear()
    win.showNormal()
    qtbot.waitUntil(lambda: not win.isMinimized(), timeout=2000)
    assert (_hwnd(win), True) in native.calls


def test_guardar_en_ajustes_lo_repone_aunque_el_flag_coincida(ctrl, native):
    win = ctrl.window
    native.top[_hwnd(win)] = False                 # Qt cree que sí; Windows dice que no
    ctrl.apply_settings(ctrl.data_handler.load_settings())
    assert native.calls == [(_hwnd(win), True)] and native.top[_hwnd(win)]
    # Sin «Siempre visible» y la ventana aún encima de todo: se le quita
    s = ctrl.data_handler.load_settings()
    s['always_on_top'] = False
    ctrl.apply_settings(s)                         # cambia el flag de Qt
    native.calls.clear()
    native.top[_hwnd(win)] = True
    ctrl.apply_settings(s)                         # el flag ya coincide
    assert native.calls == [(_hwnd(win), False)]


def test_win_native_inofensivo_sin_ventanas_nativas(ctrl):
    """Con Qt offscreen, winId() no es un HWND: ni se lee ni se cambia."""
    from utils import win_native
    hwnd = _hwnd(ctrl.window)
    assert win_native.is_topmost(hwnd) is None
    assert win_native.set_topmost(hwnd, True) is False
    assert win_native.keep_topmost(hwnd, "principal", force=True) is False
    assert win_native.is_topmost(0) is None


def _user32():
    """user32 con los prototipos de los tests Win32 de verdad (ventanas
    STATIC ocultas: nunca se muestran ni se activan)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
        wintypes.HINSTANCE, wintypes.LPVOID)
    user32.DestroyWindow.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    return user32


def _window_rect(user32, hwnd):
    import ctypes
    from ctypes import wintypes
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


@pytest.mark.skipif(sys.platform != "win32", reason="WS_EX_TOPMOST es de Windows")
def test_win_native_con_una_ventana_win32_de_verdad(monkeypatch, caplog):
    """Una ventana Win32 real, oculta y sin activar (nada se ve), fuera del
    (0, 0): poner y quitar el «encima de todo» sin moverla ni
    redimensionarla, y reponerlo con su línea en el log (y sin ella si no lo
    había perdido, aunque se fuerce)."""
    from utils import win_native
    monkeypatch.setattr(win_native, "_native_windows", lambda: True)
    user32 = _user32()
    hwnd = user32.CreateWindowExW(0, "STATIC", "PomoGrowth prueba", 0, 50, 60, 10, 10,
                                  None, None, None, None)
    assert hwnd
    try:
        rect = _window_rect(user32, hwnd)
        assert rect[:2] == (50, 60)
        assert win_native.is_topmost(hwnd) is False
        assert win_native.set_topmost(hwnd, True) and win_native.is_topmost(hwnd) is True
        assert _window_rect(user32, hwnd) == rect    # ni movida ni redimensionada
        assert win_native.set_topmost(hwnd, False) and win_native.is_topmost(hwnd) is False
        with caplog.at_level(logging.INFO):
            assert win_native.keep_topmost(hwnd, "prueba") is True
            assert win_native.keep_topmost(hwnd, "prueba") is False   # ya lo tiene
            # Forzado (fade_show, la compacta al mostrarse, «Guardar») sobre una
            # que no lo perdió: ni una línea, o el log no distinguiría una pérdida
            assert win_native.keep_topmost(hwnd, "prueba", force=True) is False
        assert win_native.is_topmost(hwnd) is True
        assert len([r for r in caplog.records if "prueba" in r.getMessage()]) == 1
        assert _window_rect(user32, hwnd) == rect
        assert not user32.IsWindowVisible(hwnd)    # nunca se mostró
    finally:
        user32.DestroyWindow(hwnd)
    assert win_native.is_topmost(hwnd) is None     # ya no es una ventana


def test_las_banderas_de_set_topmost():
    """Solo la banda: sin mover, sin redimensionar y SIN ACTIVAR (el tick lo
    repone cada 2 s: activarla le robaría el foco a la app del usuario cada
    vez). Y sin SWP_NOOWNERZORDER: ver el test siguiente."""
    from utils import win_native
    assert win_native._TOPMOST_SWP == (win_native.SWP_NOMOVE | win_native.SWP_NOSIZE
                                       | win_native.SWP_NOACTIVATE)


@pytest.mark.skipif(sys.platform != "win32", reason="El orden z es de Windows")
def test_reponer_el_encima_de_todo_deja_sus_modales_encima(monkeypatch):
    """Una ventana dueña y otra que posee (como la principal y su modal),
    Win32 reales y ocultas. Reponer el «encima de todo» de la dueña (fade_show,
    salir de minimizada, el tick) no puede dejarla encima de su modal: con
    SWP_NOOWNERZORDER pasaba, y la ventana quedaba bloqueada por un modal
    escondido detrás de ella."""
    from utils import win_native
    monkeypatch.setattr(win_native, "_native_windows", lambda: True)
    user32 = _user32()
    GW_HWNDPREV = 3
    WS_POPUP, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000000, 0x8, 0x08000000

    def make(owner=None):
        h = user32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_NOACTIVATE, "STATIC",
                                   "PomoGrowth prueba", WS_POPUP, -30000, -30000, 1, 1,
                                   owner, None, None, None)
        assert h
        return h

    def above(a, b) -> bool:
        h = user32.GetWindow(b, GW_HWNDPREV)
        for _ in range(100000):
            if not h:
                return False
            if h == a:
                return True
            h = user32.GetWindow(h, GW_HWNDPREV)
        return False

    owner = make()
    owned = make(owner)
    try:
        assert above(owned, owner)
        assert win_native.keep_topmost(owner, "prueba", force=True) is False
        assert above(owned, owner)                 # forzado: el modal sigue encima
        assert win_native.set_topmost(owner, False) and win_native.set_topmost(owner, True)
        assert above(owned, owner)
        assert not user32.IsWindowVisible(owner) and not user32.IsWindowVisible(owned)
    finally:
        user32.DestroyWindow(owned)
        user32.DestroyWindow(owner)


# ---------- 6. Segunda instancia ----------

def _channel():
    return f"PomoGrowth-prueba-{uuid.uuid4().hex}"   # nunca el de la app de verdad


_SECOND = ("import sys, time; sys.path.insert(0, sys.argv[1])\n"
           "from PySide6.QtWidgets import QApplication\n"
           "app = QApplication([])\n"
           "from services import singleton\n"
           "start = time.monotonic()\n"
           "reply = singleton.request_show(sys.argv[2], timeout_ms=int(sys.argv[3]))\n"
           "print(reply or '-', round(time.monotonic() - start, 3))\n")


def _second_instance(qtbot, name, timeout_ms=3000):
    """request_show() desde otro proceso, como la segunda instancia de
    verdad (hilo principal con su QApplication). Mientras, este hilo atiende
    el canal. Devuelve (respuesta: SHOWN, CLOSING o NO_ANSWER; segundos que
    esperó)."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen([sys.executable, "-c", _SECOND, str(V9), name, str(timeout_ms)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        qtbot.waitUntil(lambda: proc.poll() is not None, timeout=60000)
    finally:
        if proc.poll() is None:
            proc.kill()
    out = proc.stdout.read().split()
    assert proc.returncode == 0 and len(out) == 2, out
    return ("" if out[0] == "-" else out[0]), float(out[1])


def test_la_segunda_instancia_trae_al_frente_la_primera(ctrl, native, qtbot, monkeypatch):
    from PySide6.QtNetwork import QLocalServer
    from services import singleton
    name = _channel()
    server = singleton.listen_for_show(ctrl.show_from_other_instance, name=name,
                                       parent=ctrl)
    assert server is not None
    # Solo el mismo usuario puede pedírselo
    assert server.socketOptions() & QLocalServer.UserAccessOption
    win = ctrl.window
    activated = []
    real_activate = win.activateWindow
    monkeypatch.setattr(win, "activateWindow",
                        lambda: (activated.append(1), real_activate()))
    try:
        ctrl.show_compact()
        native.calls.clear()
        reply, _seconds = _second_instance(qtbot, name)
        assert reply == singleton.SHOWN
        assert win.isVisible() and not ctrl.compact_window.isVisible()
        assert (_hwnd(win), True) in native.calls  # «encima de todo» repuesto
        assert activated                           # y activa, no solo visible
        qtbot.waitUntil(lambda: win.windowOpacity() > 0.99, timeout=2000)
    finally:
        server.close()


def test_relanzar_durante_la_despedida_dice_que_se_esta_cerrando(ctrl, qtbot):
    """Salir y, durante los ~8 s de la despedida, volver a abrir PomoGrowth:
    la que se cierra no se muestra, y antes contestaba «ok» igual (la segunda
    salía sin aviso y no quedaba ninguna abierta). Ahora contesta «closing»
    y main.py espera a que se cierre para arrancar (test_main_con_otra_...)."""
    from services import singleton
    name = _channel()
    server = singleton.listen_for_show(ctrl.show_from_other_instance, name=name,
                                       parent=ctrl)
    try:
        ctrl._is_closing = True                    # lo que hace quit_with_farewell
        ctrl.window.hide()
        reply, _seconds = _second_instance(qtbot, name)
        assert reply == singleton.CLOSING
        assert not ctrl.window.isVisible()
    finally:
        ctrl._is_closing = False
        server.close()


def test_si_mostrarse_falla_no_contesta_ok(qtbot):
    """Si on_show falla, la segunda no se va creyendo que se mostró: no hay
    respuesta y da el aviso de siempre."""
    from services import singleton
    name = _channel()

    def broken():
        raise RuntimeError("roto")
    server = singleton.listen_for_show(broken, name=name)
    try:
        reply, _seconds = _second_instance(qtbot, name)
        assert reply == singleton.NO_ANSWER
    finally:
        server.close()
        server.deleteLater()


def test_el_canal_ignora_lo_que_no_es_show(ctrl, qtbot):
    from PySide6.QtNetwork import QLocalSocket
    from services import singleton
    name = _channel()
    shown = []
    server = singleton.listen_for_show(lambda: shown.append(1), name=name)
    try:
        sock = QLocalSocket()
        sock.connectToServer(name)
        assert sock.waitForConnected(1000)
        sock.write(b"borrar todo\n")
        sock.flush()
        qtbot.waitUntil(lambda: sock.state() == QLocalSocket.UnconnectedState,
                        timeout=3000)
        assert shown == []
    finally:
        server.close()
        server.deleteLater()


def test_sin_nadie_escuchando_no_bloquea(qtbot):
    from services import singleton
    start = time.monotonic()
    assert singleton.request_show(_channel(), timeout_ms=800) == singleton.NO_ANSWER
    assert time.monotonic() - start < 1.5


def test_el_plazo_por_defecto_es_corto():
    """main.py llama a request_show() sin plazo: el de por defecto es el que
    puede hacer esperar a la segunda instancia antes del aviso."""
    import inspect
    from services import singleton
    assert inspect.signature(singleton.request_show).parameters['timeout_ms'].default <= 2000


def test_una_primera_que_no_contesta_no_bloquea_el_arranque(qtbot, monkeypatch):
    """Si la primera está colgada (acepta pero no contesta), la segunda se
    rinde en su plazo y main.py da el aviso de siempre. Al conectar, la
    segunda le cede el permiso de pasar al frente (AllowSetForegroundWindow):
    si no, la primera solo parpadearía en la barra de tareas."""
    from PySide6.QtNetwork import QLocalServer
    from services import singleton
    from utils import win_native
    name = _channel()
    mute = QLocalServer()
    assert mute.listen(name)
    try:
        reply, seconds = _second_instance(qtbot, name, timeout_ms=700)
        assert reply == singleton.NO_ANSWER and seconds < 1.5
        allowed = []
        monkeypatch.setattr(win_native, "allow_foreground_to_others",
                            lambda: allowed.append(1))
        assert singleton.request_show(name, timeout_ms=300) == singleton.NO_ANSWER
        assert allowed == [1]
    finally:
        mute.close()


def test_el_nombre_del_canal_es_por_usuario_sin_el_nombre(monkeypatch):
    import getpass
    from services import singleton
    monkeypatch.setattr(getpass, "getuser", lambda: "Ana")
    a = singleton.channel_name()
    monkeypatch.setattr(getpass, "getuser", lambda: "Luis")
    b = singleton.channel_name()
    assert a != b and a.startswith("PomoGrowth-") and "Ana" not in a


@pytest.mark.parametrize("elevated, reply, freed, code, warned, asked", [
    (False, "shown", None, 0, False, True),       # la primera se muestra: sin aviso
    (False, "", None, 1, True, True),             # no contesta: el aviso de siempre
    (False, "closing", True, None, False, True),  # se despedía: arranca esta
    (False, "closing", False, 1, True, True),     # se despide y no acaba: el aviso
    (True, "shown", None, 1, True, False),        # administradora: el aviso
])
def test_main_con_otra_instancia_abierta(monkeypatch, qtbot, elevated, reply, freed, code,
                                         warned, asked):
    import main
    from PySide6.QtWidgets import QMessageBox
    from services import singleton
    calls = {'asked': [], 'warned': 0, 'waited': []}

    def request_show(*a, **k):
        calls['asked'].append((a, k))
        return reply

    def acquire_waiting(timeout_s=10.0, interval_s=0.25):
        calls['waited'].append(timeout_s)
        return freed
    monkeypatch.setattr(singleton, "request_show", request_show)
    monkeypatch.setattr(singleton, "acquire_waiting", acquire_waiting)
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: calls.__setitem__('warned', 1)))
    assert main._already_running(elevated) == code
    assert bool(calls['warned']) is warned and bool(calls['asked']) is asked
    # Con el plazo corto de por defecto (test_el_plazo_por_defecto_es_corto)
    assert all(c == ((), {}) for c in calls['asked'])
    assert calls['waited'] == ([singleton.CLOSING_WAIT_S] if reply == "closing" else [])


@pytest.mark.parametrize("acquired, relaunched, admin, already, expect", [
    (True, False, False, None, None),        # la única: sigue, con su log de fallos
    (False, False, False, 0, 0),             # otra la muestra: sale
    (False, False, False, None, None),       # otra se despedía: sigue
    (False, False, True, 1, 1),              # «Ejecutar como administrador» a mano
    (False, True, True, 1, 1),               # la relanzada que no consiguió el puerto
])
def test_reclamar_la_instancia(monkeypatch, tmp_path, acquired, relaunched, admin, already,
                               expect):
    """main._claim_instance: solo la que se queda activa el log de fallos (una
    segunda no escribe en el de la primera) y la administradora pasa como
    elevada a _already_running (ve el aviso que explica qué hacer)."""
    import main
    from services import singleton
    seen = {'elevated': [], 'fault': [], 'mutex': 0, 'waiting': []}
    monkeypatch.setattr(sys, "argv", ["main.py"] + ([singleton.ELEVATED_RESTART_FLAG]
                                                    if relaunched else []))
    monkeypatch.setattr(singleton, "acquire", lambda: acquired)
    monkeypatch.setattr(singleton, "acquire_waiting",
                        lambda timeout_s=10.0, interval_s=0.25: (
                            seen['waiting'].append(timeout_s), acquired)[1])
    monkeypatch.setattr(main, "is_admin", lambda: admin)
    monkeypatch.setattr(main, "_already_running",
                        lambda elevated: (seen['elevated'].append(elevated), already)[1])
    monkeypatch.setattr(main, "hold_named_mutex",
                        lambda name: seen.__setitem__('mutex', seen['mutex'] + 1))
    monkeypatch.setattr(main, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "enable_fault_log",
                        lambda folder, version="": seen['fault'].append(folder))
    assert main._claim_instance() == expect
    assert seen['waiting'] == ([10.0] if relaunched else [])
    assert seen['elevated'] == ([] if acquired else [relaunched or admin])
    stays = expect is None
    assert seen['fault'] == ([tmp_path] if stays else [])
    assert seen['mutex'] == (1 if stays else 0)


def test_la_sesion_escucha_y_vigila(ctrl, qtbot, monkeypatch, tmp_path):
    """main._serve_session: la primera atiende a una segunda instancia de
    verdad (otro proceso) y arranca el vigía, que se para al salir."""
    import faulthandler
    import main
    from PySide6.QtCore import QObject, Signal
    from services import singleton

    class _App(QObject):
        aboutToQuit = Signal()
    name = _channel()
    monkeypatch.setattr(singleton, "channel_name", lambda: name)
    handle = open(tmp_path / "fallos.log", "a", encoding="utf-8")
    monkeypatch.setattr(main, "fault_log_file", lambda: handle)
    app = _App()
    ctrl.window.hide()
    server, watchdog = main._serve_session(app, ctrl)
    try:
        assert server is not None and watchdog is not None
        assert watchdog._timer.isActive()
        reply, _seconds = _second_instance(qtbot, name)
        assert reply == singleton.SHOWN and ctrl.window.isVisible()
        app.aboutToQuit.emit()
        assert not watchdog._timer.isActive()
    finally:
        if watchdog is not None:
            watchdog.stop()
        faulthandler.cancel_dump_traceback_later()
        if server is not None:
            server.close()
        handle.close()


# ---------- 7a. El aviso no tapa la compacta ----------

def _card_rect(t):
    return QRect(t.mapToGlobal(QPoint(t.PAD, t.PAD)),
                 t.size() - QRect(0, 0, 2 * t.PAD, 2 * t.PAD).size())


def _compact(qtbot, x, y):
    from ui.windows.compact_window import CompactWindow
    fonts.init_fonts()
    c = CompactWindow()
    qtbot.addWidget(c)
    c.move(x, y)
    c.show()
    qtbot.waitExposed(c)
    return c


def _global(w):
    return QRect(w.mapToGlobal(QPoint(0, 0)), w.size())


@pytest.mark.parametrize("where", ["arriba", "abajo", "izquierda"])
def test_el_aviso_no_tapa_los_controles_de_la_compacta(qtbot, where):
    from ui.widgets import toast
    area = QApplication.primaryScreen().availableGeometry()
    c = _compact(qtbot, 0, 0)
    pos = {'arriba': QPoint(area.right() - c.width() - 40, area.top() + 40),
           'abajo': QPoint(area.right() - c.width() - 40, area.bottom() - c.height()),
           'izquierda': QPoint(area.left(), area.top() + 40)}[where]
    c.move(pos)
    qtbot.wait(20)
    t = toast.show_warning(c, "Se perdió la conexión con la emisora y no se pudo "
                              "recuperar", duration=4000)
    try:
        qtbot.wait(350)                            # tras su entrada
        card, compact_card = _card_rect(t), _global(c.card)
        assert not card.intersects(compact_card)
        for b in (c.play_btn, c.expand_btn, c.skip_btn):
            assert not card.intersects(_global(b))
        assert area.contains(card)                 # recortado a la pantalla
        if where == "abajo":
            assert card.bottom() < compact_card.top()      # no cabe debajo: encima
        else:
            assert card.top() > compact_card.bottom()      # debajo
    finally:
        toast.dismiss(t)


@pytest.mark.parametrize("size", ["normal", "plegada"])
def test_en_la_principal_el_aviso_sigue_en_su_esquina(ctrl, qtbot, size):
    """La principal plegada (sin el panel de tareas) es estrecha pero alta:
    el aviso sigue dentro, en su esquina. Por eso «pequeña» es estrecha Y
    baja: con «o», saldría fuera de la ventana."""
    from ui.widgets import toast
    from ui.windows.main_window import MIN_HEIGHT, MIN_WIDTH_COLLAPSED
    win = ctrl.window
    if size == "plegada":
        win.setMinimumSize(MIN_WIDTH_COLLAPSED, MIN_HEIGHT)
        win.resize(MIN_WIDTH_COLLAPSED, 720)
        qtbot.waitUntil(lambda: win.width() == MIN_WIDTH_COLLAPSED, timeout=2000)
        assert win.width() < toast.Toast.SMALL_W and win.height() >= toast.Toast.SMALL_H
    t = toast.show_info(win, "Tarea añadida", duration=2000)
    try:
        assert not t._goes_outside(win)
        qtbot.wait(350)
        card = _card_rect(t)
        geo = win.geometry()
        assert geo.contains(card)
        assert geo.right() - card.right() == pytest.approx(t.MARGIN, abs=2)
    finally:
        toast.dismiss(t)


def test_sobre_una_ventana_translucida_el_aviso_sale_fuera(qtbot):
    """Translúcida (como la compacta o los diálogos de la casa), aunque sea
    grande: su esquina inferior derecha puede ser su tarjeta, y el aviso sale
    fuera. Opaca y grande, dentro; opaca y pequeña, fuera."""
    from PySide6.QtWidgets import QWidget
    from ui.widgets import toast
    fonts.init_fonts()
    area = QApplication.primaryScreen().availableGeometry()
    cases = {}
    for name, translucent, (w, h) in (("translucida", True, (520, 320)),
                                      ("opaca", False, (520, 320)),
                                      ("pequeña", False, (300, 150))):
        win = QWidget()
        qtbot.addWidget(win)
        win.setAttribute(Qt.WA_TranslucentBackground, translucent)
        win.setGeometry(area.left() + 40, area.top() + 40, w, h)
        win.show()
        qtbot.waitExposed(win)
        t = toast.show_info(win, "Aviso", duration=2000)
        try:
            cases[name] = t._goes_outside(win)
            qtbot.wait(350)
            if cases[name]:
                assert not _card_rect(t).intersects(win.geometry()), name
        finally:
            toast.dismiss(t)
    assert cases == {"translucida": True, "opaca": False, "pequeña": True}


# ---------- 7b/7c. Avisos de la música ----------

def _music_toasts():
    from ui.widgets import toast
    return [t for t in toast._active_toasts if toast.Toast._is_alive(t)]


def test_un_solo_aviso_de_musica_a_la_vez(ctrl, qtbot):
    ctrl.music.error_occurred.emit("La emisora o el archivo no responde")
    first = ctrl._music_toast
    ctrl.music.error_occurred.emit("Se perdió la conexión con la emisora")
    qtbot.wait(50)
    alive = _music_toasts()
    assert len(alive) == 1 and alive[0] is ctrl._music_toast and alive[0] is not first
    ctrl._music_toast.dismiss()


def test_solo_el_primer_paso_a_la_siguiente_de_una_racha(ctrl, monkeypatch):
    from services.music_service import MusicService
    from ui.widgets import toast
    seen = []
    monkeypatch.setattr(toast, "show_warning",
                        lambda win, msg, duration=0: seen.append(msg))
    state = {'state': 'error', 'want': True}
    monkeypatch.setattr(MusicService, "state", property(lambda s: state['state']))
    monkeypatch.setattr(MusicService, "is_playing", property(lambda s: state['want']))
    emit = ctrl.music.error_occurred.emit
    emit("No se pudo reproducir «A»: paso a la siguiente")
    emit("No se pudo reproducir «B»: paso a la siguiente")      # misma racha: calla
    assert seen == ["No se pudo reproducir «A»: paso a la siguiente"]
    ctrl.music.state_changed.emit('playing')                     # volvió a sonar
    emit("No se pudo reproducir «C»: paso a la siguiente")      # racha nueva
    state['want'] = False                                        # se rindió
    emit("No se pudo reproducir ninguna pista de la lista")      # el aviso final
    assert seen[1:] == ["No se pudo reproducir «C»: paso a la siguiente",
                        "No se pudo reproducir ninguna pista de la lista"]


@pytest.mark.parametrize("how", ["pausa", "parar", "siguiente", "dialogo"])
def test_la_racha_se_cierra_al_pausar_o_al_elegir_otra(ctrl, monkeypatch, how):
    """Tras un «paso a la siguiente» avisado, el usuario pausa, para, pulsa
    «Siguiente» en el mini reproductor o elige otra lista en Música sin que
    llegue a sonar nada: si vuelve a fallar es otra racha y se avisa. Antes
    solo la cerraba volver a sonar, y con emisoras caídas podían pasar
    minutos sin ningún aviso hasta el final."""
    from services.music_service import MusicService
    from ui.widgets import toast
    seen = []
    monkeypatch.setattr(toast, "show_warning",
                        lambda win, msg, duration=0: seen.append(msg))
    state = {'state': 'error', 'want': True}
    monkeypatch.setattr(MusicService, "state", property(lambda s: state['state']))
    monkeypatch.setattr(MusicService, "is_playing", property(lambda s: state['want']))
    monkeypatch.setattr(MusicService, "next", lambda s, *a, **k: None)
    emit = ctrl.music.error_occurred.emit
    emit("No se pudo reproducir «Radio 1»: paso a la siguiente")      # avisado
    emit("No se pudo reproducir «Radio 2»: paso a la siguiente")      # misma racha
    ctrl.music.state_changed.emit('loading')       # el salto: la racha sigue
    emit("No se pudo reproducir «Radio 3»: paso a la siguiente")
    assert seen == ["No se pudo reproducir «Radio 1»: paso a la siguiente"]
    if how == "pausa":
        state.update(state='paused', want=False)
        ctrl.music.state_changed.emit('paused')
    elif how == "parar":
        state.update(state='idle', want=False)
        ctrl.music.state_changed.emit('idle')
    elif how == "siguiente":
        ctrl.timer_panel.mini_player.next_requested.emit()
    else:
        from ui.dialogs import music_dialog

        class _Dialog:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return 0

            def deleteLater(self):
                pass
        monkeypatch.setattr(music_dialog, "MusicDialog", _Dialog)
        ctrl.open_music()
    state.update(state='loading', want=True)       # otra vez, sin llegar a sonar
    ctrl.music.state_changed.emit('loading')
    state['state'] = 'error'
    ctrl.music.state_changed.emit('error')
    emit("No se pudo reproducir «Otra lista»: paso a la siguiente")   # racha nueva
    assert seen[1:] == ["No se pudo reproducir «Otra lista»: paso a la siguiente"]


def test_sin_ventana_a_la_vista_la_musica_avisa_en_la_bandeja(ctrl, qtbot, monkeypatch):
    from ui.widgets import toast
    toasts, notes = [], []
    monkeypatch.setattr(toast, "show_warning", lambda *a, **k: toasts.append(a))
    monkeypatch.setattr(ctrl.tray, "showMessage", lambda title, msg, *a: notes.append(msg))
    ctrl.window.showMinimized()
    qtbot.waitUntil(ctrl.window.isMinimized, timeout=2000)
    ctrl.music.error_occurred.emit("Se perdió la conexión con la emisora")
    ctrl.music.error_occurred.emit("Otra vez")                  # dentro del minuto
    assert notes == ["Se perdió la conexión con la emisora"] and toasts == []
    ctrl._music_tray_at -= ctrl.MUSIC_TRAY_MIN_INTERVAL_S + 1   # pasó el minuto
    ctrl._on_music_error("No se pudo reproducir «X»: paso a la siguiente", final=False)
    assert len(notes) == 1                                       # los saltos, no
    ctrl.music.error_occurred.emit("Se perdió la conexión con la emisora")
    assert len(notes) == 2
    # En la bandeja (oculta) y sin compacta, igual
    ctrl.window.showNormal()
    ctrl.window.hide()
    ctrl._music_tray_at = None
    ctrl.music.error_occurred.emit("Sin red")
    assert notes[-1] == "Sin red" and toasts == []


# ---------- 7d. Ningún modal cuelga de una ventana que no se ve ----------

def _inspect_modal(seen):
    w = QApplication.activeModalWidget()
    if w is None:
        return
    seen.append((w, w.parentWidget(), w.windowFlags()))
    w.reject()


def test_reabrir_desde_la_compacta_pregunta_sobre_la_compacta(ctrl, qtbot):
    ctrl.select_task(ID1)
    ctrl._task_by_id(ID1)['completed'] = True     # p. ej. la completó un sync
    ctrl.show_compact()
    seen = []
    QTimer.singleShot(300, lambda: _inspect_modal(seen))
    ctrl.compact_window.toggle_requested.emit()
    assert seen and seen[0][1] is ctrl.compact_window
    assert seen[0][2] & Qt.WindowStaysOnTopHint   # como su madre, siempre visible
    assert not ctrl.window.isVisible()


@pytest.mark.parametrize("how", ["bandeja", "otra-instancia"])
def test_mostrar_con_un_modal_sobre_la_compacta_trae_el_modal(ctrl, native, qtbot, how):
    """«¿Reabrir?» abierto sobre la compacta y, antes de contestar, doble clic
    en la bandeja u otra PomoGrowth. Cambiar a la principal dejaba el modal
    colgando de la compacta oculta y la principal, bloqueada por él, forzada
    encima: se trae al frente el modal y no se cambia de ventana."""
    ctrl.select_task(ID1)
    ctrl._task_by_id(ID1)['completed'] = True
    ctrl.show_compact()
    seen = []

    def during_modal():
        modal = QApplication.activeModalWidget()
        raised = []
        modal.raise_ = lambda: raised.append('raise')
        modal.activateWindow = lambda: raised.append('activate')
        native.calls.clear()
        shown = (ctrl.show_from_tray() if how == "bandeja"
                 else ctrl.show_from_other_instance())
        seen.append(dict(
            parent_is_compact=modal.parentWidget() is ctrl.compact_window,
            compact=ctrl.compact_window.isVisible(), modal=modal.isVisible(),
            main=ctrl.window.isVisible(),
            still_modal=QApplication.activeModalWidget() is modal,
            forced=(_hwnd(ctrl.window), True) in native.calls,
            raised=raised, shown=shown))
        modal.reject()
    QTimer.singleShot(300, during_modal)
    ctrl.compact_window.toggle_requested.emit()   # «¿Reabrir?» sobre la compacta
    assert seen == [dict(parent_is_compact=True, compact=True, modal=True, main=False,
                         still_modal=True, forced=False, raised=['raise', 'activate'],
                         shown=None if how == "bandeja" else True)]
    # Contestado el modal, mostrar la principal vuelve a ser lo de siempre
    ctrl.show_from_tray()
    assert ctrl.window.isVisible() and not ctrl.compact_window.isVisible()
    qtbot.waitUntil(lambda: ctrl.window.windowOpacity() > 0.99, timeout=2000)


def test_error_de_asana_con_la_compacta_es_un_aviso(ctrl, qtbot, monkeypatch):
    from ui.widgets import toast
    shown, modals = [], []
    monkeypatch.setattr(toast, "show_warning",
                        lambda win, msg, duration=0: shown.append((win, msg)))
    ctrl.show_compact()
    QTimer.singleShot(200, lambda: _inspect_modal(modals))
    ctrl.asana.error.emit("No se pudo conectar con Asana")
    qtbot.wait(300)
    assert modals == []
    assert shown == [(ctrl.compact_window, "Asana: No se pudo conectar con Asana")]


def test_error_de_asana_minimizada_va_a_la_bandeja(ctrl, qtbot, monkeypatch):
    notes, modals = [], []
    monkeypatch.setattr(ctrl.tray, "showMessage", lambda title, msg, *a: notes.append(msg))
    ctrl.window.showMinimized()
    qtbot.waitUntil(ctrl.window.isMinimized, timeout=2000)
    QTimer.singleShot(200, lambda: _inspect_modal(modals))
    ctrl.asana.error.emit("Token revocado")
    qtbot.wait(300)
    assert modals == [] and notes == ["Token revocado"]
    assert ctrl.window.isMinimized()


def test_error_de_asana_a_la_vista_sigue_siendo_modal(ctrl, monkeypatch):
    from ui.dialogs import branded_dialogs
    shown = []
    monkeypatch.setattr(branded_dialogs, "info",
                        lambda parent, title, text, *a, **k: shown.append((parent, text)))
    ctrl.asana.error.emit("Token revocado")
    assert shown == [(ctrl.window, "Token revocado")]


def test_error_de_asana_durante_el_splash_espera_a_la_principal(ctrl, qtbot, monkeypatch):
    from ui.dialogs import branded_dialogs
    shown = []
    monkeypatch.setattr(branded_dialogs, "info",
                        lambda parent, title, text, *a, **k: shown.append(
                            (parent, parent.isVisible())))
    win = ctrl.window
    win.hide()
    ctrl._splash = object()                        # arrancando con el splash

    def end_splash(_msg):                          # lo que hace _end_splash
        ctrl._splash = None
        win.show()
    ctrl.asana.error.connect(end_splash)
    ctrl.asana.error.emit("Sin conexión")
    assert shown == []
    qtbot.waitUntil(lambda: bool(shown), timeout=2000)
    assert shown == [(win, True)]


def _visible_modals():
    def title(w):
        label = getattr(w, '_title_label', None)
        return label.text() if label is not None else type(w).__name__
    return sorted(title(w) for w in QApplication.topLevelWidgets()
                  if w.isVisible() and w.isModal())


def test_error_de_asana_en_el_splash_y_luego_la_bienvenida(ctrl, qtbot):
    """Arranque con sync automática y Asana caída: el error sale al terminar
    el splash y la bienvenida («¿Cómo te llamas?», la presentación o el plan
    de hoy) espera a que se cierre. Antes salía encima del error, a los
    400 ms, y había que cerrarlos en orden inverso."""
    from ui.windows.splash import SplashWindow
    ctrl.window.hide()
    ctrl._splash = SplashWindow()                  # como launch() con auto-sync
    ctrl._splash.show()
    ctrl.asana.progress.connect(ctrl._splash.set_progress)
    ctrl.asana.error.connect(lambda _msg: ctrl._end_splash(-1))
    stacks = []

    def close_one():
        stacks.append(_visible_modals())
        QApplication.activeModalWidget().reject()

    def look():
        if QApplication.activeModalWidget() is not None and len(stacks) < 6:
            close_one()                            # los de la bienvenida, según salen
    closer = QTimer()
    closer.setInterval(150)
    closer.timeout.connect(look)
    QTimer.singleShot(1200, lambda: (close_one(), closer.start()))
    ctrl.asana.error.emit("No se pudo conectar con Asana")
    try:
        qtbot.waitUntil(lambda: ctrl._welcome_shown and len(stacks) >= 2
                        and QApplication.activeModalWidget() is None, timeout=15000)
    finally:
        closer.stop()
    # A los 1,2 s, solo el error (la bienvenida ya tocaba a los 0,4 s)
    assert stacks[0] == ["Asana"], stacks
    # Después, uno detrás de otro: nunca dos a la vez
    assert all(len(stack) == 1 and stack != ["Asana"] for stack in stacks[1:]), stacks


def test_un_modal_con_la_principal_minimizada_la_restaura(ctrl, qtbot, monkeypatch):
    from ui.dialogs import branded_dialogs
    seen = []
    monkeypatch.setattr(branded_dialogs, "info",
                        lambda parent, title, text, *a, **k: seen.append(
                            (parent, parent.isMinimized(), parent.isVisible())))
    win = ctrl.window
    win.showMinimized()
    qtbot.waitUntil(win.isMinimized, timeout=2000)
    ctrl.toggle_timer()                            # sin tarea activa: «Sin tarea activa»
    assert seen == [(win, False, True)]
    qtbot.waitUntil(lambda: win.windowOpacity() > 0.99, timeout=2000)


def _situacion(ctrl, qtbot, where):
    """Deja la app como el usuario la tendría al salir el modal: la principal
    minimizada o en la bandeja (oculta) sin compacta, o la compacta a la vista."""
    win = ctrl.window
    if ctrl.compact_window is not None:
        ctrl.compact_window.hide()
    if win.isMinimized() or not win.isVisible():
        win.showNormal()
    qtbot.waitUntil(lambda: win.isVisible() and not win.isMinimized()
                    and not win.is_fading(), timeout=3000)
    if where == "minimizada":
        win.showMinimized()
        qtbot.waitUntil(win.isMinimized, timeout=2000)
    elif where == "bandeja":
        win.hide()
    else:
        ctrl.show_compact()
    assert not (win.isVisible() and not win.isMinimized())   # la principal, fuera


class _FakeQuitDialog:
    """La pregunta de salir sin abrirla (BrandedDialog del controlador)."""
    seen = []

    def __init__(self, parent, *a, **k):
        type(self).seen.append(parent)

    def add_message(self, *a, **k):
        pass

    def add_buttons(self, buttons):
        from PySide6.QtWidgets import QPushButton
        self._buttons = [QPushButton() for _ in buttons]
        return self._buttons

    def exec(self):
        return 0                                   # «Cancelar»

    def deleteLater(self):
        pass


@pytest.mark.parametrize("dialog", ["sin-tarea", "salir", "asana-sin-configurar",
                                    "eliminar", "editar", "nota", "nombre", "plan",
                                    "bienvenida"])
def test_ningun_modal_cuelga_de_una_ventana_que_no_se_ve(ctrl, qtbot, monkeypatch, dialog):
    """Cada modal del controlador, con la principal minimizada, en la bandeja
    o con la compacta: cuelga de una ventana a la vista (la compacta, o la
    principal restaurada antes). Un modal sobre una ventana oculta o
    minimizada bloquea la app entera, compacta incluida, sin verse."""
    from ui.dialogs import branded_dialogs, day_plan_dialog
    from ui.windows import welcome_farewell
    seen = []

    def record(parent, *a, **k):
        seen.append(parent)
    monkeypatch.setattr(branded_dialogs, "info", record)
    monkeypatch.setattr(branded_dialogs, "confirm",
                        lambda parent, *a, **k: (record(parent), False)[1])
    monkeypatch.setattr(branded_dialogs, "edit_task",
                        lambda parent, *a, **k: record(parent))
    monkeypatch.setattr(branded_dialogs, "note_task",
                        lambda parent, *a, **k: record(parent))
    monkeypatch.setattr(branded_dialogs, "ask_user_name",
                        lambda parent, *a, **k: (record(parent), "")[1])
    monkeypatch.setattr(day_plan_dialog, "pick_day_plan",
                        lambda parent, *a, **k: record(parent))
    _FakeQuitDialog.seen = seen
    monkeypatch.setattr(branded_dialogs, "BrandedDialog", _FakeQuitDialog)

    class _Welcome:
        def __init__(self, parent, *a, **k):
            record(parent)

        def exec(self):
            return 0
    monkeypatch.setattr(welcome_farewell, "WelcomeDialog", _Welcome)
    monkeypatch.setattr(ctrl.asana, "is_configured", lambda: False)
    ctrl.sync_enabled = True

    def open_it():
        settings = ctrl.data_handler.load_settings()
        if dialog == "sin-tarea":
            ctrl.model.active_task_id = None
            ctrl.toggle_timer()
        elif dialog == "salir":
            ctrl.confirm_quit()
        elif dialog == "asana-sin-configurar":
            ctrl.sync_asana()
        elif dialog == "eliminar":
            ctrl.delete_task(ID1)
        elif dialog == "editar":
            ctrl.edit_task(ID1)
        elif dialog == "nota":
            ctrl.note_task(ID1)
        elif dialog == "nombre":
            settings.update(user_name='', user_name_asked=False)
            ctrl.data_handler.save_settings(settings)
            ctrl._ask_user_name_if_needed()
        elif dialog == "plan":
            ctrl.open_day_plan()
        else:                                      # la bienvenida, sin nombre ni plan
            settings.update(user_name='Ana', onboarding_seen=True, day_plan_on_start=False)
            ctrl.data_handler.save_settings(settings)
            ctrl._welcome_shown = False
            ctrl._show_welcome()

    for where in ("minimizada", "bandeja", "compacta"):
        _situacion(ctrl, qtbot, where)
        seen.clear()
        open_it()
        assert len(seen) == 1, (where, seen)
        parent = seen[0]
        expected = ctrl.compact_window if where == "compacta" else ctrl.window
        assert parent is expected, (where, parent)
        assert parent.isVisible() and not parent.isMinimized(), where
    assert not ctrl._is_closing                    # «Cancelar» en la pregunta de salir


def test_los_dialogos_heredan_el_siempre_visible(qtbot):
    from PySide6.QtWidgets import QWidget
    from ui.dialogs.branded_dialogs import BrandedDialog
    fonts.init_fonts()
    for top in (True, False):
        parent = QWidget()
        qtbot.addWidget(parent)
        parent.setWindowFlag(Qt.WindowStaysOnTopHint, top)
        dlg = BrandedDialog(parent, "Prueba")
        assert bool(dlg.windowFlags() & Qt.WindowStaysOnTopHint) is top
        assert dlg.windowFlags() & Qt.FramelessWindowHint
        dlg.deleteLater()


# ---------- 8. Diagnóstico ----------

def test_faulthandler_al_log_de_fallos(tmp_path, monkeypatch):
    import faulthandler
    from utils import logging_setup
    enabled = []
    monkeypatch.setattr(faulthandler, "enable",
                        lambda file=None, all_threads=True: enabled.append((file, all_threads)))
    monkeypatch.setattr(logging_setup, "_fault_file", None)
    old = tmp_path / logging_setup.FAULT_LOG_NAME
    old.write_bytes(b"x" * (logging_setup.FAULT_LOG_MAX_BYTES + 1))   # se rota
    handle = logging_setup.enable_fault_log(tmp_path, "9.9.9")
    try:
        assert handle is not None and logging_setup.fault_log_file() is handle
        assert enabled == [(handle, True)]
        assert (tmp_path / (logging_setup.FAULT_LOG_NAME + ".1")).stat().st_size > 1000
        # Un archivo nuevo empieza con la leyenda (qué es y que «Windows fatal
        # exception» no siempre es un cierre); detrás, la línea de la sesión
        lines = old.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# ") and any("Windows fatal exception" in line
                                                 for line in lines if line.startswith("# "))
        header = [line for line in lines if not line.startswith("# ")]
        assert len(header) == 1 and header[0].startswith("--- PomoGrowth 9.9.9")
        assert f"pid {os.getpid()}" in header[0]
    finally:
        handle.close()
    # La sesión siguiente, sin otra leyenda
    handle = logging_setup.enable_fault_log(tmp_path, "9.9.10")
    handle.close()
    lines = old.read_text(encoding="utf-8").splitlines()
    assert len([line for line in lines if line.startswith("# Pilas")]) == 1
    assert lines[-1].startswith("--- PomoGrowth 9.9.10")


def test_el_vigia_no_avisa_de_un_bloqueo_que_no_hubo(tmp_path, qtbot, caplog):
    """Que el archivo crezca no es un bloqueo: faulthandler apunta también
    excepciones que una DLL maneja (la app sigue) y antes otra PomoGrowth
    escribía ahí su cabecera. Solo cuenta si el hilo tardó de verdad el plazo
    del vigía."""
    import faulthandler
    from utils import logging_setup
    path = tmp_path / "fallos.log"
    handle = open(path, "a", encoding="utf-8")
    dog = logging_setup.start_ui_watchdog(handle, timeout_s=0.6, interval_ms=50)
    try:
        with caplog.at_level(logging.WARNING):
            qtbot.wait(200)
            with open(path, "a", encoding="utf-8") as other:   # otro escritor
                other.write("Windows fatal exception: access violation\n\n"
                            "Current thread 0x0001 (most recent call first):\n")
            qtbot.wait(400)                        # el hilo nunca estuvo bloqueado
        assert not [r for r in caplog.records if "bloqueado" in r.getMessage()]
        assert "Thread" not in path.read_text(encoding="utf-8")   # ni pilas volcadas
    finally:
        dog.stop()
        faulthandler.cancel_dump_traceback_later()
        handle.close()


def test_el_vigia_vuelca_las_pilas_si_el_hilo_se_bloquea(tmp_path, qtbot, caplog):
    import faulthandler
    from utils import logging_setup
    path = tmp_path / "fallos.log"
    handle = open(path, "a", encoding="utf-8")
    dog = logging_setup.start_ui_watchdog(handle, timeout_s=0.4, interval_ms=50)
    try:
        with caplog.at_level(logging.WARNING):
            qtbot.wait(700)                        # todo va bien: nada
            assert path.stat().st_size == 0 and not caplog.records
            time.sleep(1.0)                        # el hilo de la interfaz, bloqueado
            qtbot.wait(300)
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "test_el_vigia_vuelca_las_pilas" in text     # la pila del bloqueo
        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 1 and "bloqueado" in warnings[0]
    finally:
        dog.stop()
        faulthandler.cancel_dump_traceback_later()
        handle.close()


def test_sin_archivo_no_hay_vigia():
    from utils import logging_setup
    assert logging_setup.start_ui_watchdog(None) is None


def test_scrub_quita_enlaces_rutas_y_titulos():
    from utils.logging_setup import scrub
    text = scrub("No se pudo reproducir «Mi lista secreta»: https://x.example/radio"
                 "?listen_key=abc y C:\\Users\\Ana\\Music\\a.mp3")
    assert "abc" not in text and "Ana" not in text and "secreta" not in text
    assert "<enlace>" in text and "<ruta>" in text and "«…»" in text
    assert len(scrub("x" * 500)) == 200


def test_la_musica_queda_en_el_log_sin_enlaces(ctrl, caplog):
    with caplog.at_level(logging.INFO):
        ctrl.music.state_changed.emit('loading')
        ctrl.music.state_changed.emit('reconnecting')
        ctrl.music.error_occurred.emit("Falló http://radio.example/stream?key=123")
    lines = [r.getMessage() for r in caplog.records]
    assert "Música: idle → loading" in lines and "Música: loading → reconnecting" in lines
    assert any(line.startswith("Música: Falló <enlace>") for line in lines)
    assert not any("key=123" in line for line in lines)
    if ctrl._music_toast is not None:
        ctrl._music_toast.dismiss()
