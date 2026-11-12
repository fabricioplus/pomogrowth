"""Plan de hoy: sugerencias (core/day_plan.py), persistencia en DataHandler y
el diálogo «Planifica tu día» (pytest-qt, offscreen).

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_day_plan.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import day_plan  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402

TODAY = date(2026, 10, 27)   # martes
# IDs como los reales: timestamps en ms (>2^31)
BASE = 1764282734316


def _iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _task(n: int, name: str, due=None, **extra) -> dict:
    return {'id': BASE + n, 'name': name, 'completed': False, 'tags': [],
            'source': 'local', 'asana_id': None, 'due': due, **extra}


def _chunk(task_n: int, days_ago: int, minutes: float = 25, kind: str = 'pomodoro') -> dict:
    when = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time()) \
        + timedelta(hours=10)
    return {'duration': minutes * 60, 'task_id': BASE + task_n,
            'timestamp': when.isoformat(), 'type': kind}


# ---------------------------------------------------------------- sugerencias
def test_orden_de_prioridad_y_textos():
    tasks = [
        _task(1, "Sin nada"),                                   # no se sugiere
        _task(2, "En curso", None),                             # pomodoro ayer
        _task(3, "Fija", None, recurring='daily'),
        _task(4, "En 4 días", _iso(4)),
        _task(5, "En 3 días", _iso(3)),
        _task(6, "Mañana", _iso(1)),
        _task(7, "Hoy", _iso(0)),
        _task(8, "Vencida ayer", _iso(-1)),
        _task(9, "Vencida hace 3", _iso(-3)),
        _task(10, "En 14 días", _iso(14)),                      # demasiado lejos
        _task(11, "Completada", _iso(-5), completed=True),
        _task(12, "Borrada en Asana", _iso(-5), asana_deleted=True, source='asana'),
        _task(13, "Actividad vieja", None),                     # hace 5 días
        _task(14, "Solo descansos", None),                      # break ayer
    ]
    chunks = [_chunk(2, 1), _chunk(13, 5), _chunk(14, 1, kind='break')]
    sugs = day_plan.suggest(tasks, chunks, TODAY)
    got = [(s.task_id - BASE, s.reason_code, s.reason_text) for s in sugs]
    assert got == [
        (9, day_plan.OVERDUE, "Vencida hace 3 días"),
        (8, day_plan.OVERDUE, "Vencida ayer"),
        (7, day_plan.DUE_TODAY, "Vence hoy"),
        (6, day_plan.DUE_TOMORROW, "Vence mañana"),
        (5, day_plan.DUE_SOON, "Vence en 3 días"),
        (4, day_plan.DUE_SOON, "Vence en 4 días"),
        (3, day_plan.RECURRING, "Tarea fija"),
        (2, day_plan.IN_PROGRESS, "En curso"),
    ]
    scores = [s.score for s in sugs]
    assert scores == sorted(scores, reverse=True)
    # Tuplas con nombre: se pueden desempaquetar
    task_id, score, code, text = sugs[0]
    assert isinstance(task_id, int) and task_id > 2 ** 31


def test_vencidas_mas_antiguas_primero_y_atrasos_largos():
    tasks = [_task(1, "10 días", _iso(-10)), _task(2, "40 días", _iso(-40)),
             _task(3, "400 días", _iso(-400)), _task(4, "29 días", _iso(-29))]
    sugs = day_plan.suggest(tasks, [], TODAY)
    # Las de hasta un mes, las más atrasadas primero; las de más de un mes
    # (probablemente abandonadas) al final, las más recientes primero
    assert [s.task_id - BASE for s in sugs] == [4, 1, 2, 3]
    assert [s.reason_text for s in sugs] == [
        "Vencida hace 29 días", "Vencida hace 10 días",
        "Vencida hace 1 mes", "Vencida hace más de un año"]
    assert [s.reason_code for s in sugs] == [day_plan.OVERDUE, day_plan.OVERDUE,
                                             day_plan.OVERDUE_STALE, day_plan.OVERDUE_STALE]


def test_fija_con_fecha_usa_el_motivo_mas_urgente():
    sugs = day_plan.suggest([_task(1, "Fija vencida", _iso(-2), recurring='weekly')],
                            [], TODAY)
    assert sugs[0].reason_code == day_plan.OVERDUE


def test_actividad_reciente_desempata_y_ordena_en_curso():
    tasks = [_task(1, "Hoy A", _iso(0)), _task(2, "Hoy B", _iso(0)),
             _task(3, "Trabajada anteayer"), _task(4, "Trabajada hoy")]
    chunks = [_chunk(2, 0, 50), _chunk(3, 2), _chunk(4, 0)]
    sugs = day_plan.suggest(tasks, chunks, TODAY)
    # B sube por delante de A (mismo motivo, más minutos recientes)
    assert [s.task_id - BASE for s in sugs] == [2, 1, 4, 3]


def test_due_con_hora_utc_se_compara_por_fecha_local():
    lima = timezone(timedelta(hours=-5))
    tokio = timezone(timedelta(hours=9))
    # 03:00 UTC del 28 = 22:00 del 27 en Lima: vence HOY (no mañana)
    t1 = _task(1, "Asana tarde", '2026-10-28T03:00:00.000Z')
    # 04:30 UTC del 27 = 23:30 del 26 en Lima: venció ayer
    t2 = _task(2, "Asana madrugada", '2026-10-27T04:30:00Z')
    # Sin hora (due_on): la fecha tal cual, en cualquier zona
    t3 = _task(3, "Solo fecha", '2026-10-28')
    t4 = _task(4, "Fecha rota", 'no-es-fecha')
    by_id = {s.task_id - BASE: s.reason_text
             for s in day_plan.suggest([t1, t2, t3, t4], [], TODAY, tz=lima)}
    assert by_id == {1: "Vence hoy", 2: "Vencida ayer", 3: "Vence mañana"}
    # En Tokio, 20:00 UTC del 26 ya es el 27 a las 05:00
    t5 = _task(5, "Tokio", '2026-10-26T20:00:00Z')
    assert day_plan.suggest([t5], [], TODAY, tz=tokio)[0].reason_text == "Vence hoy"
    # Zona del sistema: una hora local de hoy expresada en UTC sigue siendo hoy
    local_late = datetime.combine(TODAY, datetime.min.time()).replace(hour=23, minute=30)
    utc = local_late.astimezone().astimezone(timezone.utc)
    t6 = _task(6, "Local", utc.isoformat().replace('+00:00', 'Z'))
    assert day_plan.suggest([t6], [], TODAY)[0].reason_code == day_plan.DUE_TODAY


def test_vencidas_antiguas_al_final_y_sin_preseleccionar():
    """Vencida hace más de un mes: se sugiere al final y no llega marcada."""
    tasks = [
        {'id': 1, 'name': 'Abandonada', 'completed': False, 'due': (TODAY - timedelta(days=90)).isoformat()},
        {'id': 2, 'name': 'Reciente', 'completed': False, 'due': (TODAY - timedelta(days=2)).isoformat()},
        {'id': 3, 'name': 'Próxima', 'completed': False, 'due': (TODAY + timedelta(days=4)).isoformat()},
    ]
    sugs = day_plan.suggest(tasks, [], today=TODAY)
    assert [s.task_id for s in sugs] == [2, 3, 1]
    assert sugs[-1].reason_code == day_plan.OVERDUE_STALE
    assert sugs[-1].reason_text == "Vencida hace 3 meses"
    assert day_plan.default_selection(sugs) == [2]
    # Solo hay vencidas antiguas: nada llega marcado por defecto
    only_stale = day_plan.suggest(tasks[:1], [], today=TODAY)
    assert day_plan.default_selection(only_stale) == []


def test_default_selection():
    tasks = [_task(i, f"Vencida {i}", _iso(-i)) for i in range(1, 8)]
    tasks += [_task(20, "Fija", None, recurring='daily'), _task(21, "Hoy", _iso(0))]
    sugs = day_plan.suggest(tasks, [], TODAY)
    sel = day_plan.default_selection(sugs)
    # Solo urgentes, en orden de prioridad y con tope
    assert len(sel) == 5
    assert sel == [s.task_id for s in sugs if s.reason_code in day_plan.URGENT_REASONS][:5]
    assert BASE + 20 not in sel
    assert len(day_plan.default_selection(sugs, limit=2)) == 2
    # Sin urgentes: las 3 primeras sugerencias
    calm = [_task(1, "En 5 días", _iso(5)), _task(2, "Fija", None, recurring='daily'),
            _task(3, "En 6 días", _iso(6)), _task(4, "En curso")]
    calm_sugs = day_plan.suggest(calm, [_chunk(4, 0)], TODAY)
    assert day_plan.default_selection(calm_sugs) == [s.task_id for s in calm_sugs[:3]]
    assert day_plan.default_selection([]) == []


def test_fecha_larga_en_espanol():
    assert day_plan.format_long_date(TODAY) == "martes 27 de octubre"
    assert day_plan.format_long_date(date(2025, 10, 27)) == "lunes 27 de octubre"
    assert day_plan.format_long_date(date(2026, 1, 3)) == "sábado 3 de enero"


# --------------------------------------------------------------- persistencia
def _data_file(tmp_path: Path, **extra) -> Path:
    data = {'tasks': [_task(1, "Uno"), _task(2, "Dos"), _task(3, "Tres")],
            'deleted_tasks': [], 'time_chunks': [], 'history': [],
            'ui_snapshot': {}, 'settings': {}, **extra}
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_persistencia_del_plan(tmp_path):
    path = _data_file(tmp_path)
    dh = DataHandler(path)
    assert dh.get_day_plan(today=TODAY) == []
    assert not dh.has_day_plan_today(today=TODAY)
    # Duplicados, tipos raros y bool fuera; el orden se conserva
    dh.set_day_plan([BASE + 2, BASE + 1, BASE + 2, 'x', True], today=TODAY)
    assert dh.get_day_plan(today=TODAY) == [BASE + 2, BASE + 1]
    assert dh.has_day_plan_today(today=TODAY)
    # Otro día: el plan de ayer ya no vale
    tomorrow = TODAY + timedelta(days=1)
    assert dh.get_day_plan(today=tomorrow) == []
    assert not dh.has_day_plan_today(today=tomorrow)
    # Sobrevive a un reinicio
    dh2 = DataHandler(path)
    assert dh2.get_day_plan(today=TODAY) == [BASE + 2, BASE + 1]
    # Una tarea borrada sale del plan; una completada se queda
    dh2.delete_task(BASE + 2)
    dh2.update_task_status(BASE + 1, True)
    assert dh2.get_day_plan(today=TODAY) == [BASE + 1]
    # «Hoy ya se decidió» aunque sea sin tareas
    dh2.set_day_plan([], today=TODAY)
    assert dh2.has_day_plan_today(today=TODAY)
    assert dh2.get_day_plan(today=TODAY) == []


def test_plan_ausente_o_corrupto_se_sanea(tmp_path):
    dh = DataHandler(_data_file(tmp_path))
    assert dh.data['day_plan'] == {'date': '', 'task_ids': []}
    dh = DataHandler(_data_file(tmp_path, day_plan="basura"))
    assert dh.data['day_plan'] == {'date': '', 'task_ids': []}
    dh = DataHandler(_data_file(tmp_path, day_plan={'date': TODAY.isoformat(),
                                                    'task_ids': [BASE + 1, 'a', None,
                                                                 BASE + 1, 99]}))
    assert dh.data['day_plan']['task_ids'] == [BASE + 1, 99]
    # 99 no es una tarea: no sale
    assert dh.get_day_plan(today=TODAY) == [BASE + 1]
    dh = DataHandler(_data_file(tmp_path, day_plan={'date': 5, 'task_ids': [BASE + 1]}))
    assert not dh.has_day_plan_today(today=TODAY)


# --------------------------------------------------------------------- diálogo
def _plain(html: str) -> str:
    return re.sub(r'<[^>]+>', '', html)


@pytest.fixture
def plan_data():
    tasks = [
        _task(1, "Cerrar propuesta", _iso(-2)),
        _task(2, "Enviar factura", _iso(0)),
        _task(3, "Preparar demo", _iso(1)),
        _task(4, "Responder correos", None, recurring='daily'),
        _task(5, "Reunión con el equipo", None),
        _task(6, "Leer documentación", _iso(20)),
        _task(7, "Ya hecha", _iso(-1), completed=True),
    ]
    sugs = day_plan.suggest(tasks, [], TODAY)
    return tasks, sugs, day_plan.default_selection(sugs)


@pytest.fixture
def dialog(qtbot, plan_data):
    from ui.theme import fonts
    from ui.dialogs.day_plan_dialog import DayPlanDialog
    fonts.init_fonts()
    tasks, sugs, pre = plan_data
    dlg = DayPlanDialog(None, tasks, sugs, pre, today=TODAY)
    qtbot.addWidget(dlg)
    return dlg


def test_dialogo_preseleccion_secciones_y_contador(dialog, plan_data):
    _tasks, sugs, pre = plan_data
    assert pre == [BASE + 1, BASE + 2, BASE + 3]
    assert [r.task_id for r in dialog.suggested_rows] == [s.task_id for s in sugs]
    # Otras: pendientes no sugeridas; las completadas no aparecen
    assert {r.task_id for r in dialog.other_rows} == {BASE + 5, BASE + 6}
    assert dialog.selected_ids() == pre
    assert _plain(dialog.count_label.text()) == "3 tareas para hoy"
    assert dialog._date_label.text() == "Martes 27 de octubre"
    # Hay sugeridas y nada marcado en Otras: la sección arranca plegada
    assert not dialog.others_expanded()


def test_dialogo_marcar_y_desmarcar(dialog, qtbot):
    from PySide6.QtCore import QPoint, Qt
    dialog.show()
    qtbot.waitExposed(dialog)
    first = dialog.suggested_rows[0]
    # Clic en la fila (no en la casilla): desmarca
    qtbot.mouseClick(first, Qt.LeftButton, pos=QPoint(first.width() // 2, first.height() // 2))
    assert BASE + 1 not in dialog.selected_ids()
    assert _plain(dialog.count_label.text()) == "2 tareas para hoy"
    # Clic en la casilla: vuelve a marcar (una sola vez, no dos)
    qtbot.mouseClick(first.check, Qt.LeftButton)
    assert dialog.selected_ids()[0] == BASE + 1
    # Una de Otras, tras desplegar
    dialog.set_others_expanded(True)
    other = dialog.other_rows[0]
    qtbot.mouseClick(other, Qt.LeftButton, pos=QPoint(other.width() // 2, other.height() // 2))
    assert dialog.selected_ids()[-1] == other.task_id
    # Elegir todas / quitar todas (solo afecta a las sugeridas)
    dialog.toggle_all_btn.click()
    assert all(r.is_checked() for r in dialog.suggested_rows)
    assert dialog.toggle_all_btn.text() == "Quitar todas"
    dialog.toggle_all_btn.click()
    assert dialog.selected_ids() == [other.task_id]
    assert _plain(dialog.count_label.text()) == "1 tarea para hoy"


def test_dialogo_buscador_en_otras(qtbot, plan_data):
    from ui.dialogs.day_plan_dialog import DayPlanDialog
    tasks, sugs, pre = plan_data
    # 5 tareas más sin fecha para que aparezca el buscador (> 4 en Otras)
    tasks = tasks + [_task(30 + i, f"Tarea extra {i}") for i in range(5)]
    dlg = DayPlanDialog(None, tasks, sugs, pre, today=TODAY)
    qtbot.addWidget(dlg)
    dlg.set_others_expanded(True)
    assert dlg.search.isVisibleTo(dlg)
    dlg.search.setText("reunion")   # sin tilde también encuentra «Reunión»
    visible = [r.task_id for r in dlg.other_rows if r.isVisibleTo(dlg)]
    assert visible == [BASE + 5]
    dlg.search.setText("zzz")
    assert dlg._no_results.isVisibleTo(dlg)
    dlg.search.clear()
    assert all(r.isVisibleTo(dlg) for r in dlg.other_rows)


def test_dialogo_ver_mas_sugeridas(qtbot):
    from ui.dialogs.day_plan_dialog import MAX_SUGGESTED_VISIBLE, DayPlanDialog
    tasks = [_task(i, f"Vencida {i}", _iso(-i)) for i in range(1, 10)]
    sugs = day_plan.suggest(tasks, [], TODAY)
    dlg = DayPlanDialog(None, tasks, sugs, day_plan.default_selection(sugs), today=TODAY)
    qtbot.addWidget(dlg)
    shown = [r for r in dlg.suggested_rows if r.isVisibleTo(dlg)]
    assert len(shown) == MAX_SUGGESTED_VISIBLE
    assert dlg.more_btn.isVisibleTo(dlg) and dlg.more_btn.text() == "Ver 3 más"
    dlg.more_btn.click()
    assert all(r.isVisibleTo(dlg) for r in dlg.suggested_rows)
    assert dlg.more_btn.text() == "Ver menos"


def test_dialogo_enter_acepta_y_escape_rechaza(qtbot, plan_data):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    from ui.dialogs.day_plan_dialog import DayPlanDialog
    tasks, sugs, pre = plan_data
    for key, expected in ((Qt.Key_Return, QDialog.Accepted), (Qt.Key_Escape, QDialog.Rejected)):
        dlg = DayPlanDialog(None, tasks, sugs, pre, today=TODAY)
        qtbot.addWidget(dlg)
        dlg.show()
        qtbot.waitExposed(dlg)
        # Enter con el foco en «Ahora no» también confirma (no es autoDefault)
        dlg.later_btn.setFocus()
        qtbot.keyClick(dlg.later_btn if key == Qt.Key_Return else dlg, key)
        assert dlg.result() == expected
        assert not dlg.isVisible()


def test_dialogo_overlay_sobre_la_ventana(qtbot, plan_data):
    from PySide6.QtWidgets import QWidget
    from ui.dialogs.day_plan_dialog import DayPlanDialog, _Overlay
    tasks, sugs, pre = plan_data
    win = QWidget()
    qtbot.addWidget(win)
    win.resize(800, 600)
    win.show()
    qtbot.waitExposed(win)
    dlg = DayPlanDialog(win, tasks, sugs, pre, today=TODAY)
    dlg.show()
    assert len(win.findChildren(_Overlay)) == 1
    dlg.reject()
    qtbot.waitUntil(lambda: not win.findChildren(_Overlay))


def _click_in_open_dialog(qtbot, button_name: str):
    """Programa un clic en el botón del diálogo modal en cuanto aparezca."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ui.dialogs.day_plan_dialog import DayPlanDialog

    def click():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, DayPlanDialog) and w.isVisible():
                getattr(w, button_name).click()
                return
        QTimer.singleShot(20, click)
    QTimer.singleShot(50, click)


def test_pick_day_plan_empezar_y_ahora_no(qtbot, plan_data):
    from ui.dialogs.day_plan_dialog import pick_day_plan
    tasks, sugs, pre = plan_data
    _click_in_open_dialog(qtbot, 'start_btn')
    assert pick_day_plan(None, tasks, sugs, pre) == pre
    _click_in_open_dialog(qtbot, 'later_btn')
    assert pick_day_plan(None, tasks, sugs, pre) is None


def test_resumen_breve():
    from ui.dialogs.day_plan_dialog import summary_text
    tasks = [_task(1, "a", _iso(-1)), _task(2, "b", _iso(-2)), _task(3, "c", _iso(0)),
             _task(4, "d", _iso(1)), _task(5, "e", _iso(1))]
    text = summary_text(7, day_plan.suggest(tasks, [], TODAY))
    assert text == ("Tienes 7 tareas pendientes: 2 vencidas, 1 vence hoy y 2 vencen "
                    "mañana. Marca en cuáles trabajarás hoy.")
    assert summary_text(1, []) == "Tienes 1 tarea pendiente. Marca en cuáles trabajarás hoy."
