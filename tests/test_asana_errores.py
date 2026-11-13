"""Encargo B1: los errores de Asana se ven (ya no pasan por «Sin cambios»).

Antes, `_fetch_asana_assigned` cortaba ante cualquier respuesta distinta de
200 y devolvía la lista parcial (o vacía): el sync acababa en «Sin cambios
desde Asana…» (o en «✅ completada» con la mitad), verificaba una a una las
enlazadas que «faltaban» y nadie se enteraba de que el token estaba
revocado o de que Asana pedía esperar.

Ahora un fetch que falla lanza AsanaFetchError (core/asana_errors.py) y el
sync termina por el camino de error: no fusiona nada, no toca la cola de
cambios ni las tareas, y el usuario ve un texto por tipo (línea de estado +
diálogo «Asana»). Ante un 429 o un 5xx el sync se repite solo, nunca antes
del Retry-After que pidió Asana.

Revisión F0 (encargo «asana»): un test por cada invariante del reintento; un
sync cancelado que falla no abre el diálogo; el reintento que espera a un
envío de la cola sigue siendo silencioso; el Retry-After se acota a una hora;
un listado sin fin se corta a las 200 páginas; un fallo antes de salir a la
red no atasca el sync; y la etiqueta de conexión dice qué pasa según el tipo
de error.

Todo pasa por el camino real (AsanaService → DataHandler → requests) contra
la red falsa (tests/fakes/net.py, fixture `fake_net`): una petición sin ruta
preparada (p. ej. verificar una tarea suelta) hace fallar el test.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_asana_errores.py -q
"""
import copy
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import requests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.asana_errors import (AsanaFetchError, kind_for_status,  # noqa: E402
                               parse_retry_after)
from core.data_handler import DataHandler, _new_outbox_op  # noqa: E402
from services.asana_service import AsanaService  # noqa: E402
from tests.fakes.net import ASANA_HOST, FakeResponse, asana_page, asana_path  # noqa: E402

CONFIGURED = {'asana_pat': 'pat-de-prueba', 'asana_assignee_gid': 'A',
              'asana_workspace_gid': 'W'}
LIST = asana_path('/tasks')
GID_1 = '1200000000000001'
GID_2 = '1200000000000002'

AUTH = "Asana rechazó el token de acceso (HTTP 401). Revísalo en ⚙ Configuración → Asana."
WS_HINT = "Revisa el workspace y el Assignee GID en ⚙ Configuración → Asana."
OFFLINE = "Sin conexión con Asana."
BAD_JSON = "Asana devolvió una respuesta que no se entiende."


def _remote(gid: str, name: str, completed: bool = False, projects=()) -> dict:
    return {'gid': gid, 'name': name, 'completed': completed,
            'created_at': '2026-11-01T10:00:00.000Z', 'due_on': None, 'due_at': None,
            'tags': [], 'projects': [{'gid': g, 'name': n} for g, n in projects],
            'assignee': {'gid': 'A'}, 'parent': None}


def _tasks() -> list:
    """Dos enlazadas abiertas (una con etiqueta propia) y una local."""
    return [{'id': 1, 'name': 'Informe', 'completed': False, 'tags': ['Marketing', 'mía'],
             'local_tags': ['mía'], 'source': 'asana', 'asana_id': GID_1, 'due': None},
            {'id': 2, 'name': 'Llamar', 'completed': False, 'tags': [],
             'source': 'asana', 'asana_id': GID_2, 'due': '2026-11-20'},
            {'id': 3, 'name': 'Solo aquí', 'completed': False, 'tags': [],
             'source': 'local', 'asana_id': None}]


def _write(tmp_path: Path, tasks=None, settings=None, **extra) -> Path:
    data = {'tasks': _tasks() if tasks is None else tasks, 'deleted_tasks': [],
            'time_chunks': [], 'history': [], 'ui_snapshot': {},
            'settings': dict(CONFIGURED if settings is None else settings), **extra}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def make_dh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def build(tasks=None, settings=None, **extra) -> DataHandler:
        return DataHandler(_write(tmp_path, tasks, settings, **extra))
    return build


class _Senales:
    def __init__(self, svc: AsanaService):
        self.status, self.finished, self.errors, self.online = [], [], [], []
        svc.status.connect(self.status.append)
        svc.finished.connect(self.finished.append)
        svc.error.connect(self.errors.append)
        svc.connection_state.connect(self.online.append)


# Ningún reintento programado sobrevive al test: el bloque «asana y config en
# tests» de tests/conftest.py apaga al final de cada test los servicios
# creados (uno no se libera solo y su reintento saldría a la red en medio de
# otro test).
def _service(dh: DataHandler, push: bool = True, delays=None) -> tuple:
    svc = AsanaService(dh)
    svc.push_allowed = lambda: push
    if delays is not None:
        svc.RETRY_DELAYS_MS = delays
    return svc, _Senales(svc)


def _run_sync(qtbot, svc: AsanaService) -> None:
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    qtbot.wait(20)   # _run_deferred (singleShot 0)


