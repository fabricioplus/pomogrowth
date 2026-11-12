"""Entrada rápida de tareas (ui/widgets/task_add_panel.py) y vencimiento al
añadir una tarea (DataHandler.add_task(due=...)), con pytest-qt offscreen.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_task_add.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402

from core.data_handler import DataHandler  # noqa: E402
from ui.theme import fonts  # noqa: E402
from ui.widgets.task_add_panel import (RECURRING_OPTIONS, TaskAddPanel,  # noqa: E402
                                       due_text, extract_tags, shortcut_date)


@pytest.fixture
def panel(qtbot):
    fonts.init_fonts()
    p = TaskAddPanel()
    qtbot.addWidget(p)
    p.resize(520, 140)
    p.show()
    qtbot.waitExposed(p)
    p.activateWindow()
    qtbot.waitUntil(p.isActiveWindow)
    return p


@pytest.fixture
def emitted(panel):
    calls = []
    panel.task_submitted.connect(lambda *args: calls.append(args))
    return calls


def _focus(panel, qtbot):
    panel.toggle()
    qtbot.waitUntil(panel.name_edit.hasFocus)


def _action(menu, data):
    return next(a for a in menu.actions() if a.data() == data)


# ---------------------------------------------------------------- etiquetas
def test_extract_tags_quita_las_etiquetas_del_nombre():
    assert extract_tags("Comprar pan #casa #Urgente") == ("Comprar pan", ["casa", "urgente"])
    assert extract_tags("#trabajo Llamar a Ana #trabajo") == ("Llamar a Ana", ["trabajo"])
    assert extract_tags("Diseño #café-2026 final") == ("Diseño final", ["café-2026"])
    # Un número de incidencia o «C#» no son etiquetas
    assert extract_tags("Cerrar incidencia #12") == ("Cerrar incidencia #12", [])
    assert extract_tags("Curso de C# avanzado") == ("Curso de C# avanzado", [])


def test_hashtags_del_nombre_se_emiten_como_tags(panel, emitted, qtbot):
    _focus(panel, qtbot)
    panel.name_edit.setText("Llamar a Ana #Trabajo #clientes")
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted == [("Llamar a Ana", ["trabajo", "clientes"], "", "", {})]


def test_hashtag_tecleado_pasa_a_chip_al_cerrarlo_con_espacio(panel, qtbot):
    _focus(panel, qtbot)
    qtbot.keyClicks(panel.name_edit, "Revisar #docs informe")
    assert panel.name_edit.text() == "Revisar informe"
    assert panel.tags == ["docs"]
    assert panel._tag_chips["docs"].isVisibleTo(panel)


def test_etiquetas_del_selector_con_autocompletado(panel, emitted, qtbot):
    panel.set_known_tags(["Cocina", "compras", "asana", "asana_deleted", "trabajo"])
    # Sin las internas, en minúsculas y sin repetir
    assert panel._known_tags == ["cocina", "compras", "trabajo"]
    _focus(panel, qtbot)
    popup = panel.open_tag_popup()
    qtbot.keyClicks(popup.search, "co")
    # Las existentes primero y «Crear «co»» al final: Enter elige «cocina»
    # en vez de crear una etiqueta casi repetida
    assert popup.items() == ["cocina", "compras", "co"]
    assert popup.list.currentRow() == 0
    qtbot.keyClick(popup.search, Qt.Key_Return)
    assert panel.tags == ["cocina"]
    assert "cocina" not in popup.items()  # ya elegida
    # Sin coincidencias, la única opción es crearla y Enter la crea
    qtbot.keyClicks(popup.search, "huerto")
    assert popup.items() == ["huerto"]
    qtbot.keyClick(popup.search, Qt.Key_Return)
    assert panel.tags == ["cocina", "huerto"]
    popup.close()
    # La «x» del chip la quita
    panel._tag_chips["cocina"].removed.emit()
    assert panel.tags == ["huerto"]


def test_selector_de_etiquetas_sigue_pegado_a_la_entrada_al_filtrar(panel, qtbot):
    """Al filtrar, la lista encoge o crece: la emergente mantiene su borde
    inferior (justo encima de la entrada) en vez de su borde superior."""
    panel.set_known_tags(["alfa", "beta", "cocina", "compras", "delta", "eco", "foco"])
    # Abajo en la pantalla (como en la app): sitio para abrirla encima
    screen = panel.screen().availableGeometry()
    panel.move(screen.left() + 20, screen.bottom() - panel.height() - 20)
    _focus(panel, qtbot)
    popup = panel.open_tag_popup()
    qtbot.waitExposed(popup)
    assert bottom_of(popup) <= panel.card.mapToGlobal(panel.card.rect().topLeft()).y()

    start = bottom_of(popup)
    qtbot.keyClicks(popup.search, "zz")  # solo «Crear «zz»»: encoge
    assert popup.list.count() == 1
    assert bottom_of(popup) == start
    popup.search.clear()  # vuelve la lista entera: crece
    assert popup.list.count() == 7
    assert bottom_of(popup) == start
    popup.close()


def bottom_of(widget):
    """Borde inferior (global, sin el margen de la sombra) de una emergente."""
    g = widget.geometry()
    return g.y() + g.height() - widget.M


def test_autocompletado_inline_de_hashtag(panel, qtbot):
    panel.set_known_tags(["cocina", "compras"])
    _focus(panel, qtbot)
    qtbot.keyClicks(panel.name_edit, "Cena #coc")
    completer = panel._completer
    assert completer.completionCount() == 1
    completer.activated[str].emit("cocina")
    assert panel.name_edit.text() == "Cena "
    assert panel.tags == ["cocina"]


# -------------------------------------------------------------------- fecha
def test_atajos_de_fecha_respecto_a_hoy():
    martes = date(2026, 10, 27)
    lunes = date(2026, 10, 26)
    domingo = date(2026, 11, 1)
    assert shortcut_date('today', martes) == martes
    assert shortcut_date('tomorrow', martes) == date(2026, 10, 28)
    assert shortcut_date('next_monday', martes) == date(2026, 11, 2)
    # Un lunes, «Próximo lunes» es el de la semana siguiente; un domingo, mañana
    assert shortcut_date('next_monday', lunes) == date(2026, 11, 2)
    assert shortcut_date('next_monday', domingo) == date(2026, 11, 2)
    assert due_text(martes, martes) == "Hoy"
    assert due_text(date(2026, 10, 28), martes) == "Mañana"
    assert due_text(date(2026, 11, 2), martes) == "lun 2 nov"
    assert due_text(date(2027, 1, 5), martes) == "5 ene 2027"


@pytest.mark.parametrize("key", ["today", "tomorrow", "next_monday"])
def test_menu_de_fecha_elige_el_atajo(panel, emitted, qtbot, key):
    today = date.today()
    expected = {'today': today, 'tomorrow': today + timedelta(days=1),
                'next_monday': today + timedelta(days=7 - today.weekday())}[key]
    _focus(panel, qtbot)
    menu = panel.build_date_menu()
    _action(menu, key).trigger()
    assert panel.due == expected
    assert panel.due_chip.is_active()
    panel.name_edit.setText("Con fecha")
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted == [("Con fecha", [], "", expected.isoformat(), {})]


def test_fecha_concreta_desde_el_calendario(panel, emitted, qtbot):
    target = date.today() + timedelta(days=17)
    _focus(panel, qtbot)
    popup = panel.open_calendar()
    assert popup.calendar.firstDayOfWeek() == Qt.Monday
    popup.calendar.clicked.emit(QDate(target.year, target.month, target.day))
    assert panel.due == target
    # Un día pasado no se puede elegir
    popup2 = panel.open_calendar()
    yesterday = date.today() - timedelta(days=1)
    popup2.calendar.clicked.emit(QDate(yesterday.year, yesterday.month, yesterday.day))
    assert panel.due == target
    popup2.close()
    panel.name_edit.setText("Entregar propuesta")
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted == [("Entregar propuesta", [], "", target.isoformat(), {})]


def test_quitar_fecha(panel, qtbot):
    _focus(panel, qtbot)
    panel.set_due(date.today())
    _action(panel.build_date_menu(), "clear").trigger()
    assert panel.due is None and not panel.due_chip.is_active()
    panel.set_due(date.today())
    panel.due_chip.removed.emit()  # la «x» del chip
    assert panel.due is None


# --------------------------------------------------------------- repetición
def test_ciclo_desde_el_menu(panel, emitted, qtbot):
    assert [c for _, c in RECURRING_OPTIONS] == ['', 'daily', 'weekly', 'monthly', 'quarterly']
    _focus(panel, qtbot)
    _action(panel.build_repeat_menu(), 'weekly').trigger()
    assert panel.cycle == 'weekly'
    assert panel.repeat_chip.text() == "Semanal"
    panel.name_edit.setText("Regar las plantas")
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted == [("Regar las plantas", [], "weekly", "", {})]
    panel.set_cycle('quarterly')
    panel.repeat_chip.removed.emit()
    assert panel.cycle == ''


# ------------------------------------------------------------ Enter / Escape
def test_enter_emite_los_cinco_valores_y_deja_el_campo_listo(panel, emitted, qtbot):
    _focus(panel, qtbot)
    due = date.today() + timedelta(days=1)
    panel.set_due(due)
    panel.set_cycle('monthly')
    panel.add_tag('Cliente')
    qtbot.keyClicks(panel.name_edit, "Preparar informe #trabajo")
    assert panel.submit_btn.isEnabled()
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted == [("Preparar informe", ["cliente", "trabajo"], "monthly", due.isoformat(), {})]
    # Vacío, enfocado y abierto para la siguiente; el borrador se reinicia
    assert panel.name_edit.text() == ""
    assert panel.name_edit.hasFocus()
    assert panel.is_expanded()
    assert (panel.due, panel.cycle, panel.tags) == (None, '', [])
    assert not panel.submit_btn.isEnabled()
    # Varias seguidas
    qtbot.keyClicks(panel.name_edit, "Segunda")
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted[-1] == ("Segunda", [], "", "", {})


def test_boton_anadir_emite(panel, emitted, qtbot):
    _focus(panel, qtbot)
    # ⚠ QTest.keyClicks aborta el proceso con caracteres no ASCII (ó, ñ…):
    # el texto con tildes va por setText
    qtbot.keyClicks(panel.name_edit, "Con el boton")
    qtbot.mouseClick(panel.submit_btn, Qt.LeftButton)
    assert emitted == [("Con el boton", [], "", "", {})]
    panel.name_edit.setText("Revisión del diseño")
    qtbot.mouseClick(panel.submit_btn, Qt.LeftButton)
    assert emitted[-1] == ("Revisión del diseño", [], "", "", {})


def test_nombre_vacio_no_emite_y_avisa(panel, emitted, qtbot):
    _focus(panel, qtbot)
    assert not panel.submit_btn.isEnabled()
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    panel.name_edit.setText("   ")
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted == []
    assert panel._warning
    assert "nombre" in panel.name_edit.placeholderText()
    # Solo etiquetas: pasan a chips, pero sin nombre no se emite
    panel.name_edit.setText("#idea")
    assert not panel.submit_btn.isEnabled()
    qtbot.keyClick(panel.name_edit, Qt.Key_Return)
    assert emitted == []
    assert panel.tags == ["idea"]
    assert panel.name_edit.hasFocus()


def test_escape_limpia_y_quita_el_foco(panel, emitted, qtbot):
    _focus(panel, qtbot)
    qtbot.keyClicks(panel.name_edit, "Borrador")
    panel.set_due(date.today())
    panel.set_cycle('daily')
    panel.add_tag('x')
    qtbot.keyClick(panel.name_edit, Qt.Key_Escape)
    assert panel.name_edit.text() == ""
    assert (panel.due, panel.cycle, panel.tags) == (None, '', [])
    assert not panel.name_edit.hasFocus()
    assert not panel.is_expanded()
    assert emitted == []


def test_toggle_enfoca_y_despliega_las_opciones(panel, qtbot):
    assert not panel.is_expanded()
    assert panel.options.maximumHeight() == 0
    panel.toggle()
    assert panel.name_edit.hasFocus()
    assert panel.is_expanded()
    qtbot.waitUntil(lambda: panel.options.height() >= panel.due_chip.height())


def test_pista_para_hoy(panel, qtbot):
    assert panel.plan_chip.isHidden()
    panel.set_plan_hint(True)
    assert not panel.plan_chip.isHidden()
    assert "hoy" in panel.name_edit.placeholderText()
    _focus(panel, qtbot)
    assert panel.plan_chip.isVisibleTo(panel)  # en la fila de opciones
    panel.set_plan_hint(False)
    assert not panel.plan_chip.isVisibleTo(panel)


# ------------------------------------------------- estados y descubribilidad
def test_icono_y_marcador_segun_el_estado(panel, qtbot):
    from ui.theme.tokens import tokens
    t = tokens()
    # Reposo: «+» y una invitación legible (fg_dim, no el gris de pista)
    assert panel.lead.mode() == 'add'
    assert panel.name_edit.placeholderText() == "Añadir una tarea…"
    assert t.fg_dim in panel.name_edit.styleSheet()
    # Abierta: el círculo de la tarea que se está creando y la pista de «#»
    _focus(panel, qtbot)
    assert panel.lead.mode() == 'draft'
    assert "#etiqueta" in panel.name_edit.placeholderText()
    assert t.fg_muted in panel.name_edit.styleSheet()
    # Tras guardar, un «✓» momentáneo que vuelve al círculo
    panel.name_edit.setText("Una")
    panel.submit()
    assert panel.lead.mode() == 'saved'
    qtbot.waitUntil(lambda: panel.lead.mode() == 'draft', timeout=2000)
    # Aviso de nombre vacío: texto y color de aviso, que se van al escribir
    panel.submit()
    assert panel.name_edit.placeholderText() == "Escribe un nombre para la tarea"
    assert t.fg_muted not in panel.name_edit.styleSheet()
    qtbot.keyClicks(panel.name_edit, "x")
    assert not panel._warning


def test_plegada_los_chips_no_reciben_el_foco(panel, qtbot):
    # Plegada, la fila de opciones está oculta (no solo con alto 0)
    assert panel.options.isHidden()
    assert not panel.due_chip.isVisibleTo(panel)
    _focus(panel, qtbot)
    assert not panel.options.isHidden()
    qtbot.keyClick(panel.name_edit, Qt.Key_Escape)
    qtbot.waitUntil(panel.options.isHidden, timeout=2000)


def test_tab_recorre_los_chips_en_orden_y_escape_desde_un_chip(panel, emitted, qtbot):
    _focus(panel, qtbot)
    panel.name_edit.setText("Orden")
    panel.add_tag("uno")
    panel.add_tag("dos")
    # Los logos de Asana y ClickUp van tras «Etiquetas», en el orden en que se ven
    order = [panel.name_edit, panel.due_chip, panel.repeat_chip, panel.tags_chip,
             panel.asana_btn, panel.clickup_btn, panel._tag_chips["uno"],
             panel._tag_chips["dos"], panel.submit_btn]
    for current, nxt in zip(order, order[1:]):
        assert current.hasFocus()
        qtbot.keyClick(current, Qt.Key_Tab)
        assert nxt.hasFocus(), nxt
    # Con el foco en un chip, Escape descarta igual que desde el nombre
    panel._tag_chips["uno"].setFocus()
    qtbot.keyClick(panel._tag_chips["uno"], Qt.Key_Escape)
    assert (panel.name_edit.text(), panel.tags) == ("", [])
    assert not panel.is_expanded()
    assert emitted == []


def test_salir_del_panel_desde_un_chip_pliega_y_escape_suelta_el_foco(qtbot):
    from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget
    fonts.init_fonts()
    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    add = TaskAddPanel(host)
    lay.addWidget(add)
    other = QLineEdit(host)  # el siguiente widget de la ventana
    lay.addWidget(other)
    host.resize(520, 240)
    host.show()
    qtbot.waitExposed(host)
    host.activateWindow()
    qtbot.waitUntil(host.isActiveWindow)
    # Tab desde el nombre hasta un chip y de ahí fuera del panel: sin
    # borrador se pliega y el borde vuelve al reposo
    add.focus_input()
    qtbot.waitUntil(add.name_edit.hasFocus)
    qtbot.keyClick(add.name_edit, Qt.Key_Tab)
    qtbot.wait(50)  # que se resuelva la salida del nombre (sigue dentro)
    assert add.due_chip.hasFocus() and add.is_expanded()
    other.setFocus(Qt.TabFocusReason)
    qtbot.waitUntil(lambda: not add.is_expanded(), timeout=2000)
    assert add.card._focus_anim.endValue() == 0.0 or add.card._focus == 0.0
    # Escape con el foco en un chip: nada del panel conserva el foco (no
    # salta a otro widget al ocultarse la fila de opciones)
    add.focus_input()
    qtbot.waitUntil(add.name_edit.hasFocus)
    qtbot.keyClick(add.name_edit, Qt.Key_Tab)
    assert add.due_chip.hasFocus()
    qtbot.keyClick(add.due_chip, Qt.Key_Escape)
    qtbot.waitUntil(add.options.isHidden, timeout=2000)
    focus = QApplication.focusWidget()
    assert focus is None or not add.isAncestorOf(focus)
    assert not other.hasFocus()


def test_retroceso_en_un_chip_lo_quita(panel, qtbot):
    _focus(panel, qtbot)
    panel.add_tag("borrar")
    panel.set_due(date.today())
    chip = panel._tag_chips["borrar"]
    chip.setFocus()
    qtbot.keyClick(chip, Qt.Key_Backspace)
    assert panel.tags == []
    assert panel.name_edit.hasFocus()  # de vuelta al nombre
    panel.due_chip.setFocus()
    qtbot.keyClick(panel.due_chip, Qt.Key_Delete)
    assert panel.due is None
    # Sin valor, Retroceso no hace nada (el chip «Fecha» sigue ahí)
    qtbot.keyClick(panel.due_chip, Qt.Key_Backspace)
    assert panel.due is None and panel.due_chip.text() == "Fecha"


def test_el_clic_que_cierra_el_menu_no_lo_reabre(panel, qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(panel, "_open_date_menu", lambda: opened.append("fecha"))
    _focus(panel, qtbot)
    panel.due_chip.click()
    assert opened == ["fecha"]
    # Windows repite sobre el chip el clic que cerró su menú: no reabrir
    panel._remember_close(panel.due_chip)
    panel.due_chip.click()
    assert opened == ["fecha"]
    # Un clic posterior sí lo abre, y cerrar otro menú no bloquea este
    panel.due_chip.click()
    assert opened == ["fecha", "fecha"]
    panel._remember_close(panel.repeat_chip)
    panel.due_chip.click()
    assert opened == ["fecha", "fecha", "fecha"]


def test_menus_marcan_la_opcion_vigente(panel, qtbot):
    _focus(panel, qtbot)
    tomorrow = date.today() + timedelta(days=1)
    panel.set_due(tomorrow)
    menu = panel.build_date_menu()
    assert _action(menu, "tomorrow").property("current")
    assert not _action(menu, "today").property("current")
    assert _action(menu, "pick").text() == "Elegir fecha…"  # sin pista
    # Una fecha del calendario se marca en «Elegir fecha…» con su texto
    far = date.today() + timedelta(days=40)
    panel.set_due(far)
    pick = _action(panel.build_date_menu(), "pick")
    assert pick.property("current")
    assert pick.text() == f"Elegir fecha…\t{due_text(far)}"
    panel.set_cycle('daily')
    repeat = panel.build_repeat_menu()
    assert [a.data() for a in repeat.actions() if a.property("current")] == ['daily']


def test_panel_estrecho_los_chips_no_se_apilan(panel, qtbot):
    """«Para hoy» + «Añadir» van al final de la última fila (o en una
    propia), no en una columna fija que deja los chips de uno en uno."""
    from PySide6.QtCore import QPoint, QSize
    panel.set_plan_hint(True)
    _focus(panel, qtbot)
    qtbot.waitUntil(lambda: panel.options.height() >= panel.due_chip.height())
    host = panel._chips_host
    # Cinco elementos: los tres chips y los logos de Asana y ClickUp
    chips = (panel.due_chip, panel.repeat_chip, panel.tags_chip,
             panel.asana_btn, panel.clickup_btn)
    assert panel.asana_btn.size() == panel.clickup_btn.size() == QSize(28, 28)
    need = sum(c.width() for c in chips) + 4 * 6
    actions = panel.submit_btn.parentWidget()
    # Caben los cinco, pero no además «Para hoy» + «Añadir»
    panel.resize(panel.width() - host.width() + need + 10, 200)
    qtbot.waitUntil(lambda: host.width() == need + 10)
    assert len({c.y() for c in chips}) == 1  # una sola fila, no apilados
    # «Añadir» en su propia fila, pegado al borde derecho
    right = panel.submit_btn.mapTo(host, QPoint(panel.submit_btn.width(), 0)).x()
    assert right == host.width()
    assert actions.y() > panel.tags_chip.y()
    # Con sitio de sobra, todo en una fila
    panel.resize(panel.width() + actions.width() + 40, 200)
    qtbot.waitUntil(lambda: actions.y() == panel.tags_chip.y())


def test_columnas_alineadas_con_las_tarjetas(qtbot):
    """El icono queda sobre el check de las tarjetas y el texto y los chips
    empiezan donde su título."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from ui.theme.qss import build_qss
    from ui.theme.tokens import tokens
    from ui.widgets.task_card import TaskCard
    fonts.init_fonts()
    host = QWidget()
    # La hoja de estilo de la app (fija el tamaño del check), solo aquí
    host.setStyleSheet(build_qss(tokens(), fonts.ui_family()))
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    card = TaskCard({'id': 1, 'name': "Tarea", 'tags': [], 'source': 'local'},
                    0, False, False, parent=host)
    lay.addWidget(card)
    add = TaskAddPanel(host)
    lay.addWidget(add)
    host.resize(520, 300)
    host.show()
    qtbot.waitExposed(host)
    add.focus_input()
    qtbot.waitUntil(lambda: add.options.height() >= add.due_chip.height())

    def x_in(w, ref, dx=0):
        return w.mapTo(ref, QPoint(dx, 0)).x()

    check_center = x_in(card.check, card) + card.check.width() / 2
    lead_center = x_in(add.lead, add.card) + add.lead.width() / 2
    assert abs(check_center - lead_center) <= 1
    title_x = x_in(card.name_label, card)
    text_x = x_in(add.name_edit, add.card) + 2  # margen fijo de QLineEdit
    assert abs(title_x - text_x) <= 1
    assert x_in(add.due_chip, add.card) == text_x


