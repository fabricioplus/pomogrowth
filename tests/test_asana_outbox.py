"""Regresión: los cambios hacia Asana no se pierden ni se revierten (cola persistente).

Bugs reales (≤ v9.2.1, confirmados con los datos de producción):
- `asana_push_pending` nunca se limpiaba tras un PUT con éxito (4 tareas
  atascadas): su estado local quedaba fijo y ocultaba cambios hechos en Asana.
- Completar una tarea de Asana con el sync apagado se revertía en el siguiente
  sync; renombrarla también (y sus tags añadidos a mano se borraban).
- El comentario de pomodoro completado se perdía si no había red.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_asana_outbox.py -q
Datos sintéticos en un directorio temporal y un Asana falso: nunca sale a la red.
"""
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests  # noqa: E402

import core.data_handler as data_handler_mod  # noqa: E402
from core.data_handler import (DataHandler, _classify_asana_status,  # noqa: E402
                               _new_outbox_op)
from ui.theme import fonts  # noqa: E402

TASK_ID = 1764282734999
GID = '1200000000000999'
CONFIGURED = {'asana_pat': 'pat-de-prueba', 'asana_assignee_gid': 'A',
              'asana_workspace_gid': 'W'}


class _Resp:
    def __init__(self, status: int, data=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = json.dumps(self._data)[:200]

    def json(self):
        return {'data': self._data, 'next_page': None}


class FakeAsana:
    """Asana en memoria: registra cada llamada y responde según su estado."""

    def __init__(self):
        self.remote = {GID: {'name': 'Informe mensual', 'completed': False,
                             'tags': [{'name': 'Marketing'}]}}
        self.calls = []        # (método, ruta)
        self.stories = []      # (gid, texto) de cada comentario publicado
        self._story_log = []   # lo mismo con fecha, para GET /tasks/{gid}/stories
        self.offline = False   # todas las peticiones fallan por red
        self.offline_get = False
        self.status = None     # fuerza este código HTTP en PUT/POST
        self.stories_status = None  # fuerza este código en GET …/stories
        self.post_lands_then_timeout = False  # el POST llega; la respuesta no
        self.post_budget = None  # nº de POST que llegan antes de caer la red
        self.created = []        # (gid, data) de cada POST /tasks que llegó
        self.project_names = {'P1': 'Marketing', 'P2': 'Web'}

    @staticmethod
    def _path(url: str) -> str:
        return url.split('/api/1.0', 1)[-1]

    def put(self, url, headers=None, json=None, timeout=None):
        path = self._path(url)
        self.calls.append(('PUT', path))
        if self.offline:
            raise requests.ConnectionError("sin red (simulado)")
        if self.status:
            return _Resp(self.status)
        gid = path.rsplit('/', 1)[-1]
        if gid not in self.remote:
            return _Resp(404)
        self.remote[gid].update(json['data'])
        return _Resp(200, {'gid': gid, **self.remote[gid]})

    def post(self, url, headers=None, json=None, timeout=None, params=None):
        path = self._path(url)
        self.calls.append(('POST', path))
        if self.post_budget is not None:
            if self.post_budget <= 0:
                raise requests.ConnectionError("la red cayó (simulado)")
            self.post_budget -= 1
        if self.offline:
            raise requests.ConnectionError("sin red (simulado)")
        if self.status:
            return _Resp(self.status)
        if path == '/tasks':  # alta de una tarea
            return self._create(json['data'])
        gid = path.split('/tasks/')[1].split('/')[0]
        if gid not in self.remote:
            return _Resp(404)
        text = json['data']['text']
        self.stories.append((gid, text))
        self._story_log.append({
            'gid': gid, 'text': text, 'resource_subtype': 'comment_added',
            'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')})
        if self.post_lands_then_timeout:
            raise requests.ReadTimeout("la respuesta no llegó (simulado)")
        return _Resp(201, {'gid': 'story'})

    def _create(self, data):
        gid = f"13000000000000{len(self.created) + 1:02d}"
        projects = [{'gid': p, 'name': self.project_names.get(p, p)}
                    for p in data.get('projects') or []]
        self.remote[gid] = {
            'name': data['name'], 'completed': bool(data.get('completed')), 'tags': [],
            'projects': projects, 'parent': None, 'due_on': data.get('due_on'),
            'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
        self.created.append((gid, dict(data)))
        if self.post_lands_then_timeout:
            raise requests.ReadTimeout("la respuesta no llegó (simulado)")
        return _Resp(201, {'gid': gid, 'name': data['name'],
                           'completed': bool(data.get('completed')), 'projects': projects})

    def get(self, url, headers=None, params=None, timeout=None):
        path = self._path(url)
        self.calls.append(('GET', path))
        if self.offline or self.offline_get:
            raise requests.ConnectionError("sin red (simulado)")
        if path == '/tasks':  # fetch masivo: con completed_since=now, solo abiertas
            only_open = (params or {}).get('completed_since') == 'now'
            return _Resp(200, [{'gid': g, **t} for g, t in self.remote.items()
                               if not (only_open and t['completed'])])
        if path.endswith('/stories'):
            if self.stories_status:
                return _Resp(self.stories_status)
            gid = path.split('/tasks/')[1].split('/')[0]
            if gid not in self.remote:
                return _Resp(404)
            return _Resp(200, [s for s in self._story_log if s['gid'] == gid])
        gid = path.rsplit('/', 1)[-1]  # verificación individual /tasks/{gid}
        if gid not in self.remote:
            return _Resp(404)
        return _Resp(200, {'gid': gid, 'completed': self.remote[gid]['completed']})

    def remote_task(self):
        return [{'gid': GID, **self.remote[GID]}]

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def asana(monkeypatch):
    fake = FakeAsana()
    for name in ('get', 'put', 'post'):
        monkeypatch.setattr(data_handler_mod.requests, name, getattr(fake, name))
    return fake


def _gate(monkeypatch, asana, method: str, gate, started, path='/tasks'):
    """Retiene una petición en el hilo de red hasta abrir `gate`. Un GET se
    responde con el estado de Asana de ANTES de esperar (llega obsoleto, como
    una respuesta lenta de verdad); un PUT se aplica después de esperar."""
    real = getattr(asana, method)

    def slow(url, *a, **k):
        if method == 'get':
            resp = real(url, *a, **k)
            if asana._path(url) == path:
                started.set()
                gate.wait(5)
            return resp
        started.set()
        gate.wait(5)
        return real(url, *a, **k)
    monkeypatch.setattr(data_handler_mod.requests, method, slow)


def _write_data(tmp_path: Path, tasks=None, settings=None, **extra) -> Path:
    if tasks is None:
        tasks = [{'id': TASK_ID, 'name': 'Informe mensual', 'completed': False,
                  'tags': ['Marketing'], 'source': 'asana', 'asana_id': GID,
                  'due': None}]
    data = {'tasks': tasks, 'deleted_tasks': [], 'time_chunks': [], 'history': [],
            'ui_snapshot': {}, 'settings': dict(CONFIGURED if settings is None else settings),
            **extra}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def dh(tmp_path, monkeypatch, asana):
    monkeypatch.chdir(tmp_path)
    return DataHandler(_write_data(tmp_path))


def _task(handler):
    return next(t for t in handler.get_tasks() if t['id'] == TASK_ID)


def _push(handler):
    """Un envío completo, como el del servicio pero síncrono."""
    return handler.apply_asana_push_results(
        handler.send_asana_ops(handler.take_asana_ops_for_push()))


# ---------------------------------------------------------------- núcleo
def test_envio_con_exito_vacia_la_cola_sin_dejar_marca_atascada(dh, asana):
    dh.update_task_status(TASK_ID, True)
    assert dh.pending_asana_ops_count() == 1
    summary = _push(dh)
    assert summary['sent'] == 1 and not dh.has_pending_asana_ops()
    assert asana.remote[GID]['completed'] is True
    # Antes la marca quedaba para siempre: si alguien la reabre en Asana, el
    # sync debe reflejarlo
    asana.remote[GID]['completed'] = False
    dh.merge_asana_tasks(asana.remote_task())
    assert _task(dh)['completed'] is False
    assert 'asana_push_pending' not in _task(dh)


def test_la_cola_se_guarda_antes_de_enviar(dh, tmp_path):
    """Si la app muere antes de que vuelva el envío, el cambio sigue en disco."""
    dh.update_task_status(TASK_ID, True)
    dh.queue_asana_comment(GID, "🍅")
    assert DataHandler(tmp_path / "data.json").pending_asana_ops_count() == 2


def test_la_cola_sobrevive_a_un_reinicio(dh, asana, tmp_path):
    asana.offline = True
    dh.update_task_status(TASK_ID, True)
    _push(dh)
    reloaded = DataHandler(tmp_path / "data.json")
    assert reloaded.pending_asana_ops_count() == 1
    asana.offline = False
    _push(reloaded)
    assert asana.remote[GID]['completed'] is True
    assert not reloaded.has_pending_asana_ops()


def test_cambio_durante_el_envio_no_se_pierde(dh, asana):
    dh.update_task_status(TASK_ID, True)
    ops = dh.take_asana_ops_for_push()      # en vuelo: completed=True
    dh.update_task_status(TASK_ID, False)   # el usuario la reabre mientras tanto
    dh.apply_asana_push_results(dh.send_asana_ops(ops))
    assert dh.pending_asana_ops_count() == 1  # sigue en cola con el valor nuevo
    _push(dh)
    assert asana.remote[GID]['completed'] is False
    assert not dh.has_pending_asana_ops()


def test_envios_solapados_no_duplican(dh):
    dh.queue_asana_comment(GID, "🍅 hecho")
    first = dh.take_asana_ops_for_push()
    assert len(first) == 1
    assert dh.take_asana_ops_for_push() == []  # ya en vuelo
    dh.release_asana_ops(first)
    assert len(dh.take_asana_ops_for_push()) == 1


def test_sin_red_se_reintenta_en_orden(dh, asana):
    dh.queue_asana_comment(GID, "🍅 Sesión Pomodoro completada.")
    dh.update_task_status(TASK_ID, True)
    asana.offline = True
    summary = _push(dh)
    assert summary['failed'] == 1 and summary['offline'] is True
    assert len(asana.calls) == 1  # se detiene al primer fallo de red
    outbox = dh.data['asana_outbox']
    assert [op['type'] for op in outbox] == ['comment', 'update']
    assert outbox[0]['attempts'] == 1 and outbox[1]['attempts'] == 0
    asana.offline = False
    assert _push(dh)['sent'] == 2
    assert asana.stories == [(GID, "🍅 Sesión Pomodoro completada.")]
    assert asana.remote[GID]['completed'] is True


def test_comentario_incierto_no_se_duplica(dh, asana):
    """El POST llega pero la respuesta no (timeout de lectura): antes de
    reintentar se comprueba en Asana y no se publica dos veces."""
    dh.queue_asana_comment(GID, "🍅 Sesión Pomodoro completada.")
    asana.post_lands_then_timeout = True
    assert _push(dh)['failed'] == 1
    assert len(asana.stories) == 1
    asana.post_lands_then_timeout = False
    assert _push(dh)['sent'] == 1
    assert len(asana.stories) == 1
    assert ('GET', f'/tasks/{GID}/stories') in asana.calls
    assert not dh.has_pending_asana_ops()


def test_cierre_a_mitad_de_envio_no_duplica_el_comentario(dh, asana, tmp_path):
    dh.queue_asana_comment(GID, "🍅")
    dh.send_asana_ops(dh.take_asana_ops_for_push())  # llega… y la app se cierra
    reopened = DataHandler(tmp_path / "data.json")   # sin aplicar el resultado
    assert reopened.pending_asana_ops_count() == 1
    _push(reopened)
    assert len(asana.stories) == 1
    assert not reopened.has_pending_asana_ops()


C1 = "🍅 Sesión Pomodoro completada el 23/10 a las 10:00. Tiempo enfocado: 25 minutos."
C2 = "🍅 Sesión Pomodoro completada el 23/10 a las 10:35. Tiempo enfocado: 25 minutos."


def test_dos_comentarios_sin_red_llegan_los_dos(dh, asana):
    """Dos pomodoros seguidos sin red: al volver, se publican ambos (la
    comprobación anti-duplicados no confunde el 2.º con el 1.º)."""
    asana.offline = True
    dh.queue_asana_comment(GID, C1)
    _push(dh)                       # 1.er pomodoro: falla sin red
    dh.queue_asana_comment(GID, C2)
    _push(dh)                       # 2.º: falla el 1.º; el 2.º ni se intenta
    tries = [op['send_tries'] for op in dh.data['asana_outbox']]
    assert tries == [2, 0]          # un 'skipped' no cuenta como intento
    asana.offline = False
    assert _push(dh)['sent'] == 2
    assert [text for _, text in asana.stories] == [C1, C2]


def test_red_que_cae_a_mitad_de_lote_no_pierde_comentarios(dh, asana):
    dh.queue_asana_comment(GID, C1)
    dh.queue_asana_comment(GID, C2)
    asana.post_budget = 1           # el 1.º llega; en el 2.º cae la red
    _push(dh)
    asana.post_budget = None
    _push(dh)
    assert [text for _, text in asana.stories] == [C1, C2]
    assert not dh.has_pending_asana_ops()


def test_consulta_de_comentarios_rechazada_no_bloquea_la_cola(dh, asana):
    """Un 403 al consultar las historias no se toma por «sin red»: se publica
    y decide el POST, y la cola sigue."""
    dh.queue_asana_comment(GID, C1)
    dh.data['asana_outbox'][0]['send_tries'] = 1   # ya se intentó una vez
    dh.update_task_status(TASK_ID, True)
    asana.stories_status = 403
    summary = _push(dh)
    assert summary['sent'] == 2 and summary['failed'] == 0
    assert asana.stories == [(GID, C1)]
    assert asana.remote[GID]['completed'] is True


def test_consulta_de_comentarios_con_token_rechazado_avisa_del_token(dh, asana):
    dh.queue_asana_comment(GID, C1)
    dh.data['asana_outbox'][0]['send_tries'] = 1
    asana.stories_status = 401
    summary = _push(dh)
    assert summary['auth'] is True and summary['offline'] is False
    assert dh.pending_asana_ops_count() == 1


@pytest.mark.parametrize("code, outcome", [
    (200, 'ok'), (201, 'ok'), (401, 'auth'), (408, 'retry'), (429, 'retry'),
    (451, 'retry'), (500, 'retry'), (503, 'retry'), (400, 'drop'), (403, 'drop'),
    (404, 'drop')])
def test_clasificacion_de_respuestas(code, outcome):
    assert _classify_asana_status(code) == outcome


def test_rechazo_definitivo_se_descarta_y_no_bloquea_la_cola(dh, asana):
    dh.queue_asana_comment('gid-borrada-en-asana', "🍅")  # → 404
    dh.update_task_status(TASK_ID, True)
    summary = _push(dh)
    assert summary == {'sent': 1, 'dropped': 1, 'failed': 0, 'offline': False,
                       'auth': False, 'last_error': 'HTTP 404'}
    assert not dh.has_pending_asana_ops()
    assert asana.remote[GID]['completed'] is True


def test_token_rechazado_conserva_la_cola(dh, asana):
    dh.update_task_status(TASK_ID, True)
    asana.status = 401
    summary = _push(dh)
    assert summary['failed'] == 1 and summary['auth'] is True
    assert dh.pending_asana_ops_count() == 1


def test_migracion_de_la_marca_heredada(tmp_path, monkeypatch):
    """Coincide con el estado local → ya se envió: se descarta sin reenviar.
    No coincide → el último cambio se hizo con el sync apagado: se encola."""
    monkeypatch.chdir(tmp_path)
    tasks = [
        {'id': 1, 'name': 'Atascada', 'completed': True, 'source': 'asana',
         'asana_id': 'g1', 'tags': [], 'asana_push_pending': True},
        {'id': 2, 'name': 'Reabierta con el sync apagado', 'completed': False,
         'source': 'asana', 'asana_id': 'g2', 'tags': [], 'asana_push_pending': True},
        {'id': 3, 'name': 'Local', 'completed': True, 'source': 'local',
         'asana_id': None, 'tags': [], 'asana_push_pending': True},
    ]
    path = _write_data(tmp_path, tasks=tasks)
    handler = DataHandler(path)
    assert all('asana_push_pending' not in t for t in handler.get_tasks())
    ops = handler.data['asana_outbox']
    assert [(op['gid'], op['fields']) for op in ops] == [('g2', {'completed': False})]
    # Migrado y guardado en disco al arrancar
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk['asana_outbox']) == 1
    assert all('asana_push_pending' not in t for t in on_disk['tasks'])


