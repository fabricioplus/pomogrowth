"""Tests offscreen de la UI (pytest-qt): lógica de filtros, orden y ops in-place.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_ui_smoke.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.data_handler import DataHandler  # noqa: E402
from ui.theme import fonts  # noqa: E402


# IDs como los reales: timestamps en ms (>2^31). Cazan cualquier señal
# declarada Signal(int) — int32 en C++ — que desbordaría (bug real de julio 2026:
# los clics en tarjetas morían con OverflowError y no se podía seleccionar).
ID1, ID2, ID3 = 1764282734316, 1764282734317, 1764282734318


def _make_data(tmp_path: Path) -> Path:
    tasks = [
        {'id': ID1, 'name': 'Beta', 'completed': False, 'tags': ['x'], 'source': 'local',
         'asana_id': None, 'due': '2026-07-10'},
        {'id': ID2, 'name': 'Alfa', 'completed': False, 'tags': [], 'source': 'local',
         'asana_id': None, 'due': None},
        {'id': ID3, 'name': 'Gamma', 'completed': True, 'tags': [], 'source': 'local',
         'asana_id': None, 'due': '2026-07-08'},
    ]
    data = {'tasks': tasks, 'deleted_tasks': [], 'time_chunks': [],
            'history': [], 'ui_snapshot': {}, 'settings': {'task_sort_mode': 'manual'}}
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.fixture
def ctrl(qtbot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    dh = DataHandler(_make_data(tmp_path))
    # Un test nunca sale a la red: la cola hacia Asana no envía nada
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


def test_filtro_inicial_pendientes(ctrl):
    assert ctrl.task_filter == 'pending'
    assert set(ctrl.tasks_panel._cards) == {ID1, ID2}


def test_orden_inicial_por_fecha(ctrl):
    """Al ingresar, la vista arranca ordenada por Fecha (pedido v9.1)."""
    assert ctrl.task_sort_mode == 'due'


def test_filtros(ctrl, qtbot):
    ctrl.set_task_filter('completed')
    assert set(ctrl.tasks_panel._cards) == {ID3}
    ctrl.set_task_filter('all')
    assert set(ctrl.tasks_panel._cards) == {ID1, ID2, ID3}


def test_orden_nombre_y_due(ctrl):
    ctrl.set_task_filter('all')
    ctrl.set_task_sort('name')
    assert ctrl.tasks_panel.visible_order() == [ID2, ID1, ID3]  # Alfa, Beta, Gamma
    ctrl.set_task_sort('due')
    assert ctrl.tasks_panel.visible_order() == [ID3, ID1, ID2]  # 08, 10, sin fecha
    ctrl.set_task_sort('manual')
    assert ctrl.tasks_panel.visible_order() == [ID1, ID2, ID3]


def test_seleccion_in_place(ctrl):
    ctrl.select_task(ID1)
    assert ctrl.model.active_task_id == ID1
    assert ctrl.tasks_panel._cards[ID1]._active
    ctrl.select_task(ID2)
    assert not ctrl.tasks_panel._cards[ID1]._active
    assert ctrl.tasks_panel._cards[ID2]._active


def test_completar_actualiza_datos_y_tarjeta(ctrl, qtbot):
    ctrl.set_task_filter('all')
    ctrl.toggle_task_completion(ID1)
    task = ctrl._task_by_id(ID1)
    assert task['completed'] is True
    assert ctrl.tasks_panel._cards[ID1]._completed is True
    ctrl.toggle_task_completion(ID1)
    assert ctrl._task_by_id(ID1)['completed'] is False


def test_completar_bajo_pendientes_quita_tarjeta(ctrl, qtbot):
    assert ID1 in ctrl.tasks_panel._cards
    ctrl.toggle_task_completion(ID1)
    qtbot.wait(400)  # animación de colapso
    assert ID1 not in ctrl.tasks_panel._cards
    assert ctrl._task_by_id(ID1)['completed'] is True


def test_mover_flecha_ui_y_datos(ctrl):
    ctrl.set_task_sort('manual')  # el arranque ahora es 'due'; mover exige manual
    ctrl.move_task(ID1, +1)
    assert ctrl.tasks_panel.visible_order() == [ID2, ID1]
    datos = [t['id'] for t in ctrl.data_handler.get_tasks() if not t['completed']]
    assert datos == [ID2, ID1]


def test_completar_activa_limpia_seleccion(ctrl):
    ctrl.select_task(ID1)
    ctrl.toggle_task_completion(ID1)
    assert ctrl.model.active_task_id is None


def test_anadir_tarea(ctrl):
    ctrl.add_task("Nueva", ["tag1"])
    nueva = next(t for t in ctrl.data_handler.get_tasks() if t['name'] == "Nueva")
    assert nueva['tags'] == ["tag1"]
    assert nueva['id'] in ctrl.tasks_panel._cards


def test_clic_real_en_tarjeta_selecciona(ctrl, qtbot):
    """Clic FÍSICO (no llamada directa): recorre la cadena completa de señales
    con un id >2^31 — así se cazó el OverflowError de Signal(int)."""
    from PySide6.QtCore import QPoint, Qt
    card = ctrl.tasks_panel._cards[ID1]
    qtbot.mouseClick(card, Qt.LeftButton, pos=QPoint(card.width() // 2, 10))
    assert ctrl.model.active_task_id == ID1
    qtbot.mouseClick(ctrl.tasks_panel._cards[ID2], Qt.LeftButton,
                     pos=QPoint(10, 10))
    assert ctrl.model.active_task_id == ID2


def test_clic_real_en_check_completa(ctrl, qtbot):
    """El check también viaja por señal con id grande."""
    from PySide6.QtCore import Qt
    ctrl.set_task_filter('all')
    qtbot.mouseClick(ctrl.tasks_panel._cards[ID1].check, Qt.LeftButton)
    assert ctrl._task_by_id(ID1)['completed'] is True


def test_tarea_bloqueada_durante_pomodoro(ctrl):
    """Con el pomodoro en curso, seleccionar otra tarea NO cambia el foco."""
    ctrl.select_task(ID1)
    ctrl.model.start()
    ctrl.select_task(ID2)
    assert ctrl.model.active_task_id == ID1  # bloqueada
    ctrl.model.pause()  # pausada a mitad: sigue bloqueada
    ctrl.select_task(ID2)
    assert ctrl.model.active_task_id == ID1
    ctrl.reset_timer()  # reset desbloquea
    ctrl.select_task(ID2)
    assert ctrl.model.active_task_id == ID2


def test_panel_solo_alterna_izquierda_y_derecha(ctrl):
    """El panel de tareas ya no se acopla arriba/abajo."""
    from PySide6.QtCore import Qt
    splitter = ctrl.window.splitter
    assert ctrl.panel_position == 'left'
    assert splitter.indexOf(ctrl.tasks_panel) == 0
    ctrl.cycle_panel_position()
    assert ctrl.panel_position == 'right'
    assert splitter.indexOf(ctrl.tasks_panel) == 1
    assert ctrl.data_handler.load_settings()['task_panel_position'] == 'right'
    ctrl.cycle_panel_position()
    assert ctrl.panel_position == 'left'
    assert splitter.indexOf(ctrl.tasks_panel) == 0
    assert splitter.orientation() == Qt.Horizontal


def test_posiciones_viejas_del_panel():
    """'top'/'bottom' guardadas por versiones anteriores → izquierda/derecha."""
    from ui.controllers.app_controller import normalize_panel_position
    assert normalize_panel_position('top') == 'left'
    assert normalize_panel_position('bottom') == 'right'
    assert normalize_panel_position('right') == 'right'
    assert normalize_panel_position(None) == 'left'
    assert normalize_panel_position('diagonal') == 'left'


def test_tareas_nuevas_van_arriba(ctrl):
    ctrl.add_task("Primera nueva", [])
    ctrl.add_task("Segunda nueva", [])
    nombres = [t['name'] for t in ctrl.data_handler.get_tasks()[:2]]
    assert nombres == ["Segunda nueva", "Primera nueva"]


def test_tarea_recurrente_se_reabre(ctrl):
    ctrl.add_task("Rutina diaria", [], recurring='daily')
    task = next(t for t in ctrl.data_handler.get_tasks() if t.get('recurring'))
    ctrl.toggle_task_completion(task['id'])
    assert ctrl._task_by_id(task['id'])['completed'] is True
    # Simular que se completó ayer → reabre al "arrancar"
    ctrl._task_by_id(task['id'])['completed_at'] = '2020-01-01T08:00:00'
    assert ctrl.data_handler.reopen_recurring_tasks() == 1
    assert ctrl._task_by_id(task['id'])['completed'] is False


def test_ciclos_de_reapertura(ctrl):
    """Cada ciclo respeta su período: diaria/semanal/mensual/trimestral."""
    from datetime import date, timedelta
    dh = ctrl.data_handler
    today = date.today()

    def fixed(cycle, days_ago):
        tid = dh.add_task(f"fija-{cycle}-{days_ago}", [], recurring=cycle)
        task = next(t for t in dh.get_tasks() if t['id'] == tid)
        task['completed'] = True
        task['completed_at'] = (today - timedelta(days=days_ago)).isoformat() + "T09:00:00"
        return task

    semanal_reciente = fixed('weekly', 3)    # no toca aún
    semanal_vencida = fixed('weekly', 7)     # reabre
    mensual_reciente = fixed('monthly', 10)  # no toca aún
    trimestral_reciente = fixed('quarterly', 45)  # no toca aún
    assert dh.reopen_recurring_tasks() == 1
    assert semanal_vencida['completed'] is False
    assert semanal_reciente['completed'] is True
    assert mensual_reciente['completed'] is True
    assert trimestral_reciente['completed'] is True


def test_comprar_primera_mascota_la_equipa(ctrl):
    """Comprar la primera mascota la equipa y la muestra sin pasos extra
    (regresión del "compré y no pasó nada")."""
    from ui.dialogs.rewards_dialog import RewardsDialog
    ctrl.rewards.progress.coins = 1000
    dlg = RewardsDialog(ctrl.window, ctrl)
    dlg._buy('pet_dog')
    assert 'pet_dog' in ctrl.rewards.progress.purchased_items
    assert ctrl.rewards.progress.current_pet == 'pet_dog'
    assert ctrl.timer_panel.pet.isVisible()
    # Una segunda mascota NO desplaza a la equipada (equipar sigue siendo manual)
    dlg._buy('pet_cat')
    assert ctrl.rewards.progress.current_pet == 'pet_dog'


def test_filtros_avanzados_colapsables(ctrl, qtbot):
    """El orden vive tras «Filtros avanzados»: colapsado por defecto,
    se expande al pulsar y el estado se persiste en settings."""
    lp = ctrl.tasks_panel
    assert lp.sort_box.maximumHeight() == 0  # colapsado al arrancar
    assert "Fecha" in lp.advanced_btn.text()  # muestra el orden activo
    lp._toggle_advanced()
    qtbot.wait(350)  # animación de 220 ms
    assert lp.sort_box.maximumHeight() > 0
    assert ctrl.data_handler.load_settings().get('advanced_filters_open') is True
    lp._toggle_advanced()
    qtbot.wait(350)
    assert lp.sort_box.maximumHeight() == 0
    assert ctrl.data_handler.load_settings().get('advanced_filters_open') is False


def test_editar_ciclo_de_repeticion(ctrl):
    """update_task_name_and_tags puede fijar, cambiar y quitar el ciclo."""
    dh = ctrl.data_handler
    tid = dh.add_task("Informe", [], recurring='weekly')
    task = next(t for t in dh.get_tasks() if t['id'] == tid)
    # Cambiar el ciclo
    dh.update_task_name_and_tags(tid, "Informe", [], recurring='monthly')
    assert task['recurring'] == 'monthly'
    # Quitar la repetición
    dh.update_task_name_and_tags(tid, "Informe", [], recurring='')
    assert 'recurring' not in task
    # Volverla fija de nuevo
    dh.update_task_name_and_tags(tid, "Informe", [], recurring='quarterly')
    assert task['recurring'] == 'quarterly'
    # Omitir el parámetro no toca el ciclo (compat con llamadores viejos)
    dh.update_task_name_and_tags(tid, "Informe renombrado", ["x"])
    assert task['recurring'] == 'quarterly'
    assert task['name'] == "Informe renombrado"


def test_proveedores_de_mascota():
    """provider_config elige la key/modelo del proveedor activo; el endpoint
    propio normaliza la URL y no exige key."""
    from services.pet_service import (is_configured, normalize_custom_url,
                                      provider_config)
    # Claude (default, compat con settings previos)
    cfg = provider_config({'pet_api_key': 'sk-ant-x', 'pet_model': ''})
    assert cfg['provider'] == 'claude' and cfg['model'] == 'claude-opus-4-8'
    assert is_configured(cfg)
    # OpenAI con su propia key
    cfg = provider_config({'pet_provider': 'openai', 'pet_openai_key': 'sk-x',
                           'pet_api_key': 'sk-ant-x'})
    assert cfg['provider'] == 'openai' and cfg['api_key'] == 'sk-x'
    assert cfg['model'] == 'gpt-5-mini'
    # Endpoint propio: URL basta, key opcional
    cfg = provider_config({'pet_provider': 'custom',
                           'pet_custom_url': 'https://abc.ngrok-free.app'})
    assert is_configured(cfg)
    assert not is_configured(provider_config({'pet_provider': 'custom'}))
    # Normalización de URL (pelada / con /v1 / completa / vacía)
    assert (normalize_custom_url('https://abc.ngrok-free.app')
            == 'https://abc.ngrok-free.app/v1/chat/completions')
    assert (normalize_custom_url('http://localhost:11434/v1/')
            == 'http://localhost:11434/v1/chat/completions')
    assert (normalize_custom_url('http://x/v1/chat/completions')
            == 'http://x/v1/chat/completions')
    assert normalize_custom_url('') == ''


def test_next_reopen_date_fin_de_mes():
    """La suma de meses hace clamp al fin de mes (31 ene + 1m = fin de feb)."""
    from datetime import date
    from core.data_handler import next_reopen_date
    assert next_reopen_date(date(2026, 1, 31), 'monthly') in (date(2026, 2, 28),)
    assert next_reopen_date(date(2026, 3, 15), 'quarterly') == date(2026, 6, 15)
    assert next_reopen_date(date(2026, 7, 1), 'weekly') == date(2026, 7, 8)
    assert next_reopen_date(date(2026, 7, 1), 'daily') == date(2026, 7, 2)
    # compat: recurring=True (lote anterior) se trata como diaria
    from core.data_handler import recurring_cycle
    assert recurring_cycle({'recurring': True}) == 'daily'
    assert recurring_cycle({'recurring': 'weekly'}) == 'weekly'
    assert recurring_cycle({'recurring': None}) is None


def test_filtro_fijas(ctrl):
    ctrl.add_task("Rutina", [], recurring=True)
    ctrl.set_task_filter('recurring')
    assert len(ctrl.tasks_panel._cards) == 1


def test_ciclo_marcado_en_chunk(ctrl):
    ctrl.data_handler.log_time_chunk(1500, ID1, 'pomodoro', cycle_completed=True)
    ctrl.data_handler.log_time_chunk(1500, ID1, 'pomodoro')
    chunks = ctrl.data_handler.get_time_chunks()
    assert chunks[0].get('cycle_completed') is True
    assert 'cycle_completed' not in chunks[1]
    from core.stats_extended import StatsCalculator
    calc = StatsCalculator(chunks)
    assert calc.get_summary()['cycles_total'] == 1


def test_sync_toggle_persiste(ctrl):
    ctrl._on_sync_toggled(False)
    assert ctrl.data_handler.load_settings().get('sync_enabled') is False
    ctrl._on_sync_toggled(True)
    assert ctrl.data_handler.load_settings().get('sync_enabled') is True


def test_pausar_descanso_registra_chunk_break(ctrl, qtbot):
    """Pausar durante el descanso NO debe registrar tiempo de trabajo."""
    ctrl.select_task(ID1)
    ctrl.model.last_pomodoro_task_id = ID1
    ctrl.model.is_break_time = True
    ctrl.model.remaining_time = 300
    ctrl.model.start()
    qtbot.wait(1100)  # >1 s: log_time_chunk descarta tramos menores
    ctrl.toggle_timer()  # pausa
    chunks = ctrl.data_handler.get_time_chunks()
    assert chunks, "el tramo de descanso debe registrarse"
    assert chunks[-1]['type'] == 'break'


def test_completar_tarea_activa_conserva_ciclo(ctrl):
    """Completar la tarea activa a mitad de pomodoro no borra el progreso del ciclo."""
    ctrl.select_task(ID1)
    ctrl.model.pomodoro_cycle_count = 2
    ctrl.model.start()
    ctrl.toggle_task_completion(ID1)
    assert ctrl.model.pomodoro_cycle_count == 2
    assert ctrl.model.is_running is False


def test_guardar_settings_no_pisa_sesion_pausada(ctrl, qtbot):
    """Guardar configuración con un pomodoro pausado a mitad conserva el restante."""
    ctrl.select_task(ID1)
    ctrl.model.start()
    qtbot.wait(1100)
    ctrl.toggle_timer()  # pausa a mitad
    remaining = ctrl.model.remaining_time
    assert remaining < ctrl.model.pomodoro_duration
    ctrl.apply_settings(ctrl.data_handler.load_settings())
    assert ctrl.model.remaining_time == remaining


def test_merge_no_resucita_borradas_ni_revierte_push_pendiente(ctrl):
    """El merge respeta tombstones locales y los cambios en la cola hacia Asana."""
    dh = ctrl.data_handler
    dh.add_task("De Asana", [], source='asana', asana_id='gid-1')
    task = next(t for t in dh.get_tasks() if t.get('asana_id') == 'gid-1')
    # Completada en la app; el cambio sigue en la cola (aún no llegó a Asana)
    dh.update_task_status(task['id'], True)
    dh.merge_asana_tasks([{'gid': 'gid-1', 'name': 'De Asana', 'completed': False}])
    assert task['completed'] is True  # el sync no revierte el cambio local
    # Tombstone: una tarea de Asana borrada localmente no debe resucitar
    dh.delete_task(task['id'])
    dh.merge_asana_tasks([{'gid': 'gid-1', 'name': 'De Asana', 'completed': False}])
    assert not any(t.get('asana_id') == 'gid-1' for t in dh.get_tasks())


# ---------------------------------------------------------------- Plan de hoy
def _fake_planner(monkeypatch, answer):
    """Sustituye el diálogo del plan: devuelve `answer` y apunta lo recibido."""
    import ui.dialogs.day_plan_dialog as mod
    seen = {}

    def pick(parent, tasks, suggestions, preselected, **kwargs):
        seen['preselected'] = list(preselected)
        seen.update(kwargs)
        return answer
    monkeypatch.setattr(mod, "pick_day_plan", pick)
    return seen


def test_arranque_abre_el_plan_y_filtra_hoy(ctrl, monkeypatch):
    _fake_planner(monkeypatch, [ID2])
    ctrl._welcome_shown = False
    ctrl.data_handler.load_settings()['user_name_asked'] = True  # sin «¿Cómo te llamas?»
    ctrl._show_welcome()  # sin plan para hoy: planificar en vez de bienvenida
    assert ctrl.data_handler.get_day_plan() == [ID2]
    assert ctrl.task_filter == 'today'
    assert set(ctrl.tasks_panel._cards) == {ID2}


def test_plan_ahora_no_no_guarda_nada(ctrl, monkeypatch):
    _fake_planner(monkeypatch, None)
    ctrl.open_day_plan()
    assert not ctrl.data_handler.has_day_plan_today()
    assert ctrl.task_filter == 'pending'


def test_replanificar_conserva_las_completadas_y_completar_no_oculta(ctrl, monkeypatch):
    _fake_planner(monkeypatch, [ID1, ID2])
    ctrl.open_day_plan()
    ctrl.toggle_task_completion(ID1)              # en «Hoy» se queda, tachada
    assert ID1 in ctrl.tasks_panel._cards
    seen = _fake_planner(monkeypatch, [ID2])
    ctrl.open_day_plan()
    assert seen['preselected'] == [ID1, ID2]      # replanificar parte del plan
    assert ctrl.data_handler.get_day_plan() == [ID2, ID1]


def test_anadir_en_hoy_entra_en_el_plan(ctrl, monkeypatch):
    _fake_planner(monkeypatch, [ID1])
    ctrl.open_day_plan()
    ctrl.add_task("Imprevisto", [])
    nueva = next(t for t in ctrl.data_handler.get_tasks() if t['name'] == "Imprevisto")
    assert ctrl.data_handler.get_day_plan() == [ID1, nueva['id']]
    assert nueva['id'] in ctrl.tasks_panel._cards


def test_ventana_recuerda_su_posicion_y_no_abre_fuera_de_pantalla(ctrl, qtbot):
    from PySide6.QtGui import QGuiApplication
    from ui.windows.main_window import MainWindow
    area = QGuiApplication.primaryScreen().availableGeometry()
    # Guardada y visible: se respeta
    ctrl.window.move(area.left() + 40, area.top() + 30)
    ctrl._save_window_geometry()
    saved = ctrl.data_handler.load_settings()['window_geometry']
    win = MainWindow({'window_geometry': saved})
    qtbot.addWidget(win)
    assert (win.geometry().x(), win.geometry().y()) == (saved['x'], saved['y'])
    # Fuera de toda pantalla (monitor desconectado): centrada en la principal
    lost = dict(saved, x=area.right() + 5000)
    win2 = MainWindow({'window_geometry': lost})
    qtbot.addWidget(win2)
    assert area.contains(win2.geometry().center())


def test_ocultar_y_mostrar_el_panel_de_tareas(ctrl, qtbot):
    """El «❮» de v8: ocultar el panel estrecha la ventana y se recuerda."""
    win = ctrl.window
    qtbot.wait(50)
    width = win.width()
    ctrl.toggle_task_panel()
    assert not ctrl.tasks_panel.isVisibleTo(win)
    assert win.width() < width
    assert ctrl.data_handler.load_settings()['task_panel_collapsed'] is True
    ctrl.toggle_task_panel()
    assert ctrl.tasks_panel.isVisibleTo(win)
    assert win.width() == width
    assert ctrl.data_handler.load_settings()['task_panel_collapsed'] is False
    # Cambiar de lado con el panel oculto lo vuelve a mostrar primero
    ctrl.toggle_task_panel()
    ctrl.cycle_panel_position()
    assert ctrl.tasks_panel.isVisibleTo(win) and ctrl.panel_position == 'right'


def test_minimizar_y_ocultar_con_desvanecido(ctrl, qtbot):
    win = ctrl.window
    win.fade_minimize()
    qtbot.waitUntil(win.isMinimized, timeout=2000)
    qtbot.wait(250)
    assert win.windowOpacity() == pytest.approx(win._base_opacity, abs=0.01)
    win.showNormal()
    win.fade_hide()
    qtbot.waitUntil(lambda: not win.isVisible(), timeout=2000)
    assert win.windowOpacity() == pytest.approx(win._base_opacity, abs=0.01)
    win.fade_show()
    assert win.isVisible()
    qtbot.wait(400)
    assert win.windowOpacity() == pytest.approx(win._base_opacity, abs=0.01)


def test_anadir_desde_el_campo_rapido_con_fecha_y_etiquetas(ctrl):
    """El campo nuevo: «#etiquetas» en el nombre, fecha y repetición."""
    from datetime import date
    panel = ctrl.tasks_panel.add_panel
    panel.focus_input()
    panel.name_edit.setText("Preparar demo #ventas")
    panel.set_due(date(2026, 12, 1))
    panel.set_cycle('weekly')
    panel.submit()
    nueva = next(t for t in ctrl.data_handler.get_tasks() if t['name'] == "Preparar demo")
    assert nueva['tags'] == ['ventas']
    assert nueva['due'] == '2026-12-01'
    assert nueva['recurring'] == 'weekly'
    assert nueva['id'] in ctrl.tasks_panel._cards or ctrl.task_filter != 'pending'


