"""Caracterización del sync con Asana: fija el comportamiento ACTUAL (1.0).

Red de seguridad para las fases que van a tocar el código de red: estos
tests pasan con el código de hoy y describen lo que hace, no lo que debería
hacer. Donde lo actual parece un fallo, el docstring lo marca como
«comportamiento actual, posible bug»: si una fase lo corrige, el test debe
cambiar A LA VEZ y a propósito, nunca por accidente.

Todo pasa por el camino real (AsanaService → DataHandler → requests) contra
una red falsa (tests/fakes/net.py, fixture `fake_net`): una petición que no
esté preparada hace fallar el test.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_asana_caracterizacion.py -q
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.asana_errors import AsanaFetchError  # noqa: E402
from core.data_handler import DataHandler, _new_outbox_op  # noqa: E402
from services.asana_service import AsanaService  # noqa: E402
from tests.fakes.net import ASANA_HOST, FakeResponse, asana_page, asana_path  # noqa: E402

CONFIGURED = {'asana_pat': 'pat-de-prueba', 'asana_assignee_gid': 'A',
              'asana_workspace_gid': 'W'}
LIST = asana_path('/tasks')                         # GET /tasks?assignee&workspace
OPT_FIELDS = ("gid,name,completed,permalink_url,created_at,modified_at,due_on,due_at,"
              "tags.name,projects.name,projects.gid,assignee.gid,parent.gid")
GID_VIVA = '1200000000000001'
GID_BORRADA = '1200000000000404'
NOT_FOUND = FakeResponse(404, {'errors': [{'message': 'Unknown object: 1200000000000404',
                                           'help': 'For more information on API status codes…'}]})


def _gid(i: int) -> str:
    return f"13000000000{i:05d}"


def _remote(gid: str, name: str, completed: bool = False, projects=(), tags=(),
            parent=None) -> dict:
    """Una tarea tal como la devuelve GET /tasks con los opt_fields del sync."""
    return {'gid': gid, 'name': name, 'completed': completed,
            'permalink_url': f'https://app.asana.com/0/0/{gid}',
            'created_at': '2026-11-01T10:00:00.000Z', 'modified_at': '2026-11-02T10:00:00.000Z',
            'due_on': None, 'due_at': None, 'tags': [{'name': t} for t in tags],
            'projects': [{'gid': g, 'name': n} for g, n in projects],
            'assignee': {'gid': 'A'}, 'parent': parent}


def _linked(task_id: int, gid: str, name: str, completed: bool = False, **extra) -> dict:
    return {'id': task_id, 'name': name, 'completed': completed, 'tags': [],
            'source': 'asana', 'asana_id': gid, 'due': None, **extra}


def _write(tmp_path: Path, tasks, settings=None) -> Path:
    data = {'tasks': tasks, 'deleted_tasks': [], 'time_chunks': [], 'history': [],
            'ui_snapshot': {}, 'settings': dict(CONFIGURED if settings is None else settings)}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def make_dh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def build(tasks=(), settings=None) -> DataHandler:
        return DataHandler(_write(tmp_path, list(tasks), settings))
    return build


# Desde el encargo B1, un listado con 429/5xx programa el reintento del sync
# (60 s): el bloque «asana y config en tests» de tests/conftest.py apaga al
# final de cada test los servicios creados, para que ese reintento no salga
# a la red en medio de otro test.
class _Senales:
    """Lo que el servicio cuenta a la UI durante un sync."""

    def __init__(self, svc: AsanaService):
        self.status, self.finished, self.errors = [], [], []
        self.progress, self.online = [], []
        svc.status.connect(self.status.append)
        svc.finished.connect(self.finished.append)
        svc.error.connect(self.errors.append)
        svc.progress.connect(lambda f, text: self.progress.append((round(f, 4), text)))
        svc.connection_state.connect(self.online.append)


def _sync(qtbot, dh: DataHandler) -> tuple:
    svc = AsanaService(dh)
    seen = _Senales(svc)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    return svc, seen


def _by_gid(dh: DataHandler, gid: str) -> dict:
    return next(t for t in dh.get_tasks() if t.get('asana_id') == gid)


def _paths(net) -> list:
    return [(c.method, c.path) for c in net.calls]


# =====================================================================
# (a) Tarea enlazada cuya verificación devuelve 404 → marcada como borrada
# =====================================================================
def test_404_en_la_verificacion_marca_la_tarea_borrada_de_extremo_a_extremo(
        qtbot, make_dh, fake_net, tmp_path):
    """Fija el camino completo de una tarea borrada en Asana:
    1. el listado (GET /tasks) no la trae → es candidata a verificar
       (core/data_handler.py:1795 find_asana_candidates_to_verify, llamada en
       services/asana_service.py:261);
    2. se verifica en el MISMO hilo de red, tras el listado, con
       GET /tasks/{gid}?opt_fields=gid,completed y timeout 10
       (services/asana_service.py:266-276 → core/data_handler.py:324-349);
    3. el 404 da exists=False (core/data_handler.py:343) → deleted;
    4. en el hilo de UI, apply_asana_deleted_marks (services/asana_service.py:311
       → core/data_handler.py:1838-1853) pone asana_deleted=True y AÑADE el tag
       'asana_deleted'; la tarea sigue en la lista (no pasa a deleted_tasks) y
       se guarda en disco.
    Nada se borra ni se envía a Asana (ni PUT ni DELETE).
    Comportamiento actual, posible bug: si el listado trajo alguna tarea, el
    estado final es el genérico «✅ Sincronización completada: 1 tareas.»
    (services/asana_service.py:365) y la eliminación no se menciona; solo se
    anuncia cuando el listado vino vacío (ver el test siguiente)."""
    dh = make_dh([_linked(1, GID_VIVA, 'Sigue viva'), _linked(2, GID_BORRADA, 'Borrada allí')])
    fake_net.get(ASANA_HOST, LIST, asana_page([_remote(GID_VIVA, 'Sigue viva')]))
    fake_net.get(ASANA_HOST, asana_path(f'/tasks/{GID_BORRADA}'), NOT_FOUND)
    svc, seen = _sync(qtbot, dh)

    assert _paths(fake_net) == [('GET', '/api/1.0/tasks'),
                                ('GET', f'/api/1.0/tasks/{GID_BORRADA}')]
    check = fake_net.calls[1]
    assert check.params == {'opt_fields': 'gid,completed'} and check.timeout == 10
    assert check.headers['Authorization'] == 'Bearer pat-de-prueba'

    gone = _by_gid(dh, GID_BORRADA)
    assert gone['asana_deleted'] is True and gone['tags'] == ['asana_deleted']
    assert gone['completed'] is False and gone['name'] == 'Borrada allí'
    assert dh.data['deleted_tasks'] == []
    assert 'asana_deleted' not in _by_gid(dh, GID_VIVA)
    on_disk = DataHandler(tmp_path / "data.json")
    assert _by_gid(on_disk, GID_BORRADA)['asana_deleted'] is True

    assert seen.finished == [1] and seen.errors == [] and seen.online == [True]
    assert seen.status[-1] == "✅ Sincronización completada: 1 tareas."
    assert not any('eliminad' in s for s in seen.status)   # posible bug: no se avisa
    assert ('Verificando tareas… 1/1') in [text for _, text in seen.progress]


def test_404_con_el_listado_vacio_se_anuncia(qtbot, make_dh, fake_net):
    """Fija el aviso cuando el listado no trae nada pero hubo marcas locales
    (services/asana_service.py:323-334): «✅ 1 eliminadas en Asana.» y
    finished(1) para que la UI refresque."""
    dh = make_dh([_linked(2, GID_BORRADA, 'Borrada allí')])
    fake_net.get(ASANA_HOST, LIST, asana_page([]))
    fake_net.get(ASANA_HOST, asana_path(f'/tasks/{GID_BORRADA}'), NOT_FOUND)
    _svc, seen = _sync(qtbot, dh)
    assert _by_gid(dh, GID_BORRADA)['asana_deleted'] is True
    assert seen.status[-1] == "✅ 1 eliminadas en Asana."
    assert seen.finished == [1]


def test_una_marcada_no_se_vuelve_a_verificar_y_si_reaparece_se_desmarca(
        qtbot, make_dh, fake_net):
    """Fija el ciclo de vida de la marca:
    - una tarea ya marcada asana_deleted deja de verificarse
      (core/data_handler.py:1811): el siguiente sync no pide /tasks/{gid} y
      termina en «Sin cambios desde Asana…» (services/asana_service.py:335-343);
    - si vuelve a aparecer en el listado, el merge quita la marca y el tag
      (core/data_handler.py:1760-1763)."""
    dh = make_dh([_linked(2, GID_BORRADA, 'Borrada allí', asana_deleted=True,
                          tags=['asana_deleted'])])
    fake_net.get(ASANA_HOST, LIST, asana_page([]), asana_page([_remote(GID_BORRADA, 'Restaurada')]))
    _svc, seen = _sync(qtbot, dh)
    assert _paths(fake_net) == [('GET', '/api/1.0/tasks')]
    assert seen.status[-1] == ("Sin cambios desde Asana. Confirma el Assignee GID y "
                               "el workspace.")
    assert seen.finished == [0]
    _svc, seen = _sync(qtbot, dh)
    back = _by_gid(dh, GID_BORRADA)
    assert 'asana_deleted' not in back and back['tags'] == []
    assert back['name'] == 'Restaurada'


@pytest.mark.parametrize("reply, deleted, completed", [
    (NOT_FOUND, True, False),
    (FakeResponse(403, {'errors': [{'message': 'Forbidden'}]}), False, False),
    (FakeResponse(500), False, False),
    (requests.ConnectionError, False, False),
    (requests.Timeout, False, False),
    ({'data': {'gid': GID_BORRADA, 'completed': False}}, False, False),
    ({'data': {'gid': GID_BORRADA, 'completed': True}}, False, True),
], ids=['404', '403', '500', 'sin-red', 'timeout', 'abierta', 'completada'])
def test_solo_el_404_marca_borrada(qtbot, make_dh, fake_net, reply, deleted, completed):
    """Fija la tabla de la verificación (core/data_handler.py:340-349 y
    services/asana_service.py:269-276): solo un 404 cuenta como borrada;
    403, 5xx, sin red o timeout se toman por «existe, estado desconocido»; un
    200 con completed=True la marca completada fuera de la app."""
    dh = make_dh([_linked(2, GID_BORRADA, 'Dudosa')])
    fake_net.get(ASANA_HOST, LIST, asana_page([]))
    fake_net.get(ASANA_HOST, asana_path(f'/tasks/{GID_BORRADA}'), reply)
    _svc, seen = _sync(qtbot, dh)
    task = _by_gid(dh, GID_BORRADA)
    assert bool(task.get('asana_deleted')) is deleted
    assert task['completed'] is completed
    assert seen.errors == []


def test_las_completadas_localmente_no_se_verifican(qtbot, make_dh, fake_net):
    """Fija que una tarea enlazada YA completada en la app y ausente del
    listado no se verifica (core/data_handler.py:1812): aunque la borren en
    Asana, nunca recibe la marca (no hay ruta para /tasks/{gid}: si se
    pidiera, el test fallaría)."""
    dh = make_dh([_linked(2, GID_BORRADA, 'Hecha', completed=True)])
    fake_net.get(ASANA_HOST, LIST, asana_page([]))
    _svc, seen = _sync(qtbot, dh)
    assert _paths(fake_net) == [('GET', '/api/1.0/tasks')]
    assert 'asana_deleted' not in _by_gid(dh, GID_BORRADA)
    assert seen.finished == [0]


@pytest.mark.parametrize("code, message", [
    (401, "Asana rechazó el token de acceso (HTTP 401). Revísalo en ⚙ Configuración → Asana."),
    (500, "Asana no responde bien (error 500). Se reintentará en 1 min."),
])
def test_un_error_http_del_listado_va_por_el_camino_de_error(qtbot, make_dh, fake_net,
                                                            code, message):
    """CAMBIADO A PROPÓSITO en el encargo B1 (antes: «se toma por lista
    vacía», el posible bug que fijaba este test). Un error HTTP del listado
    ya no se toma por una lista vacía: _fetch_asana_assigned lanza
    AsanaFetchError (core/data_handler.py:1595-1599, _asana_get_json) y el
    sync termina por el camino de error (services/asana_service.py:434,
    _on_error) con un texto por tipo, connection_state(False) y sin
    finished: nada de «Sin cambios desde Asana…» ni de verificar una a una
    las enlazadas (hay ruta para /tasks/{gid}, pero no se pide)."""
    dh = make_dh([_linked(1, GID_VIVA, 'Una'), _linked(2, GID_BORRADA, 'Otra')])
    fake_net.get(ASANA_HOST, LIST, FakeResponse(code, {'errors': [{'message': 'x'}]}))
    fake_net.get(ASANA_HOST, asana_path(r'/tasks/\d+'), FakeResponse(code))
    _svc, seen = _sync(qtbot, dh)
    assert _paths(fake_net) == [('GET', '/api/1.0/tasks')]
    assert seen.errors == [message] and seen.online == [False] and seen.finished == []
    assert seen.status[-1] == f"❌ {message}"
    assert not any('Sin cambios' in s for s in seen.status)
    assert not any(t.get('asana_deleted') for t in dh.get_tasks())


# =====================================================================
# (b) Merge con más de 30 tareas remotas
# =====================================================================
@pytest.mark.parametrize("total, batches", [
    (30, [30]), (31, [30, 1]), (75, [30, 30, 15])])
def test_el_merge_va_en_lotes_de_30(qtbot, make_dh, fake_net, monkeypatch, tmp_path,
                                    total, batches):
    """El 30 es AsanaService.BATCH_SIZE (services/asana_service.py:37), con
    BATCH_DELAY_MS = 120 entre lotes (:38). Fija que, con N tareas remotas,
    _on_fetched (services/asana_service.py:344-368) llama a
    DataHandler.merge_asana_tasks con trozos de 30 en el orden del listado,
    separados por un QTimer de 120 ms; cada lote se guarda en disco (merge
    hace _save, core/data_handler.py:1793); el servicio sigue `busy` hasta el
    último; el estado avanza «Sincronizando Asana… 30/N» y finished(N) se
    emite UNA vez al final. No hay tope: llegan todas."""
    assert (AsanaService.BATCH_SIZE, AsanaService.BATCH_DELAY_MS) == (30, 120)
    remote = [_remote(_gid(i), f"Remota {i}") for i in range(total)]
    fake_net.get(ASANA_HOST, LIST, asana_page(remote))
    dh = make_dh()
    real_merge = dh.merge_asana_tasks
    svc_box, spy = [], []

    def merge(batch):
        on_disk = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
        spy.append({'size': len(batch), 'first': batch[0]['name'], 'before': len(dh.get_tasks()),
                    'on_disk': len(on_disk['tasks']), 'busy': svc_box[0].busy,
                    't': time.monotonic()})
        real_merge(batch)
    monkeypatch.setattr(dh, 'merge_asana_tasks', merge)
    svc = AsanaService(dh)
    svc_box.append(svc)
    seen = _Senales(svc)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=10000)

    assert [s['size'] for s in spy] == batches
    starts = [sum(batches[:i]) for i in range(len(batches))]
    assert [s['first'] for s in spy] == [f"Remota {i}" for i in starts]
    assert [s['before'] for s in spy] == starts == [s['on_disk'] for s in spy]
    assert all(s['busy'] for s in spy)
    gaps = [b['t'] - a['t'] for a, b in zip(spy, spy[1:])]
    assert all(g >= 0.1 for g in gaps), gaps
    progress = [f"Sincronizando Asana… {sum(batches[:i + 1])}/{total}"
                for i in range(len(batches))]
    assert [s for s in seen.status if s.startswith("Sincronizando Asana… ")] == progress
    assert seen.status[-1] == f"✅ Sincronización completada: {total} tareas."
    assert seen.finished == [total]
    assert seen.progress[-1] == (1.0, f"Aplicando tareas… {total}/{total}")
    assert sorted(t['asana_id'] for t in dh.get_tasks()) == sorted(t['gid'] for t in remote)
    assert len({t['id'] for t in dh.get_tasks()}) == total   # ids únicos entre lotes


def test_merge_directo_no_tiene_tope_de_30(make_dh):
    """Fija que el 30 solo existe en el servicio: DataHandler.merge_asana_tasks
    (core/data_handler.py:1676) fusiona de una vez todo lo que recibe."""
    dh = make_dh()
    dh.merge_asana_tasks([_remote(_gid(i), f"Remota {i}") for i in range(75)])
    assert len(dh.get_tasks()) == 75


def test_cancelar_entre_lotes_deja_el_merge_a_medias(qtbot, make_dh, fake_net, monkeypatch):
    """Fija la cancelación suave a mitad del merge en lotes
    (services/asana_service.py:78-84 y 348-351): lo ya fusionado se queda (y
    está guardado), el resto no llega, el estado acaba en «Sincronización
    cancelada.» y finished NO se emite. El siguiente sync completa sin
    duplicar.
    Comportamiento actual, posible bug: sin finished, el controlador no
    refresca la lista (ui/controllers/app_controller.py:759 solo escucha
    finished), así que las 30 ya fusionadas no se ven hasta otro refresco."""
    remote = [_remote(_gid(i), f"Remota {i}") for i in range(75)]
    fake_net.get(ASANA_HOST, LIST, asana_page(remote))
    dh = make_dh()
    svc = AsanaService(dh)
    real_merge = dh.merge_asana_tasks

    def merge(batch):
        real_merge(batch)
        svc.cancel()            # p. ej. el usuario apaga el switch tras el 1.er lote
    monkeypatch.setattr(dh, 'merge_asana_tasks', merge)
    seen = _Senales(svc)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    assert len(dh.get_tasks()) == 30
    assert seen.status[-1] == "Sincronización cancelada."
    assert seen.finished == []
    monkeypatch.setattr(dh, 'merge_asana_tasks', real_merge)
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=10000)
    assert sorted(t['asana_id'] for t in dh.get_tasks()) == sorted(t['gid'] for t in remote)


# =====================================================================
# (c) Paginación con next_page.offset
# =====================================================================
PAGE_1 = [_remote(_gid(1), 'Uno'), _remote(_gid(2), 'Dos')]
PAGE_2 = [_remote(_gid(3), 'Tres'), _remote(_gid(4), 'Cuatro')]
PAGE_3 = [_remote(_gid(5), 'Cinco')]


def _three_pages(fake_net, path=LIST, pages=(PAGE_1, PAGE_2, PAGE_3), **override):
    """Responde según el offset que pide el código (un offset inesperado →
    KeyError: el test falla). `override` cambia la respuesta de un offset."""
    by_offset = {None: asana_page(list(pages[0]), 'tok-2'),
                 'tok-2': asana_page(list(pages[1]), 'tok-3'),
                 'tok-3': asana_page(list(pages[2]))}
    by_offset.update({k.replace('_', '-'): v for k, v in override.items()})
    return fake_net.get(ASANA_HOST, path, lambda call: by_offset[call.param('offset')])


@pytest.mark.parametrize("include_completed, since", [
    (False, 'now'), (True, '1970-01-01T00:00:00.000Z')])
def test_fetch_asignadas_recorre_tres_paginas(make_dh, fake_net, include_completed, since):
    """Fija _fetch_asana_assigned (core/data_handler.py:1638-1674): pide
    GET /tasks con assignee, workspace, completed_since ('now' = solo
    abiertas; con include_completed, desde 1970), limit 100 y los opt_fields
    del sync, timeout 20; sigue next_page.offset página a página (lo pone en
    el MISMO dict de params, :1658) hasta un next_page nulo, y devuelve todas
    en el orden recibido."""
    dh = make_dh()
    _three_pages(fake_net)
    got = dh._fetch_asana_assigned('A', 'W', include_completed)
    assert [t['name'] for t in got] == ['Uno', 'Dos', 'Tres', 'Cuatro', 'Cinco']
    assert [c.params.get('offset') for c in fake_net.calls] == [None, 'tok-2', 'tok-3']
    base = {'assignee': 'A', 'workspace': 'W', 'completed_since': since, 'limit': 100,
            'opt_fields': OPT_FIELDS}
    for call in fake_net.calls:
        assert {k: v for k, v in call.params.items() if k != 'offset'} == base
        assert call.timeout == 20 and call.headers['Authorization'] == 'Bearer pat-de-prueba'


def test_sync_con_tres_paginas_fusiona_todas_sin_verificar(qtbot, make_dh, fake_net):
    """De extremo a extremo: la enlazada que llega en la 3.ª página cuenta
    como vista (no se verifica: no hay ruta para /tasks/{gid}) y se
    actualiza; las demás se importan."""
    dh = make_dh([_linked(9, _gid(5), 'Nombre viejo')])
    _three_pages(fake_net)
    _svc, seen = _sync(qtbot, dh)
    assert len(fake_net.calls) == 3
    assert sorted(t['name'] for t in dh.get_tasks()) == sorted(
        ['Uno', 'Dos', 'Tres', 'Cuatro', 'Cinco'])
    assert _by_gid(dh, _gid(5))['id'] == 9
    assert seen.finished == [5]


def test_conexiones_paginan_por_workspace_y_filtran_en_local(make_dh, fake_net):
    """Fija fetch_asana_tasks_for_connections (core/data_handler.py:1855-1895):
    un listado paginado por workspace (no por proyecto), filtrado en local
    por los proyectos marcados y sin duplicados."""
    dh = make_dh()
    pages = ([_remote(_gid(1), 'P1 a', projects=[('P1', 'Marketing')]),
              _remote(_gid(2), 'Ajena', projects=[('P9', 'Ventas')])],
             [_remote(_gid(3), 'P2 b', projects=[('P2', 'Web')])],
             [_remote(_gid(1), 'P1 a', projects=[('P1', 'Marketing')])])   # repetida
    _three_pages(fake_net, pages=pages)
    conns = [{'workspace_gid': 'W', 'project_gid': 'P1'},
             {'workspace_gid': 'W', 'project_gid': 'P2'}]
    got = dh.fetch_asana_tasks_for_connections(conns, 'A')
    assert [t['name'] for t in got] == ['P1 a', 'P2 b']
    assert [c.params.get('offset') for c in fake_net.calls] == [None, 'tok-2', 'tok-3']


def test_error_http_en_la_pagina_2_invalida_el_listado(qtbot, make_dh, fake_net):
    """CAMBIADO A PROPÓSITO en el encargo B1 (antes: «devuelve solo la 1», el
    posible bug que fijaba este test: un listado incompleto pasaba por un
    sync correcto). Un error HTTP a mitad de la paginación invalida el
    listado entero (core/data_handler.py:1611, _asana_get_all): no se
    devuelve la página 1, el sync no fusiona nada (ni las de la página 1 ni
    el renombrado de la enlazada), no verifica una a una y avisa del error."""
    dh = make_dh([_linked(9, _gid(5), 'Nombre viejo')])
    _three_pages(fake_net, tok_2=FakeResponse(500, {'errors': [{'message': 'Server Error'}]}))
    with pytest.raises(AsanaFetchError) as caught:
        dh._fetch_asana_assigned('A', 'W')
    assert (caught.value.kind, caught.value.status) == ('server', 500)
    fake_net.calls.clear()
    _svc, seen = _sync(qtbot, dh)
    assert _paths(fake_net) == [('GET', '/api/1.0/tasks'), ('GET', '/api/1.0/tasks')]
    assert [t['name'] for t in dh.get_tasks()] == ['Nombre viejo']
    assert seen.status[-1] == "❌ Asana no responde bien (error 500). Se reintentará en 1 min."
    assert seen.errors == ["Asana no responde bien (error 500). Se reintentará en 1 min."]
    assert seen.finished == []


def test_sin_red_en_la_pagina_2_va_por_el_camino_de_error(qtbot, make_dh, fake_net):
    """Fija que un fallo de RED a mitad de la paginación se propaga (desde
    el encargo B1, como AsanaFetchError('network') con la excepción de
    requests como causa, core/data_handler.py:1590-1594): lo ya descargado
    se descarta, no se fusiona nada y el sync termina en error con
    connection_state(False). El texto pasó de «Error durante la
    sincronización: <excepción>» a «Sin conexión con Asana.»."""
    dh = make_dh()
    _three_pages(fake_net, tok_2=requests.ConnectionError("sin red (simulado)"))
    with pytest.raises(AsanaFetchError) as caught:
        dh._fetch_asana_assigned('A', 'W')
    assert caught.value.kind == 'network'
    assert isinstance(caught.value.__cause__, requests.ConnectionError)
    _svc, seen = _sync(qtbot, dh)
    assert dh.get_tasks() == []
    assert seen.online == [False] and seen.finished == []
    assert seen.errors == ["Sin conexión con Asana."]
    assert seen.status[-1] == "❌ Sin conexión con Asana."


def test_listar_proyectos_recorre_tres_paginas(make_dh, fake_net):
    """Fija list_asana_projects (core/data_handler.py:1132-1177, Ajustes →
    «Cargar proyectos»): GET /projects con workspace, archived=False, limit 100
    y opt_fields gid,name, siguiendo next_page.offset; con pat_override solo
    manda Authorization; descarta los que no traen gid o nombre y ordena por
    nombre sin mayúsculas. CAMBIADO A PROPÓSITO en el encargo B1: un error
    HTTP en la página 2 ya no devuelve la 1 sola (la lista de Ajustes salía
    incompleta sin aviso), lanza AsanaFetchError y Ajustes muestra el
    motivo."""
    dh = make_dh(settings={})
    pages = ([{'gid': 'P3', 'name': 'web'}, {'gid': 'P0', 'name': ''}],
             [{'gid': 'P1', 'name': 'Marketing'}, {'name': 'Sin gid'}],
             [{'gid': 'P2', 'name': 'Ventas'}])
    _three_pages(fake_net, path=asana_path('/projects'), pages=pages)
    got = dh.list_asana_projects('W', pat_override='  pat-temporal ')
    assert [p['gid'] for p in got] == ['P1', 'P2', 'P3']
    assert [c.params.get('offset') for c in fake_net.calls] == [None, 'tok-2', 'tok-3']
    assert {k: v for k, v in fake_net.calls[0].params.items()} == {
        'workspace': 'W', 'archived': False, 'limit': 100, 'opt_fields': 'gid,name'}
    assert dict(fake_net.calls[0].headers) == {'Authorization': 'Bearer pat-temporal'}
    fake_net.calls.clear()
    _three_pages(fake_net, path=asana_path('/projects'), pages=pages, tok_2=500)
    with pytest.raises(AsanaFetchError) as caught:
        dh.list_asana_projects('W', pat_override='p')
    assert (caught.value.kind, caught.value.status) == ('server', 500)


C1 = "🍅 Sesión Pomodoro completada el 13/11 a las 10:00. Tiempo enfocado: 25 minutos."


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _story(text: str, subtype: str = 'comment_added') -> dict:
    return {'gid': 's', 'text': text, 'resource_subtype': subtype, 'created_at': _now_z()}


def test_comprobacion_de_comentario_recorre_tres_paginas(make_dh, fake_net):
    """Fija _asana_comment_lookup (core/data_handler.py:509-555): un
    comentario ya intentado (send_tries > 1, :482) se busca antes de
    publicarlo en GET /tasks/{gid}/stories (opt_fields
    text,created_at,resource_subtype, limit 100) siguiendo next_page.offset;
    si aparece en la 3.ª página no se publica otra vez (ok, 'ya publicado')."""
    dh = make_dh()
    stories = asana_path(f'/tasks/{GID_VIVA}/stories')
    _three_pages(fake_net, path=stories,
                 pages=([_story('otro')], [_story(C1, 'system')], [_story(C1)]))
    op = _new_outbox_op('comment', GID_VIVA, text=C1)
    op['send_tries'] = 2
    (result,) = dh.send_asana_ops([op])
    assert (result['outcome'], result['detail']) == ('ok', 'ya publicado')
    assert _paths(fake_net) == [('GET', f'/api/1.0/tasks/{GID_VIVA}/stories')] * 3
    assert [c.params.get('offset') for c in fake_net.calls] == [None, 'tok-2', 'tok-3']
    assert fake_net.calls[0].params == {'opt_fields': 'text,created_at,resource_subtype',
                                        'limit': 100}


def test_comprobacion_de_comentario_se_rinde_a_las_50_paginas(make_dh, fake_net):
    """Fija el tope de 50 páginas (5000 historias) de la comprobación
    (core/data_handler.py:526): si no aparece, se publica (POST …/stories)."""
    dh = make_dh()
    served = []

    def endless(call):
        served.append(call.param('offset'))
        return asana_page([_story('otro')], next_offset=f"o{len(served)}")
    fake_net.get(ASANA_HOST, asana_path(f'/tasks/{GID_VIVA}/stories'), endless)
    fake_net.post(ASANA_HOST, asana_path(f'/tasks/{GID_VIVA}/stories'),
                  FakeResponse(201, {'data': {'gid': 'story'}}))
    op = _new_outbox_op('comment', GID_VIVA, text=C1)
    op['send_tries'] = 2
    (result,) = dh.send_asana_ops([op])
    assert result['outcome'] == 'ok' and result['detail'] == 'HTTP 201'
    assert fake_net.methods() == ['GET'] * 50 + ['POST']
    assert fake_net.calls[-1].json == {'data': {'text': C1}}


def test_comprobacion_de_alta_recorre_tres_paginas(make_dh, fake_net):
    """Fija _asana_create_lookup (core/data_handler.py:801-837): un alta ya
    intentada (send_tries > 1, :715) se busca antes de crearla otra vez en
    GET /tasks con assignee, workspace, modified_since (2 min antes del alta),
    limit 100 y opt_fields propios, siguiendo next_page.offset; encontrada en
    la 3.ª página, se enlaza sin POST (ok, 'ya creada')."""
    dh = make_dh()
    found = {'gid': _gid(7), 'name': 'Llamar al banco', 'completed': False,
             'created_at': _now_z(), 'projects': []}
    _three_pages(fake_net, pages=([{'gid': _gid(1), 'name': 'Otra', 'projects': []}],
                                  [], [found]))
    op = _new_outbox_op('create', 'local-7', task_id=7, fields={'name': 'Llamar al banco'},
                        workspace='W', assignee='A', projects=[], project_name='')
    op['send_tries'] = 2
    (result,) = dh.send_asana_ops([op])
    assert (result['outcome'], result['detail']) == ('ok', 'ya creada')
    assert result['result']['gid'] == _gid(7) and result['result']['task_id'] == 7
    assert fake_net.methods() == ['GET'] * 3
    assert [c.params.get('offset') for c in fake_net.calls] == [None, 'tok-2', 'tok-3']
    params = fake_net.calls[0].params
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z", params.pop('modified_since'))
    assert params == {'assignee': 'A', 'workspace': 'W', 'limit': 100,
                      'opt_fields': 'gid,name,completed,created_at,projects.gid,projects.name'}


def test_resumen_de_pendientes_recorre_tres_paginas(make_dh, fake_net):
    """Fija get_incomplete_tasks_summary (core/data_handler.py:966-1102; hoy
    no lo llama ningún sitio de la app): nombre del usuario
    (GET /users/{gid}), nombre del proyecto (GET /projects/{gid}) y cuenta de
    abiertas en GET /workspaces/{ws}/tasks/search (projects.any,
    assignee.any, completed=False, limit 100) siguiendo next_page.offset
    (:1029-1043); sin workspace, GET /projects/{gid}/tasks filtrando el
    assignee en local (:1046-1062)."""
    settings = {**CONFIGURED, 'asana_connections': [{'workspace_gid': 'W', 'project_gid': 'P1'}]}
    dh = make_dh(settings=settings)
    fake_net.get(ASANA_HOST, asana_path('/users/A'), {'data': {'name': 'Ana'}})
    fake_net.get(ASANA_HOST, asana_path('/projects/P1'), {'data': {'name': 'Marketing'}})
    _three_pages(fake_net, path=asana_path('/workspaces/W/tasks/search'),
                 pages=([{'gid': '1', 'completed': False}, {'gid': '2', 'completed': True}],
                        [{'gid': '3', 'completed': False}], [{'gid': '4', 'completed': False}]))
    assert dh.get_incomplete_tasks_summary() == {
        'user_name': 'Ana', 'total': 3, 'per_project': {'Marketing': 3}}
    search = fake_net.calls_to('GET', asana_path('/workspaces/W/tasks/search'))
    assert [c.params.get('offset') for c in search] == [None, 'tok-2', 'tok-3']
    assert {k: v for k, v in search[0].params.items() if k != 'offset'} == {
        'projects.any': 'P1', 'assignee.any': 'A', 'completed': False, 'limit': 100,
        'opt_fields': 'gid,completed'}

    # Sin workspace (ni global ni en la conexión): listado del proyecto
    dh.data['settings'].update(asana_workspace_gid='',
                               asana_connections=[{'project_gid': 'P1'}])
    fake_net.calls.clear()
    mine = {'gid': 'x', 'completed': False, 'assignee': {'gid': 'A'}}
    other = {'gid': 'y', 'completed': False, 'assignee': {'gid': 'B'}}
    _three_pages(fake_net, path=asana_path('/projects/P1/tasks'),
                 pages=([mine, other], [dict(mine, completed=True)], [mine]))
    assert dh.get_incomplete_tasks_summary()['total'] == 2
    listing = fake_net.calls_to('GET', asana_path('/projects/P1/tasks'))
    assert [c.params.get('offset') for c in listing] == [None, 'tok-2', 'tok-3']


# =====================================================================
# (d) Mensajes de validate_settings
# =====================================================================
@pytest.mark.parametrize("settings, message", [
    ({}, "Falta el PAT de Asana en Configuración."),
    ({'asana_pat': '   ', 'asana_assignee_gid': 'A', 'asana_workspace_gid': 'W'},
     "Falta el PAT de Asana en Configuración."),
    ({'asana_pat': None}, "Falta el PAT de Asana en Configuración."),
    ({'asana_pat': 'p'}, "Falta el Assignee GID en Configuración."),
    ({'asana_pat': 'p', 'asana_assignee_gid': ' \t'}, "Falta el Assignee GID en Configuración."),
    ({'asana_pat': 'p', 'asana_assignee_gid': 'A'},
     "Debes indicar Workspace GID (o configurar conexiones) en Configuración."),
    ({'asana_pat': 'p', 'asana_assignee_gid': 'A', 'asana_workspace_gid': None,
      'asana_connections': []},
     "Debes indicar Workspace GID (o configurar conexiones) en Configuración."),
    ({'asana_pat': 'p', 'asana_assignee_gid': 'A',
      'asana_connections': [{'workspace_gid': ' '}, {'project_gid': 'P1'}]},
     "Las conexiones de Asana no tienen ningún workspace válido."),
    # Con conexiones, mandan ellas: el workspace global no las salva
    ({'asana_pat': 'p', 'asana_assignee_gid': 'A', 'asana_workspace_gid': 'W',
      'asana_connections': [{'workspace_gid': ''}]},
     "Las conexiones de Asana no tienen ningún workspace válido."),
    ({'asana_pat': 'p', 'asana_assignee_gid': 'A', 'asana_workspace_gid': 'W'}, None),
    ({'asana_pat': 'p', 'asana_assignee_gid': 'A',
      'asana_connections': [{'workspace_gid': ''}, {'workspace_gid': 'W'}]}, None),
], ids=['vacio', 'pat-en-blanco', 'pat-null', 'sin-assignee', 'assignee-en-blanco',
        'sin-workspace', 'workspace-null', 'conexiones-sin-workspace',
        'conexiones-mandan', 'ok-workspace', 'ok-conexiones'])
def test_mensajes_de_validate_settings(make_dh, settings, message):
    """Fija los textos y su orden de prioridad (PAT → Assignee → conexiones o
    workspace) de AsanaService.validate_settings
    (services/asana_service.py:94-110). Los valores se recortan (strip) y un
    null cuenta como vacío. Con conexiones, el workspace global no se mira."""
    svc = AsanaService(make_dh(settings=settings))
    assert svc.validate_settings() == message
    assert svc.is_configured() is (message is None)


def test_sync_con_ajustes_invalidos_avisa_y_no_sale_a_la_red(qtbot, make_dh, fake_net):
    """Fija services/asana_service.py:210-214: sync() con la config incompleta
    emite el estado «❌ <mensaje>» y error(<mensaje>) sin tocar la red (el
    controlador lo muestra en un diálogo «Asana»,
    ui/controllers/app_controller.py:122); push_pending tampoco envía la cola
    (:121), que espera."""
    dh = make_dh([_linked(1, GID_VIVA, 'Una')], settings={'asana_pat': 'p'})
    dh.update_task_status(1, True)
    svc = AsanaService(dh)
    seen = _Senales(svc)
    svc.sync()
    svc.push_pending()
    assert seen.status == ["❌ Falta el Assignee GID en Configuración."]
    assert seen.errors == ["Falta el Assignee GID en Configuración."]
    assert not svc.busy and fake_net.calls == []
    assert dh.pending_asana_ops_count() == 1


@pytest.mark.parametrize("connections, workspace, message", [
    (['W'], '', "Debes indicar Workspace GID (o configurar conexiones) en Configuración."),
    (['W', 7, None], 'W', None),
    ([None, {'workspace_gid': 'W2'}], '', None),
    ({'workspace_gid': 'W'}, '', "Debes indicar Workspace GID (o configurar conexiones) "
                                 "en Configuración."),
], ids=['texto', 'basura-con-workspace', 'una-buena', 'dict-en-vez-de-lista'])
def test_conexion_que_no_es_un_dict_se_ignora(make_dh, connections, workspace, message):
    """CAMBIADO A PROPÓSITO en el encargo «asana» de la revisión F0 (antes:
    test_conexion_que_no_es_un_dict_revienta_la_validacion, que fijaba el
    AttributeError de validate_settings con una entrada de
    asana_connections que no es un dict: JSON editado a mano o backup raro).
    Ahora esas entradas se ignoran, nunca una excepción: sin ninguna
    conexión válida manda el workspace global."""
    svc = AsanaService(make_dh(settings={'asana_pat': 'p', 'asana_assignee_gid': 'A',
                                         'asana_workspace_gid': workspace,
                                         'asana_connections': connections}))
    assert svc.validate_settings() == message


def test_conexiones_que_no_son_un_dict_no_rompen_el_sync(qtbot, make_dh, fake_net):
    """El sync tampoco las recorre: con ['W', 7, None] y el workspace global,
    lista ese workspace como sin conexiones (antes: AttributeError en
    validate_settings y, sin él, en el hilo de red)."""
    dh = make_dh(settings={**CONFIGURED, 'asana_connections': ['W', 7, None]})
    fake_net.get(ASANA_HOST, LIST, asana_page([_remote(GID_VIVA, 'Una')]))
    _svc, seen = _sync(qtbot, dh)
    assert [c.param('workspace') for c in fake_net.calls] == ['W']
    assert seen.errors == [] and seen.finished == [1]


@pytest.mark.parametrize("settings", [
    {'asana_workspace_gid': None, 'asana_connections': [{'workspace_gid': 'W'}]},
    {'asana_workspace_gid': 'W', 'asana_project_gid': None},
], ids=['workspace-null-con-conexiones', 'proyecto-null'])
def test_workspace_null_con_conexiones_sincroniza_sin_atascarse(qtbot, make_dh, fake_net,
                                                                settings):
    """CAMBIADO A PROPÓSITO en el encargo «asana» de la revisión F0 (antes:
    test_workspace_null_con_conexiones_pasa_la_validacion_y_atasca_el_sync,
    que fijaba el fallo). sync() leía settings.get('asana_workspace_gid',
    '').strip() (igual asana_project_gid) y con un null reventaba con
    AttributeError DESPUÉS de poner _in_progress=True: el servicio quedaba
    `busy` para siempre y cada sync posterior solo decía «Sincronización ya
    en curso…». Ahora un null cuenta como vacío, igual que en
    validate_settings: el sync sale a la red, termina y el siguiente también."""
    dh = make_dh(settings={'asana_pat': 'p', 'asana_assignee_gid': 'A', **settings})
    fake_net.get(ASANA_HOST, LIST, asana_page([]))
    svc, seen = _sync(qtbot, dh)
    assert svc.validate_settings() is None
    assert [c.param('workspace') for c in fake_net.calls] == ['W']
    assert seen.errors == [] and seen.finished == [0]
    svc.sync()
    qtbot.waitUntil(lambda: not svc.busy, timeout=5000)
    assert seen.finished == [0, 0]
    assert "Sincronización ya en curso…" not in seen.status
