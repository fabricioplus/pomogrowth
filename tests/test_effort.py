"""Valoración de esfuerzo (1-5) al completar una tarea: popup, datos,
tarjeta, ajuste, estadísticas y exportación.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_effort.py -q
Datos sintéticos en un directorio temporal: nunca los reales ni la red.
"""
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402

import core.data_handler as data_handler_mod  # noqa: E402
from core import effort_stats  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from ui.theme import fonts  # noqa: E402

# IDs como los reales (timestamps en ms, > 2^31)
LOCAL, FIJA, ASANA, HECHA = 1764282734401, 1764282734402, 1764282734403, 1764282734404
GID = '1200000000000777'


def _write_data(tmp_path: Path, settings=None, **extra) -> Path:
    tasks = [
        {'id': LOCAL, 'name': 'Redactar informe', 'completed': False, 'tags': ['docs'],
         'source': 'local', 'asana_id': None, 'due': None},
        {'id': FIJA, 'name': 'Revisar correo', 'completed': False, 'tags': [],
         'source': 'local', 'asana_id': None, 'due': None, 'recurring': 'daily'},
        {'id': ASANA, 'name': 'Tarea de Asana', 'completed': False, 'tags': [],
         'source': 'asana', 'asana_id': GID, 'due': None,
         'projects': [{'gid': 'P1', 'name': 'Web'}]},
        {'id': HECHA, 'name': 'Ya hecha', 'completed': True, 'tags': [],
         'source': 'local', 'asana_id': None, 'due': None,
         'completed_at': '2026-10-01T10:00:00', 'effort': 3},
    ]
    data = {'tasks': tasks, 'deleted_tasks': [], 'time_chunks': [], 'history': [],
            'ui_snapshot': {},
            'settings': {'task_sort_mode': 'manual', **(settings or {})}, **extra}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _no_network(monkeypatch):
    def offline(*_a, **_k):
        raise AssertionError("un test no sale a la red")
    for name in ('get', 'put', 'post'):
        monkeypatch.setattr(data_handler_mod.requests, name, offline)


def _make_ctrl(qtbot, tmp_path, monkeypatch, settings=None, **extra):
    monkeypatch.chdir(tmp_path)
    _no_network(monkeypatch)
    fonts.init_fonts()
    dh = DataHandler(_write_data(tmp_path, settings, **extra))
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
def toasts(monkeypatch):
    from ui.widgets import toast
    seen = []
    monkeypatch.setattr(toast, "show_success",
                        lambda parent, msg, duration=3000: seen.append(msg))
    return seen


@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch):
    """Controlador con la pregunta de esfuerzo encendida."""
    return _make_ctrl(qtbot, tmp_path, monkeypatch, {'ask_effort_on_complete': True})


def _task(ctrl, task_id):
    return ctrl._task_by_id(task_id)


def _ratings(ctrl):
    return ctrl.data_handler.data.get('effort_ratings', [])


def _popup(ctrl):
    from ui.dialogs.effort_dialog import EffortDialog
    dlg = getattr(ctrl, '_effort_dialog', None)
    assert isinstance(dlg, EffortDialog)
    return dlg


# ---------------------------------------------------------------- popup
def test_completar_abre_el_popup_en_lugar_del_toast(ctrl, toasts):
    ctrl.set_task_filter('all')
    ctrl.toggle_task_completion(LOCAL)
    dlg = _popup(ctrl)
    assert dlg.isVisible()
    assert not dlg.isModal()  # no bloquea el timer ni los tests
    assert dlg._title_label.text() == "¿Cuánto esfuerzo te costó?"
    assert dlg._subtitle_label.text() == "Redactar informe"
    assert len(dlg.stars.stars) == 5
    assert toasts == []  # el popup sustituye a «Tarea completada»


