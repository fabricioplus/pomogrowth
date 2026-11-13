"""Webhook entrante seguro y pestaña «Webhook» de Ajustes honesta.

- Servidor (services/webhook_server.py), real, en 127.0.0.1 y con un puerto
  libre (nunca el 8081), hablado con http.client: solo POST /webhook con
  Content-Type application/json, sin Origin, con Content-Length de 64 KB como
  mucho y un objeto JSON; en 'lan', con X-PomoGrowth-Key; respuestas JSON;
  'local' escucha en 127.0.0.1, 'off' no abre nada y parar libera el puerto.
- Controlador: arranca según `webhook_mode`, rearranca al cambiar modo o
  clave, y lo recibido crea SIEMPRE una tarea local con solo nombre y
  etiquetas saneados (un JSON con source='asana' y asana_id ya no enlaza la
  tarea a Asana, donde la app escribiría con el token del usuario).
- DataHandler: sanea `webhook_mode` y `webhook_key` al cargar.
- Ajustes: «Recibir tareas» (combo, clave solo en red local con Copiar y
  Regenerar, nota con el uso) en lugar de los controles de ngrok, que no
  hacían nada.
- Robustez: un hilo por conexión con tope y plazo total por petición (un
  cliente que gotea no retiene a los demás ni a stop(), que nunca congela la
  interfaz), textos rotos (surrogates sueltos) que ya no impiden guardar, la
  cola que entra con el sync apagado y se vacía al salir, el puerto ocupado
  al arrancar (aviso y reintento al guardar) e «Importar datos».

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_webhook_seguro.py -q
Datos sintéticos en un directorio temporal, nunca los reales. Ningún test
escucha fuera de 127.0.0.1: el modo 'lan' se prueba con `host` o con un
servidor falso (escuchar en 0.0.0.0 haría saltar el aviso del cortafuegos).
"""
import http.client
import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.config import Config  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from services import webhook_server  # noqa: E402
from services.webhook_server import WebhookServer  # noqa: E402
from ui.theme import fonts  # noqa: E402

JSON = {'Content-Type': 'application/json'}
ID1 = 1764282734316


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _request(port, method='POST', path='/webhook', body=None, headers=None):
    """(código, cabeceras en minúsculas, JSON de la respuesta o None)."""
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        heads = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, heads, (json.loads(raw) if raw else None)
    finally:
        conn.close()


def _post(port, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
    return _request(port, body=body, headers={**JSON, **(headers or {})})


def _raw_post(port, headers, body=b''):
    """POST con las cabeceras exactas (http.client pone Content-Length solo)."""
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.putrequest('POST', '/webhook', skip_accept_encoding=True)
        for k, v in headers:
            conn.putheader(k, v)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def _refused(port) -> bool:
    try:
        socket.create_connection(('127.0.0.1', port), timeout=1).close()
    except OSError:
        return True
    return False


@pytest.fixture
def servidor():
    """start(mode, key) → (WebhookServer en marcha, su cola). En 127.0.0.1 y
    con un puerto que elige el sistema; se paran al acabar el test."""
    started = []

    def start(mode='local', key=''):
        q = queue.Queue()
        ws = WebhookServer(q, 0, mode=mode, key=key,
                           host='127.0.0.1' if mode == 'lan' else None)
        ws.start()
        started.append(ws)
        return ws, q
    yield start
    for ws in started:
        ws.stop()


# ======================================================================
# Servidor
# ======================================================================
def test_post_valido_encola_el_objeto_y_responde_json(servidor):
    ws, q = servidor()
    payload = {'name': 'Revisar informe', 'tags': ['n8n']}
    status, heads, body = _post(ws.address[1], payload)
    assert status == 200
    assert heads['content-type'].startswith('application/json')
    assert body == {'status': 'Task received'}
    assert q.get_nowait() == payload and q.empty()
    # Con charset en el Content-Type también vale
    status, _h, _b = _post(ws.address[1], payload,
                           {'Content-Type': 'application/json; charset=utf-8'})
    assert status == 200 and q.get_nowait() == payload


@pytest.mark.parametrize('content_type', [None, 'text/plain',
                                          'application/x-www-form-urlencoded',
                                          'multipart/form-data; boundary=x'])
def test_sin_content_type_json_415(servidor, content_type):
    """Los tipos que un navegador puede mandar sin preflight (y la falta de
    tipo) se rechazan aunque el cuerpo sea JSON."""
    ws, q = servidor()
    headers = {} if content_type is None else {'Content-Type': content_type}
    status, heads, body = _request(ws.address[1], body=b'{"name": "x"}', headers=headers)
    assert status == 415 and heads['content-type'].startswith('application/json')
    assert 'application/json' in body['status']
    assert q.empty()


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'http://127.0.0.1:8081'])
def test_con_origin_403(servidor, origin):
    """Todo POST de un navegador lleva Origin (también con DNS rebinding):
    ninguna web puede crear tareas con un fetch a 127.0.0.1."""
    ws, q = servidor()
    status, heads, _body = _post(ws.address[1], {'name': 'x'}, {'Origin': origin})
    assert status == 403 and heads['content-type'].startswith('application/json')
    assert q.empty()


def test_cuerpo_grande_413_y_64_kb_justos_valen(servidor):
    ws, q = servidor()
    port = ws.address[1]

    def body_of(size):
        head = b'{"name": "Grande", "pad": "'
        return head + b'a' * (size - len(head) - 2) + b'"}'
    exact = body_of(webhook_server.MAX_BODY)
    assert len(exact) == 64 * 1024
    status, _h, _b = _post(port, exact)
    assert status == 200 and q.get_nowait()['name'] == 'Grande'
    status, heads, _body = _post(port, body_of(webhook_server.MAX_BODY + 1))
    assert status == 413 and heads['content-type'].startswith('application/json')
    assert q.empty()


@pytest.mark.parametrize('raw', [b'[{"name": "x"}]', b'"Tarea"', b'42', b'null', b'true',
                                 b'{"name": ', b'\xff\xfe', b''])
def test_cuerpo_que_no_es_un_objeto_json_400(servidor, raw):
    ws, q = servidor()
    status, heads, body = _post(ws.address[1], raw)
    assert status == 400 and heads['content-type'].startswith('application/json')
    assert body['status'].startswith('Bad Request')
    assert q.empty()


@pytest.mark.parametrize('length', [None, 'abc', '-1', '1.5', '²'])
def test_content_length_ausente_o_invalido_400(servidor, length):
    ws, q = servidor()
    headers = [('Content-Type', 'application/json')]
    if length is not None:
        headers.append(('Content-Length', length))
    status, body = _raw_post(ws.address[1], headers)
    assert status == 400 and 'Content-Length' in body['status']
    assert q.empty()


