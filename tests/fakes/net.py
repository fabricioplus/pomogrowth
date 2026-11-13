"""Red falsa para los tests y guarda que impide salir a internet.

FakeNet
-------
Enrutador de peticiones HTTP en memoria: responde según rutas registradas
por método + host + ruta (expresión regular) y apunta cada llamada para
hacer asserts sobre ella.

    net = FakeNet()
    net.add('GET', ASANA_HOST, asana_path('/tasks'),
            asana_page([...], next_offset='p2'), asana_page([...]))   # secuencia
    net.add('GET', ASANA_HOST, asana_path(r'/tasks/(?P<gid>\\d+)'), 404)
    net.install(monkeypatch)
    ...
    assert net.calls[0].params['workspace'] == 'W'

En un test basta con pedir el fixture `fake_net` (tests/conftest.py): lo
instala y, al terminar, hace fallar el test si alguna petición no casó con
ninguna ruta o agotó su secuencia (el código de la app suele tragarse las
excepciones de red, así que el error por sí solo podría pasar inadvertido).

Rutas: el método se compara sin mayúsculas ('*' = cualquiera); el host,
también ('*' = cualquiera); la ruta es una expresión regular que debe casar
con la ruta COMPLETA de la URL (re.fullmatch, sin la query: los parámetros
llegan en `call.params`). Si varias rutas casan, gana la ÚLTIMA registrada:
un test puede pisar la respuesta por defecto que puso un fixture.

Respuestas que admite una ruta:
- FakeResponse(status_code, json_data, text, headers)
- un dict o una lista: 200 con ese JSON
- un int: ese código HTTP, sin cuerpo
- una excepción (instancia o clase): se lanza (requests.Timeout,
  requests.ConnectionError…)
- una función `f(call)` que devuelve cualquiera de las anteriores (o lanza);
  `call.match` trae los grupos de la regex de la ruta
Con varias respuestas, se sirven una por petición y en orden (paginación,
reintentos); agotada la secuencia, la siguiente petición es un error claro
(salvo `repeat_last=True`, que repite la última). Con una sola respuesta,
esa contesta siempre.

Cómo usa `requests` el código (comprobado a mano):
- core/data_handler.py: `import requests` a nivel de módulo (en un try) y
  `requests.get/put/post/delete(url, headers=, params=, json=, timeout=)`;
  captura `requests.RequestException`.
- services/asana_service.py: no importa requests; todo va por DataHandler.
- core/clickup_integration.py: `import requests` a nivel de módulo;
  `requests.get/post/put/delete(url, headers=, params=, json=, timeout=)`.
- services/pet_service.py: `import requests` dentro de la función;
  `requests.post(url, headers=, json=, timeout=)`. El proveedor Claude usa
  el SDK `anthropic`, que va por httpx (no pasa por FakeNet; la guarda sí
  lo bloquea).
- ui/controllers/app_controller.py (n8n): `import requests` dentro del
  hilo; `requests.post(url, json=, timeout=)`.
- ui/dialogs/settings_dialog.py («Probar conexión»): `requests.get(url,
  headers=, timeout=)` y `raise_for_status()`.
Todos leen el atributo del módulo `requests` en el momento de la llamada:
basta con sustituir `requests.get`, `requests.post`… en el propio módulo
(es el mismo objeto que `data_handler_mod.requests`). `install()` también
cubre `requests.Session.request` (services/music_resolver.py usa sesiones).

GuardaDeRed
-----------
tests/conftest.py la instala para toda la suite: cualquier petición real
fuera de loopback (127.0.0.0/8, localhost, ::1) falla al momento y además
hace fallar el test que la hizo.
"""
import copy
import http
import ipaddress
import json as _json
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Pattern, Union
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.structures import CaseInsensitiveDict

ASANA_HOST = 'app.asana.com'


def asana_path(path: str) -> str:
    """Regex de una ruta de la API de Asana: asana_path(r'/tasks/\\d+')."""
    return r'/api/1\.0' + path


def asana_page(data: List[Any], next_offset: Optional[str] = None) -> Dict[str, Any]:
    """Cuerpo de un listado de Asana: {'data': [...], 'next_page': {...}|None}."""
    next_page = None
    if next_offset:
        next_page = {'offset': next_offset, 'path': f'/tasks?offset={next_offset}',
                     'uri': f'https://{ASANA_HOST}/api/1.0/tasks?offset={next_offset}'}
    return {'data': data, 'next_page': next_page}