def test_tecla_4_guarda_y_cierra(ctrl, qtbot):
    ctrl.set_task_filter('all')
    ctrl.toggle_task_completion(LOCAL)
    dlg = _popup(ctrl)
    qtbot.keyClick(dlg, Qt.Key_4)
    assert not dlg.isVisible()
    assert ctrl._effort_dialog is None
    entry = _ratings(ctrl)[-1]
    assert entry['rating'] == 4
    assert entry['task_id'] == LOCAL
    assert entry['task_name'] == 'Redactar informe'
    assert entry['tags'] == ['docs']
    assert entry['recurring'] is None
    assert entry['project'] == ''
    assert _task(ctrl, LOCAL)['effort'] == 4
    # La tarjeta (completada, visible con el filtro «Todas») muestra «★ 4»
    card = ctrl.tasks_panel._cards[LOCAL]
    assert card.effort_label.text() == "4"
    assert card._effort_box.toolTip() == "Esfuerzo 4/5"
    assert "Esfuerzo 4/5" in card.toolTip()
    # Y queda en disco
    on_disk = json.loads(ctrl.data_handler.filepath.read_text(encoding="utf-8"))
    assert on_disk['effort_ratings'][-1]['rating'] == 4


def test_clic_en_la_estrella_2(ctrl, qtbot):
    ctrl.toggle_task_completion(LOCAL)
    dlg = _popup(ctrl)
    qtbot.mouseClick(dlg.stars.stars[1], Qt.LeftButton)
    assert not dlg.isVisible()
    assert _ratings(ctrl)[-1]['rating'] == 2
    assert _task(ctrl, LOCAL)['effort'] == 2


def test_vista_previa_al_pasar_el_raton(ctrl, qtbot):
    ctrl.toggle_task_completion(LOCAL)
    dlg = _popup(ctrl)
    dlg.stars.stars[2].hovered.emit(3)
    assert dlg.stars.preview == 3
    dlg.stars.set_preview(0)
    assert dlg.stars.preview == 0
    assert _ratings(ctrl) == []  # pasar el ratón no guarda nada
    dlg.reject()


@pytest.mark.parametrize("how", ["esc", "omitir", "velo"])
def test_esc_omitir_o_clic_fuera_no_guardan(ctrl, qtbot, how):
    ctrl.toggle_task_completion(LOCAL)
    dlg = _popup(ctrl)
    if how == "esc":
        qtbot.keyClick(dlg, Qt.Key_Escape)
    elif how == "omitir":
        assert dlg.skip_button.text() == "Omitir"
        qtbot.mouseClick(dlg.skip_button, Qt.LeftButton)
    else:
        assert dlg._overlay is not None
        qtbot.mouseClick(dlg._overlay, Qt.LeftButton)
    assert not dlg.isVisible()
    assert ctrl._effort_dialog is None
    assert _ratings(ctrl) == []
    assert 'effort' not in _task(ctrl, LOCAL)
    assert _task(ctrl, LOCAL)['completed'] is True


def test_el_velo_se_retira_sin_exec(ctrl, qtbot):
    """popup() no pasa por exec(): el velo se quita igual al cerrar."""
    ctrl.toggle_task_completion(LOCAL)
    dlg = _popup(ctrl)
    overlay = dlg._overlay
    assert overlay is not None and overlay.isVisible()
    qtbot.keyClick(dlg, Qt.Key_5)
    assert dlg._overlay is None
    assert not overlay.isVisible()


def test_los_dialogos_con_exec_siguen_retirando_el_velo(ctrl, qtbot):
    from PySide6.QtCore import QTimer
    from ui.dialogs.branded_dialogs import BrandedDialog
    dlg = BrandedDialog(ctrl.window, "Prueba")
    dlg.add_buttons([("Cerrar", "ghost", 0), ("Vale", "accent", 1)])
    overlay = dlg._overlay
    assert overlay is not None
    QTimer.singleShot(50, lambda: dlg.done(1))
    assert dlg.exec() == 1
    assert dlg._overlay is None
    assert not overlay.isVisible()