def test_migracion_no_pisa_una_reapertura_hecha_en_asana(qtbot, tmp_path, monkeypatch, asana):
    """Las tareas atascadas de producción: si alguien la reabrió en Asana
    después, al actualizar gana Asana (sin ningún PUT)."""
    from services.asana_service import AsanaService
    monkeypatch.chdir(tmp_path)
    tasks = [{'id': TASK_ID, 'name': 'Informe mensual', 'completed': True,
              'completed_at': '2026-09-01T10:00:00', 'source': 'asana',
              'asana_id': GID, 'tags': ['Marketing'], 'asana_push_pending': True}]
    handler = DataHandler(_write_data(tmp_path, tasks=tasks))
    asana.remote[GID]['completed'] = False  # reabierta en Asana
    svc = AsanaService(handler)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    assert 'PUT' not in asana.methods()
    assert asana.remote[GID]['completed'] is False
    assert _task(handler)['completed'] is False


def test_importar_un_backup_no_duplica_ni_reaplica_su_cola(dh, asana):
    dh.update_task_status(TASK_ID, True)
    _push(dh)                                          # completada y enviada
    backup = json.loads(json.dumps(dh.data))
    # El backup se exportó con un comentario (que luego sí llegó a Asana) y un
    # update viejo aún en cola
    asana.post(f"https://app.asana.com/api/1.0/tasks/{GID}/stories",
               json={'data': {'text': C1}})
    backup['asana_outbox'] = [_new_outbox_op('comment', GID, text=C1),
                              _new_outbox_op('update', GID, fields={'completed': False})]
    backup['tasks'][0]['asana_push_pending'] = False   # marca heredada incoherente
    dh.import_data(backup)
    assert [op['type'] for op in dh.data['asana_outbox']] == ['comment']
    assert 'asana_push_pending' not in _task(dh)
    _push(dh)
    assert len(asana.stories) == 1                     # no se duplicó
    assert asana.remote[GID]['completed'] is True      # el update viejo no pisó Asana
    assert not dh.has_pending_asana_ops()