# =====================================================================
# Sync completo: cada tipo de error del listado se ve y no fusiona nada
# =====================================================================
LISTING_ERRORS = [
    # id, respuesta de GET /tasks, tipo, texto para el usuario, reintento (ms)
    ('401', FakeResponse(401, {'errors': [{'message': 'Not Authorized'}]}),
     'auth', AUTH, None),
    ('403', FakeResponse(403, {'errors': [{'message': 'Forbidden'}]}), 'forbidden',
     f"Asana no te deja ver el workspace W (HTTP 403). {WS_HINT}", None),
    ('404', FakeResponse(404, {'errors': [{'message': 'workspace: Unknown object: W'}]}),
     'not_found', f"Asana no encuentra el workspace W (HTTP 404). {WS_HINT}", None),
    ('429-retry-after-largo',
     FakeResponse(429, {'errors': [{'message': 'Rate limit'}]}, headers={'Retry-After': '150'}),
     'rate_limit', "Asana pide esperar: demasiadas peticiones. Se reintentará en 3 min.",
     150_000),
    ('429-retry-after-corto',
     FakeResponse(429, {'errors': [{'message': 'Rate limit'}]}, headers={'retry-after': '5'}),
     'rate_limit', "Asana pide esperar: demasiadas peticiones. Se reintentará en 1 min.",
     60_000),
    ('429-sin-retry-after', FakeResponse(429), 'rate_limit',
     "Asana pide esperar: demasiadas peticiones. Se reintentará en 1 min.", 60_000),
    ('500', FakeResponse(500, {'errors': [{'message': 'Server Error'}]}), 'server',
     "Asana no responde bien (error 500). Se reintentará en 1 min.", 60_000),
    ('503', FakeResponse(503, text='Service Unavailable'), 'server',
     "Asana no responde bien (error 503). Se reintentará en 1 min.", 60_000),
    ('timeout', requests.Timeout, 'network', OFFLINE, None),
    ('sin-red', requests.ConnectionError, 'network', OFFLINE, None),
    ('json-invalido', FakeResponse(200, text='<html>Portal cautivo</html>'),
     'bad_response', BAD_JSON, None),
    ('json-sin-data', {'errors': []}, 'bad_response', BAD_JSON, None),
    ('400', FakeResponse(400, {'errors': [{'message': 'assignee: Not a recognized ID'}]}),
     'bad_response', f"Asana rechazó la petición (error 400). {WS_HINT}", None),
]


@pytest.mark.parametrize("reply, kind, message, retry_ms",
                         [case[1:] for case in LISTING_ERRORS],
                         ids=[case[0] for case in LISTING_ERRORS])
def test_un_listado_fallido_termina_en_error_sin_tocar_nada(
        qtbot, make_dh, fake_net, tmp_path, reply, kind, message, retry_ms):
    """El sync termina en error (no en «Sin cambios desde Asana…»): estado
    «❌ <texto>», error(<texto>) para el diálogo, connection_state(False) y sin
    finished. Las tareas, la cola de cambios y el archivo quedan igual, y no
    se verifica ninguna enlazada una a una (no hay ruta para /tasks/{gid}).
    Solo un 429 o un 5xx programan el reintento del sync; con un 429, nunca
    antes de su Retry-After (max(Retry-After, RETRY_DELAYS_MS[0]))."""
    dh = make_dh(asana_outbox=[_new_outbox_op('comment', GID_1, text='🍅 Pomodoro')])
    tasks_before = copy.deepcopy(dh.data['tasks'])
    outbox_before = copy.deepcopy(dh.data['asana_outbox'])
    file_before = (tmp_path / "data.json").read_bytes()
    fake_net.get(ASANA_HOST, LIST, reply)
    svc, seen = _service(dh, push=False)     # la cola no sale: así se ve que no se toca

    with pytest.raises(AsanaFetchError) as caught:
        dh._fetch_asana_assigned('A', 'W')
    assert caught.value.kind == kind
    fake_net.calls.clear()

    _run_sync(qtbot, svc)
    assert [c.path for c in fake_net.calls] == ['/api/1.0/tasks']
    assert seen.finished == []
    assert seen.errors == [message]
    assert seen.status[-1] == f"❌ {message}"
    assert not any('Sin cambios' in s or 'completada' in s for s in seen.status)
    assert seen.online == [False]
    assert dh.data['tasks'] == tasks_before
    assert dh.data['asana_outbox'] == outbox_before
    assert (tmp_path / "data.json").read_bytes() == file_before
    assert svc.retry_scheduled is (retry_ms is not None)
    if retry_ms is not None:
        assert svc._retry_timer.interval() == retry_ms


@pytest.mark.parametrize("page_2, message", [
    (FakeResponse(500, {'errors': [{'message': 'Server Error'}]}),
     "Asana no responde bien (error 500). Se reintentará en 1 min."),
    (FakeResponse(401), AUTH),
    (requests.Timeout, OFFLINE),
    (FakeResponse(200, text='{"data": [ roto'), BAD_JSON),
], ids=['500', '401', 'timeout', 'json-invalido'])
def test_un_fallo_en_la_pagina_2_de_3_no_fusiona_nada(
        qtbot, make_dh, fake_net, tmp_path, page_2, message):
    """Página 1 con una nueva y el renombrado de una enlazada, página 2 que
    falla, página 3 con otra enlazada: nada de la página 1 llega (ni la
    nueva ni el renombrado), la página 3 ni se pide y el usuario ve el
    error. Antes: «✅ Sincronización completada: 2 tareas.» con la mitad."""
    dh = make_dh()
    tasks_before = copy.deepcopy(dh.data['tasks'])
    file_before = (tmp_path / "data.json").read_bytes()
    pages = {None: asana_page([_remote('1300000000000009', 'Nueva de la página 1'),
                               _remote(GID_1, 'Informe RENOMBRADO')], 'tok-2'),
             'tok-2': page_2,
             'tok-3': asana_page([_remote(GID_2, 'Llamar')])}
    fake_net.get(ASANA_HOST, LIST, lambda call: pages[call.param('offset')])
    svc, seen = _service(dh)

    with pytest.raises(AsanaFetchError):
        dh._fetch_asana_assigned('A', 'W')
    fake_net.calls.clear()

    _run_sync(qtbot, svc)
    assert [c.param('offset') for c in fake_net.calls] == [None, 'tok-2']
    assert dh.data['tasks'] == tasks_before
    assert (tmp_path / "data.json").read_bytes() == file_before
    assert seen.finished == [] and seen.errors == [message]
    assert seen.status[-1] == f"❌ {message}"