# ----------------------------------------- crear también en Asana o ClickUp
DESTS = [{'workspace_gid': 'W', 'project_gid': '', 'project_name': ''},
         {'workspace_gid': 'W', 'project_gid': 'P1', 'project_name': 'Marketing'},
         {'workspace_gid': 'W', 'project_gid': 'P2', 'project_name': 'Web'}]


@pytest.fixture
def changes(panel):
    calls = []
    panel.targets_changed.connect(calls.append)
    return calls


def _labels(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_logos_sin_configurar_atenuados_y_piden_ajustes(panel, emitted, qtbot):
    requested = []
    panel.setup_requested.connect(requested.append)
    for btn in (panel.asana_btn, panel.clickup_btn):
        assert btn.is_dimmed() and btn.isEnabled() and not btn.isChecked()
        assert btn.isCheckable() and btn.sizeHint() == btn.minimumSizeHint()
    assert panel.asana_btn.toolTip() == "Configura Asana en Ajustes"
    assert panel.clickup_btn.toolTip() == "Configura ClickUp en Ajustes"
    panel.asana_btn.click()
    panel.clickup_btn.click()
    assert requested == ['asana', 'clickup']
    assert not panel.asana_btn.isChecked() and not panel.clickup_btn.isChecked()
    # Un estado recordado sin configurar no viaja en el alta
    panel.set_target_state(DESTS[0], True)
    _focus(panel, qtbot)
    panel.name_edit.setText("Solo local")
    panel.submit()
    assert emitted == [("Solo local", [], "", "", {})]


def test_menu_de_asana_elige_el_destino(panel, emitted, changes, qtbot, monkeypatch):
    shown = []
    monkeypatch.setattr(panel, "_show_menu", lambda menu, anchor: shown.append((menu, anchor)))
    panel.set_integrations(DESTS, clickup_ready=False)
    assert not panel.asana_btn.is_dimmed()
    assert panel.asana_btn.toolTip() == "Crear también en Asana (elige dónde)"
    _focus(panel, qtbot)
    panel.asana_btn.click()                       # apagado: pregunta dónde
    (menu, anchor), = shown
    assert anchor is panel.asana_btn
    assert _labels(menu) == ["Mis tareas (sin proyecto)", "Marketing", "Web"]
    assert not any(a.property("current") for a in menu.actions())
    _action(menu, "W/P1").trigger()
    assert panel.asana_btn.isChecked()
    assert panel.asana_btn.toolTip() == "Crear también en Asana · Marketing"
    assert changes[-1] == {'asana': DESTS[1], 'clickup': False}
    # El borrador sigue «vacío» con el logo encendido: no fuerza el despliegue
    assert panel._draft_empty()
    panel.name_edit.setText("Campaña de otoño")
    panel.submit()
    assert emitted == [("Campaña de otoño", [], "", "", {'asana': DESTS[1]})]
    # Se queda encendido para la siguiente (también tras Escape)
    assert panel.asana_btn.isChecked()
    qtbot.keyClick(panel.name_edit, Qt.Key_Escape)
    assert panel.asana_target == DESTS[1]
    assert _action(panel.build_asana_menu(), "W/P1").property("current")
    # Encendido, un clic lo apaga (sin menú)
    panel.asana_btn.click()
    assert len(shown) == 1 and not panel.asana_btn.isChecked()
    assert changes[-1] == {'asana': None, 'clickup': False}
    menu.deleteLater()


def test_menu_de_asana_por_workspace(panel):
    dests = DESTS[:2] + [{'workspace_gid': 'W2', 'project_gid': '', 'project_name': ''},
                         {'workspace_gid': 'W2', 'project_gid': 'P7', 'project_name': 'Ventas'}]
    panel.set_integrations(dests, clickup_ready=False)
    menu = panel.build_asana_menu()
    assert _labels(menu) == ["Workspace W", "Mis tareas (sin proyecto)", "Marketing",
                             "Workspace W2", "Mis tareas (sin proyecto)", "Ventas"]
    headers = [a for a in menu.actions() if a.text().startswith("Workspace")]
    assert not any(h.isEnabled() for h in headers)
    assert sum(a.isSeparator() for a in menu.actions()) == 1
    _action(menu, "W2/").trigger()
    assert panel.targets == {'asana': dests[2]}


def test_con_repeticion_asana_queda_deshabilitado(panel, emitted, qtbot):
    panel.set_integrations(DESTS, clickup_ready=True)
    panel.set_target_state(DESTS[0], True)
    _focus(panel, qtbot)
    _action(panel.build_repeat_menu(), 'weekly').trigger()
    assert not panel.asana_btn.isEnabled()
    assert "no admite" in panel.asana_btn.toolTip()
    # ClickUp sí (se crea una vez); Asana no viaja
    panel.name_edit.setText("Regar")
    panel.submit()
    assert emitted == [("Regar", [], "weekly", "", {'clickup': True})]
    # Sin ciclo vuelve, encendido como estaba
    assert panel.asana_btn.isEnabled() and panel.asana_btn.isChecked()
    assert panel.targets == {'asana': DESTS[0], 'clickup': True}


def test_clickup_se_enciende_y_apaga(panel, changes):
    panel.set_integrations(None, clickup_ready=True)
    assert not panel.clickup_btn.is_dimmed() and panel.asana_btn.is_dimmed()
    panel.clickup_btn.click()
    assert panel.clickup_btn.isChecked() and panel.targets == {'clickup': True}
    assert "activado" in panel.clickup_btn.toolTip()
    panel.clickup_btn.click()
    assert not panel.clickup_btn.isChecked() and panel.targets == {}
    assert changes == [{'asana': None, 'clickup': True}, {'asana': None, 'clickup': False}]


def test_destino_que_ya_no_esta_apaga_asana(panel, changes):
    panel.set_integrations(DESTS, clickup_ready=False)
    panel.set_target_state({**DESTS[2], 'project_name': 'Nombre viejo'}, False)
    assert panel.asana_target == DESTS[2]          # con el nombre al día
    assert changes == []                           # restaurar no avisa
    panel.set_integrations(DESTS[:2], clickup_ready=False)   # «Web» desmarcado
    assert panel.asana_target is None and not panel.asana_btn.isChecked()
    assert changes == [{'asana': None, 'clickup': False}]


def test_logos_pintan_el_color_de_marca_al_encenderse(panel):
    from ui.theme.tokens import tokens
    t = tokens()
    panel.set_integrations(DESTS, clickup_ready=True)

    def logo_pixel(btn):
        # Logo de 14 px centrado en 28: el círculo de arriba del de Asana y
        # la punta del de ClickUp quedan bajo estos píxeles
        image = btn.grab().toImage()
        dpr = image.devicePixelRatio()
        x, y = 14, 11 if btn is panel.asana_btn else 9
        return image.pixelColor(int(x * dpr), int(y * dpr))

    def close_to(color, expected, tol=24):
        expected = QColor(expected)
        return all(abs(a - b) <= tol for a, b in zip(
            (color.red(), color.green(), color.blue()),
            (expected.red(), expected.green(), expected.blue())))

    assert close_to(logo_pixel(panel.asana_btn), t.fg_dim)
    panel.set_asana_target(DESTS[0])
    panel.set_clickup(True)
    assert close_to(logo_pixel(panel.asana_btn), t.asana)
    assert close_to(logo_pixel(panel.clickup_btn), t.clickup)


# ------------------------------------------------------- DataHandler.add_task
def test_add_task_persiste_el_vencimiento(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "data.json"
    path.write_text(json.dumps({'tasks': [], 'deleted_tasks': [], 'time_chunks': [],
                                'history': [], 'ui_snapshot': {}, 'settings': {}}),
                    encoding="utf-8")
    dh = DataHandler(path)
    with_due = dh.add_task("Con fecha", ["x"], source='local', recurring='weekly',
                           due="2026-11-02")
    without = dh.add_task("Sin fecha", [], source='local')
    by_id = {t['id']: t for t in dh.data['tasks']}
    assert by_id[with_due]['due'] == "2026-11-02"
    assert by_id[with_due]['recurring'] == 'weekly'
    assert 'due' not in by_id[without]
    # Persistido en disco
    saved = {t['id']: t for t in json.loads(path.read_text(encoding="utf-8"))['tasks']}
    assert saved[with_due]['due'] == "2026-11-02"
    # El plan de hoy entiende el formato
    from core.day_plan import due_local_date
    assert due_local_date(by_id[with_due]) == date(2026, 11, 2)