def test_cuerpo_troceado_sin_content_length_400(servidor):
    ws, q = servidor()
    status, body = _raw_post(ws.address[1], [('Content-Type', 'application/json'),
                                             ('Transfer-Encoding', 'chunked')],
                             b'd\r\n{"name": "x"}\r\n0\r\n\r\n')
    assert status == 400 and q.empty()


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'PUT', 'DELETE', 'OPTIONS', 'PATCH', 'TRACE'])
def test_otros_metodos_405(servidor, method):
    ws, q = servidor()
    body = b'{"name": "x"}' if method in ('PUT', 'PATCH') else None
    status, heads, data = _request(ws.address[1], method, body=body,
                                   headers={**JSON} if body else {})
    assert status == 405 and heads.get('allow') == 'POST'
    assert heads['content-type'].startswith('application/json')
    assert data is None if method == 'HEAD' else data == {'status': 'Method Not Allowed'}
    assert q.empty()


def test_options_sin_cabeceras_cors_el_preflight_falla(servidor):
    ws, _q = servidor()
    status, heads, _body = _request(ws.address[1], 'OPTIONS', headers={
        'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type'})
    assert status == 405
    assert not [k for k in heads if k.startswith('access-control-')]


def test_otra_ruta_404(servidor):
    ws, q = servidor()
    for path in ('/', '/webhooks', '/webhook/extra', '/admin'):
        status, heads, _body = _request(ws.address[1], path=path, body=b'{"name": "x"}',
                                        headers=JSON)
        assert status == 404 and heads['content-type'].startswith('application/json'), path
    assert q.empty()


def test_lan_exige_la_clave(servidor):
    key = webhook_server.new_key()
    ws, q = servidor('lan', key)
    port = ws.address[1]
    assert _post(port, {'name': 'x'})[0] == 401                                  # sin clave
    assert _post(port, {'name': 'x'}, {'X-PomoGrowth-Key': 'otra'})[0] == 401    # mala
    assert _post(port, {'name': 'x'}, {'X-PomoGrowth-Key': key[:-1]})[0] == 401
    assert _post(port, {'name': 'x'}, {'X-PomoGrowth-Key': key + 'x'})[0] == 401
    assert _post(port, {'name': 'x'}, {'X-PomoGrowth-Key': 'ñandú'})[0] == 401   # no ASCII
    assert q.empty()
    status, _h, body = _post(port, {'name': 'Desde el portátil'}, {'X-PomoGrowth-Key': key})
    assert (status, body) == (200, {'status': 'Task received'})
    assert q.get_nowait() == {'name': 'Desde el portátil'}
    # El nombre de la cabecera no distingue mayúsculas (HTTP)
    assert _post(port, {'name': 'y'}, {'x-pomogrowth-key': key})[0] == 200


def test_lan_sin_clave_lo_rechaza_todo(servidor):
    ws, q = servidor('lan', '')
    assert _post(ws.address[1], {'name': 'x'}, {'X-PomoGrowth-Key': ''})[0] == 401
    assert _post(ws.address[1], {'name': 'x'})[0] == 401
    assert q.empty()


def test_local_no_pide_clave_y_escucha_solo_en_127_0_0_1(servidor):
    ws, q = servidor('local')
    assert ws.server.server_address[0] == '127.0.0.1'
    assert _post(ws.address[1], {'name': 'x'})[0] == 200


def test_cada_modo_escucha_donde_debe(monkeypatch):
    """Sin `host`, 'local' → 127.0.0.1 y 'lan' → 0.0.0.0 (servidor falso:
    escuchar de verdad en 0.0.0.0 haría saltar el cortafuegos)."""
    seen = []

    class FakeServer:
        def __init__(self, address, webhook_queue, mode, key):
            seen.append((address, mode, key))
            self.server_address = address
    monkeypatch.setattr(webhook_server, '_Server', FakeServer)
    WebhookServer(queue.Queue(), 8081, mode='local')
    WebhookServer(queue.Queue(), 8081, mode='lan', key='k')
    WebhookServer(queue.Queue(), 8081, mode='rara')
    assert seen == [(('127.0.0.1', 8081), 'local', ''), (('0.0.0.0', 8081), 'lan', 'k'),
                    (('127.0.0.1', 8081), 'local', '')]
    assert webhook_server.bind_host('off') is None


def test_off_no_arranca():
    port = _free_port()
    ws = WebhookServer(queue.Queue(), port, mode='off')
    ws.start()
    assert ws.server is None and ws.address is None and not ws.running
    assert _refused(port)
    ws.stop()  # no hace nada ni lanza


def test_parar_libera_el_puerto_para_volver_a_arrancar(servidor):
    ws, _q = servidor()
    port = ws.address[1]
    assert _post(port, {'name': 'x'})[0] == 200
    ws.stop()
    assert not ws.running and ws.server is None
    assert _refused(port)
    again = WebhookServer(queue.Queue(), port, mode='lan', key='k', host='127.0.0.1')
    again.start()
    try:
        assert _post(port, {'name': 'x'}, {'X-PomoGrowth-Key': 'k'})[0] == 200
    finally:
        again.stop()
    ws.stop()  # parar dos veces no lanza


@pytest.mark.skipif(os.name != 'nt', reason="SO_REUSEADDR solo es peligroso en Windows")
def test_en_windows_otro_socket_no_puede_quedarse_con_el_puerto(servidor):
    """Con SO_REUSEADDR (lo que hace HTTPServer), en Windows un segundo
    servidor ataba el mismo puerto y se repartían las peticiones."""
    ws, _q = servidor()
    with pytest.raises(OSError):
        WebhookServer(queue.Queue(), ws.address[1], mode='local')


def test_modos_iguales_en_el_servidor_y_en_los_datos():
    from core.data_handler import WEBHOOK_MODES
    assert WEBHOOK_MODES == webhook_server.MODES
    assert WEBHOOK_MODES[0] == webhook_server.DEFAULT_MODE == 'local'
    assert [k for k, _label in webhook_server.MODE_LABELS] == list(webhook_server.MODES)


def test_task_fields_toma_solo_nombre_y_etiquetas():
    tf = webhook_server.task_fields
    assert tf({'name': '  Hola  ', 'source': 'asana', 'asana_id': '1', 'tags': ['a']}) == (
        'Hola', ['a'])
    assert tf({'task_name': 'Otra'}) == ('Otra', [])
    assert tf({'name': '   ', 'task_name': 'De respaldo'}) == ('De respaldo', [])
    for bad in ([], 'x', 3, None, {'name': 5}, {'name': ''}, {'name': ['x']}, {}):
        assert tf(bad) is None, bad
    name, tags = tf({'name': 'n' * 900, 'tags': ['t' * 90, 'B', 'b', 3, None, '', '  c  ']
                     + [f'x{i}' for i in range(20)]})
    assert name == 'n' * 500
    assert tags == ['t' * 40, 'B', 'c'] + [f'x{i}' for i in range(7)]
    assert tf({'name': 'x', 'tags': 'no-es-lista'}) == ('x', [])