def test_conexiones_un_workspace_que_falla_invalida_todo(make_dh, fake_net):
    """Con varias conexiones, un workspace que falla invalida el fetch entero:
    sin él, sus enlazadas faltarían y se verificarían como borradas. Antes,
    un error HTTP devolvía lo del otro workspace (y otro error cualquiera se
    imprimía y se seguía)."""
    dh = make_dh()
    conns = [{'workspace_gid': 'W1', 'project_gid': 'P1'},
             {'workspace_gid': 'W2', 'project_gid': 'P2'}]
    by_ws = {'W1': asana_page([_remote(GID_1, 'Informe', projects=[('P1', 'Marketing')])]),
             'W2': FakeResponse(403, {'errors': [{'message': 'Forbidden'}]})}
    fake_net.get(ASANA_HOST, LIST, lambda call: by_ws[call.param('workspace')])
    with pytest.raises(AsanaFetchError) as caught:
        dh.fetch_asana_tasks_for_connections(conns, 'A')
    assert (caught.value.kind, caught.value.status) == ('forbidden', 403)
    assert str(caught.value) == f"Asana no te deja ver el workspace W2 (HTTP 403). {WS_HINT}"


def test_sync_con_conexiones_avisa_del_workspace_que_falla(qtbot, make_dh, fake_net):
    settings = {**CONFIGURED, 'asana_connections': [
        {'workspace_gid': 'W', 'project_gid': 'P1', 'project_name': 'Marketing'},
        {'workspace_gid': 'W9', 'project_gid': 'P9', 'project_name': 'Otro'}]}
    dh = make_dh(settings=settings)
    tasks_before = copy.deepcopy(dh.data['tasks'])
    by_ws = {'W': asana_page([_remote(GID_1, 'Informe RENOMBRADO',
                                      projects=[('P1', 'Marketing')])]),
             'W9': FakeResponse(404, {'errors': [{'message': 'Unknown object'}]})}
    fake_net.get(ASANA_HOST, LIST, lambda call: by_ws[call.param('workspace')])
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    message = f"Asana no encuentra el workspace W9 (HTTP 404). {WS_HINT}"
    assert seen.errors == [message] and seen.finished == []
    assert dh.data['tasks'] == tasks_before


# =====================================================================
# Reintento automático del sync (429 / 5xx)
# =====================================================================
def test_el_reintento_repite_el_sync_y_fusiona(qtbot, make_dh, fake_net):
    """Tras un 503, el temporizador de reintentos repite el sync completo; si
    Asana ya responde, fusiona y termina como un sync normal."""
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(503),
                 asana_page([_remote(GID_1, 'Informe')]))
    svc, seen = _service(dh, delays=(40, 60_000))
    _run_sync(qtbot, svc)
    assert seen.errors == ["Asana no responde bien (error 503). Se reintentará en 1 min."]
    qtbot.waitUntil(lambda: seen.finished == [1], timeout=5000)
    assert [t['asana_id'] for t in dh.get_tasks()] == [GID_1]
    assert seen.status[-1] == "✅ Sincronización completada: 1 tareas."
    assert seen.online[-1] is True
    assert not svc.retry_scheduled


def test_un_reintento_que_vuelve_a_fallar_no_abre_otro_dialogo(qtbot, make_dh, fake_net):
    """El primer fallo avisa con error() (diálogo); el reintento automático
    que vuelve a fallar solo actualiza la línea de estado y programa el
    siguiente con el retraso que toca (RETRY_DELAYS_MS[1])."""
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(500), FakeResponse(500))
    svc, seen = _service(dh, delays=(40, 120_000))
    _run_sync(qtbot, svc)
    qtbot.waitUntil(lambda: len(fake_net.calls) == 2 and not svc.busy, timeout=5000)
    qtbot.wait(20)
    assert seen.errors == ["Asana no responde bien (error 500). Se reintentará en 1 min."]
    assert seen.status[-1] == "❌ Asana no responde bien (error 500). Se reintentará en 2 min."
    assert svc._retry_timer.interval() == 120_000


def _expire_retry(qtbot, svc: AsanaService) -> None:
    """Lo que hace el temporizador al vencer (sin esperar el minuto real)."""
    assert svc.retry_scheduled
    svc._retry_timer.stop()
    svc._on_retry_timer()
    qtbot.wait(50)


def test_apagar_el_sync_anula_su_reintento(qtbot, make_dh, fake_net):
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(500))
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    svc.cancel()                      # el usuario apaga el switch
    _expire_retry(qtbot, svc)
    assert len(fake_net.calls) == 1 and not svc.busy


def test_sin_permiso_el_reintento_no_sincroniza(qtbot, make_dh, fake_net):
    """Al vencer, el reintento mira si el sync sigue permitido (switch e
    integración): si no, no sale nada."""
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(500))
    allowed = [True]
    svc, seen = _service(dh)
    svc.push_allowed = lambda: allowed[0]
    _run_sync(qtbot, svc)
    allowed[0] = False
    _expire_retry(qtbot, svc)
    assert len(fake_net.calls) == 1 and not svc.busy


def test_un_envio_de_la_cola_no_anula_el_reintento_del_sync(qtbot, make_dh, fake_net):
    """Un cambio que se envía mientras espera el reintento del sync (p. ej.
    completar una tarea) no lo cancela: el listado sigue pendiente y, al
    vencer, se repite el sync."""
    dh = make_dh()
    fake_net.get(ASANA_HOST, LIST, FakeResponse(500),
                 asana_page([_remote(GID_1, 'Informe'), _remote(GID_2, 'Llamar')]))
    fake_net.put(ASANA_HOST, asana_path(f'/tasks/{GID_1}'),
                 {'data': {'gid': GID_1, 'completed': True}})
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    assert svc.retry_scheduled
    dh.update_task_status(1, True)
    svc.push_pending()
    qtbot.waitUntil(lambda: not dh.has_pending_asana_ops() and not svc.busy, timeout=5000)
    assert svc.retry_scheduled and svc._retry_sync
    _expire_retry(qtbot, svc)
    qtbot.waitUntil(lambda: seen.finished == [2], timeout=5000)
    assert [c.method for c in fake_net.calls] == ['GET', 'PUT', 'GET']