class FakeNetError(AssertionError):
    """Petición sin ruta registrada, o secuencia de respuestas agotada."""


def _reason(code: int) -> str:
    try:
        return http.HTTPStatus(code).phrase
    except ValueError:
        return ''


class FakeResponse:
    """Lo que usa el código de una requests.Response: status_code, .json(),
    .text, .headers, .ok, raise_for_status(), .content, .url."""

    def __init__(self, status_code: int = 200, json_data: Any = None,
                 text: Optional[str] = None, headers: Optional[Dict[str, str]] = None,
                 *, url: str = '', reason: Optional[str] = None):
        self.status_code = int(status_code)
        self._json = json_data
        if text is None:
            text = _json.dumps(json_data, ensure_ascii=False) if json_data is not None else ''
        self.text = text
        self.headers = CaseInsensitiveDict(headers or {})
        if json_data is not None and 'Content-Type' not in self.headers:
            self.headers['Content-Type'] = 'application/json; charset=UTF-8'
        self.url = url
        self.reason = reason if reason is not None else _reason(self.status_code)
        self.encoding = 'utf-8'

    @property
    def content(self) -> bytes:
        return self.text.encode('utf-8')

    @property
    def ok(self) -> bool:
        return not 400 <= self.status_code < 600

    def json(self, **kwargs) -> Any:
        if self._json is not None:
            return copy.deepcopy(self._json)  # que el código no altere la respuesta guardada
        try:
            return _json.loads(self.text, **kwargs)
        except ValueError as e:
            # Como requests: su JSONDecodeError también es un ValueError
            raise requests.exceptions.JSONDecodeError(e.msg, e.doc, e.pos) from None

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 500:
            kind = 'Client Error'
        elif 500 <= self.status_code < 600:
            kind = 'Server Error'
        else:
            return
        raise requests.HTTPError(f"{self.status_code} {kind}: {self.reason} for url: {self.url}",
                                 response=self)

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        data = self.text if decode_unicode else self.content
        size = chunk_size or len(data) or 1
        for i in range(0, len(data), size):
            yield data[i:i + size]

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _for(self, url: str) -> 'FakeResponse':
        """Copia para una llamada concreta (una misma respuesta puede servir
        a varias, también desde hilos distintos)."""
        resp = copy.copy(self)
        resp.headers = CaseInsensitiveDict(self.headers)
        resp.url = url
        return resp

    def __repr__(self) -> str:
        return f"<FakeResponse [{self.status_code}]>"


Reply = Union[FakeResponse, Dict[str, Any], List[Any], int, BaseException, type, Callable]


@dataclass
class Call:
    """Una petición recibida. params/json/data/headers son COPIAS tomadas en
    el momento de la llamada (el código reutiliza su dict de params entre
    páginas)."""
    method: str
    url: str
    params: Any = None
    json: Any = None
    data: Any = None
    headers: CaseInsensitiveDict = field(default_factory=CaseInsensitiveDict)
    timeout: Any = None
    kwargs: Dict[str, Any] = field(default_factory=dict)   # allow_redirects, stream…
    match: Optional[re.Match] = None

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or '').lower()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def query(self) -> Dict[str, str]:
        """Parámetros que venían escritos en la propia URL."""
        return dict(parse_qsl(urlsplit(self.url).query))

    def param(self, name: str, default: Any = None) -> Any:
        """Un parámetro, venga en `params` o en la query de la URL."""
        if isinstance(self.params, dict) and name in self.params:
            return self.params[name]
        return self.query.get(name, default)


class _Route:
    def __init__(self, method: str, host: str, path: str, replies: List[Reply],
                 repeat_last: bool):
        self.method = method.upper()
        self.host = host.lower()
        self.path: Pattern = re.compile(path)
        self.replies = list(replies)
        self.repeat_last = repeat_last
        self.served = 0

    def match(self, method: str, host: str, path: str) -> Optional[re.Match]:
        if self.method != '*' and self.method != method:
            return None
        if self.host != '*' and self.host != host:
            return None
        return self.path.fullmatch(path)

    def take(self) -> Optional[Reply]:
        """La respuesta que toca; None si la secuencia está agotada."""
        if self.served < len(self.replies):
            reply = self.replies[self.served]
        elif self.repeat_last and self.replies:
            reply = self.replies[-1]
        else:
            return None
        self.served += 1
        return reply

    def __repr__(self) -> str:
        return f"{self.method} {self.host} {self.path.pattern}"