def test_reabrir_no_pregunta_y_quita_la_copia(ctrl, qtbot):
    ctrl.set_task_filter('all')
    ctrl.toggle_task_completion(LOCAL)
    qtbot.keyClick(_popup(ctrl), Qt.Key_4)
    ctrl.toggle_task_completion(LOCAL)  # reabrir
    assert getattr(ctrl, '_effort_dialog', None) is None
    assert 'effort' not in _task(ctrl, LOCAL)
    assert len(_ratings(ctrl)) == 1  # el histórico se conserva
    card = ctrl.tasks_panel._cards[LOCAL]
    assert not card._effort_box.isVisible()


def test_tarea_ya_completada_reabierta_pierde_la_estrella(ctrl):
    ctrl.set_task_filter('all')
    card = ctrl.tasks_panel._cards[HECHA]
    assert card.effort_label.text() == "3"  # al construir la tarjeta
    ctrl.toggle_task_completion(HECHA)
    assert 'effort' not in _task(ctrl, HECHA)


def test_fija_se_valora_en_cada_ciclo(ctrl, qtbot):
    dh = ctrl.data_handler
    ctrl.toggle_task_completion(FIJA)
    qtbot.keyClick(_popup(ctrl), Qt.Key_2)
    assert _ratings(ctrl)[-1]['recurring'] == 'daily'
    # Pasa el día: reopen_recurring_tasks la reabre (sin preguntar nada)
    _task(ctrl, FIJA)['completed_at'] = (datetime.now() - timedelta(days=1)).isoformat()
    assert dh.reopen_recurring_tasks() == 1
    assert getattr(ctrl, '_effort_dialog', None) is None
    assert 'effort' not in _task(ctrl, FIJA)
    ctrl.refresh_task_list()
    ctrl.toggle_task_completion(FIJA)
    qtbot.keyClick(_popup(ctrl), Qt.Key_5)
    assert [r['rating'] for r in _ratings(ctrl) if r['task_id'] == FIJA] == [2, 5]
    assert _task(ctrl, FIJA)['effort'] == 5


def test_tiempo_de_foco_desde_el_ultimo_completado(ctrl, qtbot):
    """focus_s suma los pomodoros de la tarea desde su completado anterior."""
    dh = ctrl.data_handler
    old = (datetime.now() - timedelta(days=2)).isoformat()
    dh.data['time_chunks'] += [
        {'duration': 1500, 'task_id': FIJA, 'timestamp': old, 'type': 'pomodoro'},
        {'duration': 300, 'task_id': FIJA, 'timestamp': old, 'type': 'break'},
        {'duration': 600, 'task_id': LOCAL, 'timestamp': old, 'type': 'pomodoro'},
    ]
    ctrl.toggle_task_completion(FIJA)
    qtbot.keyClick(_popup(ctrl), Qt.Key_3)
    assert _ratings(ctrl)[-1]['focus_s'] == 1500
    # Segundo ciclo: solo cuenta lo trabajado después
    _task(ctrl, FIJA)['completed_at'] = (datetime.now() - timedelta(days=1)).isoformat()
    dh.reopen_recurring_tasks()
    dh.data['time_chunks'].append({'duration': 900, 'task_id': FIJA, 'type': 'pomodoro',
                                   'timestamp': datetime.now().isoformat()})
    ctrl.refresh_task_list()
    ctrl.toggle_task_completion(FIJA)
    qtbot.keyClick(_popup(ctrl), Qt.Key_3)
    assert _ratings(ctrl)[-1]['focus_s'] == 900