def test_pista_para_hoy_sigue_al_filtro(ctrl):
    panel = ctrl.tasks_panel.add_panel
    ctrl.set_task_filter('today')
    assert panel._plan_hint
    ctrl.set_task_filter('pending')
    assert not panel._plan_hint


def test_el_timer_conserva_su_ancho_al_plegar_y_desplegar(ctrl, qtbot, monkeypatch):
    """Plegada, la ventana encaja justo en el timer (el ancho mínimo, sea cual
    sea el que tenía); desplegada, el timer mide lo mismo, no se mueve y la
    ventana crece por el lado de las tareas."""
    from PySide6.QtCore import QPoint
    from ui.windows.main_window import MIN_WIDTH_COLLAPSED, TIMER_WIDTH
    win, timer = ctrl.window, ctrl.timer_panel
    # La pantalla offscreen mide 800 px: sin esto, al desplegar se recolocaría
    # para no salirse de ella
    monkeypatch.setattr(win, "screen", lambda: None)

    def timer_x():
        return timer.mapToGlobal(QPoint(0, 0)).x()

    for side in ('left', 'right'):
        if ctrl.panel_position != side:
            ctrl.cycle_panel_position()
        for width in (1215, 960):
            win.setGeometry(200, 40, width, win.height())
            qtbot.wait(60)
            assert timer.width() == TIMER_WIDTH, (side, width)
            x = timer_x()
            ctrl.toggle_task_panel()
            qtbot.wait(60)
            assert win.width() == MIN_WIDTH_COLLAPSED, (side, width)
            assert (timer.width(), timer_x()) == (TIMER_WIDTH, x), (side, width)
            ctrl.toggle_task_panel()
            qtbot.wait(60)
            assert win.width() == width, (side, width)
            assert (timer.width(), timer_x()) == (TIMER_WIDTH, x), (side, width)