# Los kwargs que acepta requests (Session.request); otro nombre es un error
# también con el requests de verdad (TypeError)
_REQUEST_KWARGS = ('params', 'data', 'headers', 'cookies', 'files', 'auth', 'timeout',
                   'allow_redirects', 'proxies', 'hooks', 'stream', 'verify', 'cert', 'json')


def _snapshot(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:  # archivos abiertos, generadores…: tal cual
        return value


class FakeNet:
    def __init__(self) -> None:
        self._routes: List[_Route] = []
        self.calls: List[Call] = []
        # Peticiones sin ruta o con la secuencia agotada (el fixture `fake_net`
        # hace fallar el test si queda alguna)
        self.errors: List[str] = []
        self._lock = threading.RLock()

    # ---------------- rutas ----------------
    def add(self, method: str, host: str, path: str, *replies: Reply,
            repeat_last: Optional[bool] = None) -> _Route:
        """Registra una ruta. Una respuesta: contesta siempre. Varias: una
        por petición, en orden (agotadas, error; o la última si repeat_last)."""
        if not replies:
            raise ValueError("FakeNet.add: falta al menos una respuesta")
        if repeat_last is None:
            repeat_last = len(replies) == 1
        route = _Route(method, host, path, list(replies), repeat_last)
        with self._lock:
            self._routes.append(route)
        return route

    def get(self, host: str, path: str, *replies: Reply, **kw) -> _Route:
        return self.add('GET', host, path, *replies, **kw)

    def post(self, host: str, path: str, *replies: Reply, **kw) -> _Route:
        return self.add('POST', host, path, *replies, **kw)

    def put(self, host: str, path: str, *replies: Reply, **kw) -> _Route:
        return self.add('PUT', host, path, *replies, **kw)

    def delete(self, host: str, path: str, *replies: Reply, **kw) -> _Route:
        return self.add('DELETE', host, path, *replies, **kw)

    # ---------------- consultas ----------------
    def calls_to(self, method: Optional[str] = None, path: Optional[str] = None,
                 host: Optional[str] = None) -> List[Call]:
        """Llamadas filtradas por método, host y/o regex de ruta (fullmatch)."""
        with self._lock:
            calls = list(self.calls)
        return [c for c in calls
                if (method is None or c.method == method.upper())
                and (host is None or c.host == host.lower())
                and (path is None or re.fullmatch(path, c.path))]

    def methods(self) -> List[str]:
        with self._lock:
            return [c.method for c in self.calls]

    # ---------------- la petición ----------------
    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        unknown = set(kwargs) - set(_REQUEST_KWARGS)
        if unknown:
            raise TypeError(f"FakeNet: argumento inesperado {sorted(unknown)} "
                            f"(requests también lo rechazaría)")
        method = str(method).upper()
        url = url.decode('utf-8') if isinstance(url, bytes) else str(url)
        parts = urlsplit(url)
        host, path = (parts.hostname or '').lower(), parts.path
        extra = {k: v for k, v in kwargs.items()
                 if k not in ('params', 'json', 'data', 'headers', 'timeout')}
        call = Call(method=method, url=url, params=_snapshot(kwargs.get('params')),
                    json=_snapshot(kwargs.get('json')), data=_snapshot(kwargs.get('data')),
                    headers=CaseInsensitiveDict(kwargs.get('headers') or {}),
                    timeout=kwargs.get('timeout'), kwargs=extra)
        with self._lock:
            self.calls.append(call)
            route = reply = None
            for candidate in reversed(self._routes):
                found = candidate.match(method, host, path)
                if found is not None:
                    route, call.match = candidate, found
                    break
            if route is None:
                msg = (f"FakeNet: petición sin ruta registrada: {method} {url}\n"
                       f"  rutas: {self._routes or '(ninguna)'}")
                self.errors.append(msg)
                raise FakeNetError(msg)
            reply = route.take()
            if reply is None:
                msg = (f"FakeNet: secuencia agotada en la ruta [{route}] "
                       f"({route.served} respuestas ya servidas): {method} {url}")
                self.errors.append(msg)
                raise FakeNetError(msg)
        return self._materialize(reply, call)

    def _materialize(self, reply: Reply, call: Call) -> FakeResponse:
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, type) and issubclass(reply, BaseException):
            raise reply(f"simulado por FakeNet: {call.method} {call.url}")
        if isinstance(reply, FakeResponse):
            return reply._for(call.url)
        if isinstance(reply, bool):
            raise TypeError("FakeNet: una respuesta no puede ser un bool")
        if isinstance(reply, int):
            return FakeResponse(reply, url=call.url)
        if isinstance(reply, (dict, list)):
            return FakeResponse(200, reply, url=call.url)
        if callable(reply):
            return self._materialize(reply(call), call)
        raise TypeError(f"FakeNet: respuesta no soportada: {reply!r}")

    # ---------------- instalación ----------------
    def install(self, monkeypatch, *, sessions: bool = True) -> 'FakeNet':
        """Sustituye con monkeypatch los nombres de `requests` que usa el
        código: requests.request/get/post/put/patch/delete/head/options y,
        con sessions=True, requests.Session.request."""
        net = self

        def request(method, url, **kw):
            return net.request(method, url, **kw)

        def get(url, params=None, **kw):
            return net.request('GET', url, params=params, **kw)

        def post(url, data=None, json=None, **kw):
            return net.request('POST', url, data=data, json=json, **kw)

        def put(url, data=None, **kw):
            return net.request('PUT', url, data=data, **kw)

        def patch(url, data=None, **kw):
            return net.request('PATCH', url, data=data, **kw)

        def delete(url, **kw):
            return net.request('DELETE', url, **kw)

        def head(url, **kw):
            return net.request('HEAD', url, **kw)

        def options(url, **kw):
            return net.request('OPTIONS', url, **kw)

        for name, fn in (('request', request), ('get', get), ('post', post), ('put', put),
                         ('patch', patch), ('delete', delete), ('head', head),
                         ('options', options)):
            monkeypatch.setattr(requests, name, fn)

        if sessions:
            def session_request(session, method, url, *args, **kw):
                # Session.request admite los kwargs también por posición
                for name, value in zip(_REQUEST_KWARGS, args):
                    kw[name] = value
                merged = CaseInsensitiveDict(getattr(session, 'headers', None) or {})
                merged.update(kw.get('headers') or {})
                kw['headers'] = merged
                return net.request(method, url, **kw)
            monkeypatch.setattr(requests.sessions.Session, 'request', session_request)
        return self