def test_el_retry_after_manda_sobre_un_reintento_ya_programado(qtbot, make_dh, fake_net):
    """En un sync cuyo envío de la cola falla (reintento de 60 s) y cuyo
    listado recibe un 429 con Retry-After: 600, el reintento espera los 600 s
    (y repite el sync completo)."""
    dh = make_dh()
    dh.update_task_status(1, True)
    fake_net.put(ASANA_HOST, asana_path(f'/tasks/{GID_1}'), FakeResponse(503))
    fake_net.get(ASANA_HOST, LIST, FakeResponse(429, headers={'Retry-After': '600'}))
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    assert svc._retry_timer.interval() == 600_000
    assert svc._retry_sync is True
    assert seen.errors == ["Asana pide esperar: demasiadas peticiones. Se reintentará en 10 min."]
    assert dh.pending_asana_ops_count() == 1   # el cambio sigue en cola


# --- Invariantes del reintento (revisión F0, T1): un test por cada una ---
def test_tras_un_reintento_silencioso_el_sync_manual_vuelve_a_avisar(qtbot, make_dh, fake_net):
    """El modo silencioso es solo del reintento: sync() lo repone en cada
    arranque. Tras un reintento que vuelve a fallar en silencio, un sync
    manual que falla (token revocado) abre el diálogo otra vez."""
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(500), FakeResponse(500), FakeResponse(401))
    svc, seen = _service(dh, delays=(40, 120_000))
    _run_sync(qtbot, svc)
    qtbot.waitUntil(lambda: len(fake_net.calls) == 2 and not svc.busy, timeout=5000)
    qtbot.wait(20)
    assert len(seen.errors) == 1                      # el reintento falló en silencio
    _run_sync(qtbot, svc)                             # sync manual: token revocado
    assert seen.errors == ["Asana no responde bien (error 500). Se reintentará en 1 min.", AUTH]


def test_tras_un_listado_correcto_el_reintento_vuelve_al_primer_retraso(qtbot, make_dh,
                                                                        fake_net):
    """Un listado que llega entero pone a cero los fallos del sync: la caída
    siguiente vuelve a esperar RETRY_DELAYS_MS[0] (1 min), no el escalón
    siguiente."""
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(503), asana_page([_remote(GID_1, 'Informe')]),
                 FakeResponse(503))
    svc, seen = _service(dh, delays=(40, 120_000))
    _run_sync(qtbot, svc)
    qtbot.waitUntil(lambda: seen.finished == [1], timeout=5000)
    qtbot.wait(20)
    svc.RETRY_DELAYS_MS = (60_000, 120_000)
    _run_sync(qtbot, svc)
    assert svc._retry_timer.interval() == 60_000
    assert seen.status[-1].endswith("Se reintentará en 1 min.")


def test_un_sync_manual_sustituye_al_reintento_pendiente(qtbot, make_dh, fake_net):
    """Un sync manual con un reintento del sync pendiente lo sustituye: si
    después falla un envío de la cola, al vencer solo se reenvía la cola
    (PUT), sin repetir el listado."""
    dh = make_dh()
    fake_net.get(ASANA_HOST, LIST, FakeResponse(500),
                 asana_page([_remote(GID_1, 'Informe'), _remote(GID_2, 'Llamar')]))
    fake_net.put(ASANA_HOST, asana_path(f'/tasks/{GID_1}'), FakeResponse(503),
                 {'data': {'gid': GID_1, 'completed': True}})
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)                 # 500: reintento del sync pendiente
    assert svc.retry_scheduled
    _run_sync(qtbot, svc)                 # el usuario sincroniza a mano: sale bien
    qtbot.waitUntil(lambda: seen.finished == [2], timeout=5000)
    qtbot.wait(20)
    dh.update_task_status(1, True)
    svc.push_pending()                    # PUT 503: reintento SOLO de la cola
    qtbot.waitUntil(lambda: not svc.busy and svc.retry_scheduled, timeout=5000)
    _expire_retry(qtbot, svc)
    qtbot.waitUntil(lambda: not dh.has_pending_asana_ops() and not svc.busy, timeout=5000)
    assert [c.method for c in fake_net.calls] == ['GET', 'GET', 'PUT', 'PUT']


def test_un_retraso_que_no_cabe_en_el_temporizador_se_acota(qtbot, make_dh, fake_net):
    """El temporizador de Qt es de 32 bits (ms): un retraso mayor se acota a
    2**31-1 en vez de lanzar OverflowError dentro de _on_error (el usuario no
    vería ni la línea de estado ni el diálogo)."""
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(503))
    svc, seen = _service(dh, delays=(2**31 + 5_000,))
    _run_sync(qtbot, svc)
    assert svc._retry_timer.interval() == 2**31 - 1
    assert seen.status[-1].startswith("❌ Asana no responde bien (error 503).")
    assert len(seen.errors) == 1


def test_un_fallo_de_la_cola_no_adelanta_el_retry_after_del_sync(qtbot, make_dh, fake_net):
    """Sync con 429 + Retry-After: 600 (reintento del sync a 10 min); después
    un PUT de la cola con 429: el reintento ya programado para más tarde no
    se adelanta a 1 min (el sync, con su GET, no se repite antes de lo que
    pidió Asana)."""
    dh = make_dh()
    fake_net.get(ASANA_HOST, LIST, FakeResponse(429, headers={'Retry-After': '600'}))
    fake_net.put(ASANA_HOST, asana_path(f'/tasks/{GID_1}'), FakeResponse(429))
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    assert svc._retry_timer.interval() == 600_000 and svc._retry_sync
    dh.update_task_status(1, True)
    svc.push_pending()
    qtbot.waitUntil(lambda: not svc.busy and len(fake_net.calls) == 2, timeout=5000)
    qtbot.wait(20)
    assert svc._retry_sync
    assert svc._retry_timer.remainingTime() > 590_000