def test_tramo_final_no_cuenta_en_el_ciclo_siguiente(ctrl, qtbot):
    """Completar la fija con la sesión en curso cierra el tramo ANTES del
    completado: cuenta en este ciclo y no vuelve a sumarse en el siguiente."""
    ctrl.select_task(FIJA)
    ctrl.toggle_timer()
    assert ctrl.model.is_session_in_progress()
    # Reloj real (el de las marcas del historial): el tramo empezó hace
    # 10 min y termina ahora, al completar
    ctrl.model.start_timestamp -= 600
    ctrl.toggle_task_completion(FIJA)
    qtbot.keyClick(_popup(ctrl), Qt.Key_3)
    assert _ratings(ctrl)[-1]['focus_s'] == pytest.approx(600, abs=2)
    # Reabierta a mano y completada otra vez sin trabajar: nada de foco
    ctrl.toggle_task_completion(FIJA)
    assert not _task(ctrl, FIJA)['completed']
    ctrl.toggle_task_completion(FIJA)
    qtbot.keyClick(_popup(ctrl), Qt.Key_3)
    assert [r['task_id'] for r in _ratings(ctrl)] == [FIJA, FIJA]
    assert _ratings(ctrl)[-1]['focus_s'] == 0


def test_segunda_tarea_con_el_popup_abierto(ctrl, qtbot):
    ctrl.set_task_filter('all')
    ctrl.toggle_task_completion(LOCAL)
    first = _popup(ctrl)
    ctrl.toggle_task_completion(FIJA)
    second = _popup(ctrl)
    assert second is not first
    assert not first.isVisible()  # el anterior se cerró como omitido
    assert _ratings(ctrl) == []
    qtbot.keyClick(second, Qt.Key_1)
    assert [(r['task_id'], r['rating']) for r in _ratings(ctrl)] == [(FIJA, 1)]
    assert 'effort' not in _task(ctrl, LOCAL)


def test_tarea_de_asana_pregunta_y_no_manda_el_esfuerzo(ctrl, qtbot):
    ctrl.toggle_task_completion(ASANA)
    qtbot.keyClick(_popup(ctrl), Qt.Key_4)
    entry = _ratings(ctrl)[-1]
    assert entry['task_id'] == ASANA and entry['project'] == 'Web'
    outbox = ctrl.data_handler.data['asana_outbox']
    assert outbox, "completar una tarea de Asana se encola"
    for op in outbox:
        assert 'effort' not in (op.get('fields') or {})
        assert 'esfuerzo' not in str(op.get('text', '')).lower()
    assert all(op.get('fields') == {'completed': True}
               for op in outbox if op.get('type') == 'update')


def test_rango_validado(ctrl):
    dh = ctrl.data_handler
    for bad in (0, 6, -1, 2.5, '3', True):
        with pytest.raises(ValueError):
            dh.add_effort_rating(LOCAL, bad)
    assert dh.add_effort_rating(999, 3) is None  # tarea inexistente
    assert dh.data['effort_ratings'] == []


# ------------------------------------------- caminos que NO preguntan
def test_merge_y_marcas_de_asana_no_abren_el_popup(ctrl):
    dh = ctrl.data_handler
    dh.merge_asana_tasks([{'gid': GID, 'name': 'Tarea de Asana', 'completed': True,
                           'tags': []}])
    assert _task(ctrl, ASANA)['completed'] is True
    _task(ctrl, ASANA)['completed'] = False
    assert dh.apply_asana_completed_marks({GID}) == 1
    assert getattr(ctrl, '_effort_dialog', None) is None
    assert _ratings(ctrl) == []


def test_sin_el_ajuste_sale_el_toast_de_siempre(qtbot, tmp_path, monkeypatch, toasts):
    ctrl = _make_ctrl(qtbot, tmp_path, monkeypatch, {'ask_effort_on_complete': False})
    ctrl.toggle_task_completion(LOCAL)
    assert getattr(ctrl, '_effort_dialog', None) is None
    assert toasts and toasts[-1].startswith("Tarea completada")


def test_por_defecto_pregunta(qtbot, tmp_path, monkeypatch):
    """Sin el ajuste guardado se pregunta (el conftest lo apaga para el resto)."""
    from ui.controllers.app_controller import AppController
    monkeypatch.setattr(AppController, "ASK_EFFORT_DEFAULT", True)
    ctrl = _make_ctrl(qtbot, tmp_path, monkeypatch)
    assert 'ask_effort_on_complete' not in ctrl.data_handler.load_settings()
    ctrl.toggle_task_completion(LOCAL)
    _popup(ctrl).reject()


