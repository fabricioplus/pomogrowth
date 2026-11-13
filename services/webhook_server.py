"""Servidor HTTP para recibir tareas por webhook (POST /webhook): un hilo por
conexión, como mucho MAX_CONNECTIONS a la vez y con un plazo total por
petición (un cliente lento no retiene a los demás ni a stop()).

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
a Asana). Una vez parado, lo que una conexión ya abierta termine de enviar
recibe 503 y no se encola.
"""
import hmac
import io
import json
import os
import queue
import re
import secrets
import socketserver
import sys
import threading
import time
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
# En 'lan', a quien no trae la clave buena no se le lee más que esto
UNAUTHORIZED_DRAIN = 8 * 1024
# Cada lectura del socket espera como mucho REQUEST_TIMEOUT_S (una conexión
# muda se corta) y la petición entera, REQUEST_DEADLINE_S (también se corta
# a un cliente que gotea bytes sin agotar nunca el timeout de cada lectura)
REQUEST_TIMEOUT_S = 5
REQUEST_DEADLINE_S = 10
# Conexiones atendidas a la vez (un hilo cada una); las de más se cierran
# sin atender
MAX_CONNECTIONS = 8
# Cada cuánto mira serve_forever si se le pidió parar
POLL_INTERVAL_S = 0.25
# Lo más que stop() espera a serve_forever: corre en el hilo de la interfaz
# (al salir y al guardar Ajustes)
STOP_WAIT_S = 1.0

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


# Surrogates UTF-16 sueltos (JSON.stringify de un texto cortado a mitad de
# un emoji da "\ud83d"): no se pueden escribir en UTF-8 y, ya en los datos,
# harían fallar cada guardado. Y los caracteres de control (C0, DEL y C1)
_LONE_SURROGATE = re.compile(r'[\ud800-\udfff]')
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def _clean_text(value: Any, max_len: int) -> str:
    """Un texto recibido, saneado: surrogates sueltos → U+FFFD, caracteres de
    control → espacio, sin espacios en los bordes y recortado a max_len."""
    if not isinstance(value, str):
        return ''
    value = _CONTROL_CHARS.sub(' ', _LONE_SURROGATE.sub('�', value))
    return value.strip()[:max_len].strip()


def task_fields(data: Any) -> Optional[Tuple[str, List[str]]]:
    """De lo recibido, solo el nombre (`name` o `task_name`, sin espacios en
    los bordes y recortado a NAME_MAX_LEN) y las etiquetas (`tags`: lista de
    textos, como mucho MAX_TAGS de TAG_MAX_LEN caracteres, sin repetir), los
    dos saneados (_clean_text). Todo lo demás (source, asana_id, enlaces…) se
    descarta. None si no hay un nombre válido o lo recibido no es un objeto.
    Nunca lanza."""
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


class _DeadlineReader(io.RawIOBase):
    """Lecturas del socket con un plazo total: cada recv espera lo que quede
    hasta `deadline` (nunca más de REQUEST_TIMEOUT_S) y, agotado, TimeoutError
    (http.server cierra entonces la conexión sin responder)."""

    def __init__(self, sock, deadline: float):
        super().__init__()
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("plazo de la petición agotado")
        self._sock.settimeout(min(REQUEST_TIMEOUT_S, left))
        return self._sock.recv_into(buffer)


class _Handler(BaseHTTPRequestHandler):
    server_version = 'PomoGrowth'
    sys_version = ''
    timeout = REQUEST_TIMEOUT_S

    def setup(self):
        super().setup()
        # Lo que se lee (petición, cabeceras y cuerpo), con plazo total
        self.rfile.close()
        self.rfile = io.BufferedReader(_DeadlineReader(
            self.connection, time.monotonic() + REQUEST_DEADLINE_S))

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
        # En 'lan', sin la clave buena, como mucho unos KB
        limit = (MAX_DRAIN if self.server.mode != 'lan' or self._key_ok()
                 else UNAUTHORIZED_DRAIN)
        if not length or length > limit:
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
        if not self.server.enqueue(data):
            return self._reply(503, 'Service Unavailable: the server is stopping')
        self._reply(200, 'Task received')

    def _method_not_allowed(self):
        self._reject(405, 'Method Not Allowed', allow=True)

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = _method_not_allowed
    # Sin Access-Control-*: el preflight CORS de un navegador falla
    do_OPTIONS = _method_not_allowed


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    # Un hilo por conexión: serve_forever solo acepta, así que un cliente
    # lento no retiene a los demás ni a shutdown(). Hilos daemon y
    # server_close() sin esperarlos: parar no espera a una conexión colgada
    daemon_threads = True
    block_on_close = False
    # En Windows, SO_REUSEADDR deja a otro socket (otro programa, otra copia
    # de la app) atar el mismo puerto y quedarse con las peticiones; sin él,
    # volver a arrancar en el mismo puerto tras server_close() funciona
    # igual. En Linux/macOS evita el «address in use» por TIME_WAIT
    allow_reuse_address = os.name != 'nt'

    def __init__(self, address, webhook_queue: queue.Queue, mode: str, key: str):
        self.webhook_queue = webhook_queue
        self.mode = mode
        self.key = key
        self.stopped = False
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _Handler)

    def enqueue(self, data: dict) -> bool:
        """Encola lo recibido; False si el servidor ya se paró (al salir, la
        app vacía la cola después de parar: lo de después no lo vería)."""
        with self._lock:
            if self.stopped:
                return False
            self.webhook_queue.put(data)
            return True

    def mark_stopped(self) -> None:
        with self._lock:
            self.stopped = True

    # --- tope de conexiones simultáneas ---
    def verify_request(self, request, client_address) -> bool:
        # Sin sitio, socketserver la cierra sin atenderla
        return self._slots.acquire(blocking=False)

    def process_request(self, request, client_address):
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()  # su hilo no llegó a arrancar
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Un cliente que corta la conexión a medias no es un fallo de la app:
        # sin traza en la consola (lo demás, con la suya, como siempre)
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        # Sin el socket.getfqdn() de HTTPServer.server_bind: con 0.0.0.0
        # consulta el DNS, y esto corre en el hilo de la interfaz
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class WebhookServer:
    """Servidor de webhooks en un hilo (más uno por conexión, ver _Server).
    `mode`: 'local' | 'lan' | 'off' (con 'off' no abre ningún puerto y
    start() no hace nada). `key`: la clave del modo 'lan'; sin clave, en
    'lan' se rechaza todo. `host` sustituye a la dirección del modo (solo los
    tests: probar 'lan' sin abrir el puerto a la red). Con `port` 0 el
    sistema elige uno libre (ver `address`). Lanza OSError si el puerto está
    ocupado."""

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
        """Deja de escuchar y cierra el puerto (se puede volver a arrancar
        otro servidor en el mismo enseguida) sin esperar a las conexiones en
        curso: lo que terminen de enviar ya no se encola (503). Corre en el
        hilo de la interfaz: nunca la retiene más de STOP_WAIT_S."""
        server, self.server = self.server, None
        if server is None:
            return
        server.mark_stopped()
        thread, self.thread = self.thread, None
        if thread is not None:
            # shutdown() espera a que serve_forever salga: en un hilo aparte y
            # aquí solo un rato (sin start() no se llama: esperaría siempre)
            threading.Thread(target=server.shutdown, name='webhook-stop',
                             daemon=True).start()
            thread.join(timeout=STOP_WAIT_S)
        server.server_close()