def test_borrar_todo_conserva_la_cola(dh):
    dh.update_task_status(TASK_ID, True)
    dh.clear_data()
    assert dh.get_tasks() == [] and dh.pending_asana_ops_count() == 1


def test_verificacion_no_recompleta_una_tarea_reabierta(dh):
    dh.update_task_status(TASK_ID, True)
    dh.data['asana_outbox'].clear()          # ya enviada
    dh.update_task_status(TASK_ID, False)    # reabierta: el envío aún no llegó
    assert dh.find_asana_candidates_to_verify(set()) == []
    assert dh.apply_asana_completed_marks({GID}) == 0
    assert _task(dh)['completed'] is False


def test_tarea_borrada_en_asana_no_se_encola(dh):
    _task(dh)['asana_deleted'] = True
    dh.update_task_status(TASK_ID, True)
    dh.update_task_name_and_tags(TASK_ID, "Otro nombre", [])
    assert not dh.has_pending_asana_ops()


def test_renombrar_viaja_a_asana_y_los_tags_propios_sobreviven(dh, asana):
    dh.merge_asana_tasks(asana.remote_task())
    assert _task(dh)['tags'] == ['Marketing']
    # El diálogo de edición pasa los tags en minúsculas
    dh.update_task_name_and_tags(TASK_ID, "Informe de octubre", ['marketing', 'urgente'])
    assert dh.data['asana_outbox'][0]['fields'] == {'name': "Informe de octubre"}
    # Un sync antes del envío no revierte el nombre ni borra el tag propio
    dh.merge_asana_tasks(asana.remote_task())
    assert _task(dh)['name'] == "Informe de octubre"
    assert _task(dh)['tags'] == ['Marketing', 'urgente']
    _push(dh)
    assert asana.remote[GID]['name'] == "Informe de octubre"
    dh.merge_asana_tasks(asana.remote_task())
    assert _task(dh)['tags'] == ['Marketing', 'urgente']
    # Quitar el tag propio también se respeta
    dh.update_task_name_and_tags(TASK_ID, "Informe de octubre", ['marketing'])
    dh.merge_asana_tasks(asana.remote_task())
    assert _task(dh)['tags'] == ['Marketing']
    assert not dh.has_pending_asana_ops()  # el nombre no cambió: nada que enviar


def test_espacios_en_el_nombre_no_son_un_renombrado(dh, asana):
    asana.remote[GID]['name'] = 'Informe mensual '  # el diálogo devuelve .strip()
    dh.merge_asana_tasks(asana.remote_task())
    dh.update_task_name_and_tags(TASK_ID, 'Informe mensual', ['marketing', 'urgente'])
    assert not dh.has_pending_asana_ops()


def test_siembra_no_convierte_un_proyecto_renombrado_en_tag_propio(tmp_path, monkeypatch, asana):
    monkeypatch.chdir(tmp_path)
    tasks = [{'id': TASK_ID, 'name': 'Informe mensual', 'completed': False,
              'tags': ['Marketing'], 'source': 'asana', 'asana_id': GID,
              'projects': [{'gid': 'P1', 'name': 'Marketing'}]}]
    handler = DataHandler(_write_data(tmp_path, tasks=tasks))
    asana.remote[GID]['tags'] = []
    asana.remote[GID]['projects'] = [{'gid': 'P1', 'name': 'Marketing Q4'}]
    handler.merge_asana_tasks(asana.remote_task())
    assert _task(handler)['tags'] == ['Marketing Q4']


def test_editar_una_tarea_vieja_antes_del_primer_sync_conserva_sus_tags(tmp_path, monkeypatch, asana):
    monkeypatch.chdir(tmp_path)
    tasks = [{'id': TASK_ID, 'name': 'Informe mensual', 'completed': False,
              'tags': ['Marketing', 'urgente'], 'source': 'asana', 'asana_id': GID}]
    handler = DataHandler(_write_data(tmp_path, tasks=tasks))
    handler.update_task_name_and_tags(TASK_ID, 'Informe mensual',
                                      ['marketing', 'urgente', 'nuevo'])
    handler.merge_asana_tasks(asana.remote_task())
    assert _task(handler)['tags'] == ['Marketing', 'nuevo', 'urgente']


