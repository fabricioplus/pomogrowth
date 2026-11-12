"""Nombre y saludo personalizados + racha de días usando la app (pytest-qt,
offscreen). Datos sintéticos en un directorio temporal: nunca los reales.

    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_nombre_racha.py -q
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.data_handler import DataHandler  # noqa: E402
from ui import greeting  # noqa: E402
from ui.theme import fonts  # noqa: E402

TODAY = date(2026, 11, 2)
ID1, ID2 = 1764282734316, 1764282734317


def _plain(rich: str) -> str:
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setHtml(rich)
    return doc.toPlainText()


def _write(tmp_path: Path, **extra) -> Path:
    data = {'tasks': [], 'time_chunks': [], 'history': [], 'settings': {}}
    data.update(extra)
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _dh(tmp_path: Path, days=None, **extra) -> DataHandler:
    """DataHandler con días de uso dados y el sembrado ya hecho."""
    settings = {'activity_days_seeded': True}
    if days is not None:
        extra['activity_days'] = [d.isoformat() for d in days]
    return DataHandler(_write(tmp_path, settings=settings, **extra))


def _span(last: date, n: int):
    return [last - timedelta(days=i) for i in range(n)]


# ------------------------------------------------------------------ saludo
@pytest.mark.parametrize("hour, word", [
    (4, "Buenas noches"), (5, "Buenos días"), (11, "Buenos días"),
    (12, "Buenas tardes"), (19, "Buenas tardes"), (20, "Buenas noches"),
    (23, "Buenas noches")])
def test_greeting_por_franjas(hour, word):
    now = datetime(2026, 11, 2, hour, 30)
    assert greeting.greeting_text(now) == word          # sin nombre: el de siempre
    assert greeting.greeting_text(now, "  ") == word
    rich = greeting.greeting_text(now, "Fabricio", "#34e0a1")
    assert _plain(rich) == f"{word}, Fabricio"
    assert "<b style='color:#34e0a1'>Fabricio</b>" in rich
    # La bienvenida sigue usando las mismas franjas
    from ui.windows.welcome_farewell import _greeting
    assert _greeting(now) == word


def test_greeting_escapa_el_nombre(qapp):
    rich = greeting.greeting_text(datetime(2026, 11, 2, 9), "<i>Ana</i> & Co")
    assert "<i>" not in rich and "&lt;i&gt;Ana&lt;/i&gt; &amp; Co" in rich
    assert _plain(rich) == "Buenos días, <i>Ana</i> & Co"


def test_nombre_limpio_y_textos_de_racha():
    assert greeting.clean_name("  Ana   María \n") == "Ana María"
    assert len(greeting.clean_name("x" * 80)) == greeting.NAME_MAX_LEN
    assert greeting.first_name(" Fabricio Pérez ") == "Fabricio"
    assert greeting.streak_text(1) == "1 día seguido"
    assert greeting.streak_text(12) == "12 días seguidos"
    assert greeting.streak_tooltip(1) == "Mejor racha: 1 día"
    assert greeting.streak_tooltip(30) == "Mejor racha: 30 días"
    assert greeting.valid_streak((0, 4)) is None
    assert greeting.valid_streak(None) is None
    assert greeting.valid_streak((3, 2)) == (3, 3)


# ------------------------------------------------------------------- racha
def test_racha_de_dias_consecutivos(tmp_path):
    dh = _dh(tmp_path, _span(TODAY, 5))
    assert dh.get_usage_streak(TODAY) == (5, 5)


def test_un_hueco_rompe_la_racha_y_queda_la_mejor(tmp_path):
    old = _span(TODAY - timedelta(days=10), 7)          # 7 seguidos hace días
    recent = _span(TODAY, 3)                             # hueco y 3 hasta hoy
    dh = _dh(tmp_path, old + recent)
    assert dh.get_usage_streak(TODAY) == (3, 7)
    # Sin hoy aún apuntado, cuenta desde ayer (no marca 0 por la mañana)
    assert dh.get_usage_streak(TODAY + timedelta(days=1)) == (3, 7)
    # Dos días sin abrirla: la racha actual cae a 0, la mejor se queda
    assert dh.get_usage_streak(TODAY + timedelta(days=2)) == (0, 7)


def test_marcar_hoy_guarda_ordenado_y_sin_repetir(tmp_path):
    dh = _dh(tmp_path, [TODAY - timedelta(days=1)])
    assert dh.mark_active_day(TODAY) is True
    assert dh.mark_active_day(TODAY) is False            # ya estaba: no escribe
    assert dh.get_activity_days() == [(TODAY - timedelta(days=1)).isoformat(),
                                      TODAY.isoformat()]
    saved = json.loads(dh.filepath.read_text(encoding="utf-8"))
    assert saved['activity_days'] == dh.get_activity_days()
    assert DataHandler(dh.filepath).get_usage_streak(TODAY) == (2, 2)


def test_sembrado_unico_con_tramos_e_historial(tmp_path):
    chunks = [{'duration': 60, 'task_id': 1, 'type': 'pomodoro',
               'timestamp': f"{(TODAY - timedelta(days=d)).isoformat()}T10:00:00"}
              for d in (1, 2)]
    history = [{'ts': f"{(TODAY - timedelta(days=3)).isoformat()}T08:00:00",
                'action': 'add_task', 'payload': {}},
               {'ts': 'basura', 'action': 'x', 'payload': {}}]
    dh = DataHandler(_write(tmp_path, time_chunks=chunks, history=history))
    assert dh.get_activity_days() == []
    dh.mark_active_day(TODAY)
    assert dh.get_usage_streak(TODAY) == (4, 4)
    assert dh.load_settings()['activity_days_seeded'] is True
    # Una sola vez: lo que se borre después no vuelve a sembrarse
    dh.data['activity_days'] = [TODAY.isoformat()]
    dh.mark_active_day(TODAY)
    assert dh.get_activity_days() == [TODAY.isoformat()]


def test_se_recorta_a_los_ultimos_400_dias(tmp_path):
    raw = [d.isoformat() for d in _span(TODAY - timedelta(days=1), 450)] + ['basura']
    dh = _dh(tmp_path, activity_days=raw)
    dh.mark_active_day(TODAY)
    days = dh.get_activity_days()
    assert len(days) == DataHandler.ACTIVITY_DAYS_MAX == 400
    assert days[-1] == TODAY.isoformat() and days == sorted(days)
    assert dh.get_usage_streak(TODAY) == (400, 400)


def test_datos_corruptos_no_rompen(tmp_path):
    dh = DataHandler(_write(tmp_path, activity_days="nada"))
    assert dh.get_activity_days() == [] and dh.get_usage_streak(TODAY) == (0, 0)


def test_borrar_todo_reinicia_la_racha_con_hoy(tmp_path):
    dh = _dh(tmp_path, _span(date.today(), 6))
    dh.clear_data()
    assert dh.get_activity_days() == [date.today().isoformat()]
    assert dh.get_usage_streak() == (1, 1)
    dh.mark_active_day()                                 # no vuelve a sembrar
    assert dh.get_activity_days() == [date.today().isoformat()]


# ------------------------------------------------------------- controlador
@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    tasks = [{'id': ID1, 'name': 'Beta', 'completed': False, 'tags': [], 'source': 'local',
              'asana_id': None, 'due': None},
             {'id': ID2, 'name': 'Alfa', 'completed': False, 'tags': [], 'source': 'local',
              'asana_id': None, 'due': None}]
    dh = DataHandler(_write(tmp_path, tasks=tasks))
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


def _set_name(ctrl, name, asked=True):
    s = ctrl.data_handler.load_settings()
    s['user_name'] = name
    s['user_name_asked'] = asked
    ctrl.data_handler.save_settings(s)


def test_arrancar_apunta_hoy(ctrl):
    assert ctrl.data_handler.get_activity_days() == [date.today().isoformat()]
    assert ctrl._usage_streak() == (1, 1)


def test_cambio_de_dia_apunta_el_dia_nuevo(ctrl, monkeypatch):
    import ui.controllers.app_controller as mod
    tomorrow = date.today() + timedelta(days=1)

    class _Tomorrow(date):
        @classmethod
        def today(cls):
            return tomorrow
    monkeypatch.setattr(mod, "date", _Tomorrow)
    ctrl._tick()
    assert ctrl.data_handler.get_activity_days()[-1] == tomorrow.isoformat()
    assert ctrl.data_handler.get_usage_streak(tomorrow) == (2, 2)


def test_preguntar_nombre_y_guardar(ctrl, monkeypatch):
    from ui.dialogs import branded_dialogs
    calls = []
    monkeypatch.setattr(branded_dialogs, "ask_user_name",
                        lambda parent, *a: calls.append(1) or "Fabricio")
    ctrl._ask_user_name_if_needed()
    s = ctrl.data_handler.load_settings()
    assert s['user_name'] == "Fabricio" and s['user_name_asked'] is True
    ctrl._ask_user_name_if_needed()
    assert calls == [1]                                  # no se repite


def test_omitir_el_nombre_no_vuelve_a_preguntar(ctrl, monkeypatch):
    from ui.dialogs import branded_dialogs
    calls = []
    monkeypatch.setattr(branded_dialogs, "ask_user_name",
                        lambda parent, *a: calls.append(1) or "")
    ctrl._ask_user_name_if_needed()
    ctrl._ask_user_name_if_needed()
    s = ctrl.data_handler.load_settings()
    assert calls == [1] and s['user_name'] == '' and s['user_name_asked'] is True
    assert ctrl._welcome_lines()[0] == "Tienes 2 tareas pendientes."


def test_el_nombre_se_pregunta_antes_del_plan(ctrl, monkeypatch):
    """Encadenado: primero «¿Cómo te llamas?», luego «Planifica tu día» (con
    el nombre recién guardado y la racha), sin solaparse."""
    from ui.dialogs import branded_dialogs
    import ui.dialogs.day_plan_dialog as plan_mod
    order, seen = [], {}
    monkeypatch.setattr(branded_dialogs, "ask_user_name",
                        lambda parent, *a: order.append('nombre') or "Fabricio")

    def pick(parent, tasks, suggestions, preselected, **kwargs):
        order.append('plan')
        seen.update(kwargs)
        return None
    monkeypatch.setattr(plan_mod, "pick_day_plan", pick)
    ctrl._welcome_shown = False
    ctrl._show_welcome()
    assert order == ['nombre', 'plan']
    assert seen == {'user_name': "Fabricio", 'streak': (1, 1)}


def test_la_bienvenida_recibe_nombre_y_racha(ctrl, monkeypatch):
    from ui.windows import welcome_farewell
    _set_name(ctrl, "Fabricio")
    ctrl.data_handler.set_day_plan([ID1])                 # con plan: bienvenida
    seen = {}

    class _Welcome:
        def __init__(self, parent, lines, **kwargs):
            seen.update(kwargs, lines=lines)

        def exec(self):
            return 1
    monkeypatch.setattr(welcome_farewell, "WelcomeDialog", _Welcome)
    ctrl._welcome_shown = False
    ctrl._show_welcome()
    assert seen['user_name'] == "Fabricio" and seen['streak'] == (1, 1)
    assert seen['lines'][0] == "Fabricio, tienes 2 tareas pendientes."


def test_despedida_con_nombre_desde_el_controlador(ctrl, monkeypatch):
    from ui.windows import welcome_farewell
    _set_name(ctrl, "Fabricio Pérez")
    farewells = []
    monkeypatch.setattr(welcome_farewell, "FarewellWindow",
                        lambda on_done, **kw: farewells.append(kw))
    monkeypatch.setattr(ctrl.focus_mode, "cleanup_system_blocks", lambda: False)
    ctrl.quit_with_farewell()
    assert farewells == [{'user_name': "Fabricio Pérez"}]


def test_campo_de_nombre_en_ajustes(ctrl, qtbot):
    from PySide6.QtWidgets import QLabel
    from ui.dialogs.settings_dialog import SettingsDialog
    _set_name(ctrl, "Fabricio", asked=False)
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    assert dlg.ed_user_name.text() == "Fabricio"
    # «Perfil» abre la pestaña «Timer y ventana», antes del ciclo pomodoro
    page = dlg.tabs.widget(0)
    texts = [lbl.text() for lbl in page.findChildren(QLabel)]
    assert dlg.tabs.tabText(0) == "Timer y ventana"
    assert texts.index("Perfil") < texts.index("Tu nombre") < texts.index("Ciclo pomodoro")
    assert page.isAncestorOf(dlg.ed_user_name)
    assert dlg.ed_user_name.maxLength() == greeting.NAME_MAX_LEN
    dlg.ed_user_name.setText("  Ana   María ")
    dlg.accept_settings()
    s = ctrl.data_handler.load_settings()
    assert s['user_name'] == "Ana María" and s['user_name_asked'] is True
    dlg.reject()


def test_estadisticas_con_y_sin_nombre(ctrl, qtbot):
    from ui.dialogs.stats_dialog import StatsDialog
    dlg = StatsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    assert dlg.greeting_label is None and dlg._title_label.text() == "Estadísticas"
    dlg.reject()
    _set_name(ctrl, "Fabricio")
    dlg = StatsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    assert _plain(dlg.greeting_label.text()) == "Así va tu progreso, Fabricio"
    assert dlg._title_label.text() == "Estadísticas"
    dlg.reject()


# ------------------------------------------------------------------ diálogos
def _run_modal(qtbot, interact, call):
    """Lanza `call()` (un diálogo modal) y, ya abierto, `interact(dlg)`."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ui.dialogs.branded_dialogs import BrandedDialog
    done = []

    def step():
        dlgs = [w for w in QApplication.topLevelWidgets()
                if isinstance(w, BrandedDialog) and w.isVisible()]
        if not dlgs:
            QTimer.singleShot(20, step)
            return
        interact(dlgs[-1])
        done.append(1)
    QTimer.singleShot(0, step)
    result = call()
    assert done
    return result


