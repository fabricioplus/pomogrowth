"""Capa de datos: persistencia JSON atómica + API de Asana.

Extraído de pomogrowth_v8_ultra/app_ultra.py (extract_from_v8.py, una sola vez).
A partir de aquí este archivo es fuente normal de v9.
"""
import calendar
import copy
import time
import threading
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional, Set, Tuple

# Ciclos válidos de las tareas fijas (repetitivas)
RECURRING_CYCLES = ('daily', 'weekly', 'monthly', 'quarterly')

# Centinela para "no tocar este campo" en updates parciales
_UNSET = object()

# Tipos de operación de la cola de cambios hacia Asana (outbox). 'create':
# alta de una tarea local también en Asana (gid provisional 'local-<id>')
OUTBOX_OP_TYPES = ('update', 'comment', 'create')
# Estilos de los sonidos del temporizador (= services/sound_service.STYLE_KEYS;
# lo comprueba tests/test_timer_sounds.py)
TIMER_SOUND_STYLES = ('campana', 'marimba', 'suave', 'windows')
# Modos del servidor de webhooks entrantes (= services/webhook_server.MODES;
# lo comprueba tests/test_webhook_seguro.py). El primero es el de por defecto
WEBHOOK_MODES = ('local', 'lan', 'off')


def _new_outbox_op(op_type: str, gid: str, **payload) -> Dict[str, Any]:
    return {'id': uuid.uuid4().hex, 'type': op_type, 'gid': str(gid), **payload,
            'created_at': datetime.now().isoformat(), 'rev': 1,
            'attempts': 0, 'last_error': None}


def _classify_asana_status(code: int) -> str:
    """Qué hacer con una op según la respuesta HTTP de Asana.
    'ok' hecha · 'auth' token rechazado (parar y reintentar) · 'retry'
    transitorio (parar y reintentar; 451 = red/VPN bloqueada) · 'drop' Asana
    la rechazó y reintentar no lo arreglaría (400/402/403/404…): se descarta."""
    if 200 <= code < 300:
        return 'ok'
    if code == 401:
        return 'auth'
    if code in (408, 429, 451) or code >= 500:
        return 'retry'
    return 'drop'



def _with_local_tags(asana_tags: List[str], local_tags) -> List[str]:
    """Tags de Asana + los añadidos a mano en la app (sin duplicar mayúsculas)."""
    seen = {t.lower() for t in asana_tags}
    extra = [t for t in (local_tags or [])
             if isinstance(t, str) and t.strip() and t.lower() not in seen]
    return sorted(asana_tags + extra)


def recurring_cycle(task: Dict[str, Any]) -> Optional[str]:
    """Ciclo normalizado de una tarea fija, o None si no es recurrente.
    Compat: las tareas del lote anterior guardaban recurring=True (= diaria)."""
    value = task.get('recurring')
    if not value:
        return None
    return value if value in RECURRING_CYCLES else 'daily'