def test_tags_propios_de_datos_viejos_sobreviven(tmp_path, monkeypatch, asana):
    """Un tag añadido en v9.2.1 con el sync apagado no se borra al actualizar."""
    monkeypatch.chdir(tmp_path)
    tasks = [{'id': TASK_ID, 'name': 'Informe mensual', 'completed': False,
              'tags': ['Marketing', 'urgente'], 'source': 'asana', 'asana_id': GID}]
    handler = DataHandler(_write_data(tmp_path, tasks=tasks))
    handler.merge_asana_tasks(asana.remote_task())
    assert _task(handler)['tags'] == ['Marketing', 'urgente']


# ---------------------------------------------------------------- servicio
def test_sync_envia_la_cola_antes_de_descargar(qtbot, dh, asana):
    from services.asana_service import AsanaService
    svc = AsanaService(dh)
    dh.update_task_status(TASK_ID, True)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    assert asana.calls[0] == ('PUT', f'/tasks/{GID}')
    assert asana.methods().index('PUT') < asana.methods().index('GET')
    assert not dh.has_pending_asana_ops()
    assert _task(dh)['completed'] is True


def test_fallo_del_fetch_no_pierde_lo_ya_enviado(qtbot, dh, asana):
    from services.asana_service import AsanaService
    svc = AsanaService(dh)
    errors = []
    svc.error.connect(errors.append)
    dh.update_task_status(TASK_ID, True)
    asana.offline_get = True                 # el PUT llega, el fetch no
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    assert errors, "el sync debe terminar por el camino de error"
    assert asana.remote[GID]['completed'] is True
    assert not dh.has_pending_asana_ops()    # no se reenviará


# ---------------------------------------------------------------- app completa
@pytest.fixture
def make_ctrl(qtbot, tmp_path, monkeypatch, asana):
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    built = []

    def build(settings=None, tasks=None):
        handler = DataHandler(_write_data(tmp_path, tasks=tasks, settings=settings))
        from ui.windows.main_window import MainWindow
        from ui.controllers.app_controller import AppController
        win = MainWindow(handler.data.get('settings', {}))
        qtbot.addWidget(win)
        controller = AppController(win, handler)
        win.show()
        qtbot.waitExposed(win)
        built.append(controller)
        return controller

    yield build
    # Que ningún envío ni temporizador diferido llegue al test siguiente
    for controller in built:
        qtbot.waitUntil(lambda: not controller.asana.busy, timeout=5000)
    qtbot.wait(100)


@pytest.fixture
def ctrl(make_ctrl):
    return make_ctrl()


def test_completar_con_sync_apagado_no_se_revierte(ctrl, qtbot, asana):
    ctrl._on_sync_toggled(False)
    ctrl.toggle_task_completion(TASK_ID)
    qtbot.wait(100)
    assert asana.calls == []  # con el sync apagado no sale nada a la red
    assert ctrl.data_handler.pending_asana_ops_count() == 1
    # Un merge con Asana aún sin el cambio ya no lo revierte
    ctrl.data_handler.merge_asana_tasks(asana.remote_task())
    assert ctrl._task_by_id(TASK_ID)['completed'] is True
    # Al encender el sync, el cambio llega a Asana
    ctrl._on_sync_toggled(True)
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    assert asana.remote[GID]['completed'] is True
    assert not ctrl.data_handler.has_pending_asana_ops()
    assert ctrl._task_by_id(TASK_ID)['completed'] is True


def test_completar_con_sync_encendido_envia_al_momento(ctrl, qtbot, asana):
    ctrl.toggle_task_completion(TASK_ID)
    qtbot.waitUntil(lambda: asana.remote[GID]['completed'] is True, timeout=5000)
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)


def test_integracion_desmarcada_no_envia(make_ctrl, qtbot, asana):
    """`active_integrations == []` es que se desmarcó (antes contaba como activa)."""
    ctrl = make_ctrl({**CONFIGURED, 'active_integrations': []})
    ctrl.toggle_task_completion(TASK_ID)
    qtbot.wait(200)
    assert asana.calls == []
    assert ctrl.data_handler.pending_asana_ops_count() == 1  # espera en la cola


def test_comentario_de_pomodoro_sin_red_llega_al_reconectar(ctrl, qtbot, asana):
    asana.offline = True
    ctrl.select_task(TASK_ID)
    ctrl.model.start()
    ctrl.complete_pomodoro_session()
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    outbox = ctrl.data_handler.data['asana_outbox']
    assert [op['type'] for op in outbox] == ['comment'] and outbox[0]['attempts'] == 1
    assert ctrl.asana._retry_timer.isActive()  # reintento programado
    asana.offline = False
    ctrl.asana.push_pending()                  # lo que hará el reintento
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    assert len(asana.stories) == 1 and asana.stories[0][0] == GID
    assert re.fullmatch(r"🍅 Sesión Pomodoro completada el \d\d/\d\d a las \d\d:\d\d\. "
                        r"Tiempo enfocado: \d+ minutos\.", asana.stories[0][1])
    qtbot.wait(600)  # aviso diferido (400 ms) de complete_pomodoro_session


def test_cambio_rechazado_avisa_con_un_toast(ctrl, qtbot, asana, monkeypatch):
    from ui.widgets import toast
    warnings = []
    monkeypatch.setattr(toast, "show_warning",
                        lambda parent, msg, duration=3000: warnings.append(msg))
    asana.status = 403
    ctrl.toggle_task_completion(TASK_ID)
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    assert any("rechazó" in w for w in warnings)


def test_editar_sin_tocar_el_nombre_no_revierte_un_renombrado_remoto(ctrl, monkeypatch):
    from ui.dialogs import branded_dialogs

    def dialog(parent, name, tags, recurring=None, hint=None):
        # Un sync en lotes corre con el diálogo abierto y trae otro nombre
        ctrl._task_by_id(TASK_ID)['name'] = 'Renombrada en Asana'
        return name, ['marketing', 'urgente'], None
    monkeypatch.setattr(branded_dialogs, "edit_task", dialog)
    ctrl.edit_task(TASK_ID)
    assert ctrl._task_by_id(TASK_ID)['name'] == 'Renombrada en Asana'
    assert not ctrl.data_handler.has_pending_asana_ops()


def test_migracion_avisa_si_el_primer_sync_cambia_el_estado(make_ctrl, qtbot, asana):
    """Marca heredada coherente pero Asana distinta (reabierta allí, o el envío
    de v9.2.1 falló): manda Asana y se avisa con una notificación."""
    tasks = [{'id': TASK_ID, 'name': 'Informe mensual', 'completed': True,
              'source': 'asana', 'asana_id': GID, 'tags': ['Marketing'],
              'asana_push_pending': True}]
    ctrl = make_ctrl(tasks=tasks)
    notes = []
    ctrl.tray.showMessage = lambda title, msg, *a: notes.append(msg)
    ctrl.sync_asana()
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    qtbot.wait(100)
    assert 'PUT' not in asana.methods()
    assert ctrl._task_by_id(TASK_ID)['completed'] is False
    assert len(notes) == 1 and 'Informe mensual' in notes[0]