# ======================================================================
# DataHandler: saneado al cargar
# ======================================================================
def _make_data(tmp_path, settings=None) -> Path:
    data = {'tasks': [{'id': ID1, 'name': 'Informe', 'completed': False, 'tags': [],
                       'source': 'local', 'asana_id': None, 'due': None}],
            'deleted_tasks': [], 'time_chunks': [], 'history': [], 'ui_snapshot': {},
            'settings': {'task_sort_mode': 'manual', **(settings or {})}}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_datos_sin_ajustes_de_webhook_quedan_en_local(tmp_path):
    s = DataHandler(_make_data(tmp_path)).load_settings()
    assert (s['webhook_mode'], s['webhook_key']) == ('local', '')
    fresh = DataHandler(tmp_path / "no_existe.json").load_settings()  # instalación nueva
    assert (fresh['webhook_mode'], fresh['webhook_key']) == ('local', '')


@pytest.mark.parametrize('mode, expected', [('local', 'local'), ('lan', 'lan'), ('off', 'off'),
                                            ('LAN', 'local'), ('public', 'local'),
                                            (5, 'local'), (None, 'local'), (['lan'], 'local')])
def test_modo_invalido_vuelve_a_local(tmp_path, mode, expected):
    s = DataHandler(_make_data(tmp_path, {'webhook_mode': mode, 'webhook_key': 'k'})
                    ).load_settings()
    assert s['webhook_mode'] == expected


@pytest.mark.parametrize('key, expected', [('abc', 'abc'), ('', ''), (123, ''), (None, ''),
                                           (['x'], ''), ({'a': 1}, '')])
def test_clave_que_no_es_texto_queda_vacia(tmp_path, key, expected):
    s = DataHandler(_make_data(tmp_path, {'webhook_key': key})).load_settings()
    assert s['webhook_key'] == expected


# ======================================================================
# Controlador
# ======================================================================
class FakeWebhookServer:
    """En lugar del servidor real: apunta con qué se creó, sin abrir puertos."""
    instances = []

    def __init__(self, webhook_queue, port, mode='local', key='', host=None):
        self.queue, self.port, self.mode, self.key = webhook_queue, port, mode, key
        self.started = self.stopped = False
        FakeWebhookServer.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def build(qtbot, tmp_path, monkeypatch):
    """build(settings, real=False, port=None) → AppController sobre datos
    sintéticos. Sin `real`, con FakeWebhookServer; con `real`, el servidor de
    verdad (conftest pone uno falso por defecto) en `port` o en un puerto
    libre de 127.0.0.1 (nunca el 8081)."""
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    FakeWebhookServer.instances = []
    built = []

    def _build(settings=None, real=False, port=None):
        if real:
            monkeypatch.setattr(webhook_server, 'WebhookServer', WebhookServer)
            monkeypatch.setattr(Config, 'WEBHOOK_PORT', port or _free_port())
        else:
            monkeypatch.setattr(webhook_server, 'WebhookServer', FakeWebhookServer)
        dh = DataHandler(_make_data(tmp_path, settings))
        dh.send_asana_ops = lambda ops, *a, **k: [
            {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
             'detail': '', 'offline': False} for op in ops]
        from ui.controllers.app_controller import AppController
        from ui.windows.main_window import MainWindow
        win = MainWindow(dh.data.get('settings', {}))
        qtbot.addWidget(win)
        ctrl = AppController(win, dh)
        ctrl._webhook_timer.stop()  # la cola se procesa a mano en los tests
        win.show()
        qtbot.waitExposed(win)
        built.append(ctrl)
        return ctrl
    yield _build
    for ctrl in built:
        server = ctrl.webhook_server
        if server is not None and not isinstance(server, FakeWebhookServer):
            server.stop()


def _webhook_tasks(ctrl):
    return [t for t in ctrl.data_handler.get_tasks() if t['id'] != ID1]


def test_controlador_arranca_en_local_en_127_0_0_1(build):
    ctrl = build({'webhook_mode': 'local'}, real=True)
    assert ctrl.webhook_server is not None
    assert ctrl.webhook_server.server.server_address[:2] == ('127.0.0.1', Config.WEBHOOK_PORT)


def test_controlador_en_off_no_arranca(build):
    ctrl = build({'webhook_mode': 'off'}, real=True)
    assert ctrl.webhook_server is None
    assert _refused(Config.WEBHOOK_PORT)


def test_controlador_en_lan_usa_la_clave(build):
    ctrl = build({'webhook_mode': 'lan', 'webhook_key': 'clave-guardada'})
    [server] = FakeWebhookServer.instances
    assert (server.port, server.mode, server.key, server.started) == (
        Config.WEBHOOK_PORT, 'lan', 'clave-guardada', True)
    assert ctrl.webhook_server is server


def test_controlador_en_lan_sin_clave_la_crea_y_la_guarda(build):
    ctrl = build({'webhook_mode': 'lan', 'webhook_key': ''})
    key = ctrl.data_handler.load_settings()['webhook_key']
    assert len(key) >= 32
    assert json.loads(ctrl.data_handler.filepath.read_text(encoding='utf-8')
                      )['settings']['webhook_key'] == key
    [server] = FakeWebhookServer.instances
    assert (server.mode, server.key) == ('lan', key)


def test_guardar_ajustes_rearranca_solo_si_cambia_modo_o_clave(build):
    ctrl = build({'webhook_mode': 'local'})
    first = FakeWebhookServer.instances[-1]
    s = ctrl.data_handler.load_settings()
    ctrl.apply_settings(s)  # sin cambios: el mismo servidor
    assert len(FakeWebhookServer.instances) == 1 and not first.stopped

    s['webhook_mode'], s['webhook_key'] = 'lan', 'k1'
    ctrl.apply_settings(s)
    second = FakeWebhookServer.instances[-1]
    assert first.stopped and (second.mode, second.key, second.started) == ('lan', 'k1', True)
    assert ctrl.webhook_server is second

    s['webhook_key'] = 'k2'  # clave regenerada
    ctrl.apply_settings(s)
    third = FakeWebhookServer.instances[-1]
    assert second.stopped and (third.mode, third.key) == ('lan', 'k2')

    s['webhook_mode'] = 'off'
    ctrl.apply_settings(s)
    assert third.stopped and ctrl.webhook_server is None
    assert len(FakeWebhookServer.instances) == 3


