"""Ajustes → ClickUp honesto: en la 1.0 ClickUp solo crea tareas desde el
alta (ui/controllers/task_destinations.py), no sincroniza.

Antes la pestaña tenía una sección «Sincronización» con «Incluir tareas
completadas» y «Sincronizar al abrir la app» que se guardaban
(clickup_include_completed, clickup_auto_sync) pero nadie leía, y los textos
prometían sincronizar («Sincroniza las tareas de una lista de ClickUp»,
«Lista a sincronizar», «Lista cuyas tareas se sincronizan»). El botón de
sync del temporizador decía «Sincronizar ahora (Asana/ClickUp)» aunque solo
sincroniza Asana (AppController.sync_asana).

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_clickup_ajustes.py -q
Datos sintéticos en un directorio temporal; la red, falsa (fixture fake_net).
"""
import json
import os
import sys
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel  # noqa: E402

from core.config import Config  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from ui.theme import fonts  # noqa: E402

SYNC_KEYS = ('clickup_include_completed', 'clickup_auto_sync')
CLICKUP_HOST = 'api.clickup.com'


def _write_data(tmp_path: Path, settings=None) -> Path:
    data = {'tasks': [], 'deleted_tasks': [], 'time_chunks': [], 'history': [],
            'ui_snapshot': {},
            'settings': {'task_sort_mode': 'manual', **(settings or {})}}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def make_ctrl(qtbot, tmp_path, monkeypatch):
    """Controlador real sobre datos temporales (sin Asana configurado: nada
    sale a la red al abrir ni al guardar Ajustes)."""
    monkeypatch.chdir(tmp_path)
    # apply_settings escribe la URL de n8n en Config: que no pase al siguiente test
    monkeypatch.setattr(Config, "N8N_WEBHOOK_URL", Config.N8N_WEBHOOK_URL)
    fonts.init_fonts()

    def build(settings=None):
        handler = DataHandler(_write_data(tmp_path, settings))
        from ui.controllers.app_controller import AppController
        from ui.windows.main_window import MainWindow
        win = MainWindow(handler.data.get('settings', {}))
        qtbot.addWidget(win)
        controller = AppController(win, handler)
        win.show()
        qtbot.waitExposed(win)
        return controller

    return build


@pytest.fixture
def open_dialog(qtbot):
    def build(ctrl):
        from ui.dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(ctrl.window, ctrl)
        qtbot.addWidget(dlg)
        return dlg
    return build


def _clickup_tab(dlg):
    from ui.dialogs.settings_dialog import TABS
    index = [label for _icon, label in TABS].index("ClickUp")
    assert dlg.tabs.tabText(index) == "ClickUp"
    return dlg.tabs.widget(index)


def _labels(tab):
    return [lbl.text() for lbl in tab.findChildren(QLabel)]


def _row(tab, title):
    """La fila de ajuste con ese título (exactamente una)."""
    from ui.dialogs.settings_dialog import _SettingRow
    rows = [r for r in tab.findChildren(_SettingRow) if r.title.text() == title]
    assert len(rows) == 1, f"filas «{title}»: {len(rows)}"
    return rows[0]


# ------------------------------------------------------------- la pestaña
def test_la_pestana_no_tiene_interruptores_de_sincronizacion(make_ctrl, open_dialog):
    """Sin la sección «Sincronización» ni sus dos interruptores: el único
    on/off de la pestaña es el de la integración."""
    from ui.widgets.toggle_switch import ToggleSwitch
    ctrl = make_ctrl({'active_integrations': ['clickup'],
                      'clickup_include_completed': True, 'clickup_auto_sync': True})
    dlg = open_dialog(ctrl)
    tab = _clickup_tab(dlg)
    labels = _labels(tab)
    for gone in ("Sincronización", "Incluir tareas completadas",
                 "Sincronizar al abrir la app", "Trae también las tareas cerradas de la lista",
                 "Actualiza la lista cada vez que inicias PomoGrowth"):
        assert gone not in labels
    assert not hasattr(dlg, 'chk_cu_completed') and not hasattr(dlg, 'chk_cu_auto')
    switches = tab.findChildren(ToggleSwitch)
    assert switches == [dlg.chk_clickup] and dlg.chk_clickup.isChecked()
    # Las secciones que quedan, en orden
    assert [t for t in labels if t in ("Conexión", "Lista donde crear")] == \
        ["Conexión", "Lista donde crear"]
    dlg.reject()