def test_botones_de_ventana_a_la_altura_de_las_acciones(ctrl, qtbot):
    """Minimizar, compacto y cerrar van centrados con el resto de iconos
    (antes, pegados arriba), juntos en su cápsula."""
    tb = ctrl.window.title_bar
    qtbot.wait(30)

    def center_y(w):
        return w.mapTo(tb, w.rect().center()).y()

    for b in (tb.min_btn, tb.compact_btn, tb.close_btn):
        assert b.parentWidget() is tb._window_controls
        assert abs(center_y(b) - center_y(tb.settings_btn)) <= 1, b.toolTip()


def test_compacto_entre_minimizar_y_cerrar(ctrl, qtbot):
    """El modo compacto vive en la cápsula, entre «–» y «✕» (pedido del
    usuario), hermano de los otros dos; ya no es una acción más."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication
    from ui.theme.tokens import tokens
    from ui.widgets.icon_button import tinted_icon
    tb = ctrl.window.title_bar
    btn = tb.compact_btn
    qtbot.wait(30)
    controls = tb._window_controls.layout()
    order = [controls.itemAt(i).widget() for i in range(controls.count())]
    assert order == [tb.min_btn, btn, tb.close_btn]
    assert [b.x() for b in order] == sorted(b.x() for b in order)
    assert btn not in tb._buttons
    assert {b.size().toTuple() for b in order} == {(34, 28)}
    assert btn.isEnabled() and btn.toolTip() == "Modo compacto"

    # Icono atenuado; al pasar el ratón, del color del texto (como los glifos)
    def tinted(color):
        return btn.icon().cacheKey() == tinted_icon("compacto", color, size).cacheKey()

    t = tokens()
    size = btn.iconSize().width()
    assert tinted(t.fg_dim)
    # Del tamaño visual de los glifos: la «X» de cerrar mide ~12×10 px (a 16
    # px el icono medía 14×12 y pesaba casi el doble que ella)
    image = btn.icon().pixmap(btn.iconSize()).toImage()
    ink = [(x, y) for y in range(image.height()) for x in range(image.width())
           if image.pixelColor(x, y).alpha() >= 128]
    xs, ys = [x for x, _ in ink], [y for _, y in ink]
    dpr = image.devicePixelRatio()
    width, height = (max(xs) - min(xs) + 1) / dpr, (max(ys) - min(ys) + 1) / dpr
    assert width <= 12.5 and height <= 10.5, (width, height)
    QApplication.sendEvent(btn, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
    assert tinted(t.fg)
    QApplication.sendEvent(btn, QEvent(QEvent.Leave))
    assert tinted(t.fg_dim)

    # Un clic pide el modo compacto, como antes (sin abrirlo en el test)
    tb.compact_requested.disconnect(ctrl.show_compact)
    with qtbot.waitSignal(tb.compact_requested, timeout=500):
        qtbot.mouseClick(btn, Qt.LeftButton)


def test_cabecera_se_recoge_sin_montarse(ctrl, qtbot):
    """Al estrechar la ventana (o con el panel oculto) la cabecera quita cosas
    por orden en vez de pintar unas encima de otras."""
    from ui.windows.main_window import MIN_WIDTH_COLLAPSED
    win, tb, xp = ctrl.window, ctrl.window.title_bar, ctrl.xp_bar
    ctrl.toggle_task_panel()  # permite bajar hasta el mínimo plegado

    def spans(parent, widgets):
        out = []
        for w in widgets:
            if w.isVisibleTo(parent):
                g = w.geometry()
                if w.parentWidget() is not parent:
                    g.moveTopLeft(w.parentWidget().mapTo(parent, g.topLeft()))
                out.append((g.left(), g.right()))
        return sorted(out)

    for width in (1400, 900, 700, 560, MIN_WIDTH_COLLAPSED):
        win.resize(width, win.height())
        qtbot.wait(60)
        top = spans(tb, [tb.logo, tb.title, xp, *tb._buttons, tb.min_btn, tb.compact_btn,
                         tb.close_btn])
        inner = spans(xp, [xp.level_label, xp.bar, xp.xp_label,
                           xp.coins_icon, xp.coins_label])
        for group in (top, inner):
            for (_, r1), (l2, _) in zip(group, group[1:]):
                assert r1 < l2, (width, group)
        assert top[-1][1] < tb.width(), width
    # A lo ancho se ve todo
    win.resize(1400, win.height())
    qtbot.wait(60)
    assert tb.title.isVisibleTo(tb) and xp.xp_label.isVisibleTo(xp)


def test_orden_compacto_sin_recortes(ctrl, qtbot):
    """Al ancho mínimo los cinco órdenes no caben con su texto: pasan a
    iconos (el elegido conserva el texto) en vez de recortarse («Manu»)."""
    from ui.widgets.task_list import SORTS
    from ui.windows.main_window import MIN_WIDTH
    win, panel = ctrl.window, ctrl.tasks_panel
    panel.set_advanced_open(True, animate=False)
    buttons = [panel._sort_buttons[k] for k, _ in SORTS]

    win.resize(MIN_WIDTH, win.height())
    qtbot.wait(60)
    assert panel._sort_compact and not panel._sort_caption.isVisibleTo(panel)
    for b in buttons:
        assert b.width() >= b.sizeHint().width(), (b.text(), b.width())
        assert bool(b.text()) == b.isChecked() and b.toolTip()
    win.resize(1400, win.height())
    qtbot.wait(60)
    assert not panel._sort_compact and panel._sort_caption.isVisibleTo(panel)
    assert [b.text() for b in buttons] == [label for _, label in SORTS]


def test_lista_vacia_no_estira_el_campo_de_anadir(ctrl, qtbot):
    """Sin tareas (instalación nueva) el aviso ocupa el sitio de la lista y el
    campo de añadir conserva su alto de una línea."""
    panel = ctrl.tasks_panel
    ctrl.window.resize(1215, 795)
    panel.set_tasks([], None, {}, 'pending', 'due')
    qtbot.wait(60)
    assert panel.empty_label.isVisibleTo(panel)
    assert panel.add_panel.height() <= panel.add_panel.sizeHint().height() + 2
    assert panel.empty_label.height() > panel.add_panel.height()


def test_cambio_de_dia_retira_los_comodines_del_enfoque(ctrl):
    """La app puede pasar la noche en la bandeja: el comodín no llega a mañana."""
    from datetime import date, timedelta
    fm = ctrl.focus_mode
    fm.is_active = False  # nunca escribir el hosts real desde un test
    yesterday = date.today() - timedelta(days=1)
    fm.session_allowed = {'whatsapp.com'}
    fm._session_day = yesterday
    ctrl._last_seen_date = yesterday
    ctrl._tick()
    assert fm.session_allowed == set()


def test_si_los_comodines_no_pueden_caducar_avisa_una_vez(ctrl, monkeypatch):
    """Si el hosts no se deja reescribir al cambiar de día, esos sitios siguen
    libres: un aviso (una vez), y el núcleo lo reintenta."""
    from ui.controllers import app_controller
    shown = []
    monkeypatch.setattr(app_controller.toast, "show_warning",
                        lambda win, msg, duration=0: shown.append(msg))
    ctrl.focus_mode.expire_error = "se requieren permisos de administrador"
    ctrl._tick()
    ctrl._tick()
    assert len(shown) == 1 and "administrador" in shown[0]
    ctrl.focus_mode.expire_error = ''
    ctrl._tick()
    assert len(shown) == 1


# ---------- v9.4: firma en Ajustes, salir con la X y despedida con redes ----------

@pytest.fixture
def opened(monkeypatch):
    """URLs que la app intentó abrir: el navegador de verdad nunca se abre."""
    from PySide6.QtGui import QDesktopServices
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl",
                        staticmethod(lambda url: urls.append(url.toString()) or True))
    return urls


def test_ajustes_firma_fabricioplus_en_la_fila_de_los_botones(ctrl, qtbot, opened):
    """«Fabricioplus» ocupa el hueco de la izquierda del pie, en la MISMA fila
    que «Cancelar» y «Guardar» y centrado con ellos; abre el repositorio,
    toma el acento al pasar el ratón y sigue al tema."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication
    from core.config import Config
    from ui.dialogs.settings_dialog import SettingsDialog
    from ui.theme.tokens import THEMES, ThemeManager, tokens
    from ui.widgets.icon_button import tinted_icon
    from ui.theme.qss import build_qss
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    # La hoja de estilos de la app, solo en el diálogo: botones de 32 px como
    # en la app (sin ella miden casi lo que el enlace y no se ve si se centra)
    dlg.setStyleSheet(build_qss(tokens(), fonts.ui_family()))
    dlg.show()
    qtbot.waitExposed(dlg)
    link = dlg.github_link
    row = dlg._buttons_row
    widgets = [row.itemAt(i).widget() for i in range(row.count()) if row.itemAt(i).widget()]
    assert [w.text().strip() for w in widgets] == ["Fabricioplus", "Cancelar", "Guardar"]
    assert widgets[0] is link and link.toolTip() == Config.GITHUB_URL
    cancel = widgets[1]
    assert link.geometry().right() < cancel.geometry().left()
    assert link.x() <= dlg._lay.contentsMargins().left() + 2  # pegado a la izquierda
    assert abs(link.geometry().center().y() - cancel.geometry().center().y()) <= 1
    assert link.height() + 8 <= cancel.height()  # texto pequeño, no un botón más
    # Enter en el diálogo nunca abre la web
    assert not link.autoDefault() and not link.isDefault()
    assert link.focusPolicy() == Qt.NoFocus

    def icon_is(color):
        return link.icon().cacheKey() == tinted_icon("brand-github", color, 14).cacheKey()

    assert icon_is(tokens().fg_muted)
    QApplication.sendEvent(link, QEnterEvent(QPointF(4, 4), QPointF(4, 4), QPointF(4, 4)))
    assert icon_is(tokens().accent)
    # Con otro tema (a mano, como test_brand: emitir theme_changed despertaría
    # a ventanas ya destruidas de otros tests)
    manager = ThemeManager.instance()
    original = manager.current
    try:
        manager.current = THEMES['theme_sunset']
        link._retheme()
        assert icon_is(tokens().accent) and tokens().accent in link.styleSheet()
        QApplication.sendEvent(link, QEvent(QEvent.Leave))
        assert icon_is(tokens().fg_muted)
    finally:
        manager.current = original
        link._retheme()

    qtbot.mouseClick(link, Qt.LeftButton)
    assert opened == [Config.GITHUB_URL] == ["https://github.com/fabricioplus/pomogrowth"]
    assert dlg.isVisible()  # el diálogo sigue abierto
    dlg.reject()


