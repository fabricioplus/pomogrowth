"""Presentación del primer arranque (pytest-qt, offscreen): cuándo sale,
navegación, cierre que la marca como vista, botón de Ajustes y pintado de
las ilustraciones animadas. Datos sintéticos en un directorio temporal.

    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_onboarding.py -q
"""
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.data_handler import DataHandler  # noqa: E402
from ui.theme import fonts  # noqa: E402

ID1 = 1764282734316

# Paleta clara de prueba (la app solo trae temas oscuros): las ilustraciones
# tiran de los tokens, así que con esta también deben pintarse bien
LIGHT = dict(bg='#f4f6f8', bg_header='#e9edf1', surface='#ffffff',
             surface_hover='#f0f3f6', surface_active='#e2e8ee', fg='#14181f',
             fg_dim='#4a5363', fg_muted='#7a8394', border='#d5dbe3',
             accent='#0bbf85', accent_hover='#10d394', accent_dim='#9ce6c9',
             on_accent='#ffffff')


def _write(tmp_path, name="datos.json", **data) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _task():
    return {'id': ID1, 'name': 'Informe', 'completed': False, 'tags': [],
            'source': 'local', 'asana_id': None, 'due': None}


@pytest.fixture
def theme():
    """Cambia el tema sin emitir theme_changed (despertaría a ventanas ya
    destruidas de otros tests: MainWindow se conecta con una lambda) y lo
    restaura al acabar."""
    from ui.theme.tokens import ThemeManager
    manager = ThemeManager.instance()
    before = manager.current
    yield manager
    manager.current = before


# ------------------------------------------------------------------- datos
def test_datos_nuevos_la_dejan_pendiente(tmp_path):
    dh = DataHandler(tmp_path / "no_existe.json")
    assert dh.load_settings()['onboarding_seen'] is False
    # Al guardar (p. ej. el día de uso del arranque) la marca va al archivo:
    # si se cierra antes de verla, el próximo arranque la sigue mostrando
    dh.mark_active_day()
    again = DataHandler(tmp_path / "no_existe.json")
    assert again.load_settings()['onboarding_seen'] is False


@pytest.mark.parametrize("data", [
    {'tasks': [_task()]},
    {'time_chunks': [{'timestamp': '2026-11-01T10:00:00', 'duration': 1500,
                      'type': 'pomodoro', 'task_id': ID1}]},
    {'activity_days': ['2026-11-01']},
    {'settings': {'user_name_asked': True}},
    {'settings': {'user_name': 'Fabricio'}},
    {'history': [{'task_id': ID1}]},
], ids=["tareas", "tiempos", "dias_de_uso", "nombre_preguntado", "nombre", "historial"])
def test_datos_con_uso_previo_no_la_muestran(tmp_path, data):
    dh = DataHandler(_write(tmp_path, **data))
    assert dh.load_settings()['onboarding_seen'] is True


def test_archivo_sin_uso_la_deja_pendiente_y_se_respeta_lo_guardado(tmp_path):
    assert DataHandler(_write(tmp_path, settings={})).load_settings()['onboarding_seen'] is False
    path = _write(tmp_path, "visto.json", tasks=[], settings={'onboarding_seen': False},
                  activity_days=['2026-11-01'])
    assert DataHandler(path).load_settings()['onboarding_seen'] is False