# ---- un solo trabajo de red a la vez
def test_cambio_durante_un_sync_se_envia_al_terminar(ctrl, qtbot, asana, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    _gate(monkeypatch, asana, 'get', gate, started)
    ctrl.sync_asana()
    qtbot.waitUntil(started.is_set, timeout=3000)
    ctrl.toggle_task_completion(TASK_ID)       # durante el fetch
    gate.set()
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    assert asana.remote[GID]['completed'] is True


def test_envio_no_se_solapa_con_el_merge_de_un_sync(ctrl, qtbot, asana, monkeypatch):
    """Si el PUT saliera durante el sync, el merge con la respuesta previa
    revertiría el cambio local (la op ya habría salido de la cola)."""
    gate, started = threading.Event(), threading.Event()
    _gate(monkeypatch, asana, 'get', gate, started)
    ctrl.sync_asana()
    qtbot.waitUntil(started.is_set, timeout=3000)
    ctrl.toggle_task_completion(TASK_ID)
    qtbot.wait(300)
    assert ('PUT', f'/tasks/{GID}') not in asana.calls, "envío solapado con el sync"
    gate.set()
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    qtbot.wait(200)
    assert ctrl._task_by_id(TASK_ID)['completed'] is True
    assert asana.remote[GID]['completed'] is True


def test_cambio_durante_un_envio_se_envia_despues(ctrl, qtbot, asana, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    _gate(monkeypatch, asana, 'put', gate, started)
    ctrl.toggle_task_completion(TASK_ID)
    qtbot.waitUntil(started.is_set, timeout=3000)
    ctrl.data_handler.queue_asana_comment(GID, "🍅")
    ctrl.asana.push_pending()                  # ocupado: queda para después
    gate.set()
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    assert asana.stories == [(GID, "🍅")]


def test_sync_pedido_durante_un_envio_arranca_al_terminar(ctrl, qtbot, asana, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    _gate(monkeypatch, asana, 'put', gate, started)
    ctrl.toggle_task_completion(TASK_ID)
    qtbot.waitUntil(started.is_set, timeout=3000)
    ctrl.sync_asana()
    assert ctrl.asana.sync_pending
    gate.set()
    qtbot.waitUntil(lambda: ('GET', '/tasks') in asana.calls, timeout=5000)


def test_apagar_y_encender_durante_un_sync_no_deja_nada_atascado(ctrl, qtbot, asana, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    _gate(monkeypatch, asana, 'get', gate, started)
    ctrl.sync_asana()
    qtbot.waitUntil(started.is_set, timeout=3000)
    ctrl.toggle_task_completion(TASK_ID)       # espera turno
    ctrl._on_sync_toggled(False)
    ctrl._on_sync_toggled(True)                # nuevo sync: arranca tras el cancelado
    gate.set()
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    qtbot.waitUntil(lambda: not ctrl.asana.busy and not ctrl.asana.sync_pending,
                    timeout=5000)
    assert asana.remote[GID]['completed'] is True
    assert asana.calls.count(('GET', '/tasks')) == 2


def test_apagar_el_switch_anula_un_sync_que_esperaba(ctrl, qtbot, asana, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    _gate(monkeypatch, asana, 'put', gate, started)
    ctrl.toggle_task_completion(TASK_ID)       # envío en vuelo
    qtbot.waitUntil(started.is_set, timeout=3000)
    ctrl.sync_asana()                          # queda esperando turno
    ctrl._on_sync_toggled(False)               # el usuario apaga el switch
    gate.set()
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    qtbot.wait(200)
    assert 'GET' not in asana.methods()


# ------------------------------------------- alta también en Asana ('create')
MIS_TAREAS = {'workspace_gid': 'W', 'project_gid': '', 'project_name': ''}
MARKETING = {'workspace_gid': 'W', 'project_gid': 'P1', 'project_name': 'Marketing'}


def _by_id(handler, task_id):
    return next(t for t in handler.get_tasks() if t['id'] == task_id)


def test_alta_sin_red_sobrevive_a_un_reinicio_y_se_envia_despues(dh, asana, tmp_path):
    asana.offline = True
    tid = dh.add_task("Preparar la demo", ['casa'], asana_create=MIS_TAREAS)
    assert dh.data['asana_outbox'][0]['gid'] == f"local-{tid}"
    assert _push(dh)['failed'] == 1
    reloaded = DataHandler(tmp_path / "data.json")   # la app se cerró sin red
    assert reloaded.has_pending_asana_create(tid)
    asana.offline = False
    assert _push(reloaded)['sent'] == 1
    assert [data for _, data in asana.created] == [
        {'name': "Preparar la demo", 'workspace': 'W', 'assignee': 'A'}]
    task = _by_id(reloaded, tid)
    gid = asana.created[0][0]
    assert (task['source'], task['asana_id'], task['projects']) == ('asana', gid, [])
    # Las etiquetas se quedan solo en PomoGrowth (y sobreviven a los syncs)
    assert task['local_tags'] == ['casa'] and task['tags'] == ['casa']
    assert not reloaded.has_pending_asana_ops()
    reloaded.merge_asana_tasks([{'gid': gid, **asana.remote[gid]}])
    assert _by_id(reloaded, tid)['tags'] == ['casa']
    assert sum(t.get('asana_id') == gid for t in reloaded.get_tasks()) == 1


def test_alta_con_timeout_no_se_duplica(dh, asana):
    """El POST llega pero la respuesta no: antes de reintentar se busca en
    Asana (por nombre) y se enlaza la ya creada en vez de crear otra."""
    tid = dh.add_task("Llamar al banco", [], asana_create=MIS_TAREAS)
    asana.post_lands_then_timeout = True
    assert _push(dh)['failed'] == 1
    asana.post_lands_then_timeout = False
    assert _push(dh)['sent'] == 1
    assert len(asana.created) == 1
    assert ('GET', '/tasks') in asana.calls
    assert _by_id(dh, tid)['asana_id'] == asana.created[0][0]
    assert not dh.has_pending_asana_ops()


def test_alta_renombrada_tras_un_timeout_tampoco_se_duplica(dh, asana):
    tid = dh.add_task("Borrador", [], asana_create=MIS_TAREAS)
    asana.post_lands_then_timeout = True
    _push(dh)
    asana.post_lands_then_timeout = False
    dh.update_task_name_and_tags(tid, "Versión final", [])   # antes del reintento
    _push(dh)
    assert len(asana.created) == 1
    gid = asana.created[0][0]
    assert _by_id(dh, tid)['asana_id'] == gid
    _push(dh)                                    # el renombrado, como update
    assert asana.remote[gid]['name'] == "Versión final"
    assert not dh.has_pending_asana_ops()


def test_alta_con_sync_inmediato_sin_duplicado(qtbot, dh, asana):
    from services.asana_service import AsanaService
    tid = dh.add_task("Revisar contrato", ['legal'], asana_create=MIS_TAREAS)
    svc = AsanaService(dh)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    assert len(asana.created) == 1
    gid = asana.created[0][0]
    assert [t['id'] for t in dh.get_tasks() if t.get('asana_id') == gid] == [tid]
    assert _by_id(dh, tid)['tags'] == ['legal']
    assert not dh.has_pending_asana_ops()


def test_completar_y_renombrar_antes_del_alta_viajan_con_ella(dh, asana):
    asana.offline = True
    tid = dh.add_task("Idea", [], due="2026-11-20", asana_create=MIS_TAREAS)
    dh.update_task_status(tid, True)
    dh.update_task_name_and_tags(tid, "Idea pulida", ['x'])
    assert [op['type'] for op in dh.data['asana_outbox']] == ['create']
    asana.offline = False
    _push(dh)
    assert [data for _, data in asana.created] == [
        {'name': "Idea pulida", 'workspace': 'W', 'assignee': 'A', 'due_on': "2026-11-20",
         'completed': True}]
    assert not dh.has_pending_asana_ops()


def test_cambio_con_el_alta_en_vuelo_se_envia_despues(dh, asana):
    tid = dh.add_task("Idea", [], asana_create=MIS_TAREAS)
    ops = dh.take_asana_ops_for_push()           # el alta, en vuelo
    dh.update_task_status(tid, True)             # se completa mientras tanto
    dh.apply_asana_push_results(dh.send_asana_ops(ops))
    gid = asana.created[0][0]
    assert _by_id(dh, tid)['asana_id'] == gid
    assert [(op['type'], op['gid'], op['fields']) for op in dh.data['asana_outbox']] == [
        ('update', gid, {'completed': True})]
    _push(dh)
    assert asana.remote[gid]['completed'] is True
    assert len(asana.created) == 1


def test_borrar_antes_del_alta_la_retira(dh, asana):
    asana.offline = True
    tid = dh.add_task("Para borrar", [], asana_create=MIS_TAREAS)
    dh.delete_task(tid)
    assert not dh.has_pending_asana_ops()
    asana.offline = False
    _push(dh)
    assert asana.created == []


def test_borrar_con_el_alta_en_vuelo_no_la_resucita(dh, asana):
    tid = dh.add_task("Para borrar", [], asana_create=MIS_TAREAS)
    ops = dh.take_asana_ops_for_push()
    dh.delete_task(tid)                          # mientras el POST viaja
    dh.apply_asana_push_results(dh.send_asana_ops(ops))
    gid = asana.created[0][0]
    assert not dh.has_pending_asana_ops()
    assert any(t.get('id') == tid and t.get('asana_id') == gid
               for t in dh.data['deleted_tasks'])   # la lápida guarda el gid
    dh.merge_asana_tasks([{'gid': gid, **asana.remote[gid]}])
    assert all(t.get('asana_id') != gid for t in dh.get_tasks())


def test_borrar_con_el_alta_incierta_no_la_crea_al_reintentar(dh, asana):
    tid = dh.add_task("Para borrar", [], asana_create=MIS_TAREAS)
    asana.offline = True
    ops = dh.take_asana_ops_for_push()
    dh.delete_task(tid)
    dh.apply_asana_push_results(dh.send_asana_ops(ops))   # sin red: queda marcada
    asana.offline = False
    _push(dh)
    assert asana.created == [] and not dh.has_pending_asana_ops()


def test_destino_proyecto_frente_a_mis_tareas(dh, asana):
    in_project = dh.add_task("Campaña", ['urgente'], asana_create=MARKETING)
    loose = dh.add_task("Suelta", [], asana_create=MIS_TAREAS)
    _push(dh)
    sent = {data['name']: data for _, data in asana.created}
    assert sent["Campaña"]['projects'] == ['P1']
    assert 'projects' not in sent["Suelta"]
    task = _by_id(dh, in_project)
    assert task['projects'] == [{'gid': 'P1', 'name': 'Marketing'}]
    assert task['tags'] == ['Marketing', 'urgente'] and task['local_tags'] == ['urgente']
    assert _by_id(dh, loose)['projects'] == []


def test_alta_rechazada_queda_local(dh, asana):
    tid = dh.add_task("Sin permiso", [], asana_create=MARKETING)
    asana.status = 403
    assert _push(dh)['dropped'] == 1
    task = _by_id(dh, tid)
    assert task['source'] == 'local' and not task['asana_id']
    assert not dh.has_pending_asana_ops()


def test_un_alta_sin_tarea_no_se_carga(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    op = _new_outbox_op('create', 'local-1', fields={'name': 'x'}, workspace='W')
    handler = DataHandler(_write_data(tmp_path, asana_outbox=[op]))
    assert not handler.has_pending_asana_ops()


# ------------------------------------ sync de las tareas propias sin proyecto
def _remote_mix(asana):
    """De un proyecto marcado, de otro sin marcar, una suelta y una subtarea
    (sin proyecto, pero colgando de otra tarea)."""
    asana.remote[GID]['projects'] = [{'gid': 'P1', 'name': 'Marketing'}]
    asana.remote['SUELTA'] = {'name': 'Pedir cita', 'completed': False, 'tags': [],
                              'projects': [], 'parent': None}
    asana.remote['SUB'] = {'name': 'Paso 2', 'completed': False, 'tags': [],
                           'projects': [], 'parent': {'gid': GID}}
    asana.remote['AJENA'] = {'name': 'De otro equipo', 'completed': False, 'tags': [],
                             'projects': [{'gid': 'P9', 'name': 'Ventas'}]}


CONNS = [{'workspace_gid': 'W', 'project_gid': 'P1', 'project_name': 'Marketing'},
         {'workspace_gid': 'W', 'project_gid': 'P2', 'project_name': 'Web'}]


def _gids(tasks):
    return {t['gid'] for t in tasks}


def test_sin_proyecto_llega_la_suelta_y_no_la_subtarea(dh, asana):
    _remote_mix(asana)
    got = dh.fetch_asana_tasks_for_connections(CONNS, 'A', include_no_project=True)
    assert _gids(got) == {GID, 'SUELTA'}
    assert asana.calls.count(('GET', '/tasks')) == 1   # un listado por workspace


def test_sin_proyecto_apagado_no_llega(dh, asana):
    _remote_mix(asana)
    assert _gids(dh.fetch_asana_tasks_for_connections(CONNS, 'A')) == {GID}


def test_las_ya_enlazadas_llegan_aunque_no_pasen_el_filtro(dh, asana):
    _remote_mix(asana)
    got = dh.fetch_asana_tasks_for_connections(CONNS, 'A', linked_gids={'AJENA'})
    assert _gids(got) == {GID, 'AJENA'}


def test_un_listado_por_workspace(dh, asana):
    conns = CONNS + [{'workspace_gid': 'W2', 'project_gid': 'P7', 'project_name': 'Otro'}]
    dh.fetch_asana_tasks_for_connections(conns, 'A')
    assert asana.calls.count(('GET', '/tasks')) == 2


def test_sync_trae_las_sin_proyecto_y_mantiene_las_creadas_en_mis_tareas(
        qtbot, tmp_path, monkeypatch, asana):
    from services.asana_service import AsanaService
    monkeypatch.chdir(tmp_path)
    settings = {**CONFIGURED, 'asana_connections': CONNS, 'asana_project_gid': 'P1',
                'asana_include_no_project': False}
    handler = DataHandler(_write_data(tmp_path, settings=settings))
    _remote_mix(asana)
    tid = handler.add_task("Creada aquí", [], asana_create=MIS_TAREAS)
    svc = AsanaService(handler)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    names = {t['name'] for t in handler.get_tasks()}
    assert 'Pedir cita' not in names and 'Paso 2' not in names
    gid = _by_id(handler, tid)['asana_id']
    # Renombrada en Asana: llega aunque esté fuera de los proyectos marcados
    asana.remote[gid]['name'] = "Renombrada en Asana"
    handler.data['settings']['asana_include_no_project'] = True
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    qtbot.wait(300)  # merge en lotes
    assert _by_id(handler, tid)['name'] == "Renombrada en Asana"
    names = [t['name'] for t in handler.get_tasks()]
    assert 'Pedir cita' in names and 'Paso 2' not in names and 'De otro equipo' not in names
    assert names.count("Renombrada en Asana") == 1


# ------------------------------------------ alta desde la app (Asana, ClickUp)
CLICKUP = {'active_integrations': ['asana', 'clickup'], 'clickup_api_token': 'pk_prueba',
           'clickup_list_id': 'L1'}


def test_alta_desde_el_panel_se_crea_en_asana_al_momento(ctrl, qtbot, asana):
    panel = ctrl.tasks_panel.add_panel
    assert not panel.asana_btn.is_dimmed()
    menu = panel.build_asana_menu()
    next(a for a in menu.actions() if a.data() == 'W/').trigger()
    assert ctrl.data_handler.load_settings()['task_add_asana'] is True
    panel.focus_input()
    panel.name_edit.setText("Desde el panel")
    panel.submit()
    qtbot.waitUntil(lambda: len(asana.created) == 1 and not ctrl.data_handler.has_pending_asana_ops(),
                    timeout=5000)
    task = next(t for t in ctrl.data_handler.get_tasks() if t['name'] == "Desde el panel")
    assert task['asana_id'] == asana.created[0][0] and task['source'] == 'asana'
    assert panel.asana_btn.isChecked()  # se queda encendido para la siguiente


def test_alta_en_asana_con_sync_apagado_espera_y_avisa(ctrl, qtbot, asana, monkeypatch):
    from ui.widgets import toast
    infos = []
    monkeypatch.setattr(toast, "show_info", lambda parent, msg, duration=3000: infos.append(msg))
    ctrl._on_sync_toggled(False)
    ctrl.add_task("Sin conexión", [], '', '', {'asana': MIS_TAREAS})
    qtbot.wait(100)
    assert asana.calls == []
    assert "Se creará en Asana cuando conectes" in infos
    assert ctrl.data_handler.pending_asana_ops_count() == 1


def test_con_repeticion_no_se_crea_en_asana(ctrl, asana):
    ctrl.add_task("Rutina", [], 'daily', '', {'asana': MIS_TAREAS})
    assert not ctrl.data_handler.has_pending_asana_ops()


def test_clickup_crea_y_guarda_el_id(make_ctrl, qtbot, monkeypatch):
    from core.clickup_integration import ClickUpClient
    from ui.widgets import toast
    calls, ok = [], []
    monkeypatch.setattr(toast, "show_success", lambda parent, msg, duration=3000: ok.append(msg))

    def fake_create(self, list_id, name, description="", priority=3, due_date=None,
                    assignees=None):
        calls.append((self.api_token, list_id, name, due_date))
        return {'clickup_id': 'cu1', 'source': 'clickup',
                'clickup_data': {'url': 'https://app.clickup.com/t/cu1'}}
    monkeypatch.setattr(ClickUpClient, "create_task", fake_create)
    ctrl = make_ctrl({**CONFIGURED, **CLICKUP})
    panel = ctrl.tasks_panel.add_panel
    assert not panel.clickup_btn.is_dimmed()
    panel.clickup_btn.click()
    assert panel.targets == {'clickup': True}
    ctrl.add_task("Para ClickUp", [], '', '2026-11-05', panel.targets)
    task = next(t for t in ctrl.data_handler.get_tasks() if t['name'] == "Para ClickUp")
    qtbot.waitUntil(lambda: task.get('clickup_id') == 'cu1', timeout=5000)
    assert calls == [('pk_prueba', 'L1', "Para ClickUp", '2026-11-05')]
    assert task['clickup_url'] == 'https://app.clickup.com/t/cu1'
    assert task['source'] == 'local'
    assert any("ClickUp" in m for m in ok)


def test_clickup_avisa_si_falla(make_ctrl, qtbot, monkeypatch):
    from core.clickup_integration import ClickUpClient
    from ui.widgets import toast
    warnings = []
    monkeypatch.setattr(toast, "show_warning",
                        lambda parent, msg, duration=3000: warnings.append(msg))
    monkeypatch.setattr(ClickUpClient, "create_task", lambda self, *a, **k: None)
    ctrl = make_ctrl({**CONFIGURED, **CLICKUP})
    ctrl.add_task("Fallará", [], '', '', {'clickup': True})
    qtbot.waitUntil(lambda: bool(warnings), timeout=5000)
    assert "No se pudo crear en ClickUp" in warnings[0]


def test_clickup_create_task_sin_estado_fijo(monkeypatch):
    import core.clickup_integration as cu
    sent = []

    class Resp:
        status_code = 200

        def json(self):
            return {'id': 'cu9', 'name': 'x', 'status': {'type': 'open'}}

    monkeypatch.setattr(cu.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: sent.append(json) or Resp())
    created = cu.ClickUpClient("pk_prueba").create_task("L1", "x", due_date="2026-11-05")
    assert created['clickup_id'] == 'cu9'
    assert 'status' not in sent[0] and sent[0]['due_date_time'] is False


def test_logo_sin_configurar_abre_ajustes_en_su_pestana(make_ctrl, monkeypatch):
    from ui.dialogs.settings_dialog import SettingsDialog
    opened = []

    def fake_exec(dlg):
        opened.append(dlg.tabs.tabText(dlg.tabs.currentIndex()))
        return 0
    monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
    ctrl = make_ctrl({**CONFIGURED, 'active_integrations': []})
    panel = ctrl.tasks_panel.add_panel
    assert panel.asana_btn.is_dimmed() and panel.clickup_btn.is_dimmed()
    panel.asana_btn.click()
    panel.clickup_btn.click()
    assert opened == ["Asana", "ClickUp"]
    assert panel.targets == {}


def test_los_logos_recuerdan_como_quedaron(make_ctrl):
    ctrl = make_ctrl({**CONFIGURED, **CLICKUP, 'asana_connections': CONNS,
                      'task_add_asana': True, 'asana_create_target': MARKETING,
                      'task_add_clickup': True})
    panel = ctrl.tasks_panel.add_panel
    assert panel.asana_target == MARKETING and panel.clickup_on
    assert panel.targets == {'asana': MARKETING, 'clickup': True}
    # El proyecto se desmarca en Ajustes: Asana se apaga (y se recuerda)
    settings = ctrl.data_handler.load_settings()
    settings['asana_connections'] = CONNS[1:]
    ctrl.apply_settings(settings)
    assert panel.asana_target is None
    assert ctrl.data_handler.load_settings()['task_add_asana'] is False


def test_ajustes_incluir_mis_tareas_sin_proyecto(make_ctrl, qtbot):
    from ui.dialogs.settings_dialog import SettingsDialog
    ctrl = make_ctrl()                           # sin proyectos marcados
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    assert not dlg.chk_no_project.isChecked()
    assert "ya llegan todas" in dlg.no_project_hint.text()
    assert not dlg.no_project_hint.isHidden()
    dlg.chk_no_project.setChecked(True)
    dlg.accept_settings()
    assert ctrl.data_handler.load_settings()['asana_include_no_project'] is True
    dlg.reject()
    ctrl2 = make_ctrl({**CONFIGURED, 'asana_connections': CONNS})
    dlg2 = SettingsDialog(ctrl2.window, ctrl2)
    qtbot.addWidget(dlg2)
    assert dlg2.no_project_hint.text() == "" and dlg2.no_project_hint.isHidden()
    dlg2.reject()


# ------------------------------------------- altas pendientes (revisión 1.0)
def test_alta_con_timeout_dentro_del_sync_no_se_importa_duplicada(qtbot, dh, asana):
    """El POST llega pero la respuesta se pierde y el fetch del MISMO sync
    trae la recién creada: no se importa aparte; el reintento la enlaza."""
    from services.asana_service import AsanaService
    tid = dh.add_task("Llamar al banco", [], asana_create=MIS_TAREAS)
    asana.post_lands_then_timeout = True
    svc = AsanaService(dh)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    qtbot.wait(300)  # merge en lotes
    assert [t['name'] for t in dh.get_tasks()].count("Llamar al banco") == 1
    asana.post_lands_then_timeout = False
    _push(dh)
    gid = asana.created[0][0]
    assert len(asana.created) == 1
    assert [t['id'] for t in dh.get_tasks() if t.get('asana_id') == gid] == [tid]


def test_un_gid_ya_enlazado_no_se_enlaza_a_otra_tarea(dh):
    tid = dh.add_task("Informe mensual", [], asana_create=MIS_TAREAS)
    op = dh.data['asana_outbox'][0]
    dh._link_asana_created({'gid': GID, 'task_id': tid, 'provisional': op['gid'],
                            'projects': [], 'name': "Informe mensual",
                            'completed': False}, op)
    assert _by_id(dh, tid)['source'] == 'local' and not _by_id(dh, tid)['asana_id']
    assert [t['id'] for t in dh.get_tasks() if t.get('asana_id') == GID] == [TASK_ID]


def test_reintento_de_alta_no_toma_otra_ya_enlazada_con_el_mismo_nombre(dh, asana):
    first = dh.add_task("Revisar", [], asana_create=MIS_TAREAS)
    _push(dh)
    second = dh.add_task("Revisar", [], asana_create=MIS_TAREAS)
    asana.offline = True
    assert _push(dh)['failed'] == 1          # ni salió, pero cuenta como intento
    asana.offline = False
    _push(dh)                                # el reintento busca antes de crear
    assert len(asana.created) == 2
    assert _by_id(dh, second)['asana_id'] == asana.created[1][0]
    assert _by_id(dh, first)['asana_id'] == asana.created[0][0]


def test_editar_una_tarea_con_el_alta_pendiente_no_ofrece_repetirla(ctrl, monkeypatch):
    from ui.dialogs import branded_dialogs
    ctrl._on_sync_toggled(False)
    ctrl.add_task("Meditar", [], '', '', {'asana': MIS_TAREAS})
    tid = next(t['id'] for t in ctrl.data_handler.get_tasks() if t['name'] == "Meditar")
    seen = {}

    def dialog(parent, name, tags, recurring=None, hint=None):
        seen.update(recurring=recurring, hint=hint)
        return name, tags, None
    monkeypatch.setattr(branded_dialogs, "edit_task", dialog)
    ctrl.edit_task(tid)
    assert seen['recurring'] is None             # sin selector de repetición
    assert "Se está creando en Asana" in seen['hint']
    assert not ctrl._task_by_id(tid).get('recurring')
    assert ctrl.data_handler.has_pending_asana_create(tid)


def test_borrar_todo_retira_las_altas_pendientes(dh, asana):
    asana.offline = True
    tid = dh.add_task("Borrador privado", [], asana_create=MIS_TAREAS)
    dh.queue_asana_comment(f"local-{tid}", C1)
    dh.update_task_status(TASK_ID, True)     # de una tarea real de Asana: se queda
    dh.clear_data()
    assert [op['type'] for op in dh.data['asana_outbox']] == ['update']
    asana.offline = False
    _push(dh)
    assert asana.created == [] and asana.remote[GID]['completed'] is True


def test_importar_retira_las_altas_de_tareas_que_no_estan(dh, asana):
    backup = json.loads(json.dumps(dh.data))
    asana.offline = True
    gone = dh.add_task("Borrador privado", [], asana_create=MIS_TAREAS)
    kept = dh.add_task("También en el backup", [], asana_create=MIS_TAREAS)
    backup['tasks'].append(dict(_by_id(dh, kept)))
    dh.import_data(backup)
    assert dh.pending_asana_create_ids() == [kept]
    assert not dh.has_pending_asana_create(gone)


def test_alta_rechazada_avisa_con_su_nombre(ctrl, qtbot, asana, monkeypatch):
    from ui.widgets import toast
    warnings = []
    monkeypatch.setattr(toast, "show_warning",
                        lambda parent, msg, duration=3000: warnings.append(msg))
    asana.status = 403
    ctrl.add_task("Sin permiso", [], '', '', {'asana': MARKETING})
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    qtbot.waitUntil(lambda: bool(warnings), timeout=5000)
    assert warnings == ["No se pudo crear en Asana: Sin permiso (HTTP 403)"]


def test_comentario_detras_del_alta_sale_con_el_gid_real(dh, asana):
    asana.offline = True
    tid = dh.add_task("Informe", [], asana_create=MIS_TAREAS)
    dh.queue_asana_comment(f"local-{tid}", C1)
    _push(dh)
    asana.offline = False
    _push(dh)                                # alta y comentario, en el mismo envío
    gid = asana.created[0][0]
    assert asana.stories == [(gid, C1)]
    assert not dh.has_pending_asana_ops()


def test_comentario_espera_al_alta_en_vuelo_y_no_se_duplica(dh, asana):
    tid = dh.add_task("Informe", [], asana_create=MIS_TAREAS)
    ops = dh.take_asana_ops_for_push()       # el alta, en vuelo
    dh.queue_asana_comment(f"local-{tid}", C1)
    _push(dh)                                # el comentario espera, sin cortar la cola
    assert not any('local-' in path for _, path in asana.calls)
    assert dh.data['asana_outbox'][-1].get('send_tries') == 0
    dh.apply_asana_push_results(dh.send_asana_ops(ops))
    gid = asana.created[0][0]
    assert [(op['type'], op['gid']) for op in dh.data['asana_outbox']] == [('comment', gid)]
    asana.post_lands_then_timeout = True     # llega, pero sin respuesta
    _push(dh)
    asana.post_lands_then_timeout = False
    _push(dh)                                # la comprobación usa el gid real
    assert asana.stories == [(gid, C1)]
    assert not dh.has_pending_asana_ops()


def test_borrar_o_rechazar_el_alta_retira_sus_comentarios(dh, asana):
    asana.offline = True
    tid = dh.add_task("Para borrar", [], asana_create=MIS_TAREAS)
    dh.queue_asana_comment(f"local-{tid}", C1)
    dh.delete_task(tid)
    assert not dh.has_pending_asana_ops()
    other = dh.add_task("Sin permiso", [], asana_create=MARKETING)
    dh.queue_asana_comment(f"local-{other}", C2)
    asana.offline = False
    asana.status = 403
    assert _push(dh)['dropped'] == 1
    assert not dh.has_pending_asana_ops()
    assert dh.asana_create_rejections == [("Sin permiso", "HTTP 403")]
    assert asana.stories == []


def test_pomodoro_con_el_alta_pendiente_comenta_al_crearse(ctrl, qtbot, asana):
    asana.offline = True
    ctrl.add_task("Informe trimestral", [], '', '', {'asana': MIS_TAREAS})
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    tid = next(t['id'] for t in ctrl.data_handler.get_tasks()
               if t['name'] == "Informe trimestral")
    ctrl.select_task(tid)
    ctrl.model.start()
    ctrl.complete_pomodoro_session()
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    assert [op['type'] for op in ctrl.data_handler.data['asana_outbox']] == [
        'create', 'comment']
    asana.offline = False
    ctrl.asana.push_pending()
    qtbot.waitUntil(lambda: not ctrl.data_handler.has_pending_asana_ops(), timeout=5000)
    gid = asana.created[0][0]
    assert ctrl._task_by_id(tid)['asana_id'] == gid
    assert len(asana.stories) == 1 and asana.stories[0][0] == gid
    qtbot.wait(600)  # aviso diferido (400 ms) de complete_pomodoro_session