def test_datos_viejos_sin_lista_y_lista_corrupta(tmp_path):
    dh = DataHandler(_write_data(tmp_path))
    assert dh.data['effort_ratings'] == []
    path = _write_data(tmp_path, effort_ratings="roto")
    assert DataHandler(path).data['effort_ratings'] == []


# ---------------------------------------------------------------- ajustes
def test_ajuste_en_timer_y_ventana(ctrl, qtbot):
    from PySide6.QtWidgets import QLabel
    from ui.dialogs.settings_dialog import SettingsDialog
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    assert dlg.chk_ask_effort.isChecked() is True
    labels = [lbl.text() for lbl in dlg.tabs.widget(0).findChildren(QLabel)]
    assert "Tareas" in labels
    assert "Preguntar el esfuerzo al completar una tarea" in labels
    # «Tareas» va al final de la pestaña
    assert labels.index("Tareas") > labels.index("Ventana")
    dlg.chk_ask_effort.setChecked(False)
    dlg.accept_settings()
    assert ctrl.data_handler.load_settings()['ask_effort_on_complete'] is False
    assert ctrl.effort_prompt_enabled() is False
    dlg.deleteLater()


# ------------------------------------------------------- core/effort_stats
def _r(rating, project='', focus_s=0, when='2026-10-20T10:00:00'):
    return {'task_id': 1, 'rating': rating, 'timestamp': when, 'task_name': 't',
            'project': project, 'tags': [], 'recurring': None, 'focus_s': focus_s}


def test_effort_stats_basico():
    ratings = [_r(1, 'Web', 600), _r(3, 'Web', 1200), _r(5, '', 0), _r(4, 'App', 2400),
               {'rating': 9}, {'rating': True}, 'basura']
    assert effort_stats.average(ratings) == pytest.approx(13 / 4)
    assert effort_stats.average([]) is None
    assert effort_stats.distribution(ratings) == [1, 0, 1, 1, 1]
    assert effort_stats.average_by_project(ratings) == [
        ('Web', 2.0, 2), ('Sin proyecto', 5.0, 1), ('App', 4.0, 1)]
    # (10 + 20 + 40) min / (1 + 3 + 4) puntos; la de 0 s no cuenta
    assert effort_stats.focus_minutes_per_point(ratings) == pytest.approx(70 / 8)
    assert effort_stats.focus_minutes_per_point([_r(3)]) is None
    s = effort_stats.summary(ratings)
    assert s['count'] == 4 and s['distribution'] == [1, 0, 1, 1, 1]


def test_effort_stats_periodo():
    ratings = [_r(2, when='2026-10-01T09:00:00'), _r(4, when='2026-10-20T09:00:00'),
               _r(5, when='fecha rota')]
    assert len(effort_stats.ratings_since(ratings, None)) == 3
    since = datetime(2026, 10, 10)
    assert [r['rating'] for r in effort_stats.ratings_since(ratings, since)] == [4]


# ------------------------------------------------- estadísticas y exportación
def _stats_ctrl(qtbot, tmp_path, monkeypatch):
    now = datetime.now()
    ratings = [
        dict(_r(4, 'Web', 3000), timestamp=now.isoformat(), task_name='Hoy uno'),
        dict(_r(2, '', 600), timestamp=now.isoformat(), task_name='Hoy dos',
             tags=['a', 'b'], recurring='daily'),
        dict(_r(5, 'Web', 0), timestamp=(now - timedelta(days=20)).isoformat(),
             task_name='Hace veinte días'),
    ]
    return _make_ctrl(qtbot, tmp_path, monkeypatch, effort_ratings=ratings)