@pytest.mark.parametrize("value", ['1e306', '9' * 400, 'inf', '99999999'],
                         ids=['1e306', '400-nueves', 'inf', '99999999'])
def test_un_retry_after_desmesurado_se_acota_a_una_hora(qtbot, make_dh, fake_net, value):
    """Un Retry-After absurdo espera como mucho una hora. Antes, '1e306'
    acababa en OverflowError dentro de _on_error (estado atascado en
    «Sincronizando Asana…», sin diálogo ni reintento) y uno grande pero
    válido dejaba el reintento (y el de la cola) a ~25 días."""
    dh = make_dh(tasks=[])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(429, headers={'Retry-After': value}))
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    message = "Asana pide esperar: demasiadas peticiones. Se reintentará en 60 min."
    assert seen.errors == [message] and seen.status[-1] == f"❌ {message}"
    assert svc._retry_timer.interval() == 3_600_000


# =====================================================================
# Sync cancelado, reintento diferido y fallos antes de salir a la red
# =====================================================================
def _con_compuerta(reply):
    """Respuesta que espera a que el test abra la compuerta: la petición se
    queda en vuelo mientras tanto."""
    gate = threading.Event()

    def wait_then_reply(call):
        gate.wait(5)
        return reply
    return gate, wait_then_reply


@pytest.mark.parametrize("reply", [FakeResponse(401), FakeResponse(503),
                                   requests.ConnectionError("sin red (simulado)")],
                         ids=['401', '503', 'sin-red'])
def test_un_sync_cancelado_que_falla_no_abre_el_dialogo(qtbot, make_dh, fake_net, reply):
    """El usuario apaga el switch (cancel) con el listado en vuelo y Asana
    responde con un error: el sync acaba en «Sincronización cancelada.», sin
    diálogo ni reintento. Antes, _on_fetched miraba el error antes que la
    cancelación y abría el diálogo «Asana» de un sync que ya no se quería."""
    dh = make_dh(tasks=[])
    gate, slow = _con_compuerta(reply)
    fake_net.get(ASANA_HOST, LIST, slow)
    svc, seen = _service(dh)
    svc.sync()
    qtbot.waitUntil(lambda: len(fake_net.calls) == 1, timeout=5000)
    svc.cancel()
    gate.set()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    qtbot.wait(20)
    assert seen.errors == []
    assert seen.status[-1] == "Sincronización cancelada."
    assert not svc.retry_scheduled


def test_un_sync_cancelado_que_revienta_no_abre_el_dialogo(qtbot, make_dh, fake_net,
                                                          monkeypatch):
    """Igual si el trabajo de red revienta fuera del listado (_on_sync_crashed):
    las ops vuelven a la cola y no hay diálogo."""
    dh = make_dh()
    dh.update_task_status(1, True)
    started, gate = threading.Event(), threading.Event()

    def send_falla(ops, on_progress=None):
        started.set()
        gate.wait(5)
        raise RuntimeError("fallo inesperado (simulado)")
    monkeypatch.setattr(dh, 'send_asana_ops', send_falla)
    svc, seen = _service(dh)
    svc.sync()
    qtbot.waitUntil(started.is_set, timeout=5000)
    svc.cancel()
    gate.set()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    qtbot.wait(20)
    assert seen.errors == []
    assert seen.status[-1] == "Sincronización cancelada."
    assert len(dh.take_asana_ops_for_push()) == 1      # liberada: ya no está en vuelo


def test_apagar_y_encender_durante_un_429_deja_un_solo_dialogo(qtbot, make_dh, fake_net):
    """Apagar y encender el switch con el listado en vuelo: el sync cancelado
    recibe un 429 (Retry-After: 600) y no avisa; el nuevo, que arranca al
    terminar aquel, también recibe el 429: un solo diálogo, el suyo, y su
    reintento a los 600 s. Antes: dos diálogos seguidos."""
    dh = make_dh(tasks=[])
    gate = threading.Event()
    first = []

    def reply(call):
        if not first:
            first.append(call)
            gate.wait(5)
        return FakeResponse(429, headers={'Retry-After': '600'})
    fake_net.get(ASANA_HOST, LIST, reply)
    svc, seen = _service(dh)
    svc.sync()
    qtbot.waitUntil(lambda: len(fake_net.calls) == 1, timeout=5000)
    svc.cancel()          # switch apagado…
    svc.sync()            # …y encendido otra vez (sync_asana)
    assert svc.sync_pending
    gate.set()
    qtbot.waitUntil(lambda: len(fake_net.calls) == 2 and not svc.busy, timeout=5000)
    qtbot.wait(20)
    assert seen.errors == ["Asana pide esperar: demasiadas peticiones. Se reintentará en 10 min."]
    assert svc._retry_sync and svc._retry_timer.interval() == 600_000


def _reintento_con_envio_en_vuelo(qtbot, make_dh, fake_net, manual_antes=False):
    """Un sync falla con 500 (primer diálogo, reintento programado); después
    sale un envío de la cola que tarda y el reintento vence con él en vuelo
    (`manual_antes`: el usuario ya había pulsado sync durante el envío).
    Devuelve (svc, seen, gate): el envío termina al abrir la compuerta."""
    dh = make_dh()
    fake_net.get(ASANA_HOST, LIST, FakeResponse(500), FakeResponse(500))
    gate, slow = _con_compuerta({'data': {'gid': GID_1, 'completed': True}})
    fake_net.put(ASANA_HOST, asana_path(f'/tasks/{GID_1}'), slow)
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    assert len(seen.errors) == 1 and svc._retry_sync
    dh.update_task_status(1, True)
    svc.push_pending()                    # el envío sale y tarda
    assert svc.busy
    if manual_antes:
        svc.sync()                        # botón de sync con el envío en vuelo
    _expire_retry(qtbot, svc)             # vence el reintento: espera al envío
    assert svc.sync_pending
    return svc, seen, gate