def test_dialogo_del_nombre_guardar_con_enter(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit, QPushButton
    from ui.dialogs.branded_dialogs import ask_user_name
    fonts.init_fonts()

    def interact(dlg):
        assert dlg._title_label.text() == "¿Cómo te llamas?"
        edit = dlg.findChild(QLineEdit)
        save = [b for b in dlg.findChildren(QPushButton) if b.text() == "Guardar"][0]
        assert not save.isEnabled()                      # vacío no guarda
        qtbot.keyClick(edit, Qt.Key_Return)
        assert dlg.isVisible()
        qtbot.keyClicks(edit, "  Fabricio ")
        assert save.isEnabled()
        qtbot.keyClick(edit, Qt.Key_Return)
    assert _run_modal(qtbot, interact, lambda: ask_user_name(None)) == "Fabricio"


def test_dialogo_del_nombre_omitir(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit, QPushButton
    from ui.dialogs.branded_dialogs import ask_user_name
    fonts.init_fonts()

    def interact(dlg):
        qtbot.keyClicks(dlg.findChild(QLineEdit), "Fabricio")
        skip = [b for b in dlg.findChildren(QPushButton) if b.text() == "Omitir"][0]
        qtbot.mouseClick(skip, Qt.LeftButton)
    assert _run_modal(qtbot, interact, lambda: ask_user_name(None)) == ""


def test_bienvenida_sin_nombre_ni_racha(qtbot):
    from ui.windows.welcome_farewell import WelcomeDialog, _greeting
    fonts.init_fonts()
    dlg = WelcomeDialog(None, ["Tienes 3 tareas pendientes."])
    qtbot.addWidget(dlg)
    assert dlg._title_label.text() == _greeting(datetime.now())
    assert dlg.streak_badge is None


def test_bienvenida_con_nombre_y_racha(qtbot):
    from PySide6.QtWidgets import QLabel
    from ui.windows.welcome_farewell import WelcomeDialog, _greeting
    fonts.init_fonts()
    dlg = WelcomeDialog(None, ["Hoyos, tienes 3 tareas pendientes.", "Ánimo."],
                        user_name="Hoyos", streak=(12, 30))
    qtbot.addWidget(dlg)
    assert _plain(dlg._title_label.text()) == f"{_greeting(datetime.now())}, Hoyos"
    assert dlg.streak_badge.text() == "12 días seguidos"
    assert dlg.streak_badge.toolTip() == "Mejor racha: 30 días"
    # La línea con nombre sigue siendo la fila «Pendientes» (no «Vence hoy»)
    texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
    assert "Pendientes" in texts and "Vencen hoy" not in texts
    assert not any("Hoyos," in t for t in texts)


@pytest.mark.parametrize("name", ["🦊 Ana2", "🌸 Hoyos"])
def test_bienvenida_nombre_con_emoji_delante(qtbot, name):
    """El emoji del principio no impide quitar el nombre: ni sus cifras ni
    su «hoy» cambian la cifra o la fila de «Pendientes»."""
    from PySide6.QtWidgets import QLabel
    from ui.windows.welcome_farewell import WelcomeDialog
    fonts.init_fonts()
    dlg = WelcomeDialog(None, [f"{name}, tienes 7 tareas pendientes.", "Ánimo."],
                        user_name=name)
    qtbot.addWidget(dlg)
    texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
    assert "Pendientes" in texts and "Vencen hoy" not in texts
    assert "7" in texts and "2" not in texts
    assert not any(name.split()[-1] + "," in t for t in texts)


def test_plan_sin_nombre_ni_racha(qtbot):
    from ui.dialogs.day_plan_dialog import DayPlanDialog
    fonts.init_fonts()
    dlg = DayPlanDialog(None, [], [], [], today=TODAY)
    qtbot.addWidget(dlg)
    assert dlg._title.text() == "Planifica tu día"
    assert dlg._date_label.text() == "Lunes 2 de noviembre"
    assert dlg.streak_badge is None


def test_plan_con_nombre_y_racha(qtbot):
    from ui.dialogs.day_plan_dialog import DayPlanDialog
    from ui.windows.welcome_farewell import _greeting
    fonts.init_fonts()
    dlg = DayPlanDialog(None, [], [], [], today=TODAY, user_name="<b>Fabri</b>",
                        streak=(1, 1))
    qtbot.addWidget(dlg)
    assert _plain(dlg._title.text()) == f"{_greeting(datetime.now())}, <b>Fabri</b>"
    assert dlg._date_label.text() == "Planifica tu día · Lunes 2 de noviembre"
    assert dlg.streak_badge.text() == "1 día seguido"
    assert dlg.streak_badge.toolTip() == "Mejor racha: 1 día"
    assert dlg.card.isAncestorOf(dlg.streak_badge)


def test_despedida_con_y_sin_nombre(qtbot):
    from ui.windows.welcome_farewell import FarewellWindow
    fonts.init_fonts()
    fw = FarewellWindow(lambda: None)
    qtbot.addWidget(fw)
    assert fw.title_label.text() == "Hasta pronto"
    fw.close_now()
    fw = FarewellWindow(lambda: None, user_name="Fabricio Pérez")
    qtbot.addWidget(fw)
    assert _plain(fw.title_label.text()) == "Hasta pronto, Fabricio"
    fw.close_now()