# ------------------------------------------------------------- controlador
def _controller(qtbot, tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    dh = DataHandler(path)
    dh.send_asana_ops = lambda ops, *a, **k: [
        {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
         'detail': '', 'offline': False} for op in ops]
    from ui.windows.main_window import MainWindow
    from ui.controllers.app_controller import AppController
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    ctrl = AppController(win, dh)
    win.show()
    qtbot.waitExposed(win)
    return ctrl


@pytest.fixture
def fresh(qtbot, tmp_path, monkeypatch):
    """Controlador sobre una instalación nueva (sin archivo de datos)."""
    return _controller(qtbot, tmp_path, monkeypatch, tmp_path / "pomogrowth_data.json")


@pytest.fixture
def existing(qtbot, tmp_path, monkeypatch):
    """Controlador sobre datos de alguien que ya usaba la app."""
    return _controller(qtbot, tmp_path, monkeypatch,
                       _write(tmp_path, tasks=[_task()], activity_days=['2026-11-01']))


class _Recorder:
    """Sustituye a los diálogos del arranque y apunta el orden."""

    def __init__(self, order):
        self.order = order
        self.kwargs = {}

    def onboarding(self):
        rec = self

        class _Onboarding:
            def __init__(self, parent, **kwargs):
                rec.kwargs = kwargs

            def exec(self):
                rec.order.append('presentacion')
                return 0

            def deleteLater(self):
                pass
        return _Onboarding

    def welcome(self):
        rec = self

        class _Welcome:
            def __init__(self, parent, lines, **kwargs):
                pass

            def exec(self):
                rec.order.append('bienvenida')
                return 1
        return _Welcome


def _patch_start(monkeypatch, order, name="Fabricio"):
    from ui.dialogs import branded_dialogs, onboarding_dialog
    from ui.windows import welcome_farewell
    import ui.dialogs.day_plan_dialog as plan_mod
    rec = _Recorder(order)
    monkeypatch.setattr(branded_dialogs, "ask_user_name",
                        lambda parent, *a: order.append('nombre') or name)
    monkeypatch.setattr(onboarding_dialog, "OnboardingDialog", rec.onboarding())
    monkeypatch.setattr(welcome_farewell, "WelcomeDialog", rec.welcome())
    monkeypatch.setattr(plan_mod, "pick_day_plan",
                        lambda *a, **k: order.append('plan') or None)
    return rec


def test_sale_en_datos_nuevos_entre_el_nombre_y_la_bienvenida(fresh, monkeypatch):
    order = []
    rec = _patch_start(monkeypatch, order)
    fresh._welcome_shown = False
    fresh._show_welcome()
    assert order == ['nombre', 'presentacion', 'bienvenida']
    # Saluda con el nombre recién guardado y usa los minutos del pomodoro
    assert rec.kwargs == {'user_name': "Fabricio", 'pomodoro_min': 25}
    assert fresh.data_handler.load_settings()['onboarding_seen'] is True
    on_disk = json.loads(fresh.data_handler.filepath.read_text(encoding="utf-8"))
    assert on_disk['settings']['onboarding_seen'] is True
    # No se repite
    order.clear()
    fresh._welcome_shown = False
    fresh._show_welcome()
    assert order == ['bienvenida']


def test_antes_del_plan_de_hoy(fresh, monkeypatch):
    order = []
    _patch_start(monkeypatch, order)
    fresh.data_handler.add_task("Informe", [])  # con pendientes: plan en vez de bienvenida
    fresh._welcome_shown = False
    fresh._show_welcome()
    assert order == ['nombre', 'presentacion', 'plan']


def test_no_sale_en_datos_existentes(existing, monkeypatch):
    order = []
    _patch_start(monkeypatch, order)
    s = existing.data_handler.load_settings()
    s['user_name_asked'] = True
    existing.data_handler.save_settings(s)
    existing.data_handler.set_day_plan([ID1])
    existing._welcome_shown = False
    existing._show_welcome()
    assert order == ['bienvenida']


def test_no_sale_en_el_reinicio_como_administrador(fresh, monkeypatch):
    from services.singleton import ELEVATED_RESTART_FLAG
    order = []
    _patch_start(monkeypatch, order)
    monkeypatch.setattr(sys, "argv", [sys.argv[0], ELEVATED_RESTART_FLAG])
    fresh._welcome_shown = False
    fresh._show_welcome()
    assert order == ['nombre', 'bienvenida']
    assert fresh.data_handler.load_settings()['onboarding_seen'] is False  # queda pendiente


@pytest.mark.parametrize("how", ["oculta", "minimizada"])
def test_no_sale_con_la_app_en_segundo_plano(fresh, monkeypatch, qtbot, how):
    order = []
    _patch_start(monkeypatch, order)
    if how == "oculta":
        fresh.window.hide()
    else:
        fresh.window.showMinimized()
        qtbot.waitUntil(fresh.window.isMinimized, timeout=2000)
    fresh._show_onboarding_if_needed()
    assert order == []
    assert fresh.data_handler.load_settings()['onboarding_seen'] is False


def test_datos_ilegibles_no_la_muestran(fresh, monkeypatch):
    order = []
    _patch_start(monkeypatch, order)
    fresh.data_handler.load_error = "No se pudo leer pomogrowth_data.json"
    s = fresh.data_handler.load_settings()
    s.pop('onboarding_seen', None)
    fresh._show_onboarding_if_needed()
    assert order == []


# --------------------------------------------------- el diálogo de verdad
def _run_modal(qtbot, interact, call):
    """Lanza `call()` (abre la presentación con exec()) y, ya abierta,
    `interact(dlg)`. Nunca se queda colgado: si `interact` no la cierra, se
    cierra al final."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ui.dialogs.onboarding_dialog import OnboardingDialog
    seen = []

    def step():
        dlgs = [w for w in QApplication.topLevelWidgets()
                if isinstance(w, OnboardingDialog) and w.isVisible()]
        if not dlgs:
            QTimer.singleShot(20, step)
            return
        dlg = dlgs[-1]
        seen.append(dlg)
        try:
            interact(dlg)
        finally:
            if dlg.isVisible():
                dlg.reject()
    QTimer.singleShot(0, step)
    result = call()
    assert seen, "la presentación no llegó a abrirse"
    return seen[0], result


@pytest.mark.parametrize("how,outcome", [
    ("esc", 'skip'), ("saltar", 'skip'), ("x", 'close'), ("empezar", 'start')])
def test_cerrarla_de_cualquier_forma_la_marca_vista(fresh, qtbot, how, outcome):
    from PySide6.QtCore import Qt

    def interact(dlg):
        assert dlg.isModal()
        if how == "esc":
            qtbot.keyClick(dlg, Qt.Key_Escape)
        elif how == "saltar":
            qtbot.mouseClick(dlg.skip_btn, Qt.LeftButton)
        elif how == "x":
            qtbot.mouseClick(dlg.close_btn, Qt.LeftButton)
        else:
            for _ in range(2):
                qtbot.mouseClick(dlg.next_btn, Qt.LeftButton)
            dlg.stage.finish()
            assert dlg.next_btn.text().strip() == "¡Empezar!"
            qtbot.mouseClick(dlg.next_btn, Qt.LeftButton)
    assert fresh.data_handler.load_settings()['onboarding_seen'] is False
    dlg, _ = _run_modal(qtbot, interact, fresh.show_onboarding)
    assert dlg.outcome == outcome
    assert dlg.running_timers() == 0
    assert fresh.data_handler.load_settings()['onboarding_seen'] is True
    on_disk = json.loads(fresh.data_handler.filepath.read_text(encoding="utf-8"))
    assert on_disk['settings']['onboarding_seen'] is True


def test_el_arranque_real_la_abre_y_sigue_con_la_bienvenida(fresh, qtbot, monkeypatch):
    from PySide6.QtCore import Qt
    from ui.dialogs import branded_dialogs
    from ui.windows import welcome_farewell
    order = []
    monkeypatch.setattr(branded_dialogs, "ask_user_name", lambda parent, *a: "Ana")

    class _Welcome:
        def __init__(self, parent, lines, **kwargs):
            pass

        def exec(self):
            order.append('bienvenida')
            return 1
    monkeypatch.setattr(welcome_farewell, "WelcomeDialog", _Welcome)

    def interact(dlg):
        order.append('presentacion')
        assert dlg.parentWidget() is fresh.window
        assert "Ana" in dlg._title_label.text()
        qtbot.keyClick(dlg, Qt.Key_Escape)
    fresh._welcome_shown = False
    _run_modal(qtbot, interact, fresh._show_welcome)
    assert order == ['presentacion', 'bienvenida']


# -------------------------------------------------------------- navegación
@pytest.fixture
def dlg(qtbot):
    from ui.dialogs.onboarding_dialog import OnboardingDialog
    fonts.init_fonts()
    d = OnboardingDialog(None, user_name="  Fabricio   Pérez ", pomodoro_min=30)
    qtbot.addWidget(d)
    d.show()
    qtbot.waitExposed(d)
    yield d
    if d.isVisible():
        d.reject()


def test_contenido_y_saludo(dlg):
    from ui.dialogs.onboarding_dialog import SLIDES
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setHtml(dlg._title_label.text())
    assert doc.toPlainText() == "Hola, Fabricio"      # el primer nombre
    assert [pg.title.text() for pg in dlg.pages] == [s.title for s in SLIDES] == [
        "Una tarea, toda tu atención", "Tu espacio sin distracciones", "Mira cómo creces"]
    assert [pg.kicker.text() for pg in dlg.pages] == ["Paso 1 de 3", "Paso 2 de 3",
                                                      "Paso 3 de 3"]
    first = dlg.pages[0]
    assert "30 minutos" in first.text.text()             # los minutos de Ajustes
    assert [c.label.text() for c in first.chips][0] == "Pomodoro de 30 min"
    assert "ClickUp" in dlg.pages[1].text.text()
    # Sin nombre, la bienvenida
    from ui.dialogs.onboarding_dialog import OnboardingDialog
    other = OnboardingDialog(None)
    assert other._title_label.text() == "Te damos la bienvenida"
    other.deleteLater()


def test_navegacion_con_botones_y_puntos(dlg, qtbot):
    from PySide6.QtCore import Qt
    assert dlg.index == 0 and not dlg.back_btn.isVisible()
    assert dlg.next_btn.text().strip() == "Siguiente"
    qtbot.mouseClick(dlg.next_btn, Qt.LeftButton)
    assert dlg.index == 1 and dlg.stage.in_transition()
    qtbot.waitUntil(lambda: not dlg.stage.in_transition(), timeout=2000)
    assert dlg.back_btn.isVisible()
    assert dlg.pages[1].isVisible() and not dlg.pages[0].isVisible()
    qtbot.mouseClick(dlg.back_btn, Qt.LeftButton)
    assert dlg.index == 0
    # Un punto lleva directo a su diapositiva
    dlg.stage.finish()
    qtbot.mouseClick(dlg.dots, Qt.LeftButton, pos=dlg.dots.dot_center(2).toPoint())
    assert dlg.index == 2
    assert dlg.next_btn.text().strip() == "¡Empezar!"
    assert dlg.isVisible()


def test_navegacion_con_teclado(dlg, qtbot):
    from PySide6.QtCore import Qt
    qtbot.keyClick(dlg, Qt.Key_Right)
    assert dlg.index == 1
    qtbot.keyClick(dlg, Qt.Key_Left)
    assert dlg.index == 0
    qtbot.keyClick(dlg, Qt.Key_Left)                     # no hay anterior
    assert dlg.index == 0
    qtbot.keyClick(dlg, Qt.Key_Return)
    qtbot.keyClick(dlg, Qt.Key_Enter)
    assert dlg.index == 2
    qtbot.keyClick(dlg, Qt.Key_Right)                    # la flecha no cierra
    assert dlg.index == 2 and dlg.isVisible()
    qtbot.keyClick(dlg, Qt.Key_Return)                   # Intro en la última: empezar
    assert not dlg.isVisible()
    assert dlg.outcome == 'start' and dlg.result() == 1


def test_esc_salta(dlg, qtbot):
    from PySide6.QtCore import Qt
    qtbot.keyClick(dlg, Qt.Key_Right)
    qtbot.keyClick(dlg, Qt.Key_Escape)
    assert not dlg.isVisible()
    assert dlg.outcome == 'skip' and dlg.result() == 0


def test_transicion_desliza_y_funde(dlg, qtbot):
    from PySide6.QtWidgets import QGraphicsOpacityEffect
    dlg.go_to(1)
    old, new = dlg.pages[:2]
    assert isinstance(old.graphicsEffect(), QGraphicsOpacityEffect)
    dlg.stage._apply(0.5)
    assert 0.4 < new.graphicsEffect().opacity() < 0.6
    assert new.x() > 0 and old.x() < 0                   # entra por la derecha
    qtbot.waitUntil(lambda: not dlg.stage.in_transition(), timeout=2000)
    assert new.graphicsEffect() is None and new.pos().x() == 0
    assert not old.isVisible()


def test_temporizadores_de_animacion(dlg, qtbot):
    pages = dlg.pages
    assert pages[0].scene.is_running()
    assert not pages[1].scene.is_running() and not pages[2].scene.is_running()
    t0 = pages[0].scene.time()
    qtbot.wait(120)
    assert pages[0].scene.time() > t0                    # avanza solo
    dlg.go_to(1)
    dlg.stage.finish()
    assert not pages[0].scene.is_running() and pages[1].scene.is_running()
    assert pages[1].scene.time() < 0.2                   # empieza desde el principio
    dlg.go_to(2)                                         # se cierra a mitad de transición
    assert dlg.running_timers() >= 1
    dlg.reject()
    assert dlg.running_timers() == 0
    assert not any(pg.scene.is_running() for pg in pages)
    assert dlg.outcome == 'close'


# ------------------------------------------------------------------ pintado
def _colors(img, step=7):
    seen = set()
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            seen.add(img.pixel(x, y))
    return seen


def _signature(img, step=9):
    return tuple(img.pixel(x, y) for y in range(0, img.height(), step)
                 for x in range(0, img.width(), step))


@pytest.mark.parametrize("palette", ["oscuro", "claro", "theme_golden"])
def test_cada_diapositiva_se_pinta_en_cada_tema(qtbot, theme, palette):
    from ui.dialogs.onboarding_dialog import OnboardingDialog
    from ui.theme.tokens import THEMES
    fonts.init_fonts()
    if palette == "claro":
        theme.current = replace(THEMES['default'], **LIGHT)
    elif palette != "oscuro":
        theme.current = THEMES[palette]
    d = OnboardingDialog(None, user_name="Ana")
    qtbot.addWidget(d)
    d.resize(d.sizeHint())
    for page in d.pages:
        scene = page.scene
        scene.resize(712, 216)
        frames = []
        steps = 12
        for k in range(steps + 1):                       # todo el bucle, y una vuelta más
            scene.set_time(scene.PERIOD * k / steps * 1.5)
            img = scene.grab().toImage()
            assert not img.isNull() and img.width() >= 712
            assert len(_colors(img)) > 8                 # no es un panel vacío
            frames.append(img)
        # Hay animación: los fotogramas cambian
        assert len({_signature(f) for f in frames}) > 4
        scene.advance(0.5)
        assert not scene.grab().isNull()
        assert not page.grab().isNull()
    assert not d.grab().isNull()
    d.deleteLater()


def test_se_retematiza_en_caliente(dlg, theme):
    from ui.theme.tokens import THEMES
    theme.current = THEMES['theme_cherry']
    dlg._retheme()                                       # lo que hace theme_changed
    assert dlg._accent_override == THEMES['theme_cherry'].accent
    assert THEMES['theme_cherry'].accent in dlg.pages[0].kicker.styleSheet()
    assert THEMES['theme_cherry'].accent in dlg._title_label.text()   # el nombre
    assert not dlg.grab().isNull()


def test_escala_las_ilustraciones_al_hueco(qtbot):
    """En pantallas con poco alto (200 %) la ilustración se encoge sin
    deformarse ni romperse."""
    from ui.dialogs.onboarding_dialog import FocusScene
    fonts.init_fonts()
    scene = FocusScene(None, minutes=25)
    qtbot.addWidget(scene)
    for w, h in ((712, 216), (520, 132), (900, 300)):
        scene.resize(w, h)
        scene.set_time(4.9)
        assert scene.grab().size().width() == w


# ------------------------------------------------------------------ Ajustes
def test_boton_de_ajustes_la_abre(existing, qtbot, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel
    from ui.dialogs.settings_dialog import SettingsDialog
    dlg = SettingsDialog(existing.window, existing)
    qtbot.addWidget(dlg)
    page = dlg.tabs.widget(0)
    assert page.isAncestorOf(dlg.btn_onboarding)
    assert dlg.btn_onboarding.text().strip() == "Ver la presentación"
    texts = [lbl.text() for lbl in page.findChildren(QLabel)]
    assert texts.index("Tu nombre") < texts.index("Presentación") < texts.index("Ciclo pomodoro")
    calls = []
    monkeypatch.setattr(existing, "show_onboarding", lambda parent=None: calls.append(parent))
    qtbot.mouseClick(dlg.btn_onboarding, Qt.LeftButton)
    assert calls == [dlg]
    dlg.reject()


def test_boton_de_ajustes_abre_la_de_verdad_encima(existing, qtbot):
    from PySide6.QtCore import Qt
    from ui.dialogs.settings_dialog import SettingsDialog
    settings = SettingsDialog(existing.window, existing)
    qtbot.addWidget(settings)
    settings.show()
    qtbot.waitExposed(settings)
    seen = {}

    def interact(dlg):
        seen['parent'] = dlg.parentWidget()
        seen['overlay_on_card'] = dlg._overlay is not None and \
            dlg._overlay.parentWidget() is settings._card
        qtbot.keyClick(dlg, Qt.Key_Right)
        seen['index'] = dlg.index
        qtbot.keyClick(dlg, Qt.Key_Escape)
    _run_modal(qtbot, interact,
               lambda: qtbot.mouseClick(settings.btn_onboarding, Qt.LeftButton))
    assert seen == {'parent': settings, 'overlay_on_card': True, 'index': 1}
    assert settings.isVisible()                          # Ajustes sigue abierto
    assert existing.data_handler.load_settings()['onboarding_seen'] is True
    settings.reject()


# ------------------------------------------------------- revisión cruzada
def test_salir_con_la_presentacion_abierta_la_cierra(fresh, qtbot, monkeypatch):
    """Salir desde la bandeja con la presentación abierta: no se queda en
    pantalla animándose durante la despedida."""
    from ui.windows import welcome_farewell
    monkeypatch.setattr(welcome_farewell, "FarewellWindow", lambda on_done, **kw: None)
    monkeypatch.setattr(fresh.focus_mode, "cleanup_system_blocks", lambda: False)
    state = {}

    def interact(dlg):
        assert fresh._onboarding_dialog is dlg
        fresh.quit_with_farewell()
        state.update(visible=dlg.isVisible(), timers=dlg.running_timers())
    dlg, _ = _run_modal(qtbot, interact, fresh.show_onboarding)
    assert state == {'visible': False, 'timers': 0}
    assert fresh._onboarding_dialog is None
    assert fresh.data_handler.load_settings()['onboarding_seen'] is True


def test_la_escena_es_opaca_y_no_repinta_la_sombra(dlg, qtbot):
    """Cada fotograma de la ilustración no debe repintar el marco de la
    sombra (su QGraphicsDropShadowEffect se volvería a difuminar ~60 veces
    por segundo): la escena es opaca y rellena sus esquinas con la tarjeta."""
    from PySide6.QtCore import QEvent, QObject, Qt
    from PySide6.QtGui import QColor
    from ui.theme.tokens import tokens
    scene = dlg.pages[0].scene
    assert scene.testAttribute(Qt.WA_OpaquePaintEvent)
    corner = scene.grab().toImage().pixelColor(0, 0)
    assert corner.alpha() == 255 and corner.name() == QColor(tokens().surface).name()

    class _Count(QObject):
        paints = 0

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Paint:
                _Count.paints += 1
            return False
    counter = _Count()
    dlg._shadow.installEventFilter(counter)
    qtbot.wait(50)
    _Count.paints = 0
    qtbot.wait(300)                                  # ~18 fotogramas de la escena
    dlg._shadow.removeEventFilter(counter)
    assert scene.is_running()
    assert _Count.paints == 0


def test_tamano_acotado_a_la_pantalla_y_no_a_dos_tercios(dlg):
    """El adjustSize de Qt limita las ventanas a 2/3 de la pantalla (en 1080p
    al 150 % la ilustración salía a menos de la mitad): se usa el tamaño
    pedido, acotado solo al área útil."""
    avail = dlg.screen().availableGeometry().size()
    dlg.resize(dlg.minimumSize())
    dlg.adjustSize()
    expected = dlg.sizeHint().boundedTo(avail).expandedTo(dlg.minimumSize())
    assert dlg.size() == expected
    if dlg.sizeHint().height() > avail.height() * 2 // 3:
        assert dlg.height() > avail.height() * 2 // 3


def test_clickup_no_se_dibuja_sincronizando(qtbot, monkeypatch):
    """CONTENIDO_WEB.md, «Qué no prometer»: ClickUp solo crea tareas. La tarea
    que se sincroniza en la diapositiva 2 es solo de Asana."""
    from ui.dialogs import onboarding_dialog as od
    fonts.init_fonts()
    drawn = []
    real = od._draw_icon
    monkeypatch.setattr(od, "_draw_icon", lambda p, name, *a, **k: (
        drawn.append(name), real(p, name, *a, **k)))
    scene = od.SpaceScene(None)
    qtbot.addWidget(scene)
    scene.resize(712, 216)
    for k in range(13):
        scene.set_time(scene.PERIOD * k / 12)
        scene.grab()
    assert "sync" in drawn and "asana-mark" in drawn
    assert not any("clickup" in name for name in drawn)
    assert "crea tareas nuevas también en ClickUp" in od.SLIDES[1].text