def test_un_reintento_que_espera_a_un_envio_sigue_siendo_silencioso(qtbot, make_dh, fake_net):
    """El reintento que tuvo que esperar a un envío de la cola sigue siendo el
    reintento al arrancar: si vuelve a fallar, solo la línea de estado. Antes
    se relanzaba como un sync normal y abría un segundo diálogo."""
    svc, seen, gate = _reintento_con_envio_en_vuelo(qtbot, make_dh, fake_net)
    gate.set()
    qtbot.waitUntil(lambda: fake_net.methods().count('GET') == 2 and not svc.busy,
                    timeout=5000)
    qtbot.wait(20)
    assert len(seen.errors) == 1
    assert seen.status[-1] == "❌ Asana no responde bien (error 500). Se reintentará en 2 min."


@pytest.mark.parametrize("manual_antes", [True, False],
                         ids=['manual-antes-del-reintento', 'manual-despues'])
def test_si_ademas_se_pidio_un_sync_a_mano_gana_el_que_avisa(qtbot, make_dh, fake_net,
                                                            manual_antes):
    """Si mientras esperaba el usuario pidió además un sync a mano (antes o
    después de vencer el reintento), el sync diferido es el suyo: si falla,
    abre el diálogo."""
    svc, seen, gate = _reintento_con_envio_en_vuelo(qtbot, make_dh, fake_net, manual_antes)
    if not manual_antes:
        svc.sync()                        # botón de sync con el envío en vuelo
    gate.set()
    qtbot.waitUntil(lambda: fake_net.methods().count('GET') == 2 and not svc.busy,
                    timeout=5000)
    qtbot.wait(20)
    assert len(seen.errors) == 2


def test_un_fallo_inesperado_antes_de_salir_a_la_red_no_atasca_el_sync(
        qtbot, make_dh, fake_net, monkeypatch):
    """Si algo revienta en sync() antes de lanzar el trabajo de red (un
    ajuste raro, un dato inesperado…), el sync termina por el camino de
    error: las ops tomadas vuelven a la cola, el usuario lo ve y el
    siguiente sync arranca. Antes: «Sincronización ya en curso…» para
    siempre."""
    dh = make_dh()
    dh.update_task_status(1, True)
    real = dh.asana_gids_with_pending
    monkeypatch.setattr(dh, 'asana_gids_with_pending', lambda field: 1 / 0)
    svc, seen = _service(dh)
    svc.sync()
    assert not svc.busy and fake_net.calls == []
    assert seen.errors == ["Error durante la sincronización: division by zero"]
    assert seen.status[-1] == "❌ Error durante la sincronización: division by zero"
    monkeypatch.setattr(dh, 'asana_gids_with_pending', real)
    fake_net.get(ASANA_HOST, LIST, asana_page([_remote(GID_1, 'Informe', completed=True),
                                               _remote(GID_2, 'Llamar')]))
    fake_net.put(ASANA_HOST, asana_path(f'/tasks/{GID_1}'),
                 {'data': {'gid': GID_1, 'completed': True}})
    _run_sync(qtbot, svc)
    assert fake_net.methods() == ['PUT', 'GET']        # la op liberada viaja
    assert seen.finished == [2] and not dh.has_pending_asana_ops()


# =====================================================================
# Otros fetch de Asana: proyectos (Ajustes) y resumen de pendientes
# =====================================================================
@pytest.mark.parametrize("reply, kind", [
    (FakeResponse(401), 'auth'), (FakeResponse(429, headers={'Retry-After': '30'}), 'rate_limit'),
    (requests.ConnectionError, 'network'), (FakeResponse(200, text='no es json'), 'bad_response'),
], ids=['401', '429', 'sin-red', 'json-invalido'])
def test_listar_proyectos_lanza_el_error(make_dh, fake_net, reply, kind):
    """Antes devolvía [] («No se encontraron proyectos en este workspace»)."""
    dh = make_dh(settings={})
    fake_net.get(ASANA_HOST, asana_path('/projects'), reply)
    with pytest.raises(AsanaFetchError) as caught:
        dh.list_asana_projects('W', pat_override='pat')
    assert caught.value.kind == kind
    if kind == 'rate_limit':
        assert caught.value.retry_after == 30


def test_resumen_de_pendientes_no_cuenta_a_medias(make_dh, fake_net):
    """get_incomplete_tasks_summary: una página que falla ya no deja la
    cuenta a medias (antes: 1 de la página 1, sin aviso)."""
    settings = {**CONFIGURED, 'asana_connections': [{'workspace_gid': 'W', 'project_gid': 'P1'}]}
    dh = make_dh(settings=settings)
    fake_net.get(ASANA_HOST, asana_path('/users/A'), {'data': {'name': 'Ana'}})
    fake_net.get(ASANA_HOST, asana_path('/projects/P1'), {'data': {'name': 'Marketing'}})
    fake_net.get(ASANA_HOST, asana_path('/workspaces/W/tasks/search'),
                 asana_page([{'gid': '1', 'completed': False}], 'tok-2'), FakeResponse(500))
    with pytest.raises(AsanaFetchError) as caught:
        dh.get_incomplete_tasks_summary()
    assert (caught.value.kind, caught.value.status) == ('server', 500)


