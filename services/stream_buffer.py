"""Búfer propio para radios y streams en directo que se cortan.

QMediaPlayer (FFmpeg) no deja configurar cuánto guarda por delante, así que,
cuando un directo ya se ha cortado, MusicService lo hace pasar por este proxy
HTTP local: un hilo lee el origen y lo guarda en memoria, y al reproductor no
le llega nada (ni las cabeceras) hasta reunir N segundos (precarga por
tiempo: el directo llega a su ritmo, así que N s esperando son N s de audio,
o más si el servidor manda una ráfaga al conectar). El reproductor queda N s
por detrás del directo y:
- si el origen se corta o se queda callado, el hilo reconecta por su cuenta
  con esperas cortas y el reproductor sigue sonando del búfer sin enterarse.
  Solo se empalman conexiones de formatos que se resincronizan solos (MP3,
  AAC): Ogg, Opus, FLAC o WebM traen cabeceras propias al empezar, así que se
  sirve lo que quede y se cierra (reconecta MusicService, como sin búfer);
- si el búfer se vacía del todo (el origen calló N s o más), se cierra la
  conexión con el reproductor: lo ve como un corte, reconecta y sube un
  escalón.
El origen se pide SIN Icy-MetaData (los bloques de metadatos se mezclarían
con el audio; los títulos los lee IcyMonitor con la URL original) y con el
User-Agent del reproductor. Si el origen no da audio (HLS, una página, un
error, una radio «ICY 200 OK» que requests no entiende…) se contesta con una
redirección a la URL original: el reproductor conecta directo, como antes.

Solo escucha en 127.0.0.1 (puerto efímero) y solo sirve la ruta con el token
aleatorio de la sesión en curso. Los hilos son daemon y close() corta la
lectura (Deadline.abort) y la conexión del reproductor: no retienen el cierre
de la app ni dejan la radio conectada en pausa.
"""
import secrets
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal

from services.music_resolver import USER_AGENT, Deadline

READ_SIZE = 16 * 1024
MAX_BUFFER_BYTES = 16 * 1024 * 1024       # tope si el reproductor deja de leer
UPSTREAM_DELAYS_S = (0.25, 0.5, 1.0, 2.0)  # reconexión al origen (el último se repite)
CONNECT_TIMEOUT_S = 8
PREFILL_GRACE_S = 10      # la precarga no llega a tiempo: se sirve lo que haya
WAIT_STEP_S = 0.25        # cada cuánto se miran los plazos mientras se espera
UNDERRUN_MIN_S = 0.5      # silencio mínimo del origen para darlo por vaciado
QUICK_END_S = 30          # el origen cerró limpio antes de esto: quizá un archivo
MAX_QUICK_ENDS = 3        # tantos cierres limpios rápidos seguidos: no se empalma más
_NO_TOTAL_S = 7 * 24 * 3600   # el Deadline del origen: dura lo que dure la radio
# Formatos con cabeceras propias al empezar: empalmar dos conexiones los rompe
_NO_SPLICE = ('ogg', 'opus', 'flac', 'webm', 'matroska')
_NO_SPLICE_EXTS = ('.ogg', '.oga', '.opus', '.flac', '.webm', '.mka')
_NOT_AUDIO = ('mpegurl', 'm3u', 'text/', 'html', 'xml', 'json')
_PASS_HEADERS = ('icy-name', 'icy-genre', 'icy-br', 'icy-description', 'icy-url', 'icy-pub')

try:
    import requests
except ImportError:  # pragma: no cover - requests viene en requirements
    requests = None


def _shutdown(sock: Any) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass


def _splices(ctype: str, url: str) -> bool:
    """¿Se pueden empalmar dos conexiones del origen sin romper el audio?"""
    path = urlsplit(url).path.lower()
    return not (any(x in ctype for x in _NO_SPLICE) or path.endswith(_NO_SPLICE_EXTS))


