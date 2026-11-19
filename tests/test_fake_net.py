"""La red de los tests: FakeNet (tests/fakes/net.py) y la guarda que impide
salir a internet (tests/conftest.py).

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_fake_net.py -q
Las URLs «de fuera» son de dominios reservados (example.com) o de la red de
documentación 192.0.2.0/24: aunque la guarda fallara, no llevan a ningún sitio
real. Todo lo que debe llegar a responder lo hace un servidor en 127.0.0.1.
"""
import http.server
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import requests

from tests.fakes.net import (ASANA_HOST, FakeNetError, FakeResponse,
                             RedRealBloqueada, UrlRealBloqueada, asana_page, asana_path,
                             install_fake_net, leaves_machine)

REPO = Path(__file__).resolve().parent.parent
FUERA = "https://api.example.com/v1/cosas"
FUERA_IP = "http://192.0.2.10:9/nada"   # TEST-NET-1: nunca enrutable


# ------------------------------------------------------------ servidor local
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/redirige'):
            self.send_response(302)
            self.send_header('Location', FUERA_IP)
            self.end_headers()
            return
        body = b'hola'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# ------------------------------------------------------------ guarda de red
@pytest.mark.parametrize("url, sale", [
    ("https://app.asana.com/api/1.0/tasks", True),
    ("http://192.0.2.10/x", True),
    ("http://127.0.0.1:8080/x", False),
    ("http://127.5.6.7/x", False),
    ("http://localhost:61968/", False),
    ("http://LOCALHOST/", False),
    ("http://[::1]:8080/x", False),
    ("http://0.0.0.0/x", True),
    ("file:///C:/datos.json", False),
    ("data:text/plain,hola", False),
    ("sin-esquema/ruta", False),     # requests ni lo intenta (MissingSchema)
])
def test_que_cuenta_como_salir_de_la_maquina(url, sale, red_bloqueada):
    assert leaves_machine(url) is sale


@pytest.mark.parametrize("call", [
    lambda: requests.get(FUERA, timeout=1),
    lambda: requests.post(FUERA, json={'a': 1}, timeout=1),
    lambda: requests.put(FUERA, json={}, timeout=1),
    lambda: requests.delete(FUERA, timeout=1),
    lambda: requests.request('PATCH', FUERA, timeout=1),
    lambda: requests.Session().get(FUERA, timeout=1),
], ids=['get', 'post', 'put', 'delete', 'request', 'session'])
def test_la_guarda_bloquea_requests_hacia_fuera(call, red_bloqueada):
    with pytest.raises(RedRealBloqueada, match="Red real bloqueada"):
        call()
    attempts = red_bloqueada.take()   # retirados: este test no debe fallar por ellos
    assert [a.url for a in attempts] == [FUERA]
    assert attempts[0].via == 'requests.Session.request'


def test_el_bloqueo_parece_un_corte_de_red_para_la_app(red_bloqueada):
    """La app captura requests.RequestException/ConnectionError: con la
    guarda se comporta como sin red (y el test falla al terminar)."""
    with pytest.raises(requests.ConnectionError):
        requests.get(FUERA_IP, timeout=1)
    assert issubclass(RedRealBloqueada, requests.RequestException)
    red_bloqueada.take()


def test_la_guarda_bloquea_urllib(red_bloqueada):
    with pytest.raises(UrlRealBloqueada):
        urllib.request.urlopen(FUERA, timeout=1)
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(urllib.request.Request(FUERA_IP, method='POST'), timeout=1)
    with pytest.raises(UrlRealBloqueada):
        urllib.request.build_opener().open(FUERA, timeout=1)
    attempts = red_bloqueada.take()
    assert [(a.method, a.url) for a in attempts] == [
        ('GET', FUERA), ('POST', FUERA_IP), ('GET', FUERA)]


def test_la_guarda_bloquea_httpx(red_bloqueada):
    httpx = pytest.importorskip("httpx")
    with pytest.raises(httpx.ConnectError):
        httpx.get(FUERA, timeout=1)
    assert [a.via for a in red_bloqueada.take()] == ['httpx']


