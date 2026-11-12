"""Sincronización con Asana como servicio Qt.

Porta el flujo de v8 (sync_asana_assigned_action → fetch en hilo →
_on_asana_tasks_fetched → merge en lotes) con señales en vez de root.after.
Incluye la verificación de tareas borradas en el MISMO hilo de red (lección
de v8: hacerla en el hilo de UI congelaba la app ~10 s).

Los cambios hacia Asana (completar/reabrir, renombrar, comentarios y altas de
tareas creadas en la app) viajan por la cola persistente del DataHandler:
push_pending() la envía en segundo plano
y sync() la vacía ANTES del fetch, en el mismo hilo, para que el merge vea
Asana ya actualizado. Un solo trabajo de red a la vez: un envío durante el
merge en lotes dejaría que un lote con datos previos pisara lo recién enviado,
así que lo que se pide mientras tanto espera a que termine el trabajo en curso.
"""
import traceback
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.workers import run_async


def _cambios(n: int) -> str:
    return f"{n} cambio{'s' if n != 1 else ''}"


class AsanaService(QObject):
    progress = Signal(float, str)      # fracción 0..1, texto
    status = Signal(str)               # línea de estado del sync
    connection_state = Signal(bool)    # online/offline
    finished = Signal(int)             # nº de tareas procesadas
    error = Signal(str)
    pushed = Signal(int, int, int)     # enviados, aún en cola, rechazados por Asana
    sync_started = Signal()            # arranca un sync (también uno diferido)

    BATCH_SIZE = 30
    BATCH_DELAY_MS = 120
    # Reintentos de la cola tras un fallo transitorio (sin red, 401, 429, 5xx)
    RETRY_DELAYS_MS = (60_000, 120_000, 300_000, 600_000, 900_000)

    def __init__(self, data_handler, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.data_handler = data_handler
        self._in_progress = False
        self._cancelled = False
        self._pushing = False
        self._push_requested = False
        self._sync_requested = False
        self._closing = False
        self._retry_failures = 0
        # El controlador lo sustituye por su criterio (switch de sync +
        # integración activa): sin permiso, la cola espera
        self.push_allowed = lambda: True
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self.push_pending)

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    @property
    def busy(self) -> bool:
        """Hay un trabajo de red en curso (sync completo o envío de la cola)."""
        return self._in_progress or self._pushing

    @property
    def sync_pending(self) -> bool:
        """Hay un sync pedido que espera a que termine el trabajo en curso."""
        return self._sync_requested

    @property
    def retry_scheduled(self) -> bool:
        """Un envío falló hace poco y hay un reintento programado."""
        return self._retry_timer.isActive()

    def cancel(self) -> None:
        """Cancelación suave: la petición en vuelo no se aborta, pero el merge
        y las señales de éxito ya no se ejecutan (p. ej. al apagar el switch).
        Un sync que esperaba turno tampoco arranca. Un envío pendiente sí se
        conserva: push_pending vuelve a mirar si está permitido."""
        self._cancelled = True
        self._sync_requested = False

    def shutdown(self) -> None:
        """Al salir: ni reintentos ni trabajos nuevos. Lo que quede en la cola
        espera en el JSON al siguiente arranque."""
        self._closing = True
        self._retry_timer.stop()
        self._sync_requested = False
        self._push_requested = False

    def validate_settings(self) -> Optional[str]:
        """None si la config permite sincronizar; si no, el mensaje de error."""
        settings = self.data_handler.load_settings()
        pat = (settings.get('asana_pat') or '').strip()
        assignee = (settings.get('asana_assignee_gid') or '').strip()
        connections = settings.get('asana_connections') or []
        workspace = (settings.get('asana_workspace_gid') or '').strip()
        if not pat:
            return "Falta el PAT de Asana en Configuración."
        if not assignee:
            return "Falta el Assignee GID en Configuración."
        if connections:
            if not any((c.get('workspace_gid') or '').strip() for c in connections):
                return "Las conexiones de Asana no tienen ningún workspace válido."
        elif not workspace:
            return "Debes indicar Workspace GID (o configurar conexiones) en Configuración."
        return None

    def is_configured(self) -> bool:
        return self.validate_settings() is None

    # ---------------- cola de cambios (sin fetch) ----------------
    def push_pending(self) -> None:
        """Envía en segundo plano los cambios en cola. Sin permiso o sin
        configuración no hace nada: la cola espera al siguiente sync."""
        if self._closing or not self.data_handler.has_pending_asana_ops():
            return
        if not self.push_allowed() or not self.is_configured():
            return
        if self.busy:
            self._push_requested = True
            return
        ops = self.data_handler.take_asana_ops_for_push()
        if not ops:
            return
        self._retry_timer.stop()
        self._pushing = True
        run_async(lambda: self.data_handler.send_asana_ops(ops),
                  self._on_pushed, lambda e: self._on_push_crashed(ops, e))

    def _on_pushed(self, results) -> None:
        self._pushing = False
        self._apply_push(results, announce=True)
        self._run_deferred()

    def _on_push_crashed(self, ops, e) -> None:
        self._pushing = False
        self.data_handler.release_asana_ops(ops)
        print(f"Error inesperado enviando la cola a Asana: {e}")
        self._schedule_retry()
        self._run_deferred()

    def _apply_push(self, results, announce: bool) -> None:
        """Aplica los resultados de un envío (hilo de UI) y avisa. `announce`:
        anunciar también el éxito (en un sync completo, el estado del sync
        ya informa)."""
        if not results:
            return
        summary = self.data_handler.apply_asana_push_results(results)
        pending = self.data_handler.pending_asana_ops_count()
        if summary['failed']:
            self._schedule_retry()
            if summary['offline']:
                self.connection_state.emit(False)
            if summary['auth']:
                self.status.emit(f"⚠ Asana rechazó el token (HTTP 401): revisa el PAT "
                                 f"en ⚙ Configuración — {_cambios(pending)} en cola")
            else:
                self.status.emit(f"⏳ {_cambios(pending)} para Asana en cola "
                                 f"({summary['last_error']}) — se reintentará solo")
        else:
            self._retry_failures = 0
            if summary['sent']:
                self.connection_state.emit(True)
            if summary['dropped']:
                self.status.emit(f"⚠ Asana rechazó {_cambios(summary['dropped'])} "
                                 f"({summary['last_error']}); se descartó")
            elif summary['sent'] and announce:
                self.status.emit(f"✅ Asana actualizado ({_cambios(summary['sent'])})")
        self.pushed.emit(summary['sent'], pending, summary['dropped'])

    def _schedule_retry(self) -> None:
        delays = self.RETRY_DELAYS_MS
        self._retry_timer.start(delays[min(self._retry_failures, len(delays) - 1)])
        self._retry_failures += 1

    def _run_deferred(self) -> None:
        """Al terminar un trabajo de red, lanza el que quedó esperando."""
        if self.busy or self._closing:
            return
        if self._sync_requested:
            self._sync_requested = False
            # Pudo apagarse el switch mientras esperaba
            if self.push_allowed():
                self.sync()
        elif self._push_requested:
            self._push_requested = False
            self.push_pending()

    def _end_sync(self) -> None:
        self._in_progress = False
        # Después de las señales de fin, que quien llama emite a continuación
        QTimer.singleShot(0, self._run_deferred)

    # ---------------- sync completo ----------------
    def sync(self) -> None:
        if self._closing:
            return
        if self._in_progress:
            if self._cancelled:
                # Se apagó y se volvió a encender el switch durante un sync:
                # el cancelado termina y este arranca después
                self._sync_requested = True
                return
            self.status.emit("Sincronización ya en curso…")
            return
        problem = self.validate_settings()
        if problem:
            self.status.emit(f"❌ {problem}")
            self.error.emit(problem)
            return
        if self._pushing:
            # Un envío de la cola está en vuelo: el sync arranca al terminar
            self._sync_requested = True
            self.status.emit("Sincronizando Asana…")
            return
        self._in_progress = True
        self._cancelled = False
        self._push_requested = False  # el sync envía la cola entera
        self._retry_timer.stop()
        self.sync_started.emit()
        self.status.emit("Sincronizando Asana…")
        self.progress.emit(0.05, "Conectando con Asana…")

        settings = self.data_handler.load_settings()
        assignee = settings.get('asana_assignee_gid', '').strip()
        workspace = settings.get('asana_workspace_gid', '').strip()
        project = settings.get('asana_project_gid', '').strip() or None
        include_completed = bool(settings.get('asana_include_completed', False))
        connections = settings.get('asana_connections') or []
        # Con proyectos marcados: traer también las propias sin proyecto
        include_no_project = bool(settings.get('asana_include_no_project', False))
        ops = self.data_handler.take_asana_ops_for_push() if self.push_allowed() else []
        # Calculado aquí (hilo de UI): el hilo de red no lee la cola
        skip_verify = self.data_handler.asana_gids_with_pending('completed')
        # Las ya enlazadas llegan aunque no pasen el filtro de proyectos
        linked = {str(t['asana_id']) for t in self.data_handler.get_tasks()
                  if t.get('source') == 'asana' and t.get('asana_id')}

        def push_progress(i: int, n: int) -> None:
            self.progress.emit(0.05 + 0.10 * (i / max(1, n)),
                               f"Enviando cambios a Asana… {i}/{n}")

        def fetch():
            if connections:
                tasks = self.data_handler.fetch_asana_tasks_for_connections(
                    connections, assignee, include_completed, include_no_project, linked)
            else:
                tasks = self.data_handler.fetch_asana_tasks_for_assignee(
                    assignee, workspace, project, include_completed, include_no_project,
                    linked)
            # Verificación de candidatas en ESTE hilo (una petición por tarea
            # local ausente del fetch): puede estar borrada en Asana o haber
            # sido COMPLETADA fuera de la app (el fetch sin include_completed
            # no las trae, así que esta es la única forma de detectarlas).
            asana_ids = {a.get('gid') or a.get('id') for a in tasks
                         if a.get('gid') or a.get('id')}
            candidates = self.data_handler.find_asana_candidates_to_verify(
                asana_ids, skip_verify)
            deleted = set()
            completed = set()
            n = len(candidates)
            for i, gid in enumerate(candidates, 1):
                self.progress.emit(0.15 + 0.20 * (i / max(1, n)),
                                   f"Verificando tareas… {i}/{n}")
                try:
                    state = self.data_handler.fetch_asana_task_state(gid)
                    if not state.get('exists'):
                        deleted.add(gid)
                    elif state.get('completed'):
                        completed.add(gid)
                except Exception:
                    pass
            return tasks, deleted, completed, bool(project or connections)

        def job():
            # Primero la cola (Asana queda al día), luego el fetch. Un fallo
            # del fetch no debe perder los resultados del envío: viajan juntos
            pushed = self.data_handler.send_asana_ops(ops, push_progress) if ops else []
            # Las recién creadas en Asana (op 'create') ya cuentan como enlazadas
            linked.update(r['result']['gid'] for r in pushed if r.get('result'))
            try:
                return pushed, fetch(), None
            except Exception as e:
                traceback.print_exc()
                return pushed, None, e

        run_async(job, self._on_fetched, lambda e: self._on_sync_crashed(ops, e))

    # ---- de vuelta en el hilo de UI ----
    def _on_sync_crashed(self, ops, e) -> None:
        self.data_handler.release_asana_ops(ops)
        self._on_error(e)

    def _on_fetched(self, result) -> None:
        pushed, fetched, fetch_error = result
        self._apply_push(pushed, announce=False)
        if fetch_error is not None:
            self._on_error(fetch_error)
            return
        if self._cancelled:
            self._end_sync()
            self.status.emit("Sincronización cancelada.")
            return
        tasks, deleted_gids, completed_gids, had_filter = fetched
        marked_deleted = 0
        try:
            marked_deleted = self.data_handler.apply_asana_deleted_marks(deleted_gids)
        except Exception as e:
            print(f"No se pudieron aplicar marcas de eliminadas: {e}")
        externally_completed = 0
        try:
            externally_completed = self.data_handler.apply_asana_completed_marks(completed_gids)
        except Exception as e:
            print(f"No se pudieron aplicar marcas de completadas: {e}")
        self.connection_state.emit(True)
        total = len(tasks)
        local_changes = externally_completed + marked_deleted
        self.progress.emit(0.35, f"Aplicando {total} tareas…")
        if total == 0 and local_changes:
            # No llegaron tareas nuevas, pero sí hubo cambios locales que la
            # UI debe reflejar (finished>0 dispara el refresh)
            parts = []
            if externally_completed:
                parts.append(f"{externally_completed} completadas fuera de la app")
            if marked_deleted:
                parts.append(f"{marked_deleted} eliminadas en Asana")
            self._end_sync()
            self.status.emit("✅ " + ", ".join(parts) + ".")
            self.finished.emit(local_changes)
            return
        if total == 0:
            hints = []
            if had_filter:
                hints.append("Verifica los proyectos seleccionados.")
            hints.append("Confirma el Assignee GID y el workspace.")
            self._end_sync()
            self.status.emit("Sin cambios desde Asana. " + " ".join(hints))
            self.finished.emit(0)
            return
        # Merge en lotes pausados: no congela la UI ni una sola vez
        state = {"i": 0}

        def next_batch():
            if self._cancelled:
                self._end_sync()
                self.status.emit("Sincronización cancelada.")
                return
            start = state["i"]
            end = min(start + self.BATCH_SIZE, total)
            try:
                self.data_handler.merge_asana_tasks(tasks[start:end])
            except Exception as e:
                print(f"Error al fusionar lote de Asana: {e}")
            self.status.emit(f"Sincronizando Asana… {end}/{total}")
            self.progress.emit(0.35 + 0.65 * (end / total), f"Aplicando tareas… {end}/{total}")
            state["i"] = end
            if end < total:
                QTimer.singleShot(self.BATCH_DELAY_MS, next_batch)
            else:
                self._end_sync()
                self.status.emit(f"✅ Sincronización completada: {total} tareas.")
                self.finished.emit(total)

        next_batch()

    def _on_error(self, e) -> None:
        self._end_sync()
        self.connection_state.emit(False)
        msg = f"Error durante la sincronización: {e}"
        self.status.emit(f"❌ {msg}")
        self.error.emit(msg)
