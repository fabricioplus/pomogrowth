"""Servidor HTTP en un hilo para recibir tareas por webhook (POST /webhook).

Extraído de pomogrowth_v8_ultra/app_ultra.py (extract_from_v8.py, una sola vez).
A partir de aquí este archivo es fuente normal de v9.

Cerrado por defecto (pestaña «Webhook» de Ajustes, ajuste `webhook_mode`):
- 'local' (por defecto): escucha solo en 127.0.0.1, para programas de este
  equipo (n8n local, scripts…).
- 'lan': escucha en 0.0.0.0 y exige la cabecera X-PomoGrowth-Key con la
  clave (`webhook_key`), comparada con hmac.compare_digest.
- 'off': no abre ningún puerto.

Solo se acepta POST /webhook (otra ruta, 404; otro método, 405, también
OPTIONS y sin cabeceras CORS: el preflight de un navegador falla) con
Content-Type application/json (415 si no), sin cabecera Origin (403: corta
los envíos desde navegadores; una web cualquiera no puede crear tareas con un
fetch a 127.0.0.1), con un Content-Length válido (400 si falta) de 64 KB
como mucho (413) y un cuerpo que sea un objeto JSON (400 si no). Todas las
respuestas van en JSON.

El servidor solo encola el objeto recibido: qué se toma de él lo decide
task_fields() (el controlador crea siempre tareas locales, nunca enlazadas
a Asana).
"""
import hmac
import json
import os
import queue
import secrets
import socketserver
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

# Modos del ajuste `webhook_mode` (= core/data_handler.WEBHOOK_MODES; lo
# comprueba tests/test_webhook_seguro.py) y sus textos en Ajustes
MODES = ('local', 'lan', 'off')
DEFAULT_MODE = 'local'
MODE_LABELS = (('local', "Solo este equipo"),
               ('lan', "Red local, con clave"),
               ('off', "Desactivado"))
# Dónde escucha cada modo ('off' no escucha)
HOSTS = {'local': '127.0.0.1', 'lan': '0.0.0.0'}

WEBHOOK_PATH = '/webhook'
KEY_HEADER = 'X-PomoGrowth-Key'
MAX_BODY = 64 * 1024
# Un cuerpo rechazado se lee y se descarta (hasta este tamaño) antes de
# responder: cerrar con datos sin leer resetea la conexión y el cliente no
# llegaría a ver el código de error
MAX_DRAIN = 1024 * 1024
# El servidor atiende de una en una: un cliente lento no lo retiene más que esto
REQUEST_TIMEOUT_S = 5
# Cada cuánto mira serve_forever si se le pidió parar (lo que tarda stop())
POLL_INTERVAL_S = 0.25

# Lo que se toma de cada tarea recibida (task_fields)
NAME_MAX_LEN = 500
MAX_TAGS = 10
TAG_MAX_LEN = 40


def normalize_mode(value: Any) -> str:
    """Un modo válido; cualquier otra cosa (un JSON editado a mano) → 'local'."""
    return value if isinstance(value, str) and value in MODES else DEFAULT_MODE


def normalize_key(value: Any) -> str:
    return value if isinstance(value, str) else ''


def new_key() -> str:
    """Clave aleatoria para el modo 'lan' (32 caracteres seguros en una URL)."""
    return secrets.token_urlsafe(24)


def bind_host(mode: str) -> Optional[str]:
    """Dirección en la que escucha `mode`; None con 'off'."""
    return HOSTS.get(normalize_mode(mode))


def _clean_text(value: Any, max_len: int) -> str:
    return value.strip()[:max_len].strip() if isinstance(value, str) else ''


def task_fields(data: Any) -> Optional[Tuple[str, List[str]]]:
    """De lo recibido, solo el nombre (`name` o `task_name`, sin espacios en
    los bordes y recortado a NAME_MAX_LEN) y las etiquetas (`tags`: lista de
    textos, como mucho MAX_TAGS de TAG_MAX_LEN caracteres, sin repetir). Todo
    lo demás (source, asana_id, enlaces…) se descarta. None si no hay un
    nombre válido o lo recibido no es un objeto. Nunca lanza."""
    if not isinstance(data, dict):
        return None
    name = _clean_text(data.get('name'), NAME_MAX_LEN) or _clean_text(
        data.get('task_name'), NAME_MAX_LEN)
    if not name:
        return None
    tags: List[str] = []
    raw_tags = data.get('tags')
    for raw in raw_tags if isinstance(raw_tags, list) else ():
        tag = _clean_text(raw, TAG_MAX_LEN)
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
            if len(tags) == MAX_TAGS:
                break
    return name, tags


