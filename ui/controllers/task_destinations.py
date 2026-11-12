"""Crear también en Asana o ClickUp al añadir una tarea (los botones con el
logo del panel de alta).

Pegamento entre el panel (TaskAddPanel), los ajustes y los servicios:
- Ofrece al panel los destinos de Asana («Mis tareas» y los proyectos
  marcados en Ajustes, por workspace) y si ClickUp está listo, y recuerda en
  settings cómo dejaste los botones (task_add_asana, asana_create_target,
  task_add_clickup).
- Asana: el alta viaja por la cola persistente (op 'create' del
  DataHandler): con el sync permitido sale enseguida; si no, espera.
- ClickUp: crear y olvidar en segundo plano, sin cola: avisa del éxito o del
  error y guarda en la tarea su id y su URL.
"""
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject

from services.workers import run_async
from ui.widgets import toast

# Pestañas de Ajustes que abre un botón sin configurar (settings_dialog.TABS)
_SETUP_TABS = {'asana': "Asana", 'clickup': "ClickUp"}


class TaskDestinations(QObject):
    def __init__(self, controller):
        super().__init__(controller)
        self.ctrl = controller
        self.data_handler = controller.data_handler
        self.panel = controller.tasks_panel.add_panel
        # Tareas con el alta en Asana pendiente (al enlazarse, refrescar)
        self._awaiting = set(self.data_handler.pending_asana_create_ids())
        self.refresh()
        s = self.data_handler.load_settings()
        self.panel.set_target_state(
            s.get('asana_create_target') if s.get('task_add_asana') else None,
            bool(s.get('task_add_clickup')))
        self.panel.targets_changed.connect(self._remember)
        self.panel.setup_requested.connect(
            lambda which: controller.open_settings(_SETUP_TABS.get(which, '')))
        controller.asana.pushed.connect(self._on_asana_pushed)

    # ---------------------------------------------------------- ajustes
    def refresh(self) -> None:
        """Lo que ofrecen los botones según los ajustes (al arrancar y al
        guardar la configuración)."""
        s = self.data_handler.load_settings()
        self.panel.set_integrations(self.asana_destinations(s), self.clickup_ready(s))

    def asana_destinations(self, s: Dict[str, Any]) -> Optional[List[dict]]:
        """«Mis tareas» de cada workspace seguida de sus proyectos marcados,
        o None si Asana no está activa y configurada."""
        if not (self.ctrl._asana_integration_active(s) and self.ctrl.asana.is_configured()):
            return None
        conns = [c for c in s.get('asana_connections') or []
                 if isinstance(c, dict) and str(c.get('workspace_gid') or '').strip()]
        workspaces = list(dict.fromkeys(str(c['workspace_gid']).strip() for c in conns))
        if not workspaces:
            workspaces = [str(s.get('asana_workspace_gid') or '').strip()]
        dests = []
        for ws in filter(None, workspaces):
            dests.append({'workspace_gid': ws, 'project_gid': '', 'project_name': ''})
            for c in conns:
                project = str(c.get('project_gid') or '').strip()
                if project and str(c['workspace_gid']).strip() == ws:
                    dests.append({'workspace_gid': ws, 'project_gid': project,
                                  'project_name': str(c.get('project_name') or '')})
        return dests or None

    @staticmethod
    def clickup_ready(s: Dict[str, Any]) -> bool:
        """Integración activa, con token y una lista donde crear."""
        return ('clickup' in (s.get('active_integrations') or [])
                and bool(str(s.get('clickup_api_token') or '').strip())
                and bool(str(s.get('clickup_list_id') or '').strip()))

    def _remember(self, state: Dict[str, Any]) -> None:
        s = self.data_handler.load_settings()
        s['task_add_asana'] = bool(state.get('asana'))
        if state.get('asana'):
            s['asana_create_target'] = dict(state['asana'])
        s['task_add_clickup'] = bool(state.get('clickup'))
        self.data_handler.save_settings(s)

    # ------------------------------------------------------------- alta
    def asana_payload(self, targets: Dict[str, Any], recurring) -> Optional[dict]:
        """Destino de Asana para DataHandler.add_task, o None: sin repetición
        (la API de Asana no la admite) y con Asana activa y configurada."""
        target = (targets or {}).get('asana')
        if not isinstance(target, dict) or recurring:
            return None
        if self.asana_destinations(self.data_handler.load_settings()) is None:
            return None
        return dict(target)

    def after_add(self, task_id, name: str, due: str, targets: Dict[str, Any]) -> None:
        """Tras guardar el alta local: enviarla a Asana y a ClickUp."""
        if self.data_handler.has_pending_asana_create(task_id):
            self._awaiting.add(task_id)
            if self.ctrl._asana_sync_allowed():
                self.ctrl.asana.push_pending()
            else:
                toast.show_info(self.ctrl._feedback_window(),
                                "Se creará en Asana cuando conectes", duration=2600)
        if (targets or {}).get('clickup'):
            self.create_in_clickup(task_id, name, due)

    def _on_asana_pushed(self, *_args) -> None:
        """Un envío de la cola pudo confirmar altas: sus tarjetas pasan a
        mostrar Asana."""
        if not self._awaiting or self.ctrl._is_closing:
            return
        settled = {tid for tid in self._awaiting
                   if not self.data_handler.has_pending_asana_create(tid)}
        if settled:
            self._awaiting -= settled
            self.ctrl.refresh_task_list()

    # ---------------------------------------------------------- ClickUp
    def create_in_clickup(self, task_id, name: str, due: str = '') -> None:
        s = self.data_handler.load_settings()
        if not self.clickup_ready(s):
            return
        from core.clickup_integration import ClickUpClient
        client = ClickUpClient(str(s['clickup_api_token']).strip())
        list_id = str(s['clickup_list_id']).strip()
        run_async(lambda: client.create_task(list_id, name, due_date=due or None),
                  lambda created: self._on_clickup_created(task_id, name, created),
                  lambda e: self._on_clickup_failed(name, e))

    def _on_clickup_created(self, task_id, name: str, created) -> None:
        if not created or not created.get('clickup_id'):
            self._on_clickup_failed(name, None)
            return
        task = self.ctrl._task_by_id(task_id)
        if task is not None:
            task['clickup_id'] = created['clickup_id']
            url = (created.get('clickup_data') or {}).get('url')
            if url:
                task['clickup_url'] = url
            self.data_handler._save()
        if not self.ctrl._is_closing:
            toast.show_success(self.ctrl._feedback_window(),
                               f"Creada también en ClickUp: {name[:40]}", duration=2200)

    def _on_clickup_failed(self, name: str, error) -> None:
        if error is not None:
            print(f"ClickUp: no se pudo crear «{name}»: {error}")
        if not self.ctrl._is_closing:
            toast.show_warning(self.ctrl._feedback_window(),
                               f"No se pudo crear en ClickUp: {name[:40]} "
                               "(revisa la conexión o la lista en Ajustes)", duration=4000)