def test_guardar_ajustes_rearranca_el_servidor_real_en_el_mismo_puerto(build):
    ctrl = build({'webhook_mode': 'local'}, real=True)
    port = Config.WEBHOOK_PORT
    assert _post(port, {'name': 'x'})[0] == 200
    s = ctrl.data_handler.load_settings()
    s['webhook_mode'] = 'off'
    ctrl.apply_settings(s)
    assert ctrl.webhook_server is None and _refused(port)
    s['webhook_mode'] = 'local'
    ctrl.apply_settings(s)
    assert ctrl.webhook_server.server.server_address[:2] == ('127.0.0.1', port)
    assert _post(port, {'name': 'Otra vez'})[0] == 200


def test_puerto_ocupado_al_guardar_avisa(build, monkeypatch):
    from ui.controllers import app_controller
    warnings = []
    monkeypatch.setattr(app_controller.toast, 'show_warning',
                        lambda win, msg, duration=0: warnings.append(msg))
    ctrl = build({'webhook_mode': 'off'})

    def busy(*_a, **_k):
        raise OSError("puerto ocupado")
    monkeypatch.setattr(webhook_server, 'WebhookServer', busy)
    s = ctrl.data_handler.load_settings()
    s['webhook_mode'] = 'local'
    ctrl.apply_settings(s)
    assert ctrl.webhook_server is None
    assert len(warnings) == 1 and str(Config.WEBHOOK_PORT) in warnings[0]


def test_salir_para_el_servidor(build, monkeypatch):
    from PySide6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication.instance(), 'quit', lambda: None)
    ctrl = build({'webhook_mode': 'local'}, real=True)
    monkeypatch.setattr(ctrl.focus_mode, 'cleanup_system_blocks', lambda: False)
    monkeypatch.setattr(ctrl.model, 'is_running', False)
    port = Config.WEBHOOK_PORT
    ctrl.quit_with_farewell(farewell=False)
    assert ctrl.webhook_server is None and _refused(port)


def test_un_envio_con_source_asana_crea_una_tarea_local_sin_enlace(build):
    """El JSON elegía source y asana_id: al completarla, la app escribía con
    el token del usuario en esa tarea de Asana. Ahora todo llega como local."""
    ctrl = build()
    ctrl.webhook_queue.put({'name': 'Tarea inyectada', 'source': 'asana',
                            'asana_id': '1200000000000001', 'tags': ['x'],
                            'clickup_id': 'abc', 'permalink_url': 'https://app.asana.com/0/1/2',
                            'completed': True, 'due': '2020-01-01', 'id': ID1})
    ctrl._process_webhook_queue()
    [task] = _webhook_tasks(ctrl)
    assert task['name'] == 'Tarea inyectada' and task['tags'] == ['x']
    assert task['source'] == 'local' and task['asana_id'] is None
    assert task['completed'] is False and not task.get('due')
    for key in ('clickup_id', 'permalink_url'):
        assert key not in task
    assert task['id'] != ID1
    # Completarla no encola nada hacia Asana
    ctrl.toggle_task_completion(task['id'])
    assert ctrl.data_handler.data.get('asana_outbox') == []


def test_elementos_raros_no_rompen_y_se_procesan_los_siguientes(build, monkeypatch):
    from ui.controllers import app_controller
    ctrl = build()
    infos = []
    monkeypatch.setattr(app_controller.toast, 'show_info',
                        lambda win, msg, duration=0: infos.append(msg))
    for item in (['lista'], 'texto', 42, None, {'name': 7}, {'tags': ['sin nombre']},
                 {'name': 'Primera'}, {'task_name': 'Segunda', 'tags': 'no-es-lista'}):
        ctrl.webhook_queue.put(item)
    ctrl._process_webhook_queue()
    assert sorted(t['name'] for t in _webhook_tasks(ctrl)) == ['Primera', 'Segunda']
    assert ctrl.webhook_queue.empty()
    assert infos == ["2 tareas recibidas por webhook"]


def test_si_falla_un_alta_sigue_con_las_demas(build, monkeypatch):
    ctrl = build()
    real_add = ctrl.data_handler.add_task

    def flaky(name, **kwargs):
        if name == 'Rompe':
            raise RuntimeError("disco lleno")
        return real_add(name, **kwargs)
    monkeypatch.setattr(ctrl.data_handler, 'add_task', flaky)
    for name in ('Rompe', 'Sigue'):
        ctrl.webhook_queue.put({'name': name})
    ctrl._process_webhook_queue()
    assert [t['name'] for t in _webhook_tasks(ctrl)] == ['Sigue']


def test_nombre_largo_recortado_y_etiquetas_saneadas(build):
    ctrl = build()
    ctrl.webhook_queue.put({'name': '  ' + 'N' * 2000 + '  ',
                            'tags': ['ok', 'T' * 100, 5, {'a': 1}, ['b'], '', '  ',
                                     'OK'] + [f'e{i}' for i in range(30)]})
    ctrl._process_webhook_queue()
    [task] = _webhook_tasks(ctrl)
    assert task['name'] == 'N' * 500
    assert len(task['tags']) == 10 and all(isinstance(t, str) and len(t) <= 40
                                           for t in task['tags'])
    assert task['tags'][:2] == ['ok', 'T' * 40]


def test_con_el_sync_apagado_la_tarea_entra_igual(build):
    """Lo recibido es siempre una tarea local, que no toca Asana: no espera
    al interruptor de sincronización (antes se quedaba en una cola en memoria
    tras responder 200 y se perdía al salir)."""
    ctrl = build()
    ctrl.sync_enabled = False
    ctrl.webhook_queue.put({'name': 'Ya'})
    ctrl._process_webhook_queue()
    assert [t['name'] for t in _webhook_tasks(ctrl)] == ['Ya']
    assert ctrl.webhook_queue.empty()


def test_de_punta_a_punta_un_post_real_crea_una_tarea_local(build):
    ctrl = build({'webhook_mode': 'local'}, real=True)
    status, _h, _b = _post(Config.WEBHOOK_PORT, {'name': 'Por HTTP', 'source': 'asana',
                                                 'asana_id': '99', 'tags': ['n8n']})
    assert status == 200
    ctrl._process_webhook_queue()
    [task] = _webhook_tasks(ctrl)
    assert (task['name'], task['tags'], task['source'], task['asana_id']) == (
        'Por HTTP', ['n8n'], 'local', None)


# ======================================================================
# Ajustes: pestaña «Webhook»
# ======================================================================
def _dialog(qtbot, ctrl):
    from ui.dialogs.settings_dialog import TABS, SettingsDialog
    dlg = SettingsDialog(ctrl.window, ctrl)
    qtbot.addWidget(dlg)
    tab = dlg.tabs.widget([label for _i, label in TABS].index("Webhook"))
    return dlg, tab


def _texts(widget):
    from PySide6.QtWidgets import QLabel
    return [lbl.text() for lbl in widget.findChildren(QLabel)]