class _Handler(BaseHTTPRequestHandler):
    server_version = 'PomoGrowth'
    sys_version = ''
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, format, *args):  # sin ruido en la consola
        return

    # --- respuestas (siempre JSON) ---
    def _reply(self, code: int, message: str, allow: bool = False) -> None:
        body = json.dumps({'status': message}).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if allow:
            self.send_header('Allow', 'POST')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _reject(self, code: int, message: str, allow: bool = False) -> None:
        self._discard_body()
        self.close_connection = True
        self._reply(code, message, allow=allow)

    def send_error(self, code, message=None, explain=None):
        """Los errores del propio http.server (petición mal formada, método
        sin do_X como TRACE…) también en JSON; un método desconocido es «no
        permitido», como los demás."""
        if code == HTTPStatus.NOT_IMPLEMENTED:
            code, message = HTTPStatus.METHOD_NOT_ALLOWED, 'Method Not Allowed'
        self.close_connection = True
        phrase = self.responses.get(code, ('Error',))[0]
        self._reply(int(code), message or phrase, allow=code == HTTPStatus.METHOD_NOT_ALLOWED)

    # --- comprobaciones ---
    def _content_length(self) -> Optional[int]:
        """El Content-Length si es uno solo y un entero; None si falta, no
        vale o el cuerpo va troceado (Transfer-Encoding)."""
        if self.headers.get('Transfer-Encoding') is not None:
            return None
        values = self.headers.get_all('Content-Length') or []
        if len(values) != 1:
            return None
        value = values[0].strip()
        return int(value) if value.isascii() and value.isdigit() else None

    def _discard_body(self) -> None:
        length = self._content_length()
        if not length or length > MAX_DRAIN:
            return
        try:
            while length > 0:
                chunk = self.rfile.read(min(length, 64 * 1024))
                if not chunk:
                    break
                length -= len(chunk)
        except OSError:
            pass

    def _key_ok(self) -> bool:
        expected = self.server.key
        given = self.headers.get(KEY_HEADER)
        if not expected or given is None:
            return False
        return hmac.compare_digest(given.encode('utf-8', 'replace'),
                                   expected.encode('utf-8'))

    # --- métodos ---
    def do_POST(self):
        # Los navegadores ponen Origin en todo POST (también con DNS
        # rebinding): ningún envío desde una web llega a crear tareas
        if self.headers.get('Origin') is not None:
            return self._reject(403, 'Forbidden: browser requests are not accepted')
        if urlsplit(self.path).path != WEBHOOK_PATH:
            return self._reject(404, 'Not Found')
        if self.server.mode == 'lan' and not self._key_ok():
            return self._reject(401, f'Unauthorized: missing or wrong {KEY_HEADER}')
        content_type = (self.headers.get('Content-Type') or '').split(';', 1)[0]
        if content_type.strip().lower() != 'application/json':
            return self._reject(415, 'Unsupported Media Type: use application/json')
        length = self._content_length()
        if length is None:
            return self._reject(400, 'Bad Request: missing or invalid Content-Length')
        if length > MAX_BODY:
            return self._reject(413, f'Payload Too Large: at most {MAX_BODY} bytes')
        raw = self.rfile.read(length)
        if len(raw) < length:
            self.close_connection = True
            return self._reply(400, 'Bad Request: incomplete body')
        try:
            data = json.loads(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return self._reply(400, 'Bad Request: invalid JSON')
        if not isinstance(data, dict):
            return self._reply(400, 'Bad Request: the body must be a JSON object')
        self.server.webhook_queue.put(data)
        self._reply(200, 'Task received')

    def _method_not_allowed(self):
        self._reject(405, 'Method Not Allowed', allow=True)

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = _method_not_allowed
    # Sin Access-Control-*: el preflight CORS de un navegador falla
    do_OPTIONS = _method_not_allowed


class _Server(HTTPServer):
    # En Windows, SO_REUSEADDR deja a otro socket (otro programa, otra copia
    # de la app) atar el mismo puerto y quedarse con las peticiones; sin él,
    # volver a arrancar en el mismo puerto tras server_close() funciona
    # igual. En Linux/macOS evita el «address in use» por TIME_WAIT
    allow_reuse_address = os.name != 'nt'

    def __init__(self, address, webhook_queue: queue.Queue, mode: str, key: str):
        self.webhook_queue = webhook_queue
        self.mode = mode
        self.key = key
        super().__init__(address, _Handler)

    def server_bind(self):
        # Sin el socket.getfqdn() de HTTPServer.server_bind: con 0.0.0.0
        # consulta el DNS, y esto corre en el hilo de la interfaz
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class WebhookServer:
    """Servidor de webhooks en un hilo. `mode`: 'local' | 'lan' | 'off' (con
    'off' no abre ningún puerto y start() no hace nada). `key`: la clave del
    modo 'lan'; sin clave, en 'lan' se rechaza todo. `host` sustituye a la
    dirección del modo (solo los tests: probar 'lan' sin abrir el puerto a la
    red). Con `port` 0 el sistema elige uno libre (ver `address`). Lanza
    OSError si el puerto está ocupado."""

    def __init__(self, webhook_queue: queue.Queue, port: int, mode: str = DEFAULT_MODE,
                 key: str = '', host: Optional[str] = None):
        self.queue = webhook_queue
        self.port = port
        self.mode = normalize_mode(mode)
        self.key = normalize_key(key)
        self.server: Optional[_Server] = None
        self.thread: Optional[threading.Thread] = None
        if self.mode == 'off':
            return
        address = (bind_host(self.mode) if host is None else host, port)
        self.server = _Server(address, webhook_queue, self.mode, self.key)

    @property
    def address(self) -> Optional[Tuple[str, int]]:
        return tuple(self.server.server_address[:2]) if self.server else None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.server is None or self.thread is not None:
            return
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': POLL_INTERVAL_S},
                                       name='webhook-server', daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Para el hilo y cierra el puerto: se puede volver a arrancar otro
        servidor en el mismo. Esperar a serve_forever es breve (POLL_INTERVAL_S,
        o lo que quede de una petición en curso)."""
        server, self.server = self.server, None
        if server is None:
            return
        if self.thread is not None:
            server.shutdown()  # sin start() no se llama: esperaría para siempre
            self.thread.join(timeout=REQUEST_TIMEOUT_S)
            self.thread = None
        server.server_close()