def test_resumen_sin_busqueda_de_pago_usa_el_listado_del_proyecto(make_dh, fake_net):
    """La búsqueda por workspace es de los planes de pago (402): sin ella se
    cuenta con el listado del proyecto (antes contaba 0 en silencio)."""
    settings = {**CONFIGURED, 'asana_connections': [{'workspace_gid': 'W', 'project_gid': 'P1'}]}
    dh = make_dh(tasks=[], settings=settings)
    fake_net.get(ASANA_HOST, asana_path('/users/A'), {'data': {'name': 'Ana'}})
    fake_net.get(ASANA_HOST, asana_path('/projects/P1'), {'data': {'name': 'Marketing'}})
    fake_net.get(ASANA_HOST, asana_path('/workspaces/W/tasks/search'),
                 FakeResponse(402, {'errors': [{'message': 'Search is a premium feature'}]}))
    mine = {'gid': 'x', 'completed': False, 'assignee': {'gid': 'A'}}
    fake_net.get(ASANA_HOST, asana_path('/projects/P1/tasks'),
                 asana_page([mine, dict(mine, gid='y')]))
    assert dh.get_incomplete_tasks_summary() == {
        'user_name': 'Ana', 'total': 2, 'per_project': {'Marketing': 2}}


def test_resumen_proyecto_inexistente_lo_nombra(make_dh, fake_net):
    settings = {**CONFIGURED, 'asana_workspace_gid': '',
                'asana_connections': [{'project_gid': 'P1'}]}
    dh = make_dh(settings=settings)
    fake_net.get(ASANA_HOST, asana_path('/users/A'), {'data': {'name': 'Ana'}})
    fake_net.get(ASANA_HOST, asana_path('/projects/P1'), {'data': {'name': 'Marketing'}})
    fake_net.get(ASANA_HOST, asana_path('/projects/P1/tasks'), FakeResponse(404))
    with pytest.raises(AsanaFetchError) as caught:
        dh.get_incomplete_tasks_summary()
    assert str(caught.value) == ("Asana no encuentra el proyecto «Marketing» (HTTP 404). "
                                 "Revisa los proyectos en ⚙ Configuración → Asana.")


def test_la_404_de_una_tarea_suelta_sigue_siendo_borrada(make_dh, fake_net):
    """La verificación individual no cambia: su 404 es «borrada en Asana», no
    un error del sync."""
    dh = make_dh()
    fake_net.get(ASANA_HOST, asana_path(f'/tasks/{GID_2}'), FakeResponse(404))
    assert dh.fetch_asana_task_state(GID_2) == {'exists': False, 'completed': None}


def test_elementos_que_no_son_tareas_en_data(make_dh, fake_net):
    """Una lista 'data' con elementos que no son objetos ({'data': [1, 'x']})
    es una respuesta que no se entiende, no un listado."""
    dh = make_dh()
    fake_net.get(ASANA_HOST, LIST, {'data': [1, 'x']})
    with pytest.raises(AsanaFetchError) as caught:
        dh._fetch_asana_assigned('A', 'W')
    assert caught.value.kind == 'bad_response'


def test_un_listado_que_nunca_acaba_se_corta(qtbot, make_dh, fake_net):
    """Un next_page que no se acaba nunca (Asana o un proxy que repite el
    offset) no deja el sync dando vueltas para siempre: tras 200 páginas
    (20 000 tareas a 100 por página) el listado se da por ilegible
    (bad_response) y el sync termina en error sin fusionar nada."""
    dh = make_dh(tasks=[])
    served = []

    def endless(call):
        served.append(call.param('offset'))
        if len(served) > 1000:
            raise AssertionError("el listado no se corta nunca")
        return asana_page([_remote(f'13{len(served):014d}', f"Tarea {len(served)}")],
                          next_offset=f"o{len(served)}")
    fake_net.get(ASANA_HOST, LIST, endless)
    with pytest.raises(AsanaFetchError) as caught:
        dh._fetch_asana_assigned('A', 'W')
    assert caught.value.kind == 'bad_response'
    assert len(served) == 200
    served.clear()
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    assert len(served) == 200
    assert seen.errors == [BAD_JSON] and seen.finished == []
    assert dh.get_tasks() == []


# =====================================================================
# Piezas de core/asana_errors.py
# =====================================================================
@pytest.mark.parametrize("code, kind", [
    (401, 'auth'), (402, 'forbidden'), (403, 'forbidden'), (404, 'not_found'),
    (429, 'rate_limit'), (500, 'server'), (502, 'server'), (503, 'server'),
    (408, 'server'), (451, 'network'), (400, 'bad_response'), (410, 'bad_response'),
    (204, 'bad_response'), (302, 'bad_response')])
def test_tipo_por_codigo_http(code, kind):
    assert kind_for_status(code) == kind


def test_retry_after():
    assert parse_retry_after('120') == 120
    assert parse_retry_after(' 1.5 ') == 1.5
    assert parse_retry_after('-3') == 0
    assert parse_retry_after(None) is None
    assert parse_retry_after('') is None
    assert parse_retry_after('pronto') is None
    assert parse_retry_after('nan') is None
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=300), usegmt=True)
    assert 290 <= parse_retry_after(future) <= 300
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=300), usegmt=True)
    assert parse_retry_after(past) == 0


def test_retry_after_acotado_y_sin_excepciones():
    """Revisión F0: el Retry-After se acota a [0, 3600] s y nunca lanza. Un
    infinito o un número enorme cuenta como el tope (antes, '1e306' pasaba
    tal cual y acababa en OverflowError al programar el reintento); una
    fecha imposible, como no dicha."""
    assert parse_retry_after('1e306') == 3600
    assert parse_retry_after('9' * 400) == 3600            # float: inf
    assert parse_retry_after('inf') == 3600
    assert parse_retry_after('99999999') == 3600
    assert parse_retry_after('-inf') == 0
    assert parse_retry_after('3600') == 3600
    far = format_datetime(datetime.now(timezone.utc) + timedelta(days=30), usegmt=True)
    assert parse_retry_after(far) == 3600
    assert parse_retry_after('Thu, 01 Jan 99999999999 00:00:00 GMT') is None  # OverflowError
    assert parse_retry_after('Mon, 31 Feb 2026 10:00:00 GMT') is None