def test_textos_de_la_pestana_no_prometen_sincronizar(make_ctrl, open_dialog):
    ctrl = make_ctrl()
    dlg = open_dialog(ctrl)
    tab = _clickup_tab(dlg)
    assert _row(tab, "Integración con ClickUp").help.text() == \
        "Crea en ClickUp las tareas que añadas, en la lista que elijas"
    assert _row(tab, "Lista").help.text() == "Donde se crean las tareas nuevas"
    labels = _labels(tab)
    assert "Lista donde crear" in labels
    assert "Lista a sincronizar" not in labels
    # Ningún texto de la pestaña habla de sincronizar
    assert not [t for t in labels if "sincroniz" in t.lower()]
    dlg.reject()


# --------------------------------------------------------------- guardar
def test_guardar_no_escribe_las_claves_de_sincronizacion(make_ctrl, open_dialog):
    ctrl = make_ctrl({'active_integrations': ['clickup']})
    assert not any(k in ctrl.data_handler.load_settings() for k in SYNC_KEYS)
    dlg = open_dialog(ctrl)
    dlg.accept_settings()
    saved = ctrl.data_handler.load_settings()
    assert not any(k in saved for k in SYNC_KEYS)
    # Y en el archivo tampoco
    on_disk = json.loads(Path(ctrl.data_handler.filepath).read_text(encoding="utf-8"))
    assert not any(k in on_disk['settings'] for k in SYNC_KEYS)
    dlg.reject()


def test_las_claves_que_ya_existian_se_quedan_sin_tocar(make_ctrl, open_dialog):
    """No se borran: la 1.1 podría usarlas. Guardar las deja como estaban."""
    ctrl = make_ctrl({'clickup_include_completed': True, 'clickup_auto_sync': 'raro'})
    dlg = open_dialog(ctrl)
    dlg.accept_settings()
    saved = ctrl.data_handler.load_settings()
    assert saved['clickup_include_completed'] is True
    assert saved['clickup_auto_sync'] == 'raro'
    dlg.reject()


def test_guardar_la_pestana_deja_lista_la_creacion_desde_el_alta(
        make_ctrl, open_dialog, qtbot, fake_net):
    """Lo que sí hace la pestaña: activar, token y lista. Tras guardar, el
    logo de ClickUp del alta queda listo y la tarea nueva se crea en esa lista."""
    created = {'id': 'cu1', 'name': "Para ClickUp", 'status': {'status': 'to do'},
               'url': 'https://app.clickup.com/t/cu1', 'list': {'id': 'L1'}}
    fake_net.post(CLICKUP_HOST, r'/api/v2/list/L1/task', created)
    ctrl = make_ctrl({'active_integrations': [], 'clickup_list_id': 'L1'})
    panel = ctrl.tasks_panel.add_panel
    assert panel.clickup_btn.is_dimmed()
    dlg = open_dialog(ctrl)
    dlg.chk_clickup.setChecked(True)
    dlg.ed_cu_token.setText("  pk_prueba  ")
    dlg.accept_settings()
    saved = ctrl.data_handler.load_settings()
    assert 'clickup' in saved['active_integrations']
    assert saved['clickup_api_token'] == 'pk_prueba'
    assert saved['clickup_list_id'] == 'L1'          # combos sin cargar: el guardado
    assert not any(k in saved for k in SYNC_KEYS)
    dlg.reject()
    assert not panel.clickup_btn.is_dimmed()
    panel.clickup_btn.click()
    assert panel.targets == {'clickup': True}
    ctrl.add_task("Para ClickUp", [], '', '', panel.targets)
    task = next(t for t in ctrl.data_handler.get_tasks() if t['name'] == "Para ClickUp")
    qtbot.waitUntil(lambda: task.get('clickup_id') == 'cu1', timeout=5000)
    calls = fake_net.calls_to('POST', host=CLICKUP_HOST)
    assert len(calls) == 1
    assert calls[0].json['name'] == "Para ClickUp"
    assert calls[0].headers['Authorization'] == 'pk_prueba'
    assert task['clickup_url'] == 'https://app.clickup.com/t/cu1'
    # Crear no trae nada de vuelta: ni un GET a ClickUp
    assert fake_net.methods() == ['POST']


# ------------------------------------------------ el botón de sincronizar
def test_el_boton_de_sync_solo_promete_asana(make_ctrl, qtbot):
    """El botón llama a AppController.sync_asana: nada de ClickUp."""
    from ui.widgets.timer_panel import TimerPanel
    fonts.init_fonts()
    panel = TimerPanel()
    qtbot.addWidget(panel)
    assert panel.sync_btn.toolTip() == "Sincronizar ahora con Asana"
    ctrl = make_ctrl({'active_integrations': ['asana', 'clickup']})
    tp = ctrl.timer_panel
    for state in ('off', 'idle', 'syncing', 'online', 'offline'):
        tp.set_sync_state(state)
        assert "ClickUp" not in tp.sync_btn.toolTip()
    tp.set_sync_state('idle')
    assert tp.sync_btn.toolTip() == "Sincronizar ahora con Asana"