def test_pestana_webhook_sin_ngrok_y_con_recibir_tareas(build, qtbot):
    ctrl = build({'ngrok_authtoken': 'tok-viejo', 'ngrok_domain': 'mi.ngrok.app'})
    dlg, tab = _dialog(qtbot, ctrl)
    texts = _texts(tab)
    assert not [t for t in texts if 'ngrok' in t.lower()], texts
    for attr in ('ed_ngrok_token', 'ed_ngrok_domain'):
        assert not hasattr(dlg, attr)
    assert "n8n" in texts and "URL del webhook" in texts      # la salida, igual
    assert "Recibir tareas" in texts and "Servidor de webhooks" in texts
    cb = dlg.cb_webhook_mode
    assert [cb.itemText(i) for i in range(cb.count())] == [
        "Solo este equipo", "Red local, con clave", "Desactivado"]
    assert [cb.itemData(i) for i in range(cb.count())] == ['local', 'lan', 'off']
    assert cb.currentData() == 'local'
    note = next(t for t in texts if 'POST' in t)
    for part in (f"POST http://127.0.0.1:{Config.WEBHOOK_PORT}/webhook", "IP de este equipo",
                 "Content-Type: application/json", "X-PomoGrowth-Key",
                 '{"name": "…", "tags": ["…"]}', "tarea local", "cortafuegos"):
        assert part in note, part
    # Entra también con la sincronización apagada: la nota ya no la pide
    assert "sincronización" not in note
    # La mascota sigue con su endpoint de ngrok (otro uso, intacto)
    pet = dlg.tabs.widget(4)
    assert any('ngrok' in t for t in _texts(pet))
    # Guardar ya no escribe ngrok_*; los valores viejos se quedan sin uso
    dlg.accept_settings()
    s = ctrl.data_handler.load_settings()
    assert (s['ngrok_authtoken'], s['ngrok_domain']) == ('tok-viejo', 'mi.ngrok.app')
    dlg.deleteLater()


def test_la_clave_solo_se_ve_en_red_local_y_se_guarda(build, qtbot):
    ctrl = build({'webhook_mode': 'local', 'webhook_key': ''})
    dlg, _tab = _dialog(qtbot, ctrl)
    dlg.show()
    cb = dlg.cb_webhook_mode
    assert dlg.webhook_key_row.isHidden() and dlg._webhook_key_divider.isHidden()
    assert dlg.ed_webhook_key.text() == ''
    cb.setCurrentIndex(cb.findData('lan'))  # elegir «Red local» crea la clave
    assert not dlg.webhook_key_row.isHidden() and not dlg._webhook_key_divider.isHidden()
    key = dlg.ed_webhook_key.text()
    assert len(key) >= 32 and dlg.ed_webhook_key.isReadOnly()
    assert "clave" in dlg._webhook_mode_row.help.text()
    cb.setCurrentIndex(cb.findData('off'))
    assert dlg.webhook_key_row.isHidden()
    cb.setCurrentIndex(cb.findData('lan'))
    assert dlg.ed_webhook_key.text() == key  # volver no la cambia
    assert ctrl.data_handler.load_settings()['webhook_mode'] == 'local'  # aún sin guardar

    dlg.accept_settings()
    s = ctrl.data_handler.load_settings()
    assert (s['webhook_mode'], s['webhook_key']) == ('lan', key)
    server = FakeWebhookServer.instances[-1]
    assert (server.mode, server.key, server.started) == ('lan', key, True)
    dlg.deleteLater()


@pytest.mark.parametrize('mode', ['local', 'off'])
def test_el_combo_guarda_el_modo(build, qtbot, mode):
    ctrl = build({'webhook_mode': 'lan', 'webhook_key': 'k'})
    dlg, _tab = _dialog(qtbot, ctrl)
    assert dlg.cb_webhook_mode.currentData() == 'lan'
    assert not dlg.webhook_key_row.isHidden()
    dlg.cb_webhook_mode.setCurrentIndex(dlg.cb_webhook_mode.findData(mode))
    dlg.accept_settings()
    s = ctrl.data_handler.load_settings()
    assert (s['webhook_mode'], s['webhook_key']) == (mode, 'k')
    assert ctrl.webhook_server is (None if mode == 'off' else FakeWebhookServer.instances[-1])
    dlg.deleteLater()