def install_fake_net(monkeypatch, net: Optional[FakeNet] = None, **kw) -> FakeNet:
    """Atajo: instala `net` (o uno nuevo) y lo devuelve."""
    return (net or FakeNet()).install(monkeypatch, **kw)


# =====================================================================
# Guarda de red para toda la suite
# =====================================================================
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')
_NETWORK_SCHEMES = ('http', 'https', 'ws', 'wss', 'ftp')


def _is_loopback_host(host: str) -> bool:
    host = (host or '').strip('[]').lower().rstrip('.')
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback   # 127.0.0.0/8 y ::1
    except ValueError:
        return False


def leaves_machine(url: Any) -> bool:
    """¿Esta URL saldría de la máquina? (file:, data:, sin host o a
    loopback: no). Una URL que no se puede analizar cuenta como que sí."""
    if isinstance(url, bytes):
        url = url.decode('utf-8', 'replace')
    try:
        parts = urlsplit(str(url))
        if parts.scheme.lower() not in _NETWORK_SCHEMES:
            return False
        host = parts.hostname
    except ValueError:
        return True
    return bool(host) and not _is_loopback_host(host)


class RedRealBloqueada(requests.exceptions.ConnectionError):
    """Petición de requests a internet durante los tests. Hereda de
    ConnectionError: el código de la app la trata como «sin red» (no se
    cuelga ni revienta un hilo), y el test falla igualmente al terminar."""


class UrlRealBloqueada(urllib.error.URLError):
    """Lo mismo para urllib.request.urlopen / OpenerDirector.open."""


@dataclass
class Intento:
    test: Optional[str]
    method: str
    url: str
    via: str