def test_loopback_si_se_permite(local_server, red_bloqueada):
    assert requests.get(f"{local_server}/x", timeout=5).text == 'hola'
    assert requests.Session().get(f"{local_server}/x", timeout=5).status_code == 200
    with urllib.request.urlopen(f"{local_server}/x", timeout=5) as resp:
        assert resp.read() == b'hola'
    assert red_bloqueada.take() == []


def test_una_redireccion_hacia_fuera_tambien_se_bloquea(local_server, red_bloqueada):
    with pytest.raises(RedRealBloqueada):
        requests.get(f"{local_server}/redirige", timeout=5)
    attempts = red_bloqueada.take()
    assert [(a.url, a.via) for a in attempts] == [(FUERA_IP, 'requests.Session.send')]


def test_un_test_que_sale_a_internet_falla(tmp_path, red_bloqueada):
    """De extremo a extremo, en un pytest aparte con los fixtures de
    tests/conftest.py: el test que intenta salir falla aunque la app se
    trague el error; el que usa fake_net con una ruta sin preparar, también."""
    (tmp_path / "conftest.py").write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        from tests.conftest import _guarda_de_red_sesion, red_bloqueada, fake_net  # noqa
    """), encoding="utf-8")
    (tmp_path / "test_hijo.py").write_text(textwrap.dedent(f"""
        import requests

        def test_se_traga_el_error():
            try:
                requests.get({FUERA_IP!r}, timeout=1)
            except requests.RequestException:
                pass                      # como hace la app

        def test_ruta_sin_preparar(fake_net):
            try:
                requests.get("https://app.asana.com/api/1.0/tasks", timeout=1)
            except Exception:
                pass

        def test_limpio(fake_net):
            fake_net.get("app.asana.com", "/api/1\\\\.0/users/me", {{'data': {{}}}})
            assert requests.get("https://app.asana.com/api/1.0/users/me").ok
    """), encoding="utf-8")
    # La codificación, fija en los dos lados: el hijo hereda el entorno (una
    # PYTHONIOENCODING=utf-8, como la de algunas terminales, le hacía
    # escribir en UTF-8) y el padre leía con la del sistema (cp1252):
    # «intentó» llegaba como «intentÃ³» y el test fallaba con la guarda bien
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:qt",
         str(tmp_path / "test_hijo.py")],
        cwd=tmp_path, capture_output=True, encoding="utf-8", errors="replace", timeout=180,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "El test intentó salir a internet" in out and FUERA_IP in out, out
    assert "FakeNet recibió peticiones sin respuesta preparada" in out, out
    assert "3 passed, 2 errors" in out, out


# ------------------------------------------------------------ FakeNet
def test_enruta_por_metodo_host_y_ruta(fake_net):
    fake_net.get(ASANA_HOST, asana_path('/tasks'), {'data': ['lista']})
    fake_net.get(ASANA_HOST, asana_path(r'/tasks/(?P<gid>\d+)'),
                 lambda call: {'data': {'gid': call.match['gid']}})
    fake_net.put(ASANA_HOST, asana_path(r'/tasks/\d+'), FakeResponse(200, {'data': 'put'}))
    fake_net.get('app.clickup.com', r'/api/v2/user', 401)
    base = "https://app.asana.com/api/1.0"
    assert requests.get(f"{base}/tasks").json() == {'data': ['lista']}
    assert requests.get(f"{base}/tasks/42").json() == {'data': {'gid': '42'}}
    assert requests.put(f"{base}/tasks/42", json={}).json() == {'data': 'put'}
    assert requests.get("https://app.clickup.com/api/v2/user").status_code == 401
    # Otro método, otro host o una ruta que solo casa en parte: sin ruta
    for call in (lambda: requests.delete(f"{base}/tasks/42"),
                 lambda: requests.get("https://evil.example/api/1.0/tasks"),
                 lambda: requests.get(f"{base}/tasks/42/stories")):
        with pytest.raises(FakeNetError, match="sin ruta registrada"):
            call()
    assert len(fake_net.errors) == 3
    fake_net.errors.clear()   # provocados a propósito


def test_gana_la_ultima_ruta_registrada(fake_net):
    fake_net.get('*', r'/.*', 500)
    fake_net.get('api.example.com', r'/ok', 204)
    assert requests.get("https://api.example.com/ok").status_code == 204
    assert requests.get("https://otra.example.com/lo-que-sea").status_code == 500


def test_secuencias_y_su_fin(fake_net):
    route = fake_net.get(ASANA_HOST, asana_path('/tasks'),
                         asana_page([1], next_offset='p2'), asana_page([2], 'p3'), asana_page([3]))
    url = "https://app.asana.com/api/1.0/tasks"
    assert [requests.get(url).json()['data'] for _ in range(3)] == [[1], [2], [3]]
    assert route.served == 3
    with pytest.raises(FakeNetError, match="secuencia agotada"):
        requests.get(url)
    fake_net.errors.clear()
    fake_net.post('*', r'/reintento', requests.ConnectionError, 503, 201, repeat_last=True)
    with pytest.raises(requests.ConnectionError, match="simulado por FakeNet"):
        requests.post("https://x.example/reintento")
    assert [requests.post("https://x.example/reintento").status_code
            for _ in range(3)] == [503, 201, 201]


def test_simula_excepciones(fake_net):
    fake_net.get('*', r'/lento', requests.Timeout("se agotó el tiempo (simulado)"))
    fake_net.get('*', r'/caido', requests.ConnectionError)
    with pytest.raises(requests.Timeout, match="simulado"):
        requests.get("https://a.example/lento", timeout=3)
    with pytest.raises(requests.ConnectionError):
        requests.get("https://a.example/caido")
    assert [c.path for c in fake_net.calls] == ['/lento', '/caido']
    assert fake_net.calls[0].timeout == 3


def test_registra_copias_de_lo_enviado(fake_net):
    fake_net.post(ASANA_HOST, asana_path('/tasks'), FakeResponse(201, {'data': {'gid': '1'}}))
    params = {'opt_fields': 'gid', 'offset': None}
    body = {'data': {'name': 'x'}}
    requests.post("https://app.asana.com/api/1.0/tasks?x=1", params=params, json=body,
                  headers={'Authorization': 'Bearer pat'}, timeout=15)
    params['offset'] = 'cambiado después'   # como hace la paginación del código
    body['data']['name'] = 'cambiado'
    (call,) = fake_net.calls_to('POST', asana_path('/tasks'))
    assert call.params == {'opt_fields': 'gid', 'offset': None}
    assert call.json == {'data': {'name': 'x'}}
    assert call.headers['authorization'] == 'Bearer pat'   # sin distinguir mayúsculas
    assert call.query == {'x': '1'} and call.param('x') == '1'
    assert (call.method, call.host, call.path, call.timeout) == (
        'POST', ASANA_HOST, '/api/1.0/tasks', 15)


def test_acepta_los_kwargs_reales_y_rechaza_los_inventados(fake_net):
    fake_net.add('*', '*', r'/.*', 200)
    requests.get("https://a.example/x", params={'a': 1}, headers={}, timeout=5,
                 allow_redirects=False, stream=True, verify=True)
    requests.post("https://a.example/x", data=b'raw', json=None, timeout=(3, 10))
    requests.Session().request('GET', "https://a.example/s", {'p': 1}, None, {'X': 'y'})
    assert fake_net.calls[0].kwargs == {'allow_redirects': False, 'stream': True,
                                        'verify': True}
    assert fake_net.calls[1].data == b'raw' and fake_net.calls[1].timeout == (3, 10)
    assert fake_net.calls[2].params == {'p': 1} and fake_net.calls[2].headers['X'] == 'y'
    with pytest.raises(TypeError, match="inesperado"):
        requests.get("https://a.example/x", tiemout=5)


def test_fake_response():
    resp = FakeResponse(404, {'errors': [{'message': 'Unknown object'}]},
                        headers={'Retry-After': '3'}, url='https://a.example/t')
    assert not resp.ok and resp.reason == 'Not Found'
    assert resp.headers['retry-after'] == '3'
    assert resp.headers['Content-Type'].startswith('application/json')
    assert '"Unknown object"' in resp.text and resp.content == resp.text.encode()
    body = resp.json()
    body['errors'].clear()                  # el código no altera la respuesta guardada
    assert resp.json()['errors']
    with pytest.raises(requests.HTTPError, match="404 Client Error") as err:
        resp.raise_for_status()
    assert err.value.response is resp
    with pytest.raises(requests.HTTPError, match="503 Server Error"):
        FakeResponse(503).raise_for_status()
    FakeResponse(204).raise_for_status()
    texto = FakeResponse(200, text='no es json')
    assert texto.ok and texto.text == 'no es json'
    with pytest.raises(ValueError):          # requests.JSONDecodeError es un ValueError
        texto.json()
    with pytest.raises(requests.exceptions.JSONDecodeError):
        FakeResponse(200).json()
    assert FakeResponse(200, text='{"a": 1}').json() == {'a': 1}


def test_install_fake_net_sin_sesiones(monkeypatch, red_bloqueada):
    net = install_fake_net(monkeypatch, sessions=False)
    net.get('a.example', r'/x', 200)
    assert requests.get("https://a.example/x").status_code == 200
    with pytest.raises(RedRealBloqueada):       # la sesión sigue en manos de la guarda
        requests.Session().get("https://a.example/x", timeout=1)
    red_bloqueada.take()


def test_la_respuesta_desde_un_hilo(fake_net):
    fake_net.get('*', r'/h', 200)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        requests.get("https://a.example/h").status_code)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [200] * 8 and len(fake_net.calls) == 8


# ------------------------------------- los módulos de la app pasan por FakeNet
def test_clickup_pasa_por_fake_net(fake_net):
    from core.clickup_integration import ClickUpClient
    fake_net.get('api.clickup.com', r'/api/v2/user',
                 {'user': {'id': 7, 'username': 'ana', 'email': 'a@b.c'}})
    fake_net.delete('api.clickup.com', r'/api/v2/task/(?P<id>\w+)', 204)
    client = ClickUpClient("pk_prueba")
    assert client.test_connection() == (True, "✅ Conectado como: ana")
    assert client.delete_task('cu1') is True
    assert [(c.method, c.path) for c in fake_net.calls] == [
        ('GET', '/api/v2/user'), ('DELETE', '/api/v2/task/cu1')]
    assert fake_net.calls[0].headers['Authorization'] == 'pk_prueba'


def test_mascota_endpoint_propio_pasa_por_fake_net(fake_net):
    from services import pet_service
    fake_net.post('mi-tunel.example', r'/v1/chat/completions',
                  {'choices': [{'message': {'content': ' ¡Ánimo! '}}]})
    text = pet_service._call_openai_style(
        "https://mi-tunel.example/v1/chat/completions", "k", "llama3", "sys", "user",
        max_tokens_key='max_tokens', timeout=90)
    assert text == '¡Ánimo!'
    (call,) = fake_net.calls
    assert call.json['model'] == 'llama3' and call.timeout == 90
    assert call.headers['Authorization'] == 'Bearer k'


def test_n8n_pasa_por_fake_net(fake_net, monkeypatch):
    from core.config import Config
    from ui.controllers.app_controller import AppController
    monkeypatch.setattr(Config, "N8N_WEBHOOK_URL", "https://n8n.example/webhook/pomo")
    fake_net.post('n8n.example', r'/webhook/pomo', 200)
    AppController.send_data_to_n8n(None, {'evento': 'pomodoro'})   # no usa self
    deadline = time.monotonic() + 5
    while not fake_net.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    (call,) = fake_net.calls
    assert call.json == {'evento': 'pomodoro'} and call.timeout == 10


def test_data_handler_pasa_por_fake_net(fake_net, tmp_path, monkeypatch):
    from core.data_handler import DataHandler
    monkeypatch.chdir(tmp_path)
    dh = DataHandler(tmp_path / "datos.json")
    dh.data['settings']['asana_pat'] = 'pat-de-prueba'
    fake_net.get(ASANA_HOST, asana_path(r'/tasks/(?P<gid>\d+)'),
                 lambda call: {'data': {'gid': call.match['gid'], 'completed': True}})
    assert dh.fetch_asana_task_state('123') == {'exists': True, 'completed': True}
    assert fake_net.calls[0].headers['Authorization'] == 'Bearer pat-de-prueba'