def test_la_x_pregunta_antes_de_salir(ctrl, qtbot, monkeypatch):
    """La X ya no oculta en la bandeja: pregunta y, con «Salir», sale del
    todo (quit_with_farewell); con «Cancelar», todo sigue igual."""
    from PySide6.QtCore import Qt
    answers, quits = [], []
    monkeypatch.setattr(ctrl, "_ask_quit", lambda: answers.pop(0))
    monkeypatch.setattr(ctrl, "quit_with_farewell", lambda: quits.append(1))
    tb, win = ctrl.window.title_bar, ctrl.window
    assert tb.close_btn.toolTip() == "Salir de PomoGrowth"

    answers.append(False)  # «Cancelar» (o Esc)
    qtbot.mouseClick(tb.close_btn, Qt.LeftButton)
    qtbot.wait(300)  # lo que tardaba el desvanecido al ocultarse
    assert answers == [] and quits == []
    assert win.isVisible() and not ctrl._is_closing

    answers.append(True)   # «Salir»
    qtbot.mouseClick(tb.close_btn, Qt.LeftButton)
    assert answers == [] and quits == [1]


def test_pregunta_de_salir_de_la_casa(ctrl, qtbot):
    """El diálogo de verdad: título, «Cancelar»/«Salir», Esc cancela, «Salir»
    acepta y, con un pomodoro en marcha, avisa de que su tiempo se guarda."""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QLabel, QPushButton
    seen = []

    def answer(how):
        dlg = ctrl._quit_dialog
        buttons = dlg._card.findChildren(QPushButton)
        seen.append((dlg._title_label.text(), [b.text() for b in buttons],
                     " ".join(lb.text() for lb in dlg._card.findChildren(QLabel)
                              if lb.wordWrap())))
        if how == "esc":
            qtbot.keyClick(dlg, Qt.Key_Escape)
        else:
            next(b for b in buttons if b.text() == how).click()

    QTimer.singleShot(100, lambda: answer("esc"))
    assert ctrl._ask_quit() is False
    QTimer.singleShot(100, lambda: answer("Cancelar"))
    assert ctrl._ask_quit() is False
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    assert ctrl.model.is_running
    QTimer.singleShot(100, lambda: answer("Salir"))
    assert ctrl._ask_quit() is True
    assert ctrl._quit_dialog is None
    assert [s[:2] for s in seen] == [("¿Estás seguro de salir del programa?",
                                      ["Cancelar", "Salir"])] * 3
    assert "se guarda" not in seen[0][2] and "minimízalo" in seen[0][2]
    assert "pomodoro en marcha" in seen[2][2] and "se guarda" in seen[2][2]
    assert ctrl.model.is_running and not ctrl._is_closing  # preguntar no toca nada
    ctrl.toggle_timer()