class GuardaDeRed:
    """Bloquea las peticiones reales fuera de loopback por las entradas que
    usa el código y la suite:
    - requests.sessions.Session.request: por ella pasan requests.get, post,
      put, delete, patch, head y request (requests.api crea una Session);
    - requests.sessions.Session.send: envíos directos y redirecciones;
    - urllib.request.urlopen y urllib.request.OpenerDirector.open;
    - httpx (si está instalado: lo usa el SDK `anthropic` de la mascota).
    Los falsos de los tests (monkeypatch de requests.get…) quedan por
    encima y no llegan hasta aquí."""

    def __init__(self) -> None:
        self.current_test: Optional[str] = None
        self.attempts: List[Intento] = []
        self._lock = threading.Lock()

    def _block(self, method: str, url: Any, via: str, exc_type: type) -> None:
        url = url.decode('utf-8', 'replace') if isinstance(url, bytes) else str(url)
        with self._lock:
            self.attempts.append(Intento(self.current_test, str(method).upper(), url, via))
        raise exc_type(f"Red real bloqueada en los tests: {str(method).upper()} {url} "
                       f"(vía {via}). Usa el fixture fake_net (tests/fakes/net.py) o un "
                       f"servidor en 127.0.0.1.")

    # ---- por test ----
    def start(self, test: str) -> None:
        with self._lock:
            self.current_test = test

    def finish(self) -> List[Intento]:
        """Cierra el test en curso y devuelve (y retira) sus intentos."""
        with self._lock:
            test, self.current_test = self.current_test, None
            mine = [a for a in self.attempts if a.test == test]
            self.attempts = [a for a in self.attempts if a.test != test]
        return mine

    def take(self) -> List[Intento]:
        """Retira los intentos del test en curso (para los tests que
        provocan un bloqueo a propósito)."""
        with self._lock:
            mine = [a for a in self.attempts if a.test == self.current_test]
            self.attempts = [a for a in self.attempts if a.test != self.current_test]
        return mine

    def orphans(self) -> List[Intento]:
        """Intentos hechos fuera de cualquier test (hilos que siguieron vivos)."""
        with self._lock:
            return [a for a in self.attempts if a.test is None]

    # ---- instalación ----
    def install(self, monkeypatch) -> 'GuardaDeRed':
        guard = self
        session_cls = requests.sessions.Session
        original_request = session_cls.request
        original_send = session_cls.send

        def request(session, method, url, *args, **kwargs):
            if leaves_machine(url):
                guard._block(method, url, 'requests.Session.request', RedRealBloqueada)
            return original_request(session, method, url, *args, **kwargs)

        def send(session, prepared, **kwargs):
            if leaves_machine(getattr(prepared, 'url', '')):
                guard._block(getattr(prepared, 'method', '?'), prepared.url,
                             'requests.Session.send', RedRealBloqueada)
            return original_send(session, prepared, **kwargs)

        monkeypatch.setattr(session_cls, 'request', request)
        monkeypatch.setattr(session_cls, 'send', send)

        original_urlopen = urllib.request.urlopen
        original_open = urllib.request.OpenerDirector.open

        def _target(url):
            if isinstance(url, urllib.request.Request):
                return url.get_method(), url.full_url
            return 'GET', url

        def urlopen(url, *args, **kwargs):
            method, full = _target(url)
            if leaves_machine(full):
                guard._block(method, full, 'urllib.request.urlopen', UrlRealBloqueada)
            return original_urlopen(url, *args, **kwargs)

        def opener_open(opener, fullurl, *args, **kwargs):
            method, full = _target(fullurl)
            if leaves_machine(full):
                guard._block(method, full, 'urllib.request.OpenerDirector.open',
                             UrlRealBloqueada)
            return original_open(opener, fullurl, *args, **kwargs)

        monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
        monkeypatch.setattr(urllib.request.OpenerDirector, 'open', opener_open)

        try:
            import httpx
        except ImportError:
            return self

        class HttpxRealBloqueada(httpx.ConnectError):
            pass

        original_handle = httpx.HTTPTransport.handle_request
        original_ahandle = httpx.AsyncHTTPTransport.handle_async_request

        def handle_request(transport, request):
            if leaves_machine(request.url):
                guard._block(request.method, request.url, 'httpx', HttpxRealBloqueada)
            return original_handle(transport, request)

        async def handle_async_request(transport, request):
            if leaves_machine(request.url):
                guard._block(request.method, request.url, 'httpx', HttpxRealBloqueada)
            return await original_ahandle(transport, request)

        monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle_request)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request',
                            handle_async_request)
        return self


def describe(attempts: List[Intento]) -> str:
    return "\n".join(f"  {a.method} {a.url} (vía {a.via})" for a in attempts)