def test_copiar_y_regenerar_la_clave(build, qtbot, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    class FakeClipboard:  # el portapapeles de verdad no se toca
        def __init__(self):
            self.value = None

        def setText(self, text):
            self.value = text
    clipboard = FakeClipboard()
    monkeypatch.setattr(QGuiApplication, 'clipboard', staticmethod(lambda: clipboard))
    ctrl = build({'webhook_mode': 'lan', 'webhook_key': 'clave-vieja'})
    dlg, tab = _dialog(qtbot, ctrl)
    dlg.tabs.setCurrentWidget(tab)
    dlg.show()
    qtbot.waitExposed(dlg)
    assert dlg.ed_webhook_key.text() == 'clave-vieja'
    qtbot.mouseClick(dlg.btn_copy_webhook_key, Qt.LeftButton)
    assert clipboard.value == 'clave-vieja'
    assert "copiada" in dlg.webhook_key_status.text()
    qtbot.mouseClick(dlg.btn_new_webhook_key, Qt.LeftButton)
    new = dlg.ed_webhook_key.text()
    assert new != 'clave-vieja' and len(new) >= 32
    assert ctrl.data_handler.load_settings()['webhook_key'] == 'clave-vieja'  # hasta guardar
    dlg.accept_settings()
    assert ctrl.data_handler.load_settings()['webhook_key'] == new
    assert FakeWebhookServer.instances[-1].key == new
    dlg.deleteLater()


def test_ir_a_red_local_y_volver_sin_guardar_no_rearranca(build, qtbot):
    """Elegir «Red local» crea una clave en el campo; volver al modo de antes
    y guardar no rearranca el servidor: la clave solo cuenta en 'lan'."""
    ctrl = build({'webhook_mode': 'local', 'webhook_key': ''})
    first = FakeWebhookServer.instances[-1]
    dlg, _tab = _dialog(qtbot, ctrl)
    cb = dlg.cb_webhook_mode
    cb.setCurrentIndex(cb.findData('lan'))
    assert dlg.ed_webhook_key.text()
    cb.setCurrentIndex(cb.findData('local'))
    dlg.accept_settings()
    assert len(FakeWebhookServer.instances) == 1 and not first.stopped
    assert ctrl.webhook_server is first
    dlg.deleteLater()


def test_importar_datos_aplica_su_webhook_y_la_pestana_lo_muestra(build, qtbot, monkeypatch,
                                                                  tmp_path):
    """«Importar datos» puede traer otro modo y otra clave: el servidor se
    rearranca con ellos (como al guardar) y la pestaña Webhook los muestra,
    así que «Guardar» después no vuelve a escribir los de antes."""
    from ui.dialogs import settings_dialog
    ctrl = build({'webhook_mode': 'local', 'n8n_webhook_url': ''})
    first = FakeWebhookServer.instances[-1]
    dlg, _tab = _dialog(qtbot, ctrl)
    # Copia sintética a importar y el archivo de datos que _import_data
    # respalda antes (relativo al cwd, que build lleva a tmp_path)
    assert Path.cwd() == tmp_path
    (tmp_path / "pomogrowth_data.json").write_text(
        ctrl.data_handler.filepath.read_text(encoding="utf-8"), encoding="utf-8")
    backup = tmp_path / "copia" / "backup.json"
    backup.parent.mkdir()
    backup.write_text(json.dumps({
        'tasks': [{'id': ID1, 'name': 'Del backup', 'completed': False, 'tags': [],
                   'source': 'local', 'asana_id': None, 'due': None}],
        'settings': {'webhook_mode': 'lan', 'webhook_key': 'clave-importada',
                     'n8n_webhook_url': 'https://n8n.example/webhook/importado'}}),
        encoding="utf-8")
    monkeypatch.setattr(settings_dialog.QFileDialog, 'getOpenFileName',
                        staticmethod(lambda *a, **k: (str(backup), "JSON (*.json)")))
    monkeypatch.setattr(settings_dialog, 'confirm', lambda *a, **k: True)
    dlg._import_data()
    assert "importados" in dlg.data_status.text()
    second = FakeWebhookServer.instances[-1]
    assert first.stopped and (second.mode, second.key, second.started) == (
        'lan', 'clave-importada', True)
    assert ctrl.webhook_server is second
    assert dlg.cb_webhook_mode.currentData() == 'lan'
    assert dlg.ed_webhook_key.text() == 'clave-importada'
    assert not dlg.webhook_key_row.isHidden()
    assert dlg.ed_n8n.text() == 'https://n8n.example/webhook/importado'
    dlg.accept_settings()
    s = ctrl.data_handler.load_settings()
    assert (s['webhook_mode'], s['webhook_key'], s['n8n_webhook_url']) == (
        'lan', 'clave-importada', 'https://n8n.example/webhook/importado')
    assert len(FakeWebhookServer.instances) == 2  # guardar no rearranca otra vez
    dlg.deleteLater()


# ======================================================================
# Robustez del servidor: hilos, plazos, tope de conexiones y parada
# ======================================================================
class _Goteo:
    """Un cliente que envía el principio de una petición y después un byte
    cada `every` s (por debajo del timeout de cada lectura, 5 s) durante
    `seconds` s como mucho; luego cierra. Su hilo sigue vivo mientras siga
    conectado y enviando."""

    def __init__(self, port, every=0.3, seconds=4.0,
                 first=b'POST /webhook HTTP/1.1\r\nHost: x\r\nX-Relleno: '):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=10)
        self.sock.sendall(first)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(every, seconds), daemon=True)
        self.thread.start()

    def _run(self, every, seconds):
        end = time.monotonic() + seconds
        try:
            while not self._stop.wait(every) and time.monotonic() < end:
                self.sock.sendall(b'a')
        except OSError:
            pass

    def close(self):
        self._stop.set()
        self.thread.join(5)
        self.sock.close()


def _closed_by_server(sock, timeout) -> bool:
    """True si el servidor cierra la conexión (FIN o reset) antes de `timeout`."""
    sock.settimeout(timeout)
    try:
        return sock.recv(1024) == b''
    except (ConnectionResetError, ConnectionAbortedError):
        return True
    except TimeoutError:
        return False


def test_un_cliente_que_gotea_no_retiene_a_los_demas(servidor):
    """Antes el servidor atendía de una en una y el timeout contaba por cada
    lectura: un equipo de la red sin clave que goteaba cabeceras bloqueaba
    todas las entregas."""
    key = webhook_server.new_key()
    ws, q = servidor('lan', key)
    port = ws.address[1]
    goteo = _Goteo(port, seconds=6.0)  # sin clave y sin terminar nunca
    time.sleep(0.3)  # que el servidor ya lo esté atendiendo
    try:
        t0 = time.monotonic()
        status, _h, _b = _post(port, {'name': 'Legítima'}, {'X-PomoGrowth-Key': key})
        elapsed = time.monotonic() - t0
    finally:
        goteo.close()
    assert status == 200 and elapsed < 2, elapsed
    assert q.get_nowait() == {'name': 'Legítima'}


def test_parar_con_un_cliente_lento_no_bloquea_y_el_puerto_se_reabre(servidor):
    """stop() corre en el hilo de la interfaz (al salir y al guardar
    Ajustes): con un cliente que gotea, la congelaba mientras durara."""
    ws, _q = servidor()
    port = ws.address[1]
    goteo = _Goteo(port, seconds=4.0)
    time.sleep(0.3)
    try:
        t0 = time.monotonic()
        ws.stop()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5, elapsed
        assert not ws.running and ws.server is None
        # El mismo puerto, otra vez, con ese cliente aún colgado
        assert goteo.thread.is_alive()
        q2 = queue.Queue()
        again = WebhookServer(q2, port, mode='local')
        again.start()
        try:
            assert _post(port, {'name': 'Tras rearrancar'})[0] == 200
        finally:
            again.stop()
        assert q2.get_nowait() == {'name': 'Tras rearrancar'}
    finally:
        goteo.close()


def test_el_plazo_total_corta_a_un_cliente_que_gotea(servidor, monkeypatch):
    """REQUEST_TIMEOUT_S cuenta por cada lectura: un byte cada 0,3 s nunca lo
    agota. El plazo total de la petición sí lo corta."""
    monkeypatch.setattr(webhook_server, 'REQUEST_DEADLINE_S', 1.0, raising=False)
    ws, q = servidor()
    goteo = _Goteo(ws.address[1], seconds=8.0)
    t0 = time.monotonic()
    try:
        closed = _closed_by_server(goteo.sock, timeout=5)
        elapsed = time.monotonic() - t0
    finally:
        goteo.close()
    assert closed and elapsed < 3, elapsed
    assert q.empty()


def test_una_conexion_muda_se_corta_por_el_timeout_de_cada_lectura(servidor, monkeypatch):
    """Cada lectura espera como mucho REQUEST_TIMEOUT_S (lo aplica el lector
    del handler en cada recv, no solo _Handler.timeout): una conexión que no
    envía nada se cierra sin esperar al plazo total y deja su sitio."""
    assert 0 < webhook_server.REQUEST_TIMEOUT_S <= webhook_server.REQUEST_DEADLINE_S <= 30
    monkeypatch.setattr(webhook_server, 'REQUEST_TIMEOUT_S', 0.5)
    ws, q = servidor()
    muda = socket.create_connection(('127.0.0.1', ws.address[1]), timeout=5)
    try:
        t0 = time.monotonic()
        assert _closed_by_server(muda, timeout=4)
        assert time.monotonic() - t0 < 2
    finally:
        muda.close()
    assert q.empty()