def _alt_f4():
    """QCloseEvent de un cierre del sistema (Alt+F4, «Cerrar ventana» de la
    barra de tareas), que llega espontáneo; un close() del código, no. Desde
    Python no se puede crear uno espontáneo de verdad."""
    from PySide6.QtGui import QCloseEvent

    class AltF4(QCloseEvent):
        def spontaneous(self):
            return True
    return AltF4()


def test_alt_f4_pasa_por_la_misma_pregunta(ctrl, qtbot, monkeypatch):
    from PySide6.QtWidgets import QApplication
    answers, asked, quits = [], [], []

    def ask():
        asked.append(1)
        return answers.pop(0)
    monkeypatch.setattr(ctrl, "_ask_quit", ask)
    monkeypatch.setattr(ctrl, "quit_with_farewell", lambda: quits.append(1))
    win = ctrl.window

    answers.append(False)
    event = _alt_f4()
    QApplication.sendEvent(win, event)
    assert asked == [1] and quits == [] and not event.isAccepted()
    qtbot.wait(300)
    assert win.isVisible()  # ya no se oculta sin más

    answers.append(True)
    event = _alt_f4()
    QApplication.sendEvent(win, event)
    assert quits == [1] and not event.isAccepted()  # sale quit_with_farewell

    # En pleno cierre (la despedida, QApplication.quit) no se pregunta
    ctrl._is_closing = True
    event = _alt_f4()
    QApplication.sendEvent(win, event)
    assert event.isAccepted() and len(asked) == 2
    ctrl._is_closing = False
    # Un close() desde el código (tests, scripts) tampoco pregunta
    win.close()
    assert len(asked) == 2 and not win.isVisible()