def _add_months(d: date, months: int) -> date:
    """Suma meses de calendario con clamp al fin de mes (31 ene + 1m = 28/29 feb)."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_reopen_date(completed: date, cycle: str) -> date:
    """Fecha en la que una tarea fija completada vuelve a Pendientes."""
    if cycle == 'weekly':
        return completed + timedelta(days=7)
    if cycle == 'monthly':
        return _add_months(completed, 1)
    if cycle == 'quarterly':
        return _add_months(completed, 3)
    return completed + timedelta(days=1)  # daily (y fallback)

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None
    REQUESTS_AVAILABLE = False

from core.asana_errors import AsanaFetchError, error_from_response
from core.config import Config
from core.music_library import default_music, normalize_music
from core.radio_catalog import apply_default_radios
from core.ambient_catalog import normalize_mix as normalize_ambient_mix
from core.ambient_catalog import clamp_volume
from utils.safe_json import load_json_safe, save_json_atomic


class DataHandler:
    # Radios lofi de serie (core/radio_catalog.py): se siembran al cargar. Los
    # tests lo apagan (tests/conftest.py): la guarda de red no ve a FFmpeg y
    # una radio de serie que sonara en un test saldría a internet
    SEED_DEFAULT_RADIOS = True

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.load_error: Optional[str] = None
        # Ops de la outbox que un envío tiene en vuelo: id → rev enviada. Solo
        # en memoria: tras un cierre, lo que estaba en vuelo se reenvía.
        self._outbox_inflight: Dict[str, int] = {}
        # Altas que Asana rechazó: (nombre, detalle). El controlador las avisa
        # una vez y vacía la lista
        self.asana_create_rejections: List[Tuple[str, str]] = []
        self._migrated_on_load = False
        # Tareas de la migración (marca heredada) cuyo estado cambió al
        # sincronizar: el controlador avisa de ellas una vez
        self.legacy_resynced: List[str] = []
        self.data: Dict[str, Any] = self._load()
        # Sanear posibles IDs duplicados o inválidos al iniciar
        try:
            if self._sanitize_ids() or self._migrated_on_load:
                self._save()
        except Exception as e:
            print(f"Advertencia: no se pudo sanear IDs al iniciar: {e}")

    def _load(self) -> Dict[str, Any]:
        default_data = {
            'tasks': [],
            'deleted_tasks': [],
            'time_chunks': [],
            'history': [],
            'ui_snapshot': {},
            'asana_outbox': [],
            'day_plan': {'date': '', 'task_ids': []},
            'music': default_music(),
            # Valoración de esfuerzo (1-5) al completar tareas
            'effort_ratings': [],
            'settings': {
                'ngrok_authtoken': '', 'ngrok_domain': '', 'asana_pat': '', 'asana_assignee_gid': '', 'asana_workspace_gid': '',
                'asana_project_gid': '', 'asana_include_completed': False, 'asana_auto_sync_on_start': False, 'asana_connections': [],
                'webhook_mode': WEBHOOK_MODES[0], 'webhook_key': ''
            }
        }
        if not self.filepath.exists():
            # Instalación nueva: la presentación del primer arranque, pendiente
            default_data['settings']['onboarding_seen'] = False
            # …y las radios de serie. Sin guardar: el primer guardado las
            # persiste y, hasta entonces, cada arranque las siembra igual
            self._seed_default_radios(default_data['music'])
            return default_data
        # Carga segura: si el archivo está corrupto se preserva una copia y se
        # intenta el .bak; el error queda en self.load_error para avisar en UI.
        data, error = load_json_safe(self.filepath, default=None)
        if error:
            self.load_error = error
            return default_data
        try:
            # ⭐ Validar que data sea un diccionario
            if not isinstance(data, dict):
                self.load_error = "El archivo de datos no tiene el formato esperado"
                print(f"⚠️ Archivo JSON corrupto (no es dict), usando datos por defecto")
                return default_data
            data.setdefault('tasks', []);
            data.setdefault('deleted_tasks', []);
            data.setdefault('time_chunks', []);
            data.setdefault('history', []);
            data.setdefault('ui_snapshot', {});
            data.setdefault('settings', {})
            data['settings'].setdefault('ngrok_authtoken', ''); data['settings'].setdefault('ngrok_domain', '')
            data['settings'].setdefault('asana_pat', '')
            data['settings'].setdefault('asana_assignee_gid', '')
            data['settings'].setdefault('asana_workspace_gid', '')
            data['settings'].setdefault('asana_project_gid', '')
            data['settings'].setdefault('asana_include_completed', False)
            data['settings'].setdefault('asana_auto_sync_on_start', False)
            # v9: duraciones del timer persistidas (v8 las perdía al cerrar)
            data['settings'].setdefault('pomodoro_min', Config.DEFAULT_POMODORO_MIN)
            data['settings'].setdefault('break_min', Config.DEFAULT_BREAK_MIN)
            data['settings'].setdefault('long_break_min', Config.DEFAULT_LONG_BREAK_MIN)
            data['settings'].setdefault('pomodoros_per_cycle', Config.DEFAULT_POMODOROS_PER_CYCLE)
            # Modo Libre: último tipo de temporizador elegido ('pomodoro' | 'free')
            data['settings'].setdefault('timer_kind', 'pomodoro')
            # Nuevo: soporte multi-conexión Asana (lista de workspaces/proyectos)
            data['settings'].setdefault('asana_connections', [])
            # Nombre y racha de uso: el nombre para el saludo (se pregunta una
            # vez) y los días en que se abrió la app (ver mark_active_day)
            data['settings'].setdefault('user_name', '')
            data['settings'].setdefault('user_name_asked', False)
            data.setdefault('activity_days', [])
            # Presentación del primer arranque (ui/dialogs/onboarding_dialog):
            # un archivo con uso previo (tareas, tiempos, días de uso o el
            # nombre ya preguntado) es de quien actualiza: no se le muestra
            data['settings'].setdefault('onboarding_seen', bool(
                data['tasks'] or data['deleted_tasks'] or data['time_chunks']
                or data['history'] or data['activity_days']
                or data['settings'].get('user_name_asked')
                or data['settings'].get('user_name')))
            # Alta también en Asana/ClickUp (botones del panel) y sync de las
            # tareas propias sin proyecto
            data['settings'].setdefault('asana_include_no_project', False)
            data['settings'].setdefault('task_add_asana', False)
            data['settings'].setdefault('task_add_clickup', False)
            # Sonidos del temporizador (services/sound_service.py): avisos al
            # acabar pomodoro y descanso; estilo 'campana' | 'marimba' |
            # 'suave' | 'windows' (los del sistema, como hasta la 1.0)
            # Saneados (un JSON editado a mano no debe tumbar el arranque ni
            # Ajustes): el servicio y el diálogo leen el mismo valor
            sounds_on = data['settings'].get('timer_sounds_enabled')
            data['settings']['timer_sounds_enabled'] = (
                sounds_on if isinstance(sounds_on, bool) else True)
            data['settings']['timer_sound_volume'] = clamp_volume(
                data['settings'].get('timer_sound_volume'), 70)
            if data['settings'].get('timer_sound_style') not in TIMER_SOUND_STYLES:
                data['settings']['timer_sound_style'] = 'campana'
            # Webhook entrante (services/webhook_server.py): 'local' (solo este
            # equipo, por defecto) | 'lan' (red local, con clave) | 'off'. La
            # clave, solo si es texto (el modo 'lan' sin clave lo rechaza todo)
            mode = data['settings'].get('webhook_mode')
            data['settings']['webhook_mode'] = (
                mode if isinstance(mode, str) and mode in WEBHOOK_MODES else WEBHOOK_MODES[0])
            key = data['settings'].get('webhook_key')
            data['settings']['webhook_key'] = key if isinstance(key, str) else ''

            # ⭐ Validar y limpiar tareas corruptas
            valid_tasks = []
            for task in data.get('tasks', []):
                if isinstance(task, dict):
                    task.setdefault('tags', [])
                    task.setdefault('source', 'local')
                    task.setdefault('asana_id', None)
                    task.setdefault('due', None)
                    valid_tasks.append(task)
                else:
                    print(f"⚠️ Tarea corrupta ignorada (tipo {type(task)}): {task}")
            data['tasks'] = valid_tasks
            
            # ⭐ Validar y limpiar tareas eliminadas
            valid_deleted = []
            for task in data.get('deleted_tasks', []):
                if isinstance(task, dict):
                    task.setdefault('tags', [])
                    task.setdefault('source', 'local')
                    task.setdefault('asana_id', None)
                    task.setdefault('due', None)
                    valid_deleted.append(task)
                else:
                    print(f"⚠️ Tarea eliminada corrupta ignorada (tipo {type(task)}): {task}")
            data['deleted_tasks'] = valid_deleted

            # Cola de cambios hacia Asana: solo ops bien formadas
            outbox = data.get('asana_outbox')
            data['asana_outbox'] = [
                op for op in (outbox if isinstance(outbox, list) else [])
                if isinstance(op, dict) and op.get('gid') and op.get('id')
                and op.get('type') in OUTBOX_OP_TYPES
                # Un alta sin la tarea a la que pertenece no se podría enlazar
                and (op.get('type') != 'create' or op.get('task_id'))]
            if self._migrate_push_pending(data):
                self._migrated_on_load = True
            data['day_plan'] = self._valid_day_plan(data.get('day_plan'))
            data['music'] = self._valid_music(data.get('music'))
            # Valoración de esfuerzo (1-5) al completar tareas: histórico aparte
            if not isinstance(data.get('effort_ratings'), list):
                data['effort_ratings'] = []
            # Sonidos de ambiente: la mezcla (volúmenes, general, «solo durante
            # el enfoque» y la última que sonó), saneada; ver core/ambient_catalog
            data['settings']['ambient'] = normalize_ambient_mix(
                data['settings'].get('ambient'))
            Config.N8N_WEBHOOK_URL = data.get('settings', {}).get('n8n_webhook_url', '')
            # Radios de serie que aún no se le ofrecieron (también a quien
            # actualiza): al final de todo, con el archivo ya validado, para
            # que una siembra nunca acompañe a un load_error. Se guarda al
            # arrancar (__init__) o al acabar import_data
            if self._seed_default_radios(data['music']):
                self._migrated_on_load = True
            return data
        except (AttributeError, TypeError, KeyError, ValueError, OverflowError) as e:
            # Estructura interna inesperada (p. ej. 'settings' no es dict, o
            # un número infinito que no se puede convertir)
            self.load_error = f"El archivo de datos tiene una estructura inesperada: {e}"
            print(f"⚠️ {self.load_error}")
            return default_data

    def _save(self):
        # Escritura atómica con backup: un cierre abrupto ya no puede dejar
        # el archivo de datos truncado (antes se sobreescribía en el sitio).
        if not save_json_atomic(self.filepath, self.data):
            print(f"Error guardando datos en {self.filepath}")

    def _sanitize_ids(self) -> bool:
        """Garantiza que todas las tareas tengan IDs enteros únicos.
        Si detecta duplicados o tipos inválidos, reasigna nuevos IDs y actualiza referencias en time_chunks.
        Devuelve True si hubo cambios.
        """
        tasks = self.data.get('tasks', [])
        if not tasks:
            return False
        changed = False
        used: set[int] = set()
        id_map: Dict[int, int] = {}
        # Generar candidato base a partir del tiempo actual para  colisiones
        candidate = int(time.time() * 1000)
        def next_id() -> int:
            nonlocal candidate
            while candidate in used:
                candidate += 1
            used.add(candidate)
            return candidate
        # Primer barrido: registrar IDs válidos y marcar duplicados
        for t in tasks:
            tid = t.get('id')
            if not isinstance(tid, int) or tid in used:
                new_id = next_id()
                if isinstance(tid, int):
                    id_map[tid] = new_id
                t['id'] = new_id
                changed = True
            else:
                used.add(tid)
        # Actualizar time_chunks con los IDs reasignados
        if changed:
            for chunk in self.data.get('time_chunks', []):
                cid = chunk.get('task_id')
                if isinstance(cid, int) and cid in id_map:
                    chunk['task_id'] = id_map[cid]
        return changed

    def _get_asana_headers(self) -> Optional[Dict[str, str]]:
        asana_pat = self.data.get('settings', {}).get('asana_pat')
        if not asana_pat:
            return None
        return {
            "Authorization": f"Bearer {asana_pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    
    def check_asana_task_exists(self, task_gid: str) -> bool:
        """
        Verifica si una tarea existe en Asana haciendo una petición individual.
        Retorna True si existe, False si fue eliminada o no se puede acceder.
        """
        return bool(self.fetch_asana_task_state(task_gid).get('exists', True))

    def fetch_asana_task_state(self, task_gid: str) -> Dict[str, Any]:
        """Consulta el estado real de una tarea en Asana.

        Devuelve {'exists': bool, 'completed': Optional[bool]}. Con esto el sync
        detecta tareas completadas FUERA de la app (p. ej. con PomoGrowth
        cerrada): con include_completed=False el fetch masivo no las trae, así
        que la única forma de enterarse es esta consulta individual.
        completed=None significa "no se pudo determinar".
        """
        headers = self._get_asana_headers()
        if not headers or not REQUESTS_AVAILABLE:
            return {'exists': True, 'completed': None}
        try:
            url = f"{Config.ASANA_API_URL}/tasks/{task_gid}"
            response = requests.get(url, headers=headers,
                                    params={"opt_fields": "gid,completed"}, timeout=10)
            if response.status_code == 200:
                data = (response.json() or {}).get('data') or {}
                return {'exists': True, 'completed': bool(data.get('completed', False))}
            if response.status_code == 404:
                return {'exists': False, 'completed': None}
            # Otros errores (403, 500, etc.): asumir que existe, estado desconocido
            return {'exists': True, 'completed': None}
        except Exception as e:
            print(f"Error verificando tarea {task_gid}: {e}")
            return {'exists': True, 'completed': None}

    # ------------------ ASANA: cola de cambios salientes (outbox) ------------------
    # Todo cambio del usuario hacia Asana (completar/reabrir, renombrar,
    # comentarios) se encola aquí y se persiste ANTES de intentar enviarlo:
    # sin red, con el sync apagado o con la app cerrada a medias, el cambio
    # espera en el JSON. El merge no pisa los campos con un cambio en cola.
    # Hilos: encolar/tomar/aplicar resultados, en el hilo de UI;
    # send_asana_ops (la red), en un hilo de fondo y sin tocar self.data.

    def _enqueue_asana_update(self, gid: str, fields: Dict[str, Any]) -> None:
        """Encola un cambio de campos (sin guardar: lo hace quien llama). Se
        fusiona con el update en cola de la misma tarea: solo viaja el último
        valor de cada campo."""
        if not gid or not fields:
            return
        outbox = self.data.setdefault('asana_outbox', [])
        for op in outbox:
            if op.get('type') == 'update' and op.get('gid') == str(gid):
                op.setdefault('fields', {}).update(fields)
                # Si estaba en vuelo, la rev nueva hace que se reenvíe
                op['rev'] = op.get('rev', 0) + 1
                return
        outbox.append(_new_outbox_op('update', gid, fields=dict(fields)))

    def queue_asana_update(self, gid: str, fields: Dict[str, Any]) -> None:
        self._enqueue_asana_update(gid, fields)
        self._save()

    def queue_asana_comment(self, gid: str, text: str) -> None:
        if not gid or not text:
            return
        self.data.setdefault('asana_outbox', []).append(
            _new_outbox_op('comment', gid, text=text))
        self._save()

    def has_pending_asana_ops(self) -> bool:
        return bool(self.data.get('asana_outbox'))

    def pending_asana_ops_count(self) -> int:
        return len(self.data.get('asana_outbox') or [])

    def asana_gids_with_pending(self, field: str) -> Set[str]:
        """GIDs con un update en cola que toca `field`."""
        return {op.get('gid') for op in list(self.data.get('asana_outbox') or [])
                if op.get('type') == 'update' and field in (op.get('fields') or {})}

    def take_asana_ops_for_push(self) -> List[Dict[str, Any]]:
        """Copia de las ops en cola que no están ya en vuelo, y las marca en
        vuelo: dos envíos solapados nunca mandan la misma op. Los comentarios
        anotan su intento (`send_tries`) en disco ANTES de salir: si este
        envío acaba de forma incierta (timeout, 5xx, la app se cierra a
        mitad), el siguiente comprobará en Asana antes de publicar otra vez."""
        batch = []
        tried_comment = False
        linked = None
        for op in self.data.get('asana_outbox') or []:
            if op['id'] in self._outbox_inflight:
                continue
            if op.get('type') in ('comment', 'create'):
                op['send_tries'] = op.get('send_tries', 0) + 1
                tried_comment = True
                if op.get('type') == 'create':
                    self._note_asana_create_name(op)
            self._outbox_inflight[op['id']] = op.get('rev', 0)
            batch.append(copy.deepcopy(op))
            if op.get('type') == 'create':
                # Gids ya enlazados (o en lápidas): la búsqueda anti-duplicados
                # del alta no debe tomar una tarea que ya es de otra
                if linked is None:
                    linked = sorted({str(t['asana_id']) for t in
                                     (self.data.get('tasks') or [])
                                     + (self.data.get('deleted_tasks') or [])
                                     if isinstance(t, dict) and t.get('asana_id')})
                batch[-1]['skip_gids'] = linked
        if tried_comment:
            self._save()
        return batch

    def release_asana_ops(self, ops: List[Dict[str, Any]]) -> None:
        """Devuelve a la cola, sin cambios, ops tomadas cuyo envío no ocurrió."""
        for op in ops:
            self._outbox_inflight.pop(op.get('id'), None)

    def send_asana_ops(self, ops: List[Dict[str, Any]],
                       on_progress: Optional[Callable[[int, int], None]] = None
                       ) -> List[Dict[str, Any]]:
        """(Hilo de red) Envía las ops en orden; NO toca self.data. Un
        resultado por op: {'id', 'rev', 'outcome', 'detail', 'offline'} con
        outcome 'ok' | 'drop' | 'retry' | 'auth' | 'skipped' (no intentada).
        Ante un fallo transitorio se detiene: el resto queda para el
        siguiente intento, en su orden. Una op con el gid provisional de un
        alta ('local-<id>') sale con el gid real si el alta se confirmó antes
        en este lote; si no, espera en la cola sin detener el resto."""
        results: List[Dict[str, Any]] = []
        headers = self._get_asana_headers()
        halted = False
        real_gids: Dict[str, str] = {}  # gid provisional → real, de este lote
        for i, op in enumerate(ops):
            created = None
            gid = str(op.get('gid') or '')
            if op.get('type') != 'create' and gid in real_gids:
                op['gid'] = real_gids[gid]  # es una copia: la cola la reescribe el enlace
            if not headers or not REQUESTS_AVAILABLE:
                outcome, detail, offline = 'skipped', 'falta el PAT de Asana', False
            elif halted:
                outcome, detail, offline = 'skipped', '', False
            elif op.get('type') != 'create' and str(op.get('gid')).startswith('local-'):
                outcome, detail, offline = 'skipped', 'espera el alta en Asana', False
            else:
                if on_progress:
                    on_progress(i + 1, len(ops))
                if op.get('type') == 'create':
                    outcome, detail, offline, created = self._send_asana_create(op, headers)
                else:
                    outcome, detail, offline = self._send_asana_op(op, headers)
            results.append({'id': op.get('id'), 'rev': op.get('rev', 0),
                            'outcome': outcome, 'detail': detail, 'offline': offline})
            halted = halted or outcome in ('retry', 'auth')
            if op.get('type') == 'create' and outcome == 'ok' and created:
                results[-1]['result'] = created  # gid real y proyectos
                real_gids[gid] = created['gid']
        return results

    def _send_asana_op(self, op: Dict[str, Any], headers: Dict[str, str]
                       ) -> Tuple[str, str, bool]:
        gid = op.get('gid')
        try:
            if op.get('type') == 'update':
                resp = requests.put(f"{Config.ASANA_API_URL}/tasks/{gid}",
                                    headers=headers,
                                    json={'data': op.get('fields') or {}}, timeout=15)
            elif op.get('type') == 'comment':
                if op.get('send_tries', 1) > 1:
                    # Un intento anterior pudo llegar: publicar solo si no está
                    found, detail, offline = self._asana_comment_lookup(op, headers)
                    if found == 'posted':
                        print(f"Asana: el comentario de {gid} ya estaba publicado.")
                        return 'ok', 'ya publicado', False
                    if found in ('auth', 'retry'):
                        return found, detail, offline
                    # 'absent' (o la consulta se rechazó con otro 4xx): publicar,
                    # y que decida la respuesta del POST
                resp = requests.post(f"{Config.ASANA_API_URL}/tasks/{gid}/stories",
                                     headers=headers,
                                     json={'data': {'text': op.get('text') or ''}},
                                     timeout=15)
            else:
                return 'drop', f"tipo de cambio desconocido: {op.get('type')}", False
        except requests.RequestException as e:
            print(f"Asana: sin conexión al enviar {op.get('type')} de {gid}: {e}")
            return 'retry', 'sin conexión', True
        outcome = _classify_asana_status(resp.status_code)
        if outcome == 'ok':
            print(f"Asana: {op.get('type')} de la tarea {gid} enviado.")
        else:
            print(f"Asana: {op.get('type')} de la tarea {gid} → HTTP "
                  f"{resp.status_code} ({outcome}): {resp.text[:300]}")
        return outcome, f"HTTP {resp.status_code}", False

    def _asana_comment_lookup(self, op: Dict[str, Any], headers: Dict[str, str]
                              ) -> Tuple[str, str, bool]:
        """¿Está ya en la tarea este comentario (de un intento anterior)?
        Devuelve (estado, detalle, sin_red) con estado 'posted' | 'absent' |
        'auth' | 'retry'. Coincidencia EXACTA de texto: el comentario de
        pomodoro lleva la fecha y hora de la sesión, así que dos pomodoros
        seguidos no se confunden entre sí."""
        text = (op.get('text') or '').strip()
        try:
            # Margen de 2 min por desfase entre el reloj local y el de Asana
            since = (datetime.fromisoformat(op.get('created_at')).astimezone(timezone.utc)
                     - timedelta(minutes=2))
        except (TypeError, ValueError):
            since = None
        url = f"{Config.ASANA_API_URL}/tasks/{op.get('gid')}/stories"
        params = {'opt_fields': 'text,created_at,resource_subtype', 'limit': 100}
        try:
            for _ in range(50):  # 5000 historias: de sobra para una tarea
                resp = requests.get(url, headers=headers, params=params, timeout=15)
                if resp.status_code != 200:
                    verdict = _classify_asana_status(resp.status_code)
                    if verdict in ('auth', 'retry'):
                        return verdict, f"HTTP {resp.status_code}", False
                    # 403/404…: no se puede consultar; el POST dirá qué pasa
                    return 'absent', f"HTTP {resp.status_code}", False
                body = resp.json() or {}
                for story in body.get('data') or []:
                    if story.get('resource_subtype', 'comment_added') != 'comment_added':
                        continue
                    if str(story.get('text') or '').strip() != text:
                        continue
                    try:
                        created = datetime.fromisoformat(
                            str(story.get('created_at')).replace('Z', '+00:00'))
                        if since is not None and created < since:
                            continue
                    except ValueError:
                        pass
                    return 'posted', '', False
                offset = (body.get('next_page') or {}).get('offset')
                if not offset:
                    return 'absent', '', False
                params['offset'] = offset
        except requests.RequestException as e:
            print(f"Asana: no se pudo comprobar el comentario de {op.get('gid')}: {e}")
            return 'retry', 'sin conexión', True
        return 'absent', '', False

    def apply_asana_push_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """(Hilo de UI) Saca de la cola lo enviado o rechazado; lo fallido se
        queda con su intento anotado. Una op que cambió mientras volaba
        (rev distinta) se queda para viajar con el valor nuevo."""
        summary = {'sent': 0, 'dropped': 0, 'failed': 0, 'offline': False,
                   'auth': False, 'last_error': ''}
        if not results:
            return summary
        outbox = self.data.get('asana_outbox') or []
        by_id = {op.get('id'): op for op in outbox}
        done = set()
        untried = False
        for r in results:
            self._outbox_inflight.pop(r.get('id'), None)
            op = by_id.get(r.get('id'))
            outcome = r.get('outcome')
            if outcome == 'skipped':
                # No llegó a intentarse: no cuenta como intento (si no, la
                # comprobación anti-duplicados se haría sin motivo)
                if (op is not None and op.get('type') in ('comment', 'create')
                        and op.get('send_tries')):
                    op['send_tries'] -= 1
                    untried = True
                continue
            if outcome in ('ok', 'drop'):
                summary['sent' if outcome == 'ok' else 'dropped'] += 1
                if outcome == 'drop':
                    summary['last_error'] = r.get('detail') or ''
                    if (op is not None and op.get('type') == 'create'
                            and not op.get('cancelled')):
                        # Alta rechazada: el controlador avisa con su nombre
                        self.asana_create_rejections.append(
                            (str((op.get('fields') or {}).get('name') or '').strip(),
                             r.get('detail') or ''))
                if r.get('result'):
                    # Alta hecha: la tarea queda enlazada (lo editado mientras
                    # viajaba sigue como update) y el alta sale de la cola
                    self._link_asana_created(r['result'], op)
                    if op is not None:
                        done.add(op['id'])
                elif op is not None and op.get('rev', 0) == r.get('rev'):
                    done.add(op['id'])
            else:  # 'retry' / 'auth'
                summary['failed'] += 1
                summary['offline'] = summary['offline'] or bool(r.get('offline'))
                summary['auth'] = summary['auth'] or outcome == 'auth'
                summary['last_error'] = r.get('detail') or ''
                if op is not None:
                    op['attempts'] = op.get('attempts', 0) + 1
                    op['last_error'] = r.get('detail')
        if done:
            self.data['asana_outbox'] = [op for op in self.data.get('asana_outbox') or []
                                         if op.get('id') not in done]
        # Comentarios que esperaban un alta rechazada o retirada: ya no saldrán
        purged = self._purge_orphan_asana_ops()
        if summary['sent'] or summary['dropped'] or summary['failed'] or untried or purged:
            self._save()
        return summary

    # ------------------ ASANA: alta de tareas locales (op 'create') ------------------
    # Una tarea creada en PomoGrowth con «crear también en Asana» espera aquí
    # como op 'create' con gid provisional 'local-<id>'. Mientras espera, la
    # tarea sigue siendo local: completarla o renombrarla se fusiona en el
    # alta y borrarla la retira. Al confirmarse, la tarea pasa a enlazada.

    def _enqueue_asana_create(self, task: Dict[str, Any], target: Dict[str, Any]) -> None:
        """Encola el alta de `task` (sin guardar: lo hace quien llama).
        target: {workspace_gid, project_gid ('' = Mis tareas), project_name}."""
        settings = self.data.get('settings', {})
        workspace = str(target.get('workspace_gid') or '').strip()
        if not workspace:
            return
        project = str(target.get('project_gid') or '').strip()
        fields = {'name': task.get('name') or ''}
        due = str(task.get('due') or '')
        if len(due) == 10:  # 'YYYY-MM-DD', como el due_on de Asana
            fields['due_on'] = due
        self.data.setdefault('asana_outbox', []).append(_new_outbox_op(
            'create', f"local-{task['id']}", task_id=task['id'], fields=fields,
            workspace=workspace,
            assignee=str(settings.get('asana_assignee_gid') or '').strip(),
            projects=[project] if project else [],
            project_name=str(target.get('project_name') or '').strip()))

    def _pending_asana_create(self, task_id) -> Optional[Dict[str, Any]]:
        return next((op for op in self.data.get('asana_outbox') or []
                     if op.get('type') == 'create' and op.get('task_id') == task_id), None)

    def has_pending_asana_create(self, task_id) -> bool:
        return self._pending_asana_create(task_id) is not None

    def pending_asana_create_ids(self) -> List[int]:
        return [op.get('task_id') for op in self.data.get('asana_outbox') or []
                if op.get('type') == 'create']

    def _merge_into_asana_create(self, task_id, fields: Dict[str, Any]) -> None:
        """Un cambio hecho antes de que llegue el alta viaja con ella (o, si
        ya está en vuelo, la rev nueva lo manda luego como update)."""
        op = self._pending_asana_create(task_id)
        if op is not None and fields:
            op.setdefault('fields', {}).update(fields)
            op['rev'] = op.get('rev', 0) + 1

    def _drop_asana_create(self, task_id) -> None:
        """Tarea borrada con el alta pendiente: se retira. Si está en vuelo
        se marca: si llegó a crearse, la lápida guardará su gid; si no, ya no
        se intenta."""
        op = self._pending_asana_create(task_id)
        if op is not None:
            if op['id'] in self._outbox_inflight:
                op['cancelled'] = True
            else:
                self.data['asana_outbox'].remove(op)
        # Los comentarios de sesión que esperaban el alta se van con ella
        self._purge_orphan_asana_ops()

    def _purge_orphan_asana_ops(self) -> bool:
        """Quita las ops con gid provisional 'local-<id>' cuya alta ya no
        está en la cola (o está retirada): nunca podrían enviarse. Las que
        están en vuelo esperan a su resultado. True si quitó alguna."""
        outbox = self.data.get('asana_outbox') or []
        waiting = {op.get('gid') for op in outbox
                   if op.get('type') == 'create' and not op.get('cancelled')}
        keep = [op for op in outbox
                if op.get('type') == 'create' or op.get('gid') in waiting
                or not str(op.get('gid') or '').startswith('local-')
                or op.get('id') in self._outbox_inflight]
        if len(keep) == len(outbox):
            return False
        outbox[:] = keep
        return True

    def _drop_asana_creates_without_task(self) -> bool:
        """Altas pendientes de tareas que ya no existen (tras «Borrar todos
        los datos» o importar un backup): se retiran como al borrar la tarea.
        Los updates y comentarios de tareas de Asana se conservan."""
        ids = {t.get('id') for t in self.data.get('tasks') or [] if isinstance(t, dict)}
        before = copy.deepcopy(self.data.get('asana_outbox') or [])
        for task_id in {op.get('task_id') for op in before
                        if op.get('type') == 'create' and op.get('task_id') not in ids}:
            self._drop_asana_create(task_id)
        self._purge_orphan_asana_ops()
        return before != (self.data.get('asana_outbox') or [])

    @staticmethod
    def _note_asana_create_name(op: Dict[str, Any]) -> None:
        """Anota el nombre con el que sale el alta: si se renombra antes de
        reintentar, la búsqueda anti-duplicados aún reconoce la ya creada."""
        name = str((op.get('fields') or {}).get('name') or '').strip()
        sent = op.setdefault('sent_names', [])
        if name and name not in sent:
            sent.append(name)

    def _send_asana_create(self, op: Dict[str, Any], headers: Dict[str, str]
                           ) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
        """(Hilo de red) POST /tasks. Si un intento anterior pudo llegar,
        antes busca la tarea en Asana para no crearla dos veces."""
        try:
            if op.get('send_tries', 1) > 1:
                found, detail, offline, created = self._asana_create_lookup(op, headers)
                if found == 'created':
                    print(f"Asana: la tarea {op.get('gid')} ya estaba creada.")
                    return 'ok', 'ya creada', False, created
                if found in ('auth', 'retry'):
                    return found, detail, offline, None
            if op.get('cancelled'):
                return 'ok', 'borrada antes de crearse', False, None
            fields = op.get('fields') or {}
            data = {'name': fields.get('name') or '', 'workspace': op.get('workspace')}
            if op.get('assignee'):
                data['assignee'] = op['assignee']
            if fields.get('due_on'):
                data['due_on'] = fields['due_on']
            if fields.get('completed'):
                data['completed'] = True
            if op.get('projects'):
                data['projects'] = list(op['projects'])
            resp = requests.post(f"{Config.ASANA_API_URL}/tasks", headers=headers,
                                 params={'opt_fields': 'gid,name,completed,projects.gid,'
                                                       'projects.name'},
                                 json={'data': data}, timeout=15)
        except requests.RequestException as e:
            print(f"Asana: sin conexión al crear {op.get('gid')}: {e}")
            return 'retry', 'sin conexión', True, None
        outcome = _classify_asana_status(resp.status_code)
        if outcome != 'ok':
            print(f"Asana: create de {op.get('gid')} → HTTP {resp.status_code} "
                  f"({outcome}): {resp.text[:300]}")
            return outcome, f"HTTP {resp.status_code}", False, None
        try:
            body = (resp.json() or {}).get('data') or {}
        except ValueError:
            body = {}
        if not body.get('gid'):
            # Creada, pero sin saber su gid: el reintento la buscará
            return 'retry', 'respuesta sin gid', False, None
        print(f"Asana: tarea {op.get('gid')} creada como {body['gid']}.")
        return 'ok', f"HTTP {resp.status_code}", False, self._asana_created_result(op, body)

    @staticmethod
    def _asana_created_result(op: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        projects = [{'gid': p.get('gid'), 'name': p.get('name')}
                    for p in body.get('projects') or [] if isinstance(p, dict) and p.get('gid')]
        if not projects and op.get('projects'):
            # Respuesta sin los proyectos: los del alta
            projects = [{'gid': gid, 'name': op.get('project_name') or None}
                        for gid in op['projects']]
        # name/completed tal como quedaron en Asana: si difieren del alta (se
        # editó mientras viajaba, o se encontró la de un intento anterior),
        # el enlace los manda como update
        return {'gid': str(body['gid']), 'task_id': op.get('task_id'),
                'provisional': op.get('gid'), 'projects': projects,
                'name': body.get('name'), 'completed': bool(body.get('completed'))}

    @staticmethod
    def _asana_create_since(op: Dict[str, Any]) -> datetime:
        try:
            # Margen de 2 min por desfase entre el reloj local y el de Asana
            return (datetime.fromisoformat(op.get('created_at')).astimezone(timezone.utc)
                    - timedelta(minutes=2))
        except (TypeError, ValueError):
            return datetime.now(timezone.utc) - timedelta(days=1)

    @classmethod
    def _matches_asana_create(cls, op: Dict[str, Any], task: Dict[str, Any]) -> bool:
        """¿Puede ser `task` (de Asana) la creada por esta alta? Mismo nombre
        (alguno de los enviados), en sus proyectos y creada después del alta."""
        names = set(op.get('sent_names') or [])
        names.add(str((op.get('fields') or {}).get('name') or '').strip())
        if str(task.get('name') or '').strip() not in names or not task.get('gid'):
            return False
        in_projects = {p.get('gid') for p in task.get('projects') or []
                       if isinstance(p, dict)}
        if not set(op.get('projects') or []) <= in_projects:
            return False
        try:
            created = datetime.fromisoformat(
                str(task.get('created_at')).replace('Z', '+00:00'))
            if created < cls._asana_create_since(op):
                return False
        except (TypeError, ValueError):
            pass
        return True

    def _asana_create_lookup(self, op: Dict[str, Any], headers: Dict[str, str]
                             ) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
        """¿Llegó ya a Asana esta alta (un POST cuya respuesta se perdió)?
        Busca entre las tareas asignadas modificadas desde poco antes del
        alta una con el MISMO nombre (alguno de los enviados) y, si iba a un
        proyecto, en ese proyecto. Devuelve (estado, detalle, sin_red,
        resultado) con estado 'created' | 'absent' | 'auth' | 'retry'. Descarta
        los gids que ya son de otra tarea de la app (`skip_gids`)."""
        if not op.get('assignee') or not op.get('workspace'):
            return 'absent', '', False, None
        since = self._asana_create_since(op)
        params = {'assignee': op['assignee'], 'workspace': op['workspace'],
                  'modified_since': since.strftime('%Y-%m-%dT%H:%M:%S.000Z'), 'limit': 100,
                  'opt_fields': 'gid,name,completed,created_at,projects.gid,projects.name'}
        skip = set(op.get('skip_gids') or ())
        try:
            for _ in range(50):
                resp = requests.get(f"{Config.ASANA_API_URL}/tasks", headers=headers,
                                    params=params, timeout=15)
                if resp.status_code != 200:
                    verdict = _classify_asana_status(resp.status_code)
                    if verdict in ('auth', 'retry'):
                        return verdict, f"HTTP {resp.status_code}", False, None
                    return 'absent', f"HTTP {resp.status_code}", False, None
                body = resp.json() or {}
                for task in body.get('data') or []:
                    if str(task.get('gid')) in skip or not self._matches_asana_create(op, task):
                        continue
                    return 'created', '', False, self._asana_created_result(op, task)
                offset = (body.get('next_page') or {}).get('offset')
                if not offset:
                    return 'absent', '', False, None
                params['offset'] = offset
        except requests.RequestException as e:
            print(f"Asana: no se pudo comprobar el alta de {op.get('gid')}: {e}")
            return 'retry', 'sin conexión', True, None
        return 'absent', '', False, None

    def _link_asana_created(self, result: Dict[str, Any], op: Optional[Dict[str, Any]]) -> None:
        """(Hilo de UI) Alta confirmada: la tarea local pasa a enlazada
        (source='asana'; sus etiquetas siguen solo en PomoGrowth) y las ops
        con el gid provisional pasan al real. Si se borró mientras viajaba, la
        lápida guarda el gid para que el sync no la resucite. Un gid que ya
        es de otra tarea de la app no se enlaza a una segunda."""
        gid, task_id = result['gid'], result.get('task_id')
        task = next((t for t in self.data.get('tasks', []) if t.get('id') == task_id), None)
        taken = any(str(t.get('asana_id')) == gid for t in self.data.get('tasks', [])
                    if t.get('id') != task_id and t.get('asana_id'))
        if task is None:
            for gone in self.data.get('deleted_tasks') or []:
                if gone.get('id') == task_id and not gone.get('asana_id'):
                    gone['asana_id'] = gid
            # Borrada mientras viajaba: sus comentarios de sesión no salen
            outbox = self.data.get('asana_outbox') or []
            outbox[:] = [o for o in outbox
                         if o is op or o.get('gid') != result.get('provisional')]
        elif not task.get('asana_id') and not taken:
            local = [x for x in task.get('tags') or [] if isinstance(x, str)]
            projects = result.get('projects') or []
            task['source'] = 'asana'
            task['asana_id'] = gid
            task['projects'] = projects
            task['local_tags'] = local
            task['tags'] = _with_local_tags(
                [str(p['name']).strip() for p in projects if p.get('name')], local)
        for other in self.data.get('asana_outbox') or []:
            if other.get('gid') == result.get('provisional') and other is not op:
                other['gid'] = gid
        if task is not None and op is not None and str(task.get('asana_id')) == gid:
            # Completada o renombrada mientras el alta viajaba (o antes de
            # un reintento que encontró la ya creada): lo que difiera, a Asana
            fields = op.get('fields') or {}
            changed = {}
            if ('name' in fields and str(fields['name']).strip()
                    != str(result.get('name') or '').strip()):
                changed['name'] = fields['name']
            if 'completed' in fields and bool(fields['completed']) != bool(result.get('completed')):
                changed['completed'] = bool(fields['completed'])
            self._enqueue_asana_update(gid, changed)

    @staticmethod
    def _migrate_push_pending(data: Dict[str, Any]) -> bool:
        """Retira la marca heredada `asana_push_pending` (≤ v9.2.1). Solo la
        ponía completar/reabrir con el sync encendido, y nunca se limpiaba
        tras un envío con éxito:
        - Si coincide con el estado local, ese cambio casi seguro ya salió: no
          se reenvía (pisaría una reapertura hecha en Asana después, justo lo
          que la marca ocultaba) y el primer sync deja mandar a Asana. Queda
          `asana_legacy_check` para avisar si ese sync cambia el estado (p. ej.
          el envío de v9.2.1 falló sin que nadie lo viera).
        - Si no coincide, el último cambio se hizo con el sync apagado y nunca
          salió: se encola el estado local, como hace ahora la app.
        Nada se encola para tareas marcadas como eliminadas en Asana."""
        changed = False
        outbox = data.setdefault('asana_outbox', [])
        for task in data.get('tasks', []):
            if 'asana_push_pending' not in task:
                continue
            flag = task.pop('asana_push_pending')
            changed = True
            gid = task.get('asana_id')
            if (flag is None or not gid or task.get('source') != 'asana'
                    or task.get('asana_deleted')):
                continue
            if bool(flag) == bool(task.get('completed')):
                task['asana_legacy_check'] = True
                continue
            local = bool(task.get('completed'))
            for op in outbox:
                if op.get('type') == 'update' and op.get('gid') == str(gid):
                    op.setdefault('fields', {}).setdefault('completed', local)
                    break
            else:
                outbox.append(_new_outbox_op('update', gid, fields={'completed': local}))
        return changed

    def import_data(self, imported: Dict[str, Any]) -> None:
        """Reemplaza los datos por un backup, conservando la cola actual hacia
        Asana. De la cola del archivo solo se añaden los comentarios, marcados
        como ya intentados: antes de publicarlos se comprueba en Asana, así que
        no se duplican si ya llegaron. Sus cambios de estado o nombre no se
        reaplican (pisarían Asana con estados viejos), ni se migran sus marcas
        heredadas."""
        imported = dict(imported)
        for task in imported.get('tasks') or []:
            if isinstance(task, dict):
                task.pop('asana_push_pending', None)
        outbox = list(self.data.get('asana_outbox') or [])
        known = {op.get('id') for op in outbox}
        for op in imported.get('asana_outbox') or []:
            if (isinstance(op, dict) and op.get('type') == 'comment'
                    and op.get('gid') and op.get('id') not in known):
                outbox.append({**op, 'send_tries': max(1, op.get('send_tries') or 0)})
        imported['asana_outbox'] = outbox
        # La biblioteca de música no viaja en los backups viejos: sin una
        # válida en el archivo, se conserva la actual (con su marca de las
        # radios de serie ya ofrecidas)
        if not self._has_valid_music(imported.get('music')):
            imported['music'] = copy.deepcopy(self.data.get('music') or default_music())
        self.data = imported
        self._save()
        # Releer con los setdefault/validaciones de _load. Un backup con
        # música pero sin marca de semillas (anterior a las radios de serie)
        # las recibe ahí; uno con marca se respeta tal cual
        self._migrated_on_load = False
        self.data = self._load()
        migrated = self._migrated_on_load
        # Las altas pendientes de tareas que no están en el backup no se crean
        if self._drop_asana_creates_without_task() or migrated:
            self._save()

    def _delete_asana_task_in_thread(self, task_gid: str):
        headers = self._get_asana_headers()
        if not headers:
            print("No se eliminó en Asana: Falta el Token de Acceso Personal (PAT).")
            return
        try:
            url = f"{Config.ASANA_API_URL}/tasks/{task_gid}"
            response = requests.delete(url, headers=headers, timeout=15)
            if response.status_code in (200, 204):
                print(f"Tarea {task_gid} eliminada correctamente en Asana.")
            else:
                print(f"Error al eliminar la tarea {task_gid} en Asana: {response.status_code} - {response.text}")
        except requests.RequestException as e:
            print(f"Error de red al eliminar tarea en Asana: {e}")

    def delete_asana_task(self, task_gid: str):
        threading.Thread(target=self._delete_asana_task_in_thread, args=(task_gid,), daemon=True).start()

    # --------- NUEVO: Utilidades para bienvenida ---------
    def get_incomplete_tasks_summary(self) -> Optional[Dict[str, Any]]:
        """Devuelve un resumen de tareas incompletas por proyecto para el usuario configurado.
        Estructura: { 'user_name': str|None, 'total': int, 'per_project': { project_name: count } }
        Si un listado de Asana falla lanza AsanaFetchError (antes contaba a
        medias, o 0, sin avisar). Los nombres de usuario y proyecto siguen
        siendo de mejor esfuerzo (sin ellos, el GID).
        """
        headers = self._get_asana_headers()
        if not headers or not REQUESTS_AVAILABLE:
            return None
        s = self.data.get('settings', {})
        assignee_gid = (s.get('asana_assignee_gid') or '').strip() or None
        workspace_gid_global = (s.get('asana_workspace_gid') or '').strip() or None
        connections = s.get('asana_connections') or []
        # Fallback a un solo proyecto si no hay conexiones múltiples
        single_project_gid = s.get('asana_project_gid')
        project_gids: List[str] = [c.get('project_gid') for c in connections if isinstance(c, dict) and c.get('project_gid')]
        if not project_gids and single_project_gid:
            project_gids = [single_project_gid]
        if not project_gids:
            return None
        per_project: Dict[str, int] = {}
        total = 0
        # Obtener nombre y GID del usuario (mejor esfuerzo)
        user_name = None
        if assignee_gid:
            user_name = self.get_asana_user_name(assignee_gid)
        else:
            # Intentar resolver /users/me
            try:
                me_resp = requests.get(f"{Config.ASANA_API_URL}/users/me", headers=headers, timeout=12)
                if me_resp.status_code == 200:
                    me_data = (me_resp.json() or {}).get('data') or {}
                    assignee_gid = me_data.get('gid') or assignee_gid
                    user_name = me_data.get('name') or user_name
            except Exception:
                pass
        # Mapa proyecto->workspace (por conexiones), con fallback al global
        project_workspace_map: Dict[str, Optional[str]] = {}
        for c in connections:
            if isinstance(c, dict):
                pg = c.get('project_gid')
                wg = (c.get('workspace_gid') or '').strip() or None
                if pg:
                    project_workspace_map[pg] = wg or workspace_gid_global
        # Rellenar para proyectos sin conexión detallada
        for pg in project_gids:
            if pg not in project_workspace_map:
                project_workspace_map[pg] = workspace_gid_global

        # Preferir el endpoint de búsqueda por workspace para filtros confiables
        # cuando haya workspace. Un listado que falla (también en una página
        # intermedia) lanza AsanaFetchError: nunca se cuenta a medias
        for pgid in project_gids:
            pname = self.get_asana_project_name(pgid) or pgid
            count: Optional[int] = None
            workspace_gid = project_workspace_map.get(pgid)
            if workspace_gid:
                # /workspaces/{workspace_gid}/tasks/search con filtros
                params = {
                    "projects.any": pgid,
                    **({"assignee.any": assignee_gid} if assignee_gid else {}),
                    "completed": False,
                    "limit": 100,
                    "opt_fields": "gid,completed"
                }
                try:
                    items = self._asana_get_all(
                        f"{Config.ASANA_API_URL}/workspaces/{workspace_gid}/tasks/search",
                        headers, params, 20, target=f"el workspace {workspace_gid}")
                    count = sum(1 for t in items if not t.get('completed', False))
                except AsanaFetchError as e:
                    # La búsqueda es solo de los planes de pago (402): sin
                    # ella, el listado del proyecto (antes contaba 0)
                    if e.status != 402:
                        raise
            if count is None:
                # Listado por proyecto y filtrado cliente
                params = {"limit": 100, "opt_fields": "gid,completed,assignee.gid"}
                items = self._asana_get_all(
                    f"{Config.ASANA_API_URL}/projects/{pgid}/tasks", headers, params, 20,
                    target=f"el proyecto «{pname}»",
                    hint="Revisa los proyectos en ⚙ Configuración → Asana.")
                count = sum(1 for t in items
                            if (not assignee_gid
                                or (t.get('assignee') or {}).get('gid') == assignee_gid)
                            and not t.get('completed', False))
            total += count
            per_project[pname] = per_project.get(pname, 0) + count
        # Fallback local si API no devuelve nada útil pero hay datos locales
        if total == 0:
            try:
                tasks = self.get_tasks()
                per_project_local: Dict[str, int] = {}
                total_local = 0
                # Construir mapa project_gid->nombre, usando cache y conexiones
                name_cache: Dict[str, str] = {}
                for pg in project_gids:
                    name_cache[pg] = self.get_asana_project_name(pg) or pg
                # Contabilizar
                for t in tasks:
                    if not t.get('completed', False):
                        projs = t.get('projects') or []
                        if projs and isinstance(projs, list):
                            # Tomar todos los nombres asociados
                            taken = False
                            for p in projs:
                                pname = p.get('name') or None
                                if pname:
                                    per_project_local[pname] = per_project_local.get(pname, 0) + 1
                                    total_local += 1
                                    taken = True
                            if not taken:
                                per_project_local['Sin proyecto'] = per_project_local.get('Sin proyecto', 0) + 1
                                total_local += 1
                        else:
                            per_project_local['Sin proyecto'] = per_project_local.get('Sin proyecto', 0) + 1
                            total_local += 1
                if total_local > 0:
                    return {"user_name": user_name, "total": total_local, "per_project": per_project_local}
            except Exception:
                pass
        return {"user_name": user_name, "total": total, "per_project": per_project}

    def get_asana_project_name(self, project_gid: str) -> Optional[str]:
        """Obtiene el nombre del proyecto de Asana por su GID."""
        headers = self._get_asana_headers()
        if not headers or not REQUESTS_AVAILABLE or not project_gid:
            return None
        try:
            url = f"{Config.ASANA_API_URL}/projects/{project_gid}"
            resp = requests.get(url, headers=headers, params={"opt_fields": "name"}, timeout=15)
            if resp.status_code == 200:
                return (resp.json().get("data") or {}).get("name")
        except requests.RequestException:
            return None
        return None
    
    def get_asana_user_name(self, user_gid: str) -> Optional[str]:
        """Obtiene el nombre del usuario de Asana por su GID."""
        headers = self._get_asana_headers()
        if not headers or not REQUESTS_AVAILABLE or not user_gid:
            return None
        try:
            url = f"{Config.ASANA_API_URL}/users/{user_gid}"
            resp = requests.get(url, headers=headers, params={"opt_fields": "name"}, timeout=15)
            if resp.status_code == 200:
                return (resp.json().get("data") or {}).get("name")
        except requests.RequestException:
            return None
        return None

    def list_asana_projects(self, workspace_gid: str, pat_override: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lista proyectos de Asana dentro de un Workspace.
        Devuelve una lista de dicts con al menos: {'gid': str, 'name': str}
        Permite un PAT temporal (pat_override) para primer uso antes de guardar.
        Si Asana no responde bien (también en una página intermedia) lanza
        AsanaFetchError: Ajustes muestra el motivo en vez de «no hay
        proyectos» o una lista incompleta.
        """
        # Construir headers usando override si se provee
        if not REQUESTS_AVAILABLE or not workspace_gid:
            return []
        headers = None
        if pat_override:
            pat = pat_override.strip()
            if pat:
                headers = {"Authorization": f"Bearer {pat}"}
        if headers is None:
            headers = self._get_asana_headers()
        if not headers:
            return []
        params = {
            "workspace": workspace_gid,
            "archived": False,
            "limit": 100,
            "opt_fields": "gid,name",
        }
        results = self._asana_get_all(f"{Config.ASANA_API_URL}/projects", headers, params, 20,
                                      target=f"el workspace {workspace_gid}",
                                      hint="Revisa el Workspace GID y el token de acceso.")
        # Orden alfabético por nombre
        results = [p for p in results if isinstance(p, dict) and p.get("gid") and p.get("name")]
        results.sort(key=lambda p: p.get("name", "").lower())
        return results

    def add_task(self, name: str, tags: List[str], source: str = 'local', asana_id: Optional[str] = None,
                 recurring=False, due: Optional[str] = None,
                 asana_create: Optional[Dict[str, Any]] = None) -> int:
        """recurring: False/None (normal), True (fija diaria, compat) o un
        ciclo de RECURRING_CYCLES ('daily'/'weekly'/'monthly'/'quarterly').
        due: vencimiento 'YYYY-MM-DD' (como el due_on de Asana) o None.
        asana_create: destino para crearla también en Asana; el alta se
        encola en la misma escritura (ver _enqueue_asana_create)."""
        new_task = {'id': int(time.time()*1000), 'name': name, 'completed': False, 'tags': tags,
                    'source': source, 'asana_id': asana_id,
                    'created_at': datetime.now().isoformat()}
        if recurring:
            new_task['recurring'] = recurring if recurring in RECURRING_CYCLES else 'daily'
        if due:
            new_task['due'] = due
        # Las tareas nuevas van ARRIBA de la lista, no al final
        self.data['tasks'].insert(0, new_task)
        # Asegurar unicidad de IDs ante ráfagas de creación
        self._sanitize_ids()
        if asana_create and source == 'local':
            self._enqueue_asana_create(new_task, asana_create)
        # Snapshot (no la referencia viva) y log ANTES del save: una sola
        # escritura del JSON por alta
        self._log_history('add_task', {'task': {**new_task, 'tags': list(tags)}})
        self._save()
        return new_task['id']
    
    def save_settings(self, settings: Dict[str, Any]): 
        self.data['settings'] = settings
        self._save()
    
    def load_settings(self) -> Dict[str, Any]: 
        return self.data.get('settings', {})
    
    def update_task_status(self, task_id: int, completed: bool):
        for task in self.data['tasks']:
            if task.get('id') == task_id:
                before = {'id': task_id, 'completed': task.get('completed', False)}
                task['completed'] = completed
                if completed:
                    task['completed_at'] = datetime.now().isoformat()
                else:
                    task.pop('completed_at', None)
                    task.pop('effort', None)  # la copia de la tarjeta; el histórico queda
                if (task.get('source') == 'asana' and task.get('asana_id')
                        and not task.get('asana_deleted')):
                    # Cambio del usuario: a la cola hacia Asana, también con el
                    # sync apagado (antes se perdía y el siguiente sync lo revertía)
                    self._enqueue_asana_update(task['asana_id'], {'completed': bool(completed)})
                elif task.get('source') == 'local':
                    self._merge_into_asana_create(task_id, {'completed': bool(completed)})
                self._log_history('update_status', {'before': before, 'after': {'id': task_id, 'completed': completed}})
                break
        self._save()

    def reopen_recurring_tasks(self) -> int:
        """Reabre tareas fijas completadas cuyo ciclo ya venció (diaria: al día
        siguiente; semanal: +7 días; mensual/trimestral: +1/+3 meses de
        calendario). Devuelve cuántas se reabrieron. Se llama al arrancar y al
        cambiar de día."""
        reopened = 0
        today = datetime.now().date()
        for task in self.data.get('tasks', []):
            cycle = recurring_cycle(task)
            if not cycle or not task.get('completed'):
                continue
            done_raw = str(task.get('completed_at') or '')[:10]
            try:
                done_date = date.fromisoformat(done_raw)
            except ValueError:
                # Sin fecha de completado (dato viejo): reabrir ya
                done_date = None
            if done_date is None or today >= next_reopen_date(done_date, cycle):
                task['completed'] = False
                task.pop('completed_at', None)
                task.pop('effort', None)  # cada ciclo se valora de nuevo
                reopened += 1
        if reopened:
            self._log_history('reopen_recurring', {'count': reopened})
            self._save()
        return reopened
    
    # --------- Valoración de esfuerzo ---------
    def add_effort_rating(self, task_id: int, rating: int,
                          project: str = '') -> Optional[Dict[str, Any]]:
        """Guarda la valoración de esfuerzo (1-5) de una tarea recién
        completada en 'effort_ratings' (histórico: las fijas suman una por
        ciclo) y una copia ligera en task['effort'] para la tarjeta. No va a
        Asana ni da XP. Devuelve la entrada, o None si la tarea no existe."""
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValueError(f"Esfuerzo fuera de rango (1-5): {rating!r}")
        task = next((t for t in self.data.get('tasks', []) if t.get('id') == task_id), None)
        if task is None:
            return None
        entry = {
            'task_id': task_id,
            'rating': rating,
            'timestamp': datetime.now().isoformat(),
            'task_name': task.get('name', ''),
            'project': project or '',
            'tags': list(task.get('tags') or []),
            'recurring': recurring_cycle(task),
            'focus_s': round(self._effort_focus_seconds(task)),
        }
        task['effort'] = rating
        self.data.setdefault('effort_ratings', []).append(entry)
        self._log_history('effort_rating', {'id': task_id, 'rating': rating})
        self._save()
        return entry

    def _effort_focus_seconds(self, task: Dict[str, Any]) -> float:
        """Segundos de pomodoro de la tarea desde su último completado (el
        anterior al actual, según el historial; sin él, desde siempre)."""
        task_id = task.get('id')
        current = str(task.get('completed_at') or '')
        since = ''
        for entry in self.data.get('history', []):
            payload = entry.get('payload') or {}
            after = payload.get('after') if isinstance(payload, dict) else None
            ts = str(entry.get('ts') or '')
            if (entry.get('action') == 'update_status' and isinstance(after, dict)
                    and after.get('id') == task_id and after.get('completed')
                    and (not current or ts < current) and ts > since):
                since = ts
        total = 0.0
        for c in self.data.get('time_chunks', []):
            if c.get('task_id') != task_id or c.get('type', 'pomodoro') != 'pomodoro':
                continue
            if since and str(c.get('timestamp') or '') <= since:
                continue
            try:
                total += float(c.get('duration') or 0)
            except (TypeError, ValueError):
                continue
        return total

    def update_task_name_and_tags(self, task_id: int, new_name: str, new_tags: List[str],
                                  recurring=_UNSET):
        """recurring (opcional): '' o None quita la repetición; un ciclo de
        RECURRING_CYCLES la fija; omitido, no se toca."""
        for task in self.data['tasks']:
            if task.get('id') == task_id:
                before = {'id': task_id, 'name': task.get('name'), 'tags': task.get('tags', []),
                          'recurring': task.get('recurring')}
                gid = task.get('asana_id') if task.get('source') == 'asana' else None
                if gid:
                    # Renombrado real (no solo espacios en los extremos) de una
                    # tarea que sigue existiendo en Asana: a la cola
                    if (not task.get('asana_deleted')
                            and new_name.strip() != (task.get('name') or '').strip()):
                        self._enqueue_asana_update(gid, {'name': new_name})
                    # Los tags de una tarea de Asana (sus etiquetas + proyectos)
                    # los rehace cada sync: los añadidos a mano se guardan aparte
                    # para que sobrevivan (antes el sync los borraba)
                    # (dato viejo sin local_tags: lo siembra el próximo merge
                    # comparando con los tags reales de Asana)
                    if 'local_tags' in task:
                        old_local = {str(x).lower() for x in task['local_tags'] or []}
                        from_asana = {str(x).lower() for x in task.get('tags') or []} - old_local
                        task['local_tags'] = [x for x in new_tags
                                              if str(x).lower() not in from_asana]
                elif new_name.strip() != (task.get('name') or '').strip():
                    self._merge_into_asana_create(task_id, {'name': new_name})
                task['name'] = new_name
                task['tags'] = new_tags
                if recurring is not _UNSET:
                    if recurring:
                        task['recurring'] = recurring if recurring in RECURRING_CYCLES else 'daily'
                    else:
                        task.pop('recurring', None)
                self._log_history('update_task', {'before': before,
                                                  'after': {'id': task_id, 'name': new_name,
                                                            'tags': new_tags,
                                                            'recurring': task.get('recurring')}})
                break
        self._save()
    
    def delete_task(self, task_id: int):
        # Mover tarea a 'deleted_tasks' y preservar time_chunks como historial
        remaining = []
        deleted_task = None
        for t in self.data['tasks']:
            if t.get('id') == task_id:
                deleted_task = dict(t)
                deleted_task['deleted_at'] = datetime.now().isoformat()
                continue
            remaining.append(t)
        self.data['tasks'] = remaining
        self._drop_asana_create(task_id)  # alta en Asana aún pendiente
        if deleted_task:
            self.data.setdefault('deleted_tasks', []).append(deleted_task)
            self._log_history('delete_task', {'task': deleted_task})
        # No eliminamos time_chunks para mantener historial
        self._save()
    
    def reorder_tasks(self, reordered_tasks: List[Dict[str, Any]]):
        """Guarda las tareas en el nuevo orden."""
        try:
            self.data['tasks'] = reordered_tasks
            self._log_history('reorder_tasks', {'count': len(reordered_tasks)})
            self._save()
        except Exception as e:
            print(f"Error guardando reorden: {e}")
            traceback.print_exc()
    
    def log_time_chunk(self, duration: float, task_id: Optional[int], chunk_type: str, project_name: Optional[str] = None,
                       cycle_completed: bool = False, ended_at: Optional[float] = None,
                       mode: Optional[str] = None):
        """ended_at: epoch en que terminó el tramo (por defecto, ahora). Con
        una suspensión de por medio el tramo terminó antes de despertar, y las
        estadísticas lo reparten por el día/hora de este timestamp.
        mode: 'free' en los tramos del modo Libre (type sigue siendo
        'pomodoro': cuentan como tiempo enfocado, no como pomodoros)."""
        if duration < 1 or task_id is None: return
        when = datetime.fromtimestamp(ended_at) if ended_at else datetime.now()
        chunk = {'duration': duration, 'task_id': task_id, 'timestamp': when.isoformat(), 'type': chunk_type}
        if project_name:
            # Guardar el nombre de proyecto (Asana) para mejorar el tracking por proyecto
            chunk['project'] = project_name
        if cycle_completed:
            # Este pomodoro cerró un ciclo completo (N pomodoros + descansos)
            chunk['cycle_completed'] = True
        if mode:
            chunk['mode'] = mode
        self.data['time_chunks'].append(chunk); self._save()
    
    def get_tasks(self) -> List[Dict[str, Any]]: return self.data.get('tasks', [])
    
    def get_time_chunks(self) -> List[Dict[str, Any]]: return self.data.get('time_chunks', [])

    # --------- Historial y Snapshot de UI ---------
    def _log_history(self, action: str, payload: Dict[str, Any]):
        """Solo anota en memoria: el _save() lo hace la operación que llama
        (antes se escribía el JSON completo dos veces por cada mutación)."""
        try:
            entry = {'ts': datetime.now().isoformat(), 'action': action, 'payload': payload}
            self.data.setdefault('history', []).append(entry)
            # Mantener tamaño razonable opcionalmente
            if len(self.data['history']) > 5000:
                self.data['history'] = self.data['history'][-5000:]
        except Exception:
            pass

    def save_ui_snapshot(self, snapshot: Dict[str, Any]):
        try:
            self.data['ui_snapshot'] = snapshot
            self._save()
        except Exception:
            pass

    def get_ui_snapshot(self) -> Dict[str, Any]:
        return self.data.get('ui_snapshot', {})

    # --------- Plan de hoy (tareas elegidas al empezar el día) ---------
    @staticmethod
    def _valid_day_plan(plan: Any) -> Dict[str, Any]:
        """{'date': 'YYYY-MM-DD', 'task_ids': [int, ...]} saneado: ids enteros
        (no bool), sin repetir y en su orden; cualquier otra cosa, plan vacío."""
        if not isinstance(plan, dict) or not isinstance(plan.get('date'), str):
            return {'date': '', 'task_ids': []}
        ids, seen = [], set()
        raw_ids = plan.get('task_ids')
        for tid in raw_ids if isinstance(raw_ids, list) else []:
            if isinstance(tid, int) and not isinstance(tid, bool) and tid not in seen:
                seen.add(tid)
                ids.append(tid)
        return {'date': plan['date'], 'task_ids': ids}

    def get_day_plan(self, today: Optional[date] = None) -> List[int]:
        """Ids del plan de hoy, en el orden elegido. Vacía si el plan guardado
        es de otro día; los ids de tareas que ya no existen se descartan (las
        completadas se quedan: siguen siendo parte del día)."""
        today = today or date.today()
        plan = self._valid_day_plan(self.data.get('day_plan'))
        if plan['date'] != today.isoformat():
            return []
        existing = {t.get('id') for t in self.data.get('tasks', [])}
        return [tid for tid in plan['task_ids'] if tid in existing]

    def set_day_plan(self, ids, today: Optional[date] = None) -> None:
        """Guarda el plan de hoy (reemplaza el anterior). Una lista vacía
        cuenta como «hoy ya se decidió»: has_day_plan_today() da True."""
        today = today or date.today()
        self.data['day_plan'] = self._valid_day_plan(
            {'date': today.isoformat(), 'task_ids': list(ids or [])})
        self._save()

    def has_day_plan_today(self, today: Optional[date] = None) -> bool:
        """¿Ya se guardó un plan (aunque sea vacío) con la fecha de hoy?"""
        plan = self.data.get('day_plan')
        return (isinstance(plan, dict)
                and plan.get('date') == (today or date.today()).isoformat())

    # --------- Música personalizada (core.music_library) ---------
    @staticmethod
    def _valid_music(music: Any) -> Dict[str, Any]:
        """data['music'] saneado: listas/items mal formados fuera, límites de
        tamaño, ids únicos y valores por defecto (ver normalize_music)."""
        return normalize_music(music)

    @staticmethod
    def _has_valid_music(music: Any) -> bool:
        """¿Trae un backup una biblioteca de música aprovechable?"""
        return isinstance(music, dict) and isinstance(music.get('lists'), list)

    def _seed_default_radios(self, music: Dict[str, Any]) -> bool:
        """Siembra en `music` (saneada) las radios de serie que nunca se le
        ofrecieron (core/radio_catalog.py). True si cambió algo. Un fallo
        aquí nunca impide cargar los datos: se avisa y se sigue sin ellas."""
        if not self.SEED_DEFAULT_RADIOS:
            return False
        try:
            return apply_default_radios(music)
        except Exception as e:
            print(f"Advertencia: no se pudieron añadir las radios de serie: {e}")
            return False

    def get_music(self) -> Dict[str, Any]:
        """Copia validada de la biblioteca de música (con valores por defecto)."""
        return copy.deepcopy(self._valid_music(self.data.get('music')))

    def save_music(self, music: Dict[str, Any]) -> None:
        self.data['music'] = self._valid_music(music)
        self._save()

    # --------- Racha de uso (días en que se abrió la app) ---------
    ACTIVITY_DAYS_MAX = 400  # días que se guardan (los más recientes)

    @classmethod
    def _valid_activity_days(cls, days: Any) -> List[str]:
        """'YYYY-MM-DD' válidos, sin repetir, ordenados y recortados a los
        últimos ACTIVITY_DAYS_MAX."""
        valid = set()
        for d in days if isinstance(days, (list, set, tuple)) else []:
            if isinstance(d, str):
                try:
                    valid.add(date.fromisoformat(d[:10]).isoformat())
                except ValueError:
                    continue
        return sorted(valid)[-cls.ACTIVITY_DAYS_MAX:]

    def _seed_activity_days(self) -> Set[str]:
        """Días con actividad ya registrada (tramos de tiempo e historial):
        quien usa la app desde hace meses no empieza la racha en 1."""
        days = set()
        for chunk in self.data.get('time_chunks') or []:
            if isinstance(chunk, dict) and isinstance(chunk.get('timestamp'), str):
                days.add(chunk['timestamp'][:10])
        for entry in self.data.get('history') or []:
            if isinstance(entry, dict) and isinstance(entry.get('ts'), str):
                days.add(entry['ts'][:10])
        return days

    def mark_active_day(self, today: Optional[date] = None) -> bool:
        """Apunta hoy como día de uso (al arrancar y al cambiar de día). La
        primera vez siembra con el histórico (marca en settings). Devuelve
        True si cambió algo."""
        today = today or date.today()
        days = set(self._valid_activity_days(self.data.get('activity_days')))
        days.add(today.isoformat())
        settings = self.data.setdefault('settings', {})
        seeding = not settings.get('activity_days_seeded')
        if seeding:
            days |= self._seed_activity_days()
            settings['activity_days_seeded'] = True
        new = self._valid_activity_days(list(days))
        changed = seeding or new != self.data.get('activity_days')
        self.data['activity_days'] = new
        if changed:
            self._save()
        return changed

    def get_activity_days(self) -> List[str]:
        return list(self._valid_activity_days(self.data.get('activity_days')))

    def get_usage_streak(self, today: Optional[date] = None) -> Tuple[int, int]:
        """(racha actual, mejor racha) en días seguidos de uso. La actual
        cuenta desde hoy o, si hoy aún no consta, desde ayer (no marca 0 por
        la mañana); un día sin uso la rompe."""
        today = today or date.today()
        days = {date.fromisoformat(d) for d in self.get_activity_days()}
        day = today if today in days else today - timedelta(days=1)
        current = 0
        while day in days:
            current += 1
            day -= timedelta(days=1)
        best = run = 0
        previous = None
        for d in sorted(days):
            run = run + 1 if previous is not None and d - previous == timedelta(days=1) else 1
            best = max(best, run)
            previous = d
        return current, max(best, current)

    def get_all_tags(self) -> List[str]:
        all_tags = set()
        for task in self.data.get('tasks', []):
            for tag in task.get('tags', []):
                all_tags.add(tag)
        return sorted(list(all_tags))
    
    def remove_last_added_asana_tag(self) -> int:
        """Elimina la última etiqueta 'asana' agregada (si hay duplicadas) por cada tarea.
        Devuelve el número de tareas modificadas. Preserva una sola ocurrencia.
        """
        modified = 0
        try:
            for task in self.data.get('tasks', []):
                tags = task.get('tags')
                if not isinstance(tags, list) or not tags:
                    continue
                idxs = [i for i, t in enumerate(tags) if isinstance(t, str) and t.lower() == 'asana']
                if len(idxs) > 1:
                    # Remover la última (se asume fue la agregada recientemente)
                    last_idx = idxs[-1]
                    try:
                        tags.pop(last_idx)
                        task['tags'] = tags
                        modified += 1
                    except Exception:
                        pass
        except Exception:
            pass
        if modified:
            self._save()
        return modified
    
    def clear_data(self):
        current_settings = self.data.get('settings', {})
        # La cola hacia Asana se conserva: son cambios ya hechos que esperan envío
        # La música (listas, volumen) es configuración, no datos de trabajo
        self.data = {'tasks': [], 'time_chunks': [], 'settings': current_settings,
                     'asana_outbox': self.data.get('asana_outbox') or [],
                     'music': self._valid_music(self.data.get('music'))}
        # …salvo las altas de las tareas que se acaban de borrar
        self._drop_asana_creates_without_task()
        # La racha de uso vuelve a empezar: hoy cuenta (la app está abierta)
        self.data['activity_days'] = [date.today().isoformat()]
        self._save()

    # ------------------ ASANA SYNC (READ) ------------------
    # Las lecturas de listados pasan por _asana_get_json/_asana_get_all: un
    # error (HTTP, red, JSON ilegible) lanza AsanaFetchError en vez de
    # devolver una lista vacía o a medias que el sync tomaría por buena.
    def _asana_get_json(self, url: str, headers: Dict[str, str],
                        params: Optional[Dict[str, Any]], timeout: float,
                        target: str = '', hint: str = '') -> Dict[str, Any]:
        """GET a Asana que tiene que salir bien: el cuerpo JSON (un dict) o
        AsanaFetchError."""
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.RequestException as e:
            print(f"Error de red al consultar Asana ({url}): {e}")
            raise AsanaFetchError('network', target=target, hint=hint, detail=str(e)) from e
        if resp.status_code != 200:
            err = error_from_response(resp, target, hint)
            print(f"Error al leer de Asana ({url}): HTTP {resp.status_code} "
                  f"({err.kind}) - {err.detail}")
            raise err
        try:
            body = resp.json()
        except ValueError as e:  # también el JSONDecodeError de requests
            print(f"Asana devolvió un JSON ilegible ({url}): {e}")
            raise AsanaFetchError('bad_response', 200, target=target, hint=hint,
                                  detail=f"JSON ilegible: {e}") from e
        if not isinstance(body, dict):
            raise AsanaFetchError('bad_response', 200, target=target, hint=hint,
                                  detail=f"se esperaba un objeto, llegó {type(body).__name__}")
        return body

    # Tope de páginas de un listado (a 100 por página, 20 000 elementos): un
    # next_page que no se acaba nunca (Asana o un proxy que repite el offset)
    # no puede dejar el sync dando vueltas para siempre
    ASANA_MAX_PAGES = 200

    def _asana_get_all(self, url: str, headers: Dict[str, str], params: Dict[str, Any],
                       timeout: float, target: str = '', hint: str = '') -> List[Dict[str, Any]]:
        """Todas las páginas de un listado (siguiendo next_page.offset, en el
        mismo dict de params) o AsanaFetchError: una página que falla invalida
        el listado entero, nunca se devuelve a medias. Pasado el tope de
        páginas, bad_response."""
        results: List[Dict[str, Any]] = []
        for _ in range(self.ASANA_MAX_PAGES):
            body = self._asana_get_json(url, headers, params, timeout, target, hint)
            data = body.get('data')
            if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                raise AsanaFetchError('bad_response', 200, target=target, hint=hint,
                                      detail="falta la lista 'data' o trae elementos raros")
            results.extend(data)
            next_page = body.get('next_page')
            offset = next_page.get('offset') if isinstance(next_page, dict) else None
            if not offset:
                return results
            params["offset"] = offset
        print(f"Asana: el listado ({url}) no se acaba tras {self.ASANA_MAX_PAGES} páginas")
        raise AsanaFetchError('bad_response', 200, target=target, hint=hint,
                              detail=f"más de {self.ASANA_MAX_PAGES} páginas (next_page sin fin)")

    def fetch_asana_tasks_for_assignee(self, assignee_gid: str, workspace_gid: str, project_gid: Optional[str] = None, include_completed: bool = False,
                                       include_no_project: bool = False,
                                       linked_gids: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        tasks = self._fetch_asana_assigned(assignee_gid, workspace_gid, include_completed)
        # Filtrado local por proyecto si se especificó
        if project_gid:
            return self._filter_asana_fetch(tasks, {project_gid}, include_no_project, linked_gids)
        return tasks

    @staticmethod
    def _filter_asana_fetch(tasks: List[Dict[str, Any]], project_gids: Set[str],
                            include_no_project: bool = False,
                            linked_gids: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        """Del listado de «asignadas a mí» de un workspace: las de un proyecto
        marcado, las sueltas sin proyecto si se pidieron (no las subtareas:
        vienen sin proyecto pero cuelgan de otra tarea) y, siempre, las que ya
        están enlazadas en la app (p. ej. creadas desde aquí en «Mis tareas»)."""
        linked = set(linked_gids or ())
        out = []
        for t in tasks:
            projects = [p for p in t.get("projects") or [] if isinstance(p, dict)]
            in_projects = {p.get("gid") or p.get("id") for p in projects}
            parent = t.get("parent")
            has_parent = bool(parent.get("gid") if isinstance(parent, dict) else parent)
            if (in_projects & project_gids
                    or (include_no_project and not projects and not has_parent)
                    or (t.get("gid") or t.get("id")) in linked):
                out.append(t)
        return out

    def _fetch_asana_assigned(self, assignee_gid: str, workspace_gid: str,
                              include_completed: bool = False) -> List[Dict[str, Any]]:
        """GET /tasks?assignee&workspace: todas las tareas asignadas del
        workspace («Mis tareas», con o sin proyecto), página a página.

        Cualquier fallo (HTTP, sin red, JSON ilegible), también en una página
        intermedia, lanza AsanaFetchError: el sync debe terminar en el camino
        de ERROR, no como un «éxito» con 0 tareas (o con la mitad) que además
        dispararía la verificación una a una de las que faltan."""
        headers = self._get_asana_headers()
        if not headers or not REQUESTS_AVAILABLE:
            print("Asana fetch omitido: Falta PAT o 'requests'.")
            return []
        params = {
            "assignee": assignee_gid,
            "workspace": workspace_gid,
            "completed_since": "1970-01-01T00:00:00.000Z" if include_completed else "now",
            "limit": 100,
            "opt_fields": "gid,name,completed,permalink_url,created_at,modified_at,due_on,due_at,tags.name,projects.name,projects.gid,assignee.gid,parent.gid"
        }
        return self._asana_get_all(
            f"{Config.ASANA_API_URL}/tasks", headers, params, 20,
            target=f"el workspace {workspace_gid}",
            hint="Revisa el workspace y el Assignee GID en ⚙ Configuración → Asana.")

    def merge_asana_tasks(self, asana_tasks: List[Dict[str, Any]]):
        if not asana_tasks:
            return
        by_asana_id = {t.get('asana_id'): t for t in self.data['tasks'] if t.get('asana_id')}
        # Tombstones: tareas de Asana eliminadas LOCALMENTE no deben resucitar
        # en cada sync (antes el merge las recreaba indefinidamente)
        locally_deleted_gids = {t.get('asana_id') for t in self.data.get('deleted_tasks', [])
                                if t.get('asana_id')}
        # Campos con un cambio local aún en la cola hacia Asana, por tarea
        pending_fields: Dict[str, Set[str]] = {}
        # Altas ya intentadas cuya respuesta no llegó (timeout, 5xx): si el
        # fetch trae la recién creada, no se importa aparte; el próximo envío
        # la encuentra y la enlaza a su tarea local
        uncertain_creates = []
        for op in self.data.get('asana_outbox') or []:
            if op.get('type') == 'update':
                pending_fields.setdefault(op.get('gid'), set()).update(op.get('fields') or {})
            elif op.get('type') == 'create' and op.get('send_tries', 0) >= 1:
                uncertain_creates.append(op)

        for a in asana_tasks:
            gid = a.get('gid') or a.get('id')
            if not gid:
                continue
            name = a.get('name', 'Sin nombre')
            completed = bool(a.get('completed', False))
            # Etiquetas nativas de Asana
            tags_asana = [tag.get('name') for tag in (a.get('tags') or []) if isinstance(tag, dict) and tag.get('name')]
            due = a.get('due_at') or a.get('due_on')
            # Extraer información de proyectos (gid y nombre) si viene en payload
            projects_info = []
            project_names: List[str] = []
            try:
                for p in (a.get('projects') or []):
                    if isinstance(p, dict) and (p.get('gid') or p.get('id')):
                        projects_info.append({
                            'gid': p.get('gid') or p.get('id'),
                            'name': p.get('name')
                        })
                        if p.get('name'):
                            project_names.append(p.get('name'))
            except Exception:
                projects_info = []
                project_names = []
            # Construir conjunto de etiquetas final: solo etiquetas de Asana + nombres de proyectos (sin añadir 'asana')
            final_tags_set = set()
            for tname in tags_asana:
                if isinstance(tname, str) and tname.strip():
                    clean = tname.strip()
                    final_tags_set.add(clean)
            for pname in project_names:
                if isinstance(pname, str) and pname.strip():
                    final_tags_set.add(pname.strip())
            final_tags = sorted(final_tags_set)
            created_at = a.get('created_at') or None
            if gid in by_asana_id:
                # Actualizar existente. Un campo con un cambio local en la cola
                # conserva el valor local (la cola lo llevará a Asana); el
                # resto lo dicta Asana
                t = by_asana_id[gid]
                pending = pending_fields.get(gid, ())
                if 'name' not in pending:
                    t['name'] = name
                legacy = t.pop('asana_legacy_check', None)
                if 'completed' not in pending:
                    if legacy and bool(t.get('completed')) != completed:
                        self.legacy_resynced.append(t.get('name') or 'Sin nombre')
                    t['completed'] = completed
                if 'local_tags' not in t:
                    # Datos ≤ v9.2.1: lo que no viene de Asana (ni de un
                    # proyecto que tenía, aunque se haya renombrado) se
                    # añadió a mano
                    derived = {x.lower() for x in final_tags}
                    derived |= {str(p.get('name') or '').strip().lower()
                                for p in t.get('projects') or [] if isinstance(p, dict)}
                    t['local_tags'] = [x for x in t.get('tags') or []
                                       if isinstance(x, str) and x.lower() not in derived
                                       and x.lower() not in ('asana', 'asana_deleted')]
                t['tags'] = _with_local_tags(final_tags, t['local_tags'])
                t['due'] = due
                if projects_info:
                    t['projects'] = projects_info
                if created_at and not t.get('created_at'):
                    t['created_at'] = created_at
                # Limpiar marca de eliminada si existía
                t.pop('asana_deleted', None)
                if 'asana_deleted' in t.get('tags', []):
                    t['tags'] = [tag for tag in t['tags'] if tag != 'asana_deleted']
            elif gid in locally_deleted_gids:
                continue  # borrada localmente: no recrear
            elif any(self._matches_asana_create(op, {**a, 'gid': gid})
                     for op in uncertain_creates):
                continue  # la creó un alta pendiente: la enlazará su reintento
            else:
                # Crear nueva tarea local enlazada a Asana
                new_task = {
                    'id': int(time.time()*1000) + len(self.data['tasks']), # evitar colisiones simples en ráfagas
                    'name': name,
                    'completed': completed,
                    'tags': final_tags,
                    'local_tags': [],
                    'source': 'asana',
                    'asana_id': gid,
                    'due': due,
                    'projects': projects_info,
                    'created_at': created_at or datetime.now().isoformat()
                }
                self.data['tasks'].append(new_task)
        
        # La detección de tareas eliminadas en Asana (una petición HTTP por
        # tarea) ya NO se hace aquí: este método corre en el hilo de la UI y
        # congelaba la app ~10 s en el arranque. Ahora la verificación corre
        # en el hilo del fetch (find_asana_candidates_to_verify +
        # apply_asana_deleted_marks).

        # Sanear posibles colisiones tras merge masivo
        self._sanitize_ids()
        self._save()

    def find_asana_candidates_to_verify(self, asana_ids: set,
                                        skip_gids: Optional[Set[str]] = None) -> List[str]:
        """GIDs de tareas locales de Asana ausentes del sync (a verificar por red).

        Se excluyen las ya completadas localmente (verificar cada una costaría
        una petición HTTP por sync para siempre) y las que tienen un cambio de
        completado en la cola (la verificación re-completaría una tarea recién
        reabierta cuyo envío aún no aterrizó). Corre en el hilo de red: quien
        llama pasa `skip_gids` calculado en el hilo de UI.
        """
        if skip_gids is None:
            skip_gids = self.asana_gids_with_pending('completed')
        out = []
        for task in self.data.get('tasks', []):
            if (task.get('source') == 'asana' and task.get('asana_id')
                    and task['asana_id'] not in asana_ids
                    and not task.get('asana_deleted')
                    and not task.get('completed')
                    and str(task['asana_id']) not in skip_gids):
                out.append(task['asana_id'])
        return out

    def apply_asana_completed_marks(self, completed_gids) -> int:
        """Marca como completadas las tareas que se verificaron completadas en
        Asana (cerradas fuera de la app). Devuelve cuántas se marcaron. Se
        salta las reabiertas en la app mientras el sync estaba en la red."""
        if not completed_gids:
            return 0
        pending = self.asana_gids_with_pending('completed')
        marked = 0
        for task in self.data.get('tasks', []):
            if (task.get('asana_id') in completed_gids and not task.get('completed')
                    and str(task.get('asana_id')) not in pending):
                if task.pop('asana_legacy_check', None):
                    self.legacy_resynced.append(task.get('name') or 'Sin nombre')
                task['completed'] = True
                task['completed_at'] = datetime.now().isoformat()
                marked += 1
                print(f"✅ Tarea completada en Asana (fuera de la app): {task.get('name', 'Sin nombre')}")
        if marked:
            self._save()
        return marked

    def apply_asana_deleted_marks(self, deleted_gids) -> int:
        """Marca como eliminadas las tareas cuyos GIDs se verificaron ausentes."""
        if not deleted_gids:
            return 0
        marked = 0
        for task in self.data.get('tasks', []):
            if task.get('asana_id') in deleted_gids and not task.get('asana_deleted'):
                task['asana_deleted'] = True
                if 'asana_deleted' not in task.get('tags', []):
                    task.setdefault('tags', []).append('asana_deleted')
                marked += 1
                print(f"⚠️ Tarea eliminada en Asana: {task.get('name', 'Sin nombre')}")
        if marked:
            self._save()
            print(f"ℹ️ {marked} tareas marcadas como eliminadas en Asana")
        return marked

    def fetch_asana_tasks_for_connections(self, connections: List[Dict[str, Any]], assignee_gid: str, include_completed: bool = False,
                                          include_no_project: bool = False,
                                          linked_gids: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        """Obtiene tareas desde múltiples conexiones (workspace/proyecto).
        Cada conexión es un dict: {'workspace_gid': str, 'project_gid': Optional[str]}
        Devuelve una lista única por gid. Un solo listado por workspace (antes
        uno por proyecto, todos iguales) filtrado en local: proyectos marcados,
        las propias sin proyecto si `include_no_project` y las ya enlazadas.
        """
        if not connections:
            return []
        # workspace → proyectos marcados (None: una conexión sin proyecto, todo)
        by_ws: Dict[str, Optional[Set[str]]] = {}
        for c in connections:
            if not isinstance(c, dict):
                continue  # JSON editado a mano o backup raro: se ignora
            ws = (c.get('workspace_gid') or '').strip()
            pr = (c.get('project_gid') or '').strip() or None
            if not ws:
                continue
            if pr is None:
                by_ws[ws] = None
            elif by_ws.get(ws, set()) is not None:
                by_ws.setdefault(ws, set()).add(pr)
        all_tasks: List[Dict[str, Any]] = []
        for ws, projects in by_ws.items():
            # Un workspace que falla (AsanaFetchError) invalida el fetch
            # entero: sin él, sus tareas enlazadas faltarían en el listado y
            # se verificarían una a una como si se hubieran borrado
            tasks = self._fetch_asana_assigned(assignee_gid, ws, include_completed)
            if projects is not None:
                tasks = self._filter_asana_fetch(tasks, projects, include_no_project,
                                                 linked_gids)
            all_tasks.extend(tasks)
        # De-duplicar por gid/id
        uniq: Dict[str, Dict[str, Any]] = {}
        for t in all_tasks:
            gid = t.get('gid') or t.get('id')
            if gid and gid not in uniq:
                uniq[gid] = t
        return list(uniq.values())