def test_json_muy_anidado_400(servidor):
    ws, q = servidor()
    body = b'[' * 50_000  # RecursionError en json.loads, por debajo de MAX_BODY
    assert len(body) <= webhook_server.MAX_BODY
    status, _h, data = _post(ws.address[1], body)
    assert status == 400 and data['status'] == 'Bad Request: invalid JSON'
    assert q.empty()


def test_cuerpo_mas_corto_que_content_length_400(servidor):
    ws, q = servidor()
    body = b'{"name": "x"}'
    sock = socket.create_connection(('127.0.0.1', ws.address[1]), timeout=5)
    try:
        sock.sendall(b'POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                     b'Content-Length: %d\r\n\r\n' % (len(body) + 20) + body)
        sock.shutdown(socket.SHUT_WR)
        reply = b''
        while chunk := sock.recv(4096):
            reply += chunk
    finally:
        sock.close()
    assert reply.startswith(b'HTTP/1.0 400') and b'incomplete body' in reply
    assert q.empty()


def test_dos_content_length_400(servidor):
    ws, q = servidor()
    status, body = _raw_post(ws.address[1], [('Content-Type', 'application/json'),
                                             ('Content-Length', '13'),
                                             ('Content-Length', '13')], b'{"name": "x"}')
    assert status == 400 and 'Content-Length' in body['status']
    assert q.empty()


def test_las_conexiones_de_mas_se_cierran_sin_atender(servidor, monkeypatch):
    monkeypatch.setattr(webhook_server, 'MAX_CONNECTIONS', 2, raising=False)
    ws, q = servidor()
    port = ws.address[1]
    mudas = [socket.create_connection(('127.0.0.1', port), timeout=5) for _ in range(2)]
    try:
        time.sleep(0.3)
        extra = socket.create_connection(('127.0.0.1', port), timeout=5)
        try:
            t0 = time.monotonic()
            assert _closed_by_server(extra, timeout=3)
            assert time.monotonic() - t0 < 1.5
        finally:
            extra.close()
    finally:
        for s in mudas:
            s.close()
    # Cerradas las mudas, quedan sitios libres (en cuanto sus hilos lo notan)
    deadline = time.monotonic() + 3
    while True:
        try:
            status = _post(port, {'name': 'Con sitio'})[0]
            break
        except OSError:  # aún sin sitio: cerrada sin atender
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    assert status == 200 and q.get_nowait() == {'name': 'Con sitio'}


def test_lan_con_clave_mala_no_espera_un_cuerpo_grande(servidor):
    """En 'lan', a quien no trae la clave buena no se le leen más que unos
    KB: no se espera a que llegue (ni se lee) un cuerpo de medio mega."""
    key = webhook_server.new_key()
    ws, q = servidor('lan', key)
    sock = socket.create_connection(('127.0.0.1', ws.address[1]), timeout=4)
    try:
        sock.sendall(b'POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                     b'X-PomoGrowth-Key: mala\r\nContent-Length: 500000\r\n\r\n')
        t0 = time.monotonic()
        status_line = sock.makefile('rb').readline()
        elapsed = time.monotonic() - t0
    finally:
        sock.close()
    assert status_line.split()[1] == b'401' and elapsed < 1.5, (status_line, elapsed)
    assert q.empty()


def test_lo_que_termina_de_llegar_tras_parar_no_se_encola(servidor):
    """Al salir, la app vacía la cola después de parar el servidor: lo que
    una conexión ya abierta termine de enviar después recibe 503, no un 200
    que nadie atendería."""
    ws, q = servidor()
    body = json.dumps({'name': 'Tarde'}).encode('utf-8')
    sock = socket.create_connection(('127.0.0.1', ws.address[1]), timeout=4)
    try:
        sock.sendall(b'POST /webhook HTTP/1.1\r\nHost: x\r\n')  # cabeceras a medias
        time.sleep(0.3)
        ws.stop()
        sock.sendall(b'Content-Type: application/json\r\nContent-Length: %d\r\n\r\n'
                     % len(body) + body)
        status_line = sock.makefile('rb').readline()
    finally:
        sock.close()
    assert status_line.split()[1:2] == [b'503'], status_line
    assert q.empty()


# ======================================================================
# Textos rotos: un surrogate UTF-16 suelto no impide guardar
# ======================================================================
# Lo que manda JSON.stringify de un texto cortado a mitad de un emoji
# ("Revisar 😀".slice(0, 9)) y una etiqueta con el otro medio
SURROGATE_BODY = b'{"name": "Revisar \\ud83d", "tags": ["n8n \\udc00", "ok"]}'


def test_task_fields_sin_surrogates_sueltos_ni_caracteres_de_control():
    tf = webhook_server.task_fields
    name, tags = tf(json.loads(SURROGATE_BODY))
    assert (name, tags) == ('Revisar �', ['n8n �', 'ok'])
    for text in [name] + tags:
        text.encode('utf-8')  # ya no lanza UnicodeEncodeError
    assert tf({'name': 'Línea 1\nLínea 2\t\x00\x7f\x85', 'tags': ['a\x1bb', '\r\n']}) == (
        'Línea 1 Línea 2', ['a b'])
    assert tf({'name': '\x00\n\t', 'task_name': 'De respaldo'}) == ('De respaldo', [])
    assert tf({'name': '\x00\n\t'}) is None
    # Un emoji entero (o su par de surrogates escapado en el JSON) no se toca
    assert tf({'name': 'Hecho \U0001F600', 'tags': ['ñandú']}) == ('Hecho \U0001F600', ['ñandú'])
    assert tf(json.loads('{"name": "Par \\ud83d\\ude00"}')) == ('Par \U0001F600', [])


def _session_survives(path, expected_names):
    """La sesión entera está en disco: tareas, ajuste e histórico."""
    again = DataHandler(path)
    assert again.load_error is None
    assert [t['name'] for t in again.get_tasks()] == expected_names
    assert again.load_settings()['pomodoro_min'] == 50
    assert [h['action'] for h in again.data['history']].count('add_task') == 2


def test_un_post_con_un_surrogate_suelto_no_rompe_los_guardados(servidor, tmp_path):
    """Antes la tarea entraba en memoria con el surrogate y desde ahí TODOS
    los guardados de la sesión fallaban en silencio (UnicodeEncodeError):
    al reiniciar se había perdido todo lo de esa sesión."""
    ws, q = servidor()
    assert _post(ws.address[1], SURROGATE_BODY)[0] == 200
    path = _make_data(tmp_path)
    dh = DataHandler(path)
    name, tags = webhook_server.task_fields(q.get_nowait())
    dh.add_task(name=name, tags=tags, source='local')  # = _process_webhook_queue
    dh.add_task(name='A mano, después', tags=[], source='local')
    s = dh.load_settings()
    s['pomodoro_min'] = 50
    dh.save_settings(s)
    _session_survives(path, ['A mano, después', 'Revisar �', 'Informe'])


def test_un_surrogate_suelto_de_otro_origen_tampoco_envenena_la_sesion(tmp_path):
    """Red de seguridad (utils/safe_json): venga de donde venga el texto roto,
    el JSON se escribe escapado (ensure_ascii) y se vuelve a cargar igual."""
    path = _make_data(tmp_path)
    dh = DataHandler(path)
    dh.add_task(name='Roto \ud83d', tags=['x\udc00'], source='local')
    dh.add_task(name='A mano, después', tags=[], source='local')
    s = dh.load_settings()
    s['pomodoro_min'] = 50
    dh.save_settings(s)
    _session_survives(path, ['A mano, después', 'Roto \ud83d', 'Informe'])
    json.loads(path.read_text(encoding='utf-8'))  # JSON válido


def test_safe_json_reintenta_escapado_solo_si_hace_falta(tmp_path):
    from utils.safe_json import load_json_safe, save_json_atomic
    path = tmp_path / "datos.json"
    assert save_json_atomic(path, {'nombre': 'ñandú'})
    assert 'ñandú' in path.read_text(encoding='utf-8')  # legible, como siempre
    assert save_json_atomic(path, {'nombre': 'ñandú \ud83d'})
    assert load_json_safe(path) == ({'nombre': 'ñandú \ud83d'}, None)
    assert not (tmp_path / "datos.json.tmp").exists()


# ======================================================================
# Controlador: salida y puerto ocupado
# ======================================================================
def test_salir_crea_las_tareas_que_quedaban_en_la_cola(build, monkeypatch):
    """Lo que llegó entre el último repaso de la cola y la parada ya recibió
    un 200: al salir se crea (sin refrescar la lista ni avisar)."""
    from PySide6.QtWidgets import QApplication

    from ui.controllers import app_controller
    monkeypatch.setattr(QApplication.instance(), 'quit', lambda: None)
    infos = []
    monkeypatch.setattr(app_controller.toast, 'show_info',
                        lambda win, msg, duration=0: infos.append(msg))
    ctrl = build({'webhook_mode': 'local'}, real=True)
    monkeypatch.setattr(ctrl.focus_mode, 'cleanup_system_blocks', lambda: False)
    monkeypatch.setattr(ctrl.model, 'is_running', False)
    ctrl.sync_enabled = False
    assert _post(Config.WEBHOOK_PORT, {'name': 'Justo antes de salir', 'tags': ['n8n']})[0] == 200
    ctrl.quit_with_farewell(farewell=False)
    assert ctrl.webhook_server is None and ctrl.webhook_queue.empty()
    assert [t['name'] for t in _webhook_tasks(ctrl)] == ['Justo antes de salir']
    on_disk = DataHandler(ctrl.data_handler.filepath).get_tasks()
    assert 'Justo antes de salir' in [t['name'] for t in on_disk]
    assert not [m for m in infos if 'webhook' in m]


def test_puerto_ocupado_al_arrancar_avisa_y_guardar_lo_reintenta(build, qtbot, monkeypatch):
    """Antes: solo un print (el exe no tiene consola) y «Guardar» con el
    mismo modo no reintentaba nunca."""
    from ui.controllers import app_controller
    warnings = []
    monkeypatch.setattr(app_controller.toast, 'show_warning',
                        lambda win, msg, duration=0: warnings.append(msg))
    busy = socket.socket()  # otro programa con el puerto
    try:
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        port = busy.getsockname()[1]
        ctrl = build({'webhook_mode': 'local'}, real=True, port=port)
        assert ctrl.webhook_server is None
        # El aviso, cuando la ventana principal ya se ve
        qtbot.waitUntil(lambda: len(warnings) == 1, timeout=3000)
        assert str(port) in warnings[0]
    finally:
        busy.close()
    # El otro programa soltó el puerto: «Guardar» sin cambios lo reintenta
    ctrl.apply_settings(ctrl.data_handler.load_settings())
    assert ctrl.webhook_server is not None
    assert ctrl.webhook_server.address == ('127.0.0.1', port)
    assert _post(port, {'name': 'Por fin'})[0] == 200
    assert len(warnings) == 1


def test_tras_fallar_el_puerto_al_guardar_guardar_otra_vez_lo_reintenta(build, monkeypatch):
    from ui.controllers import app_controller
    warnings = []
    monkeypatch.setattr(app_controller.toast, 'show_warning',
                        lambda win, msg, duration=0: warnings.append(msg))
    ctrl = build({'webhook_mode': 'off'})
    attempts = []

    class BusyOnce(FakeWebhookServer):
        def __init__(self, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("puerto ocupado")
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(webhook_server, 'WebhookServer', BusyOnce)
    s = ctrl.data_handler.load_settings()
    s['webhook_mode'] = 'local'
    ctrl.apply_settings(s)
    assert ctrl.webhook_server is None and len(warnings) == 1
    ctrl.apply_settings(s)  # el otro programa ya lo soltó
    assert len(attempts) == 2 and len(warnings) == 1
    assert ctrl.webhook_server is FakeWebhookServer.instances[-1]
    assert ctrl.webhook_server.started
    ctrl.apply_settings(s)  # en marcha y sin cambios: nada
    assert len(attempts) == 2


# ======================================================================
# conftest: ningún test escucha en el 8081
# ======================================================================
_LEFT_RUNNING = []


def test_conftest_ningun_test_usa_el_puerto_de_la_app():
    """conftest («# --- webhook en tests ---»): el controlador recibe un
    servidor falso, el puerto por defecto es 0 (uno libre) y un servidor real
    que el test deje en marcha se para al acabar (lo mira el siguiente)."""
    assert Config.WEBHOOK_PORT == 0
    assert webhook_server.WebhookServer is not WebhookServer  # el falso
    ws = WebhookServer(queue.Queue(), Config.WEBHOOK_PORT)
    ws.start()
    assert ws.running and ws.address[1] not in (0, 8081)
    _LEFT_RUNNING.append(ws)  # sin stop()


def test_conftest_paro_el_servidor_que_quedo_en_marcha():
    if not _LEFT_RUNNING:
        pytest.skip("va detrás de test_conftest_ningun_test_usa_el_puerto_de_la_app")
    ws = _LEFT_RUNNING.pop()
    assert not ws.running and ws.server is None