def test_una_sola_pregunta_a_la_vez(ctrl, monkeypatch):
    """Otra X o un Alt+F4 con la pregunta abierta no abren una segunda, y
    ya cerrando no se pregunta."""
    from PySide6.QtWidgets import QApplication
    asked = []

    def ask():
        asked.append(1)
        ctrl.confirm_quit()
        QApplication.sendEvent(ctrl.window, _alt_f4())
        return False
    monkeypatch.setattr(ctrl, "_ask_quit", ask)
    ctrl.confirm_quit()
    assert asked == [1]
    ctrl.confirm_quit()  # cerrada la primera, se puede volver a preguntar
    assert asked == [1, 1]
    ctrl._is_closing = True
    ctrl.confirm_quit()
    QApplication.sendEvent(ctrl.window, _alt_f4())
    assert asked == [1, 1]
    ctrl._is_closing = False


@contextmanager
def _failsafe_quit_dialog(ctrl, ms=3000):
    """Red de seguridad de los tests con la pregunta de salir real: si algo la
    deja abierta, la cierra a los `ms` y el test falla en vez de quedarse
    colgado en su exec() (no hay pytest-timeout)."""
    from PySide6.QtCore import QTimer
    stuck = []

    def unstick():
        if ctrl._quit_dialog is not None:
            stuck.append(1)
            ctrl._quit_dialog.reject()
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(unstick)
    guard.start(ms)
    try:
        yield
    finally:
        guard.stop()
    assert not stuck, "la pregunta de salir se quedó abierta"


