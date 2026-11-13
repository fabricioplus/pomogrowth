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

Todo pasa por el camino real (AsanaService → DataHandler → requests) contra
la red falsa (tests/fakes/net.py, fixture `fake_net`): una petición sin ruta
preparada (p. ej. verificar una tarea suelta) hace fallar el test.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_asana_errores.py -q
"""
import copy
import json
import os
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


# Servicios creados en el test en curso (ver _sin_reintentos_colgados)
_SERVICIOS = []


@pytest.fixture(autouse=True)
def _sin_reintentos_colgados():
    """Ningún reintento programado sobrevive al test: el AsanaService de un
    test no se libera solo (su temporizador lo retiene) y un reintento que
    venciera más tarde saldría a la red en medio de otro test."""
    yield
    while _SERVICIOS:
        _SERVICIOS.pop().shutdown()


def _service(dh: DataHandler, push: bool = True, delays=None) -> tuple:
    svc = AsanaService(dh)
    _SERVICIOS.append(svc)
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


def test_tipo_desconocido_no_se_admite():
    with pytest.raises(ValueError):
        AsanaFetchError('otro')


# =====================================================================
# Lo que ve el usuario en la app
# =====================================================================
@pytest.fixture
def make_ctrl(qtbot, tmp_path, monkeypatch):
    from ui.theme import fonts
    monkeypatch.chdir(tmp_path)
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