def _section_texts(dlg):
    from PySide6.QtWidgets import QLabel, QWidget
    section = dlg.scroll.widget().findChild(QWidget, "effortSection")
    assert section is not None
    return [lbl.text() for lbl in section.findChildren(QLabel)]


def test_seccion_de_esfuerzo_respeta_el_periodo(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QGridLayout, QWidget
    from ui.dialogs.stats_dialog import StatsDialog
    ctrl = _stats_ctrl(qtbot, tmp_path, monkeypatch)
    dlg = StatsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    # La rejilla de resumen sigue con sus 8 tarjetas
    grids = [w.layout() for w in dlg.scroll.widget().findChildren(QWidget)
             if isinstance(w.layout(), QGridLayout)]
    assert any(g.count() == 8 for g in grids)
    # 7 días: las dos de hoy
    assert dlg.effort_average.text() == "3,0 / 5"
    assert dlg.effort_count.text().startswith("2 valoraciones")
    texts = _section_texts(dlg)
    assert "Esfuerzo" in texts and "Esfuerzo por proyecto" in texts
    assert "4 · Duro" in texts and "2 · Fácil" in texts
    assert "Web" in texts and "Sin proyecto" in texts
    # Histórico: las tres
    dlg._change_period(0)
    assert dlg.effort_average.text() == "3,7 / 5"
    assert dlg.effort_count.text().startswith("3 valoraciones")
    # Sin valoraciones en el periodo
    ctrl.data_handler.data['effort_ratings'] = []
    dlg._change_period(1)
    assert dlg.effort_average.text() == "—"
    assert "Sin valoraciones en este periodo" in _section_texts(dlg)
    dlg.done(0)


def test_exportar_csv_con_archivo_de_esfuerzo(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from ui.dialogs.stats_dialog import StatsDialog
    ctrl = _stats_ctrl(qtbot, tmp_path, monkeypatch)
    dlg = StatsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    dest = tmp_path / "salida.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(dest), "")))
    dlg._export_csv()
    assert dest.exists()
    effort_file = tmp_path / "salida_esfuerzo.csv"
    assert effort_file.exists()
    with open(effort_file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=';'))
    assert rows[0] == ["fecha", "tarea", "proyecto", "etiquetas", "fija", "esfuerzo",
                       "minutos_foco"]
    assert len(rows) == 4  # cabecera + todas las valoraciones (no solo el periodo)
    dos = next(r for r in rows if r[1] == 'Hoy dos')
    assert dos[2:] == ['Sin proyecto', 'a, b', 'daily', '2', '10.0']
    assert "salida_esfuerzo.csv" in dlg.export_status._full
    dlg.done(0)


def test_exportar_xlsx_con_hoja_de_esfuerzo(qtbot, tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    from PySide6.QtWidgets import QFileDialog
    from ui.dialogs.stats_dialog import StatsDialog
    ctrl = _stats_ctrl(qtbot, tmp_path, monkeypatch)
    dlg = StatsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    dest = tmp_path / "salida.xlsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(dest), "")))
    dlg._export_xlsx()
    wb = openpyxl.load_workbook(dest)
    assert wb.sheetnames == ["PomoGrowth", "Esfuerzo"]
    rows = list(wb["Esfuerzo"].iter_rows(values_only=True))
    assert rows[0][5] == "esfuerzo"
    assert sorted(r[5] for r in rows[1:]) == [2, 4, 5]
    dlg.done(0)


def test_en_modo_compacto_se_ancla_a_la_mini_ventana(ctrl, qtbot):
    ctrl.show_compact()
    qtbot.waitUntil(lambda: ctrl.compact_window is not None
                    and ctrl.compact_window.isVisible() and not ctrl.window.isVisible())
    ctrl.toggle_task_completion(LOCAL)
    dlg = _popup(ctrl)
    assert dlg.parentWidget() is ctrl.compact_window
    qtbot.keyClick(dlg, Qt.Key_3)
    assert _ratings(ctrl)[-1]['rating'] == 3
    ctrl.show_normal()