def _tray_action(ctrl, text):
    return next(a for a in ctrl.tray.contextMenu().actions() if a.text() == text)


def test_cerrar_del_tray_pregunta_como_la_x(ctrl, monkeypatch):
    """«Cerrar» del menú de la bandeja también pregunta: con «Cancelar» todo
    sigue igual y con «Salir» sale una vez (quit_with_farewell)."""
    answers, quits = [], []
    monkeypatch.setattr(ctrl, "_ask_quit", lambda: answers.pop(0))
    monkeypatch.setattr(ctrl, "quit_with_farewell", lambda: quits.append(1))
    close = _tray_action(ctrl, "Cerrar")

    answers.append(False)  # «Cancelar» (o Esc)
    close.trigger()
    assert answers == [] and quits == []
    assert ctrl.window.isVisible() and not ctrl._is_closing

    answers.append(True)   # «Salir»
    close.trigger()
    assert answers == [] and quits == [1]


def test_cerrar_del_tray_con_la_pregunta_abierta(ctrl, monkeypatch):
    """«Cerrar» de la bandeja con la pregunta de la X ya abierta no abre otra:
    decide la que está abierta y se sale una sola vez."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QPushButton
    quits, seen = [], []
    monkeypatch.setattr(ctrl, "quit_with_farewell", lambda: quits.append(1))
    close = _tray_action(ctrl, "Cerrar")

    def from_tray():
        dlg = ctrl._quit_dialog
        close.trigger()  # con la pregunta abierta
        seen.append((dlg, ctrl._quit_dialog))
        next(b for b in dlg._card.findChildren(QPushButton) if b.text() == "Salir").click()
    QTimer.singleShot(100, from_tray)
    with _failsafe_quit_dialog(ctrl):
        ctrl.confirm_quit()
    assert len(seen) == 1 and seen[0][0] is not None and seen[0][1] is seen[0][0]
    assert quits == [1] and ctrl._quit_dialog is None


def test_minimizada_pregunta_sobre_la_ventana_restaurada(ctrl, qtbot, monkeypatch):
    """Minimizada, «Cerrar» de la bandeja (o «Cerrar ventana» de la barra de
    tareas) la restaura antes de preguntar: la pregunta no sale sobre una
    ventana que no se ve. Con «Cancelar» se queda a la vista."""
    asked, quits = [], []
    monkeypatch.setattr(ctrl, "_ask_quit",
                        lambda: asked.append(ctrl.window.isMinimized()) or False)
    monkeypatch.setattr(ctrl, "quit_with_farewell", lambda: quits.append(1))
    win = ctrl.window
    win.showMinimized()
    qtbot.waitUntil(win.isMinimized)
    _tray_action(ctrl, "Cerrar").trigger()
    assert asked == [False] and quits == [] and win.isVisible()
    qtbot.waitUntil(lambda: win.windowOpacity() > 0.99, timeout=2000)  # su fade de vuelta


def test_salir_con_la_pregunta_abierta_la_cierra(ctrl, monkeypatch):
    """Si algo sale (quit_with_farewell) con la pregunta abierta, la cierra en
    vez de dejarla flotando, y sale una sola vez."""
    from PySide6.QtCore import QTimer
    from ui.windows import welcome_farewell
    farewells = []
    monkeypatch.setattr(welcome_farewell, "FarewellWindow",
                        lambda on_done: farewells.append(on_done))
    monkeypatch.setattr(ctrl.focus_mode, "cleanup_system_blocks", lambda: False)
    QTimer.singleShot(100, ctrl.quit_with_farewell)
    with _failsafe_quit_dialog(ctrl):
        ctrl.confirm_quit()
    assert ctrl._is_closing and len(farewells) == 1
    assert ctrl._quit_dialog is None and not ctrl.window.isVisible()


def _farewell(qtbot, duration_ms=None):
    """Despedida con un on_done que cuenta; sin `duration_ms`, la de la app."""
    from ui.windows.welcome_farewell import FarewellWindow
    fonts.init_fonts()
    calls = []
    extra = {} if duration_ms is None else {'duration_ms': duration_ms}
    fw = FarewellWindow(lambda: calls.append(1), **extra)
    qtbot.addWidget(fw)
    return fw, calls


def test_despedida_con_las_redes_de_config(qtbot, opened):
    """«Hasta pronto» con la invitación y una fila por cuenta: los 10 enlaces
    salen de Config.SOCIAL_LINKS, en orden y con su tooltip. Dura ~8 s (para
    poder hacer clic) y sigue centrada y de un ancho razonable."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel
    from core.config import Config
    fw, calls = _farewell(qtbot)
    names = ["Facebook", "Instagram", "YouTube", "TikTok", "LinkedIn"]
    expected = [(f"{name} · {handle}", url) for handle, links in Config.SOCIAL_LINKS
                for name, (_net, url) in zip(names, links)]
    assert [(link.toolTip(), link.url) for link in fw.links] == expected
    assert len(expected) == 10 and len({url for _t, url in expected}) == 10
    assert all(url.startswith("https://") for _t, url in expected)
    texts = [lb.text() for lb in fw._card.findChildren(QLabel)]
    for text in ("Hasta pronto", "¿Disfrutas de PomoGrowth?",
                 "Puedes darnos tu apoyo.",
                 "@xgrowthlab", "@fabricioplus"):
        assert text in texts, text
    assert fw._countdown.duration() >= 8000
    assert 420 <= fw._card.width() <= 480
    area = fw.screen().availableGeometry()
    assert abs(fw.geometry().center().x() - area.center().x()) <= 1
    assert abs(fw.geometry().center().y() - area.center().y()) <= 1

    # Un enlace abre su web y la despedida se va poco después, una sola vez
    qtbot.mouseClick(fw.links[7], Qt.LeftButton)  # YouTube de @fabricioplus
    assert opened == ["https://youtube.com/@fabricioplus"]
    qtbot.waitUntil(lambda: calls == [1], timeout=2000)
    assert not fw.isVisible()
    qtbot.keyClick(fw, Qt.Key_Escape)
    fw.close()
    qtbot.wait(400)
    assert calls == [1]