class _Session:
    """Una conexión del reproductor con su lector del origen y su búfer.
    Todo el estado compartido se toca con `cond` tomado."""

    def __init__(self, url: str, prefill_s: float, sid: int, owner: 'StreamBuffer'):
        self.url = url
        self.prefill_s = max(0.0, float(prefill_s))
        self.sid = sid
        self.token = secrets.token_urlsafe(18)
        self.owner = owner
        self.cond = threading.Condition()
        self.chunks: Deque[bytes] = deque()
        self.size = 0
        self.closed = False
        self.started = time.monotonic()
        self.first_data: Optional[float] = None   # primer byte del origen
        self.last_data: Optional[float] = None    # último byte del origen
        self.ready = False          # precarga hecha: ya se sirve
        self.bypass = False         # redirigir al origen (no se puede almacenar)
        self.ended = False          # el origen acabó y no se empalma: vaciar y cerrar
        self.headers: Dict[str, str] = {'Content-Type': 'audio/mpeg'}
        self.splice = True
        self.clean_end = False      # la última conexión al origen acabó limpia (sin excepción)
        self.user_agent = USER_AGENT
        self.reader: Optional[threading.Thread] = None
        self.deadline: Optional[Deadline] = None
        self.client_sock: Any = None
        self.client_gen = 0

    # ---------------------------------------------------------- reproductor
    def attach(self, user_agent: str, sock: Any) -> int:
        """Llega (o vuelve) el reproductor: el más reciente se queda el búfer.
        La primera vez arranca el lector con su User-Agent."""
        with self.cond:
            if self.closed:
                return -1
            old, self.client_sock = self.client_sock, sock
            self.client_gen += 1
            gen = self.client_gen
            if self.reader is None:
                self.user_agent = user_agent or USER_AGENT
                self.reader = threading.Thread(target=self._read_loop,
                                               name="pomogrowth-bufer", daemon=True)
                self.reader.start()
            self.cond.notify_all()
        if old is not None:
            _shutdown(old)
        return gen

    def _check_ready(self, now: float) -> None:
        """Con cond tomado: ¿hay ya N s de audio (o venció la espera)?"""
        if self.ready or self.bypass or self.closed:
            return
        if self.first_data is not None and now - self.first_data >= self.prefill_s:
            self.ready = True
        elif now - self.started > self.prefill_s + PREFILL_GRACE_S:
            if self.size:
                self.ready = True
            else:
                self.bypass = True
        if self.ready or self.bypass:
            self.cond.notify_all()

    def wait_start(self, gen: int) -> str:
        """Espera a la precarga: 'serve', 'bypass' o 'closed'."""
        with self.cond:
            while True:
                if self.closed or gen != self.client_gen:
                    return 'closed'
                self._check_ready(time.monotonic())
                if self.bypass:
                    return 'bypass'
                if self.ready:
                    return 'serve'
                self.cond.wait(WAIT_STEP_S)

    def take(self, gen: int) -> Optional[bytes]:
        """Lo siguiente que mandar al reproductor; None = cerrar su conexión
        (sesión cerrada, otro reproductor, origen acabado o búfer vacío)."""
        with self.cond:
            while True:
                if self.closed or gen != self.client_gen:
                    return None
                if self.chunks:
                    data = b''.join(self.chunks)
                    self.chunks.clear()
                    self.size = 0
                    return data
                if self.ended:
                    return None
                now = time.monotonic()
                quiet = max(self.prefill_s, UNDERRUN_MIN_S)
                if self.last_data is not None and now - self.last_data >= quiet:
                    # El origen calló más que el búfer: el reproductor se ha
                    # quedado sin nada. Cerrar = un corte para MusicService
                    self.closed = True
                    self.cond.notify_all()
                    break
                self.cond.wait(WAIT_STEP_S)
        self._abort_reader()
        self.owner._report(self, 'underrun')
        return None

    # ---------------------------------------------------------- origen
    def _read_loop(self) -> None:
        failures = 0
        quick_ends = 0
        first = True
        while True:
            began = time.monotonic()
            got = self._read_once(first)
            first = False
            with self.cond:
                if self.closed or self.bypass:
                    break
                if not self.splice and self.first_data is not None:
                    self.ended = True     # no se empalma: se vacía y se cierra
                    self.cond.notify_all()
                    break
                # Audio y cierre limpio al poco, una y otra vez: es un archivo
                # que se acaba (una pista servida sin tamaño), no una radio que
                # se corta. No se empalma sin fin: se vacía y se cierra
                quick = got and self.clean_end and time.monotonic() - began < QUICK_END_S
                quick_ends = quick_ends + 1 if quick else 0
                if quick_ends >= MAX_QUICK_ENDS:
                    self.ended = True
                    self.cond.notify_all()
                    break
                failures = 0 if got else failures + 1
                delay = UPSTREAM_DELAYS_S[min(failures, len(UPSTREAM_DELAYS_S) - 1)]
                self.cond.wait(delay)     # close() o un bypass por la gracia la despiertan
                if self.closed or self.bypass:
                    break
        if self.bypass:
            self.owner._report(self, 'bypassed')

    def _read_once(self, first: bool) -> bool:
        """Una conexión al origen, hasta que se corte. True si trajo audio."""
        if requests is None:
            with self.cond:
                self.bypass = True
                self.cond.notify_all()
            return False
        self.clean_end = False
        dl = Deadline(_NO_TOTAL_S)
        with self.cond:
            if self.closed or self.bypass:
                return False   # ya va directo: no abrir otra conexión al origen
            self.deadline = dl
        # Un origen callado se reconecta antes de que el búfer se agote
        read_timeout = max(2.0, min(8.0, self.prefill_s / 2))
        headers = {'User-Agent': self.user_agent, 'Accept-Encoding': 'identity'}
        got = False
        try:
            with dl, dl.session.get(self.url, stream=True, headers=headers,
                                    timeout=(CONNECT_TIMEOUT_S, read_timeout)) as resp:
                ctype = (resp.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
                if resp.status_code >= 400 or any(x in ctype for x in _NOT_AUDIO):
                    if first:
                        self._set_bypass()
                    return False
                if first:
                    self._take_headers(resp.headers, ctype)
                raw = resp.raw
                while True:
                    chunk = raw.read1(READ_SIZE)
                    if not chunk:
                        self.clean_end = True
                        return got
                    if first and not got and chunk.lstrip().startswith(b'#EXTM3U'):
                        self._set_bypass()   # una lista HLS servida como audio
                        return False
                    got = True
                    if not self._push(chunk):
                        return got
        except Exception:  # noqa: BLE001 - red caída, cabeceras raras, corte…
            with self.cond:
                if first and self.first_data is None and not self.closed:
                    self.bypass = True
                    self.cond.notify_all()
            return got
        finally:
            with self.cond:
                if self.deadline is dl:
                    self.deadline = None

    def _set_bypass(self) -> None:
        with self.cond:
            self.bypass = True
            self.cond.notify_all()

    def _take_headers(self, src, ctype: str) -> None:
        with self.cond:
            if ctype:
                self.headers['Content-Type'] = ctype
            for name in _PASS_HEADERS:
                value = src.get(name)
                if value:
                    self.headers[name] = value
            self.splice = _splices(ctype, self.url)

    def _push(self, chunk: bytes) -> bool:
        with self.cond:
            if self.closed or self.bypass:
                return False
            now = time.monotonic()
            if self.first_data is None:
                self.first_data = now
            self.last_data = now
            self.chunks.append(chunk)
            self.size += len(chunk)
            while self.size > MAX_BUFFER_BYTES and len(self.chunks) > 1:
                self.size -= len(self.chunks.popleft())
            self._check_ready(now)
            self.cond.notify_all()
            return True

    # ---------------------------------------------------------- cierre
    def _abort_reader(self) -> None:
        with self.cond:
            dl = self.deadline
        if dl is not None:
            dl.abort()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            sock, self.client_sock = self.client_sock, None
            self.cond.notify_all()
        self._abort_reader()
        if sock is not None:
            _shutdown(sock)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "PomoGrowth"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802 - nombre de la API de http.server
        owner: StreamBuffer = self.server.owner
        session = owner._session_for(self.path)
        if session is None:
            self.send_error(404)
            return
        gen = session.attach(self.headers.get('User-Agent') or '', self.connection)
        if gen < 0:
            return
        outcome = session.wait_start(gen)
        try:
            if outcome == 'bypass':
                session._abort_reader()
                self.send_response(302)
                self.send_header('Location', session.url)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if outcome != 'serve':
                return
            self.send_response(200)
            with session.cond:
                headers = dict(session.headers)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            while True:
                data = session.take(gen)
                if data is None:
                    return
                self.wfile.write(data)   # bloquea si el reproductor no lee: es el freno
        except OSError:
            pass   # el reproductor se fue o close() cortó su conexión


class _Server(ThreadingHTTPServer):
    daemon_threads = True     # un reproductor colgado no retiene el cierre

    def handle_error(self, request, client_address):
        pass   # el reproductor cortó a mitad de petición: nada que contar


class StreamBuffer(QObject):
    """Proxy local con búfer para la conexión en curso (una sola a la vez).
    open() devuelve la URL local que dar al reproductor (None si el proxy no
    puede abrir: entonces se conecta directo); close() la corta."""
    underrun = Signal(int)    # sid: el búfer se vació y se cerró el reproductor
    bypassed = Signal(int)    # sid: el origen no se puede almacenar; va directo

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[_Session] = None
        self._next_sid = 0

    @property
    def port(self) -> int:
        server = self._server
        return server.server_address[1] if server is not None else 0

    @property
    def session_id(self) -> int:
        """La sesión en curso (0 si no hay)."""
        session = self._session
        return session.sid if session is not None else 0

    def open(self, url: str, prefill_s: float) -> Optional[str]:
        self.close()
        if not self._ensure_server():
            return None
        with self._lock:
            self._next_sid += 1
            session = self._session = _Session(url, prefill_s, self._next_sid, self)
        return f"http://127.0.0.1:{self.port}/{session.token}"

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()

    def shutdown(self) -> None:
        """Al cerrar la app: corta la sesión y el servidor."""
        self.close()
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def _ensure_server(self) -> bool:
        with self._lock:
            if self._server is not None:
                return True
            try:
                server = _Server(("127.0.0.1", 0), _Handler)
            except OSError as e:
                print(f"Música: no se pudo abrir el búfer local: {e}")
                return False
            server.owner = self
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, args=(0.25,),
                                            name="pomogrowth-bufer-http", daemon=True)
            self._thread.start()
            return True

    def _session_for(self, path: str) -> Optional[_Session]:
        token = path.split('?', 1)[0].strip('/')
        with self._lock:
            session = self._session
        if session is None or not token or not secrets.compare_digest(token, session.token):
            return None
        return session

    def _report(self, session: _Session, what: str) -> None:
        """Avisa desde los hilos (la señal llega encolada al hilo de UI)."""
        if self._session is not session:
            return   # una sesión vieja: ya no le importa a nadie
        try:
            (self.underrun if what == 'underrun' else self.bypassed).emit(session.sid)
        except RuntimeError:
            pass   # el servicio ya se destruyó