def test_tipo_desconocido_no_se_admite():
    with pytest.raises(ValueError):
        AsanaFetchError('otro')


# =====================================================================
# Lo que ve el usuario en la app
# =====================================================================
@pytest.fixture
def make_ctrl(qtbot, tmp_path, monkeypatch):
    from core.config import Config
    from ui.theme import fonts
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Config, "WEBHOOK_PORT", 0)   # «Recibir tareas»: un puerto libre
    fonts.init_fonts()
    built = []

    def build(settings=None):
        handler = DataHandler(_write(tmp_path, settings=settings))
        from ui.controllers.app_controller import AppController
        from ui.windows.main_window import MainWindow
        win = MainWindow(handler.data.get('settings', {}))
        qtbot.addWidget(win)
        controller = AppController(win, handler)
        win.show()
        qtbot.waitExposed(win)
        built.append(controller)
        return controller

    yield build
    for controller in built:
        qtbot.waitUntil(lambda: not controller.asana.busy, timeout=5000)
        controller.asana.shutdown()
    qtbot.wait(50)


def test_la_app_muestra_el_error_del_sync(make_ctrl, qtbot, fake_net, monkeypatch):
    """El sync con el token revocado: diálogo «Asana» con el texto, la línea
    de estado con «❌ …» y el icono de sync fuera de «sincronizando» (antes:
    «Sin cambios desde Asana. Confirma el Assignee GID y el workspace.»)."""
    from ui.dialogs import branded_dialogs
    shown = []
    monkeypatch.setattr(branded_dialogs, "info",
                        lambda parent, title, text, *a, **k: shown.append((title, text)))
    fake_net.get(ASANA_HOST, LIST, FakeResponse(401, {'errors': [{'message': 'Not Authorized'}]}))
    ctrl = make_ctrl()
    before = copy.deepcopy(ctrl.data_handler.get_tasks())
    ctrl.sync_asana()
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    qtbot.wait(50)
    assert shown == [("Asana", AUTH)]
    assert ctrl.timer_panel.sync_status.text() == f"❌ {AUTH}"
    assert ctrl.timer_panel._sync_state == 'offline'
    assert ctrl.data_handler.get_tasks() == before


def test_ajustes_cargar_proyectos_muestra_el_error(make_ctrl, qtbot, fake_net):
    """«Cargar proyectos» con un token que Asana rechaza: el motivo, no «No se
    encontraron proyectos en este workspace»."""
    from ui.dialogs.settings_dialog import SettingsDialog
    fake_net.get(ASANA_HOST, asana_path('/projects'), FakeResponse(401))
    ctrl = make_ctrl()
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    dlg.ed_pat.setText('pat-malo')
    dlg.ed_workspace.setText('W')
    dlg._load_projects()
    qtbot.waitUntil(dlg.btn_load_projects.isEnabled, timeout=5000)
    assert dlg.projects_status.text() == f"Error cargando proyectos: {AUTH}"
    assert dlg.projects_status._kind == 'error'
    dlg.reject()


@pytest.mark.parametrize("reply, label", [
    (FakeResponse(401), "● Asana: revisa el token"),
    (FakeResponse(403), "● Asana: revisa la configuración"),
    (FakeResponse(404), "● Asana: revisa la configuración"),
    (FakeResponse(429, headers={'Retry-After': '30'}), "● Asana no responde"),
    (FakeResponse(503), "● Asana no responde"),
    (requests.ConnectionError, "● Sin conexión con Asana"),
], ids=['401', '403', '404', '429', '503', 'sin-red'])
def test_la_etiqueta_de_conexion_dice_lo_que_pasa(make_ctrl, qtbot, fake_net, monkeypatch,
                                                 reply, label):
    """La etiqueta junto al switch dice algo cierto según el tipo de error
    del listado (antes, siempre «● Sin conexión con Asana», también con el
    token revocado o el workspace mal puesto) y vuelve a «● Asana conectado»
    con el siguiente sync correcto."""
    from ui.dialogs import branded_dialogs
    monkeypatch.setattr(branded_dialogs, "info", lambda *args, **kwargs: None)
    fake_net.get(ASANA_HOST, LIST, reply,
                 asana_page([_remote(GID_1, 'Informe'), _remote(GID_2, 'Llamar')]))
    ctrl = make_ctrl()
    connection = ctrl.timer_panel.connection_label
    ctrl.sync_asana()
    qtbot.waitUntil(lambda: not ctrl.asana.busy, timeout=5000)
    qtbot.wait(50)
    assert connection.text() == label
    ctrl.sync_asana()
    qtbot.waitUntil(lambda: len(fake_net.calls) == 2 and not ctrl.asana.busy, timeout=5000)
    qtbot.wait(50)
    assert connection.text() == "● Asana conectado"


def test_un_envio_sin_red_tras_un_401_dice_sin_conexion(qtbot, make_dh, fake_net):
    """El tipo que lee la etiqueta es el del ÚLTIMO error: un envío de la cola
    que se queda sin red después de un 401 del listado ya no es «revisa el
    token»."""
    dh = make_dh()
    fake_net.get(ASANA_HOST, LIST, FakeResponse(401))
    fake_net.put(ASANA_HOST, asana_path(f'/tasks/{GID_1}'), requests.ConnectionError)
    svc, seen = _service(dh)
    _run_sync(qtbot, svc)
    assert svc.last_error_kind == 'auth'
    dh.update_task_status(1, True)
    svc.push_pending()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    assert seen.online == [False, False]
    assert svc.last_error_kind == 'network'