@pytest.mark.parametrize("which", [0, 1, 2, 3])
def test_despedida_repositorio_de_github_encima_de_las_cuentas(qtbot, opened, which):
    """Encima de las cuentas, el repositorio y Ko-fi, cada uno en su fila:
    «fabricioplus/pomogrowth» y «Dar estrella» abren Config.GITHUB_URL,
    «Apóyanos en Ko-fi» y «Donar», Config.KOFI_URL, y la despedida se va
    una vez."""
    from PySide6.QtCore import Qt
    from core.config import Config
    fw, calls = _farewell(qtbot)
    repo, star = fw.repo_links
    kofi, donate = fw.kofi_links
    assert repo.text().strip() == "fabricioplus/pomogrowth"
    assert star.text().strip() == "Dar estrella" and star._icon_name == "star"
    assert repo.url == star.url == Config.GITHUB_URL
    assert kofi.text().strip() == "Apóyanos en Ko-fi" and kofi._icon_name == "brand-kofi"
    assert donate.text().strip() == "Donar" and donate._icon_name == "coffee"
    assert kofi.url == donate.url == Config.KOFI_URL == "https://ko-fi.com/L3Q421O7ZW"
    assert "ko-fi.com/L3Q421O7ZW" in donate.toolTip()
    # Repositorio, luego Ko-fi y luego la primera cuenta, en el mismo carril
    first_account = fw.links[0]
    ys = [w.mapTo(fw._card, w.rect().center()).y() for w in (repo, kofi, first_account)]
    assert ys == sorted(ys) and len(set(ys)) == 3
    rail = first_account.parentWidget().parentWidget()
    assert repo.parentWidget().parentWidget() is kofi.parentWidget().parentWidget() is rail
    # Cada cápsula, a la altura de su enlace y a su derecha
    for link, pill in ((repo, star), (kofi, donate)):
        assert abs(link.geometry().center().y() - pill.geometry().center().y()) <= 1
        assert pill.geometry().left() > link.geometry().right()
        assert pill.height() == pill.PILL_HEIGHT
        assert pill.focusPolicy() == Qt.NoFocus and not pill.autoDefault()
    # Cada icono de la despedida tiene su SVG (si falta, tinted_icon pinta un
    # hueco sin avisar: brand-kofi.svg llegó a estar fuera de git)
    from ui.widgets.icon_button import ICONS_DIR
    links = fw.repo_links + fw.kofi_links
    for button in links + fw.links:
        assert (ICONS_DIR / f"{button._icon_name}.svg").is_file(), button._icon_name
    qtbot.mouseClick(links[which], Qt.LeftButton)
    assert opened == [Config.KOFI_URL if which >= 2 else Config.GITHUB_URL]
    qtbot.waitUntil(lambda: calls == [1], timeout=2000)
    fw.close()
    qtbot.wait(300)
    assert calls == [1]


def test_readme_y_config_tienen_los_mismos_enlaces():
    """El README repite los enlaces de Config (Markdown no puede leerlos): que
    no se desincronicen, y que una errata en una URL de Config no pase sin
    verse (el test de la despedida compara Config consigo mismo)."""
    from core.config import Config
    text = (V9 / "README.md").read_text(encoding="utf-8")
    refs = dict(re.findall(r"^\[([a-z]+-[a-z]+)\]: (\S+)$", text, re.M))
    for handle, links in Config.SOCIAL_LINKS:
        for network, url in links:
            assert refs.get(f"{handle[1:]}-{network}") == url, (handle, network)
    assert refs["fabricioplus-github"] == Config.GITHUB_URL.rsplit("/", 1)[0]
    assert f"git clone {Config.GITHUB_URL}.git" in text
    assert f"]({Config.GROWTHLAB_URL})" in text
    assert f"]({Config.KOFI_URL})" in text


def test_despedida_colores_de_las_redes(qtbot):
    """Atenuados en reposo; al pasar el ratón, el color de la marca (TikTok,
    el del texto) o el acento si la marca no se lee sobre el fondo."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication
    from ui.theme.tokens import tokens
    from ui.widgets.icon_button import tinted_icon
    from ui.windows.welcome_farewell import network_hover_color
    fw, _calls = _farewell(qtbot)
    t = tokens()
    brand = {"facebook": "#1877F2", "instagram": "#E4405F", "youtube": "#FF0000",
             "tiktok": t.fg, "linkedin": "#0A66C2"}
    for link in fw.links:
        network = link._icon_name.removeprefix("brand-")
        assert link.icon().cacheKey() == tinted_icon(link._icon_name, t.fg_dim, 16).cacheKey()
        QApplication.sendEvent(link, QEnterEvent(QPointF(4, 4), QPointF(4, 4), QPointF(4, 4)))
        assert link.icon().cacheKey() == tinted_icon(link._icon_name, brand[network],
                                                     16).cacheKey(), network
        QApplication.sendEvent(link, QEvent(QEvent.Leave))
    assert network_hover_color("linkedin", "#0A66C2") == t.accent  # no se leería


@pytest.mark.parametrize("how", ["tiempo", "esc", "cerrar", "enlace", "fuera"])
def test_despedida_llama_a_on_done_una_sola_vez(qtbot, opened, how):
    from PySide6.QtCore import Qt
    fw, calls = _farewell(qtbot, duration_ms=300 if how == "tiempo" else 8000)
    if how == "esc":
        qtbot.keyClick(fw, Qt.Key_Escape)
    elif how == "cerrar":
        qtbot.mouseClick(fw.close_link, Qt.LeftButton)
    elif how == "enlace":
        qtbot.mouseClick(fw.links[0], Qt.LeftButton)
    elif how == "fuera":
        # Alt+F4 sobre ella o un script: sin on_done, con la ventana y la
        # bandeja ya ocultas, la app quedaría viva y sin ninguna interfaz
        fw.close()
    qtbot.waitUntil(lambda: calls == [1], timeout=2000)
    # Lo que llegue después (otra tecla, otro clic, el cierre) no repite
    qtbot.keyClick(fw, Qt.Key_Escape)
    fw.close_now()
    fw.close()
    qtbot.wait(400)
    assert calls == [1]
    assert opened == ([fw.links[0].url] if how == "enlace" else [])


def test_despedida_pausa_con_el_raton_encima(qtbot):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication
    fw, calls = _farewell(qtbot, duration_ms=400)
    fw._hover_armed = True  # el ratón ya se movió desde que apareció
    QApplication.sendEvent(fw._card, QEnterEvent(QPointF(5, 5), QPointF(5, 5),
                                                 QPointF(5, 5)))
    assert fw.is_paused() and fw._bar._paused
    qtbot.wait(900)
    assert calls == [] and fw.isVisible()
    QApplication.sendEvent(fw._card, QEvent(QEvent.Leave))
    assert not fw.is_paused()
    qtbot.waitUntil(lambda: calls == [1], timeout=2000)


def test_despedida_bajo_el_cursor_no_se_queda_pausada(qtbot, monkeypatch):
    """Si aparece justo bajo el ratón (donde estaba «Salir») no se pausa
    hasta que el ratón se mueva: soltar el ratón no la deja para siempre."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QEnterEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication
    from ui.windows.welcome_farewell import FarewellWindow
    monkeypatch.setattr(FarewellWindow, "_cursor_on_card", lambda self: True)
    fw, calls = _farewell(qtbot, duration_ms=8000)
    QApplication.sendEvent(fw._card, QEnterEvent(QPointF(5, 5), QPointF(5, 5),
                                                 QPointF(5, 5)))
    assert not fw.is_paused()
    move = QMouseEvent(QEvent.MouseMove, QPointF(9, 9), QPointF(9, 9), QPointF(9, 9),
                       Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(fw._card, move)
    assert fw.is_paused()
