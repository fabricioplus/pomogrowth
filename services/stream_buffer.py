"""Proxy local de las radios: UNA sola conexión con la emisora, sus títulos
ICY y el búfer adaptativo de los cortes.

Por qué: las radios de pago (DI.FM y compañía) solo admiten una sesión a la
vez por cuenta («one stream at a time»): una 2.ª conexión con la misma clave
(una consulta de títulos aparte, una reconexión que se solapa con la
anterior, un 302 que hace repetir la petición al reproductor…) pausa la
cuenta. Por eso MusicService hace pasar por aquí todo directo por enlace (no
HLS ni una pista ya conocida) desde la PRIMERA conexión, y este módulo es el
único que habla con la emisora:

- open(url, precarga, known_live, budget) prepara una sesión con un token
  aleatorio y devuelve la URL local que se da al reproductor (None si el
  servidor local no abre o falta requests: entonces se conecta directo).
  Solo escucha en 127.0.0.1 (puerto efímero) y solo sirve el token de la
  sesión en curso: lo demás, 404. FFmpeg solo conecta a 127.0.0.1.
- Cuando llega el reproductor, el hilo lector (daemon) de la sesión conecta
  con el origen con el User-Agent del reproductor (uno solo hacia la
  emisora) y «Icy-MetaData: 1». Antes espera, con plazo (PREVIOUS_WAIT_S), a
  que la conexión de la sesión anterior esté cerrada: nunca dos a la vez. Lo
  espera ese hilo, no el de la interfaz.
- Títulos en la misma conexión: IcyDemuxer (services.icy) quita los bloques
  de metadatos del audio y la señal metadata(sid, {'title','name','bitrate',
  'mime'}) lleva las cabeceras icy-* (al conectar) y cada StreamTitle cuando
  el audio que lo acompaña se entrega al reproductor (no N s antes). Sin
  icy-metaint no se demultiplexa; con uno que no vale se reconecta sin pedir
  metadatos (nunca se mezclan con el audio).
- Precarga por tiempo: al reproductor no le llega nada (ni las cabeceras)
  hasta reunir N s de audio desde el primer byte. Con 0 (la primera conexión
  y los arranques) se sirve con el primer byte: la latencia de ir directo.
  Si en N + PREFILL_GRACE_S no llegó nada, se contesta 502.

La primera conexión de la sesión decide qué recibe el reproductor:
- 401, 402, 403, 407, 429 o 451, o una página HTML donde iba el audio: la
  emisora rechaza la conexión. Una sola petición: se guarda en last_rejected
  (sid, estado), se emite rejected(sid, estado) y al reproductor se le
  contesta con ese error (no un 302: FFmpeg repetiría la petición).
- Otro 4xx/5xx, un error de red o una conexión sin audio: 502 (last_failure
  = (sid, 'http' | 'network')); MusicService decide (escalera o salto).
- 302 a la URL original, siempre con la conexión del lector ya cerrada (así
  nunca hay dos): solo para lo que no es audio almacenable (una lista HLS
  servida como audio, un «ICY 200 OK» de SHOUTcast v1 que requests no
  entiende), para lo que el proxy no puede abrir y el reproductor sí (un
  certificado https que requests no valida, p. ej. el de un antivirus que
  inspecciona HTTPS, o un fallo del proxy del sistema, que FFmpeg no usa:
  la emisora no llegó a ver ninguna petición) y para las pistas: con
  known_live=False (aún no se sabe si es un directo, como las entradas de un
  .pls), unas cabeceras con Content-Length o Accept-Ranges y sin icy-* son
  las de un archivo, que el reproductor pide directo con Range para poder
  saltar (bypassed y track_detected). Antes se mira el principio del cuerpo:
  una página HTML es un rechazo y una lista HLS va como tal, aunque vengan
  con tipo de audio. Con icy-*, o sin tamaño ni rangos, es un directo y se
  sirve aquí (una pista servida por partes también llega así: se sabe al
  acabar, y no se empalma).

Cortes y reconexiones (siempre secuenciales, cada conexión cerrada antes de
abrir la siguiente):
- Una conexión se da por muerta tras DEAD_AIR_S de silencio (fijo: un hipo
  de red corto no abre otra) o cuando el origen la cierra.
- Con precarga 0 la sesión no empalma: el reproductor recibe el fin, como
  si fuera directo, y MusicService reconecta con su escalera de precargas.
- Con precarga (las reconexiones de un directo confirmado) el lector
  reconecta solo, con esperas RETRY_DELAYS_S (la primera ≥ 2 s: el servidor
  registra el cierre), y el reproductor sigue sonando del búfer. Tras
  len(RETRY_DELAYS_S) reconexiones seguidas sin STABLE_AUDIO_S de audio
  (también si cierran limpio al poco: un archivo que se acaba) se rinde: se
  sirve lo que quede y se cierra, y decide MusicService. Una conexión
  rechazada (401/403…) acaba igual y emite rejected.
- Solo se empalman directos (known_live o con icy-*) de formatos que se
  resincronizan (MP3, AAC: Ogg, Opus, FLAC o WebM no) y con el MISMO formato
  que la primera conexión (services.audio_format): si cambia, se sirve lo
  que quede y se cierra, y MusicService recarga con un decodificador nuevo.
- budget (ConnectionBudget, de MusicService) pone el tope global: cada
  conexión con el origen gasta una (justo antes de abrirla: una sesión que
  se cierra mientras espera a la anterior no gasta); sin saldo no se conecta.

El reproductor no deja de recibir mientras el lector siga dentro de su
presupuesto (se le corta solo al cerrar la sesión o cuando ya no habrá más
audio; si eso se sabe antes de cumplir la precarga, se le sirve ya lo que
haya). Se le sirve por partes (HTTP/1.1 chunked) para que el final diga lo
mismo que el del origen: si este acabó entero (por partes o con su tamaño),
recibe la parte final vacía, el fin de la pista; si no (un corte, el lector
se rinde, otro formato, un rechazo), se le deja una parte a medias antes de
cerrar. Cerrar sin más tras una parte entera no basta: FFmpeg lee el cierre
al buscar el tamaño de la siguiente y lo toma por el fin del archivo
(AVERROR_EOF), y MusicService guardaría una radio sin icy-* como pista; a
mitad de una parte es «Stream ends prematurely» (EIO), un error, y
MusicService reconecta como si fuera directo. close() corta
la lectura (Deadline.abort: el servidor ve el cierre al momento, aunque en
Windows el hilo bloqueado en un origen callado tarde hasta DEAD_AIR_S en
despertar) y la conexión del reproductor; pausa, parar, cambiar de item y
cerrar la app dejan 0 conexiones con la emisora.
"""
import secrets
import socket
import ssl
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal

from services.audio_format import FormatProbe, normalize_ctype, same_format
from services.icy import IcyDemuxer, has_stream_title, parse_metaint, parse_stream_title
from services.music_resolver import (USER_AGENT, Deadline, fix_header_text,
                                     is_icy_status_line, parse_bitrate)

READ_SIZE = 16 * 1024
MAX_BUFFER_BYTES = 16 * 1024 * 1024       # tope si el reproductor deja de leer
CONNECT_TIMEOUT_S = 8
PREFILL_GRACE_S = 10      # la precarga no llega a tiempo: se sirve lo que haya (o 502)
WAIT_STEP_S = 0.25        # cada cuánto se miran los plazos mientras se espera
_NO_TOTAL_S = 7 * 24 * 3600   # el Deadline del origen: dura lo que dure la radio
# La emisora no nos quiere ahora (cuenta en otro sitio o pausada, demasiadas
# peticiones…): ni un intento más
REJECT_STATUS = (401, 402, 403, 407, 429, 451)
# Formatos con cabeceras propias al empezar: empalmar dos conexiones los rompe
_NO_SPLICE = ('ogg', 'opus', 'flac', 'webm', 'matroska')
_NO_SPLICE_EXTS = ('.ogg', '.oga', '.opus', '.flac', '.webm', '.mka')
_PLAYLIST_TYPES = ('mpegurl', 'm3u')          # HLS: lo abre FFmpeg directo
_NOT_AUDIO = ('text/', 'xml', 'json')
_PASS_HEADERS = ('icy-name', 'icy-genre', 'icy-br', 'icy-description', 'icy-url', 'icy-pub')
_HTML_STARTS = (b'<!doctype html', b'<html')

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


def _only_proxy_fails(exc: BaseException) -> bool:
    """¿Falló algo que solo le pasa al proxy, no al reproductor, y antes de
    que la emisora viera ninguna petición? Un certificado que requests (con
    certifi) no valida, porque FFmpeg no lo comprueba: el de un antivirus o
    un proxy corporativo que inspeccionan HTTPS, una cadena incompleta… Y el
    proxy del sistema (requests lo toma de Windows; FFmpeg, no). Se mira toda
    la cadena: requests envuelve el error de urllib3, que envuelve el de ssl."""
    proxy_errors: Tuple[type, ...] = ()
    if requests is not None:
        proxy_errors = (requests.exceptions.ProxyError,)
    seen, todo = set(), [exc]
    while todo:
        cur = todo.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError) or isinstance(cur, proxy_errors):
            return True
        for nxt in (cur.__cause__, cur.__context__, getattr(cur, 'reason', None), *cur.args):
            if isinstance(nxt, BaseException):
                todo.append(nxt)
    return False


class ConnectionBudget:
    """Tope de conexiones con el origen en una ventana deslizante: `limit`
    en `window_s` segundos. Lo comparten MusicService (hilo de UI) y los
    lectores del proxy (sus hilos): cada conexión pide saldo antes de abrir."""

    def __init__(self, limit: int, window_s: float):
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self.exhausted = False        # se negó alguna conexión por falta de saldo
        self._times: Deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._times and now - self._times[0] >= self.window_s:
            self._times.popleft()

    def full(self) -> bool:
        """¿La siguiente conexión pasaría del tope?"""
        with self._lock:
            self._prune(time.monotonic())
            return len(self._times) >= self.limit

    def take(self) -> bool:
        """Apunta una conexión; False (y exhausted) si no queda saldo."""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if len(self._times) >= self.limit:
                self.exhausted = True
                return False
            self._times.append(now)
            return True


class _Title:
    """Marca en la cola del búfer: a partir de aquí suena este título."""
    __slots__ = ('text',)

    def __init__(self, text: Optional[str]):
        self.text = text


class _Conn:
    """Lo que se sabe de la conexión en curso con el origen."""

    def __init__(self, first: bool):
        self.first = first                 # la sesión aún no entregó audio
        self.audio_at: Optional[float] = None   # primer audio de esta conexión
        self.probe = FormatProbe()
        self.held: Optional[List[Union[bytes, _Title]]] = None   # audio en espera del formato
        self.ctype = ''
        self.sniffed = False               # ya se miró el principio del cuerpo
        self.redirect = ''                 # 302 que publicar con la conexión ya cerrada
        self.clean = False                 # acabó entera (por partes o con su tamaño)

    def audio_s(self) -> float:
        return time.monotonic() - self.audio_at if self.audio_at is not None else 0.0


class _Session:
    """Una sesión del reproductor: su lector del origen y su búfer. Todo el
    estado compartido se toca con `cond` tomado."""

    def __init__(self, url: str, prefill_s: float, sid: int, owner: 'StreamBuffer',
                 known_live: bool, budget: Optional[ConnectionBudget],
                 previous: Optional['_Session']):
        self.url = url
        self.prefill_s = max(0.0, float(prefill_s))
        self.sid = sid
        self.owner = owner
        self.known_live = known_live      # directo confirmado (icy-* o el resolver)
        self.budget = budget
        self.previous = previous          # sesión anterior: su conexión se cierra antes
        self.token = secrets.token_urlsafe(18)
        self.cond = threading.Condition()
        self.chunks: Deque[Union[bytes, _Title]] = deque()
        self.size = 0
        self.closed = False
        self.released = False             # cerrada y con la conexión ya cortada (abort)
        self.connecting = False           # a medio conectar o esperando las cabeceras
        self.waiting_previous = False     # su lector aún espera a que cierre la anterior
        self.started: Optional[float] = None      # el lector empezó a conectar
        self.first_data: Optional[float] = None   # primer audio entregado a la cola
        self.ready = False                # precarga hecha: ya se sirve
        self.outcome = ''                 # antes de servir: 'bypass' (302) o 'fail'
        self.fail_status = 502
        self.fail_kind = ''               # 'http', 'network' o 'budget' (last_failure)
        self.ended = False                # no habrá más audio: vaciar y cerrar
        self.last_clean = False           # la última conexión acabó entera
        self.clean_end = False            # el fin es el del origen: se le dice al reproductor
        self.headers: Dict[str, str] = {'Content-Type': 'audio/mpeg'}
        self.splice = False               # las reconexiones se empalman
        self.format: Optional[Tuple[str, Any]] = None   # (tipo, firma) de la 1.ª conexión
        self.want_icy = True              # pedir Icy-MetaData (no tras un metaint inválido)
        self.meta: Dict[str, Any] = {'title': None, 'name': None, 'bitrate': None,
                                     'mime': None}
        self.dropped_title: Optional[_Title] = None   # título de audio descartado por el tope
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
                self.waiting_previous = self.previous is not None
                self.reader = threading.Thread(target=self._read_loop,
                                               name="pomogrowth-bufer", daemon=True)
                self.reader.start()
            self.cond.notify_all()
        if old is not None:
            _shutdown(old)
        return gen

    def _check_ready(self, now: float) -> None:
        """Con cond tomado: ¿hay ya N s de audio (o venció la espera)?"""
        if self.ready or self.outcome or self.closed:
            return
        if self.ended:
            # Ya no llegará más audio (otro formato, el lector se rindió, un
            # rechazo): esperar a la precarga solo retrasaría la recarga
            if self.size:
                self.ready = True
            else:
                self._fail_locked()
        elif self.first_data is not None and now - self.first_data >= self.prefill_s:
            self.ready = True
        elif self.started is not None and now - self.started > self.prefill_s + PREFILL_GRACE_S:
            if self.size:
                self.ready = True
            else:   # nada en el plazo: como un error de red (502, no 302)
                self._fail_locked()
        if self.ready or self.outcome:
            self.cond.notify_all()

    def _fail_locked(self) -> None:
        """Con cond tomado: la sesión no llegó a servir audio; 502."""
        self.outcome = 'fail'
        self.owner.last_failure = (self.sid, self.fail_kind or 'network')
        self.cond.notify_all()

    def wait_start(self, gen: int) -> str:
        """Espera a la precarga: 'serve', 'bypass', 'fail' o 'closed'."""
        with self.cond:
            while True:
                if self.closed or gen != self.client_gen:
                    return 'closed'
                self._check_ready(time.monotonic())
                if self.outcome:
                    return self.outcome
                if self.ready:
                    return 'serve'
                self.cond.wait(WAIT_STEP_S)

    def take(self, gen: int) -> Optional[bytes]:
        """Lo siguiente que mandar al reproductor (b'' si solo había títulos);
        None = cerrar su conexión (sesión cerrada, otro reproductor, o ya no
        habrá más audio y no queda nada). Mientras el lector siga dentro de su
        presupuesto se espera, aunque el origen calle."""
        titles: List[_Title] = []
        with self.cond:
            while True:
                if self.closed or gen != self.client_gen:
                    data = None
                    break
                if self.dropped_title is not None:
                    titles.append(self.dropped_title)
                    self.dropped_title = None
                if self.chunks:
                    parts = []
                    while self.chunks:
                        item = self.chunks[0]
                        if isinstance(item, bytes):
                            parts.append(self.chunks.popleft())
                            self.size -= len(item)
                        elif parts:
                            break   # el audio de antes del título se entrega primero
                        else:
                            titles.append(self.chunks.popleft())
                    data = b''.join(parts)
                    break
                if self.ended:
                    data = None
                    break
                if titles:
                    data = b''
                    break
                self.cond.wait(WAIT_STEP_S)
        for title in titles:
            self._announce_title(title.text)
        return data

    def ended_cleanly(self, gen: int) -> bool:
        """¿El audio acabó como acabó el origen, entero (no un corte)?"""
        with self.cond:
            return (self.ended and self.clean_end and not self.closed
                    and gen == self.client_gen and not self.chunks)

    # ---------------------------------------------------------- origen
    def _read_loop(self) -> None:
        try:
            self._wait_previous()
            self._run()
        finally:
            # Lo que no se decidió (o un fallo inesperado del lector) no deja
            # al reproductor esperando: 502 o fin tras lo que quede
            self._give_up()

    def _run(self) -> None:
        retries = 0     # reconexiones seguidas sin STABLE_AUDIO_S de audio
        while True:
            state, audio_s = self._read_once()
            if state == 'stop':
                return
            delays = tuple(self.owner.RETRY_DELAYS_S)
            with self.cond:
                if self.closed or self.outcome or self.ended:
                    return
                if state == 'plain':
                    pass                  # metaint inválido: otra vez, sin metadatos
                elif self.first_data is None:
                    return                # la 1.ª conexión no dio audio: 502
                elif not self.splice:
                    # Sin empalme: se vacía y se cierra, con el mismo final que
                    # el origen (entero o cortado)
                    self.clean_end = self.last_clean
                    return
                else:
                    if audio_s >= self.owner.STABLE_AUDIO_S:
                        retries = 0
                    if retries >= len(delays):
                        return            # el origen no vuelve: decide MusicService
            delay = delays[min(retries, len(delays) - 1)] if delays else 0.0
            retries += 1
            if not self._sleep(delay):
                return

    def _wait_previous(self) -> None:
        """Una sola conexión con la emisora: la de la sesión anterior tiene
        que estar cerrada antes de abrir esta (con plazo; desde este hilo)."""
        with self.cond:
            previous, self.previous = self.previous, None
        try:
            if previous is not None and previous is not self:
                previous.wait_released(self.owner.PREVIOUS_WAIT_S)
        finally:
            with self.cond:
                self.waiting_previous = False
                self.cond.notify_all()

    def wait_released(self, timeout: float) -> bool:
        """¿Esta sesión ya no tiene conexión con el origen? Su lector terminó,
        o la sesión está cerrada con la conexión cortada, sin otra a medias y
        sin seguir esperando a la suya anterior (con «siguiente» varias veces
        seguidas, la de antes de antes aún podría estar abierta)."""
        end = time.monotonic() + max(0.0, timeout)
        with self.cond:
            while True:
                reader = self.reader
                if reader is None or not reader.is_alive():
                    return True
                if self.released and not (self.connecting or self.waiting_previous):
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(min(left, WAIT_STEP_S))

    def _sleep(self, seconds: float) -> bool:
        """Espera antes de reconectar; False si la sesión se cerró."""
        end = time.monotonic() + seconds
        with self.cond:
            while not (self.closed or self.outcome):
                left = end - time.monotonic()
                if left <= 0:
                    return True
                self.cond.wait(left)
        return False

    def _give_up(self) -> None:
        """El lector deja de intentarlo. Sin audio entregado: se contesta 502;
        con audio: se sirve lo que quede y se cierra."""
        with self.cond:
            if self.closed or self.outcome or self.ended:
                return
            if self.first_data is None:
                self._fail_locked()
            else:
                self.ended = True
                self.cond.notify_all()

    def _read_once(self) -> Tuple[str, float]:
        """Una conexión con el origen, hasta que se corte. → ('drop', s de
        audio) si se cortó, ('plain', 0) para repetir sin metadatos o
        ('stop', 0) si ya no hay que seguir (lo decidido queda en la sesión,
        también la falta de saldo del tope global)."""
        dl = Deadline(_NO_TOTAL_S)
        with self.cond:
            if self.closed or self.outcome:
                return 'stop', 0.0
            # El saldo se gasta aquí, con la sesión viva: una cerrada mientras
            # esperaba a la anterior no cuenta una conexión que no abre
            budget = self.budget
            if budget is not None and not budget.take():
                self.fail_kind = self.fail_kind or 'budget'
                return 'stop', 0.0
            self.deadline = dl
            self.connecting = True
            conn = _Conn(first=self.first_data is None)
            if self.started is None:
                self.started = time.monotonic()
        # «Connection: close», como FFmpeg: si el origen redirige (bigFM a su
        # CDN), la conexión del 302 no queda abierta en el pool de requests
        # junto a la del audio. Cada reconexión abre otra de todos modos
        headers = {'User-Agent': self.user_agent, 'Accept-Encoding': 'identity',
                   'Connection': 'close'}
        if self.want_icy:
            headers['Icy-MetaData'] = '1'
        try:
            with dl, dl.session.get(self.url, stream=True, headers=headers,
                                    timeout=(CONNECT_TIMEOUT_S, self.owner.DEAD_AIR_S)) as resp:
                self._connected()
                result = self._consume(resp, conn)
        except Exception as exc:  # noqa: BLE001 - red caída, silencio, cabeceras raras…
            result = self._conn_error(exc, conn)
        finally:
            with self.cond:
                self.connecting = False
                self.last_clean = conn.clean
                if self.deadline is dl:
                    self.deadline = None
                self.cond.notify_all()
        if conn.redirect:
            # La conexión ya está cerrada (with): ahora puede conectar el reproductor
            self._bypass(conn.redirect)
            return 'stop', 0.0
        return result

    def _connected(self) -> None:
        with self.cond:
            self.connecting = False
            self.cond.notify_all()

    def _conn_error(self, exc: Exception, conn: _Conn) -> Tuple[str, float]:
        with self.cond:
            if self.closed:
                return 'stop', 0.0
        if conn.first and is_icy_status_line(exc):
            conn.redirect = 'icy'   # «ICY 200 OK»: requests no lo entiende y FFmpeg sí
            return 'stop', 0.0
        if conn.first and conn.audio_at is None and _only_proxy_fails(exc):
            # Certificado que requests no valida o proxy del sistema caído: la
            # emisora no vio ninguna petición y el reproductor sí puede abrirla
            conn.redirect = 'tls'
            return 'stop', 0.0
        return 'drop', conn.audio_s()

    def _consume(self, resp, conn: _Conn) -> Tuple[str, float]:
        """Cabeceras y cuerpo de una conexión ya abierta."""
        status = resp.status_code
        conn.ctype = (resp.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
        has_icy = any(k.lower().startswith('icy-') for k in resp.headers.keys())
        if status in REJECT_STATUS:
            self._reject(status)
            return 'stop', 0.0
        if status >= 400:
            self._http_failure(conn)
            return 'drop', 0.0
        if 'html' in conn.ctype:
            self._reject(status)    # la página de «cuenta en pausa» donde iba el audio
            return 'stop', 0.0
        if any(x in conn.ctype for x in _PLAYLIST_TYPES):
            if conn.first:
                conn.redirect = 'hls'
            else:
                self._end()          # una reconexión que ya no es este audio
            return 'stop', 0.0
        if any(x in conn.ctype for x in _NOT_AUDIO) and not has_icy:
            self._http_failure(conn)
            return 'drop', 0.0
        # Las cabeceras de un archivo; se confirma con el principio del cuerpo
        # (una página o una lista HLS servidas con tipo de audio también las traen)
        track = conn.first and not self.known_live and not has_icy and bool(
            resp.headers.get('Content-Length')
            or (resp.headers.get('Accept-Ranges') or 'none').strip().lower() != 'none')
        demux = None
        if self.want_icy:
            try:
                metaint = parse_metaint(resp.headers.get('icy-metaint'))
            except ValueError:
                # No se sabe dónde van los bloques: mezclarlos con el audio no
                with self.cond:
                    self.want_icy = False
                return 'plain', 0.0
            demux = IcyDemuxer(metaint) if metaint else None
        reference = self.format or ('', None)
        if track:
            pass                     # no se va a servir aquí: nada que publicar
        elif conn.first:
            self._take_headers(resp.headers, conn.ctype, has_icy)
        elif normalize_ctype(conn.ctype) != reference[0]:
            self._end()              # otro formato: no se empalma
            return 'stop', 0.0
        elif reference[1] is not None:
            conn.held = []           # el audio espera a saber si es el mismo formato
        # Un cierre al final de un cuerpo delimitado es su fin; sin tamaño ni
        # partes, un cierre puede ser un corte (FFmpeg lo trata como error)
        delimited = bool(resp.headers.get('Content-Length')) or 'chunked' in (
            resp.headers.get('Transfer-Encoding') or '').lower()
        raw = resp.raw
        while True:
            chunk = raw.read1(READ_SIZE)
            if not chunk:
                conn.clean = delimited
                return 'drop', conn.audio_s()
            audio, blocks = demux.feed(chunk) if demux is not None else (chunk, [])
            if audio and not conn.sniffed:
                conn.sniffed = True
                head = audio.lstrip()[:16].lower()
                if head.startswith(_HTML_STARTS):
                    self._reject(status)
                    return 'stop', 0.0
                if head.startswith(b'#extm3u'):
                    if conn.first:
                        conn.redirect = 'hls'   # una lista HLS servida como audio
                    else:
                        self._end()
                    return 'stop', 0.0
                if track:
                    conn.redirect = 'track'   # un archivo: que lo pida el reproductor con Range
                    return 'stop', 0.0
            items = self._interleave(audio, blocks)
            if not items:
                continue
            if audio and conn.audio_at is None:
                conn.audio_at = time.monotonic()
            if not self._deliver(conn, audio, items):
                return 'stop', 0.0

    def _http_failure(self, conn: _Conn) -> None:
        if conn.first:
            with self.cond:
                self.fail_kind = 'http'   # el origen contestó: ir directo no cambiaría nada

    @staticmethod
    def _interleave(audio: bytes, blocks: List[Tuple[int, bytes]]
                    ) -> List[Union[bytes, _Title]]:
        """Audio y marcas de título en el orden en que venían."""
        items: List[Union[bytes, _Title]] = []
        pos = 0
        for at, block in blocks:
            if not has_stream_title(block):
                continue
            if at > pos:
                items.append(audio[pos:at])
                pos = at
            items.append(_Title(parse_stream_title(block)))
        if pos < len(audio):
            items.append(audio[pos:])
        return items

    def _deliver(self, conn: _Conn, audio: bytes, items: List[Union[bytes, _Title]]) -> bool:
        """Pasa lo leído a la cola. En una reconexión, primero se comprueba
        que el formato sea el de la primera conexión. False = parar."""
        if conn.held is None:
            if conn.first and not conn.probe.done and conn.probe.feed(audio):
                with self.cond:   # la referencia para las reconexiones
                    self.format = (self.format[0] if self.format else '',
                                   conn.probe.signature)
            return self._push(items)
        conn.held.extend(items)
        if not conn.probe.feed(audio):
            return True
        held, conn.held = conn.held, None
        reference = self.format or ('', None)
        if not same_format(reference, (normalize_ctype(conn.ctype), conn.probe.signature)):
            self._end()
            return False
        return self._push(held)

    def _push(self, items: List[Union[bytes, _Title]]) -> bool:
        with self.cond:
            if self.closed or self.outcome or self.ended:
                return False
            now = time.monotonic()
            for item in items:
                self.chunks.append(item)
                if isinstance(item, bytes):
                    self.size += len(item)
                    if self.first_data is None:
                        self.first_data = now
            while self.size > MAX_BUFFER_BYTES and len(self.chunks) > 1:
                item = self.chunks.popleft()
                if isinstance(item, bytes):
                    self.size -= len(item)
                else:
                    self.dropped_title = item
            self._check_ready(now)
            self.cond.notify_all()
            return True

    def _take_headers(self, src, ctype: str, has_icy: bool) -> None:
        audio = ctype.startswith('audio/') or ctype == 'application/ogg'
        with self.cond:
            if ctype:
                self.headers['Content-Type'] = ctype
            for name in _PASS_HEADERS:
                value = src.get(name)
                if value:
                    self.headers[name] = value
            # Solo se empalma un directo (y con búfer: con precarga 0 la sesión
            # acaba con su conexión, como si fuera directa); una pista servida
            # sin tamaño se repetiría
            self.splice = (self.prefill_s > 0 and _splices(ctype, self.url)
                           and (self.known_live or has_icy))
            self.format = (normalize_ctype(ctype), None)
            self.meta = {'title': None,
                         'name': fix_header_text(src.get('icy-name')) or None,
                         'bitrate': parse_bitrate(src.get('icy-br')) or None,
                         'mime': ctype if audio else None}
            info = dict(self.meta)
        self.owner._report(self, 'metadata', info)

    def _announce_title(self, title: Optional[str]) -> None:
        with self.cond:
            if self.closed or self.meta.get('title') == title:
                return
            self.meta['title'] = title
            info = dict(self.meta)
        self.owner._report(self, 'metadata', info)

    def _reject(self, status: int) -> None:
        """La emisora rechaza la conexión: ni un intento más. MusicService lo
        sabe antes de que el reproductor reciba el error (last_rejected por si
        el fallo de FFmpeg llega antes que la señal)."""
        with self.cond:
            if self.closed:
                return
        self.owner._report(self, 'rejected', status)
        with self.cond:
            if self.first_data is None:
                self.outcome = 'fail'
                self.fail_status = status if status >= 400 else 502
            else:
                self.ended = True    # se sirve lo que quede y se cierra
            self.cond.notify_all()

    def _end(self) -> None:
        with self.cond:
            self.ended = True
            self.cond.notify_all()

    def _bypass(self, reason: str) -> None:
        with self.cond:
            if self.closed or self.outcome:
                return
            self.outcome = 'bypass'
            self.cond.notify_all()
        if reason == 'track':
            self.owner._report(self, 'track')
        self.owner._report(self, 'bypassed')

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
        with self.cond:
            # La conexión con el origen ya está cortada (el servidor ve el
            # cierre); una que estuviera a medio abrir se corta al conectar
            self.released = True
            self.cond.notify_all()
        if sock is not None:
            _shutdown(sock)


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 para servir por partes (chunked): la parte final vacía es el
    # fin del audio; una parte a medias, un corte. Sin tamaño ni partes,
    # FFmpeg tomaría cualquier cierre por un error, y el fin de una pista
    # servida por partes parecería el corte de una radio
    protocol_version = "HTTP/1.1"
    server_version = "PomoGrowth"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def _empty(self, status: int, location: str = '') -> None:
        self.send_response(status)
        if location:
            self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):  # noqa: N802 - nombre de la API de http.server
        self.close_connection = True      # una petición por conexión
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
                # El lector ya cerró su conexión: ahora conecta el reproductor
                self._empty(302, session.url)
                return
            if outcome == 'fail':
                # El error tal cual (403…) o 502: FFmpeg falla y MusicService
                # decide. Un 302 haría que FFmpeg lo intentara otra vez
                session._abort_reader()
                self._empty(session.fail_status)
                return
            if outcome != 'serve':
                return
            self.send_response(200)
            with session.cond:
                headers = dict(session.headers)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            while True:
                data = session.take(gen)
                if data is None:
                    if session.ended_cleanly(gen):
                        self.wfile.write(b'0\r\n\r\n')
                    else:
                        # Un corte: el tamaño de una parte y cerrar sin ella.
                        # Cerrar tras una parte entera, FFmpeg lo lee como el
                        # fin del archivo (AVERROR_EOF), no como un error
                        self.wfile.write(b'1\r\n')
                    return
                if data:
                    # Bloquea si el reproductor no lee: es el freno
                    self.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
        except OSError:
            pass   # el reproductor se fue o close() cortó su conexión


class _Server(ThreadingHTTPServer):
    daemon_threads = True     # un reproductor colgado no retiene el cierre

    def handle_error(self, request, client_address):
        pass   # el reproductor cortó a mitad de petición: nada que contar


class StreamBuffer(QObject):
    """Proxy local para la conexión en curso (una sola a la vez).
    open() devuelve la URL local que dar al reproductor (None si el proxy no
    puede abrir: entonces se conecta directo); close() la corta."""
    bypassed = Signal(int)            # sid: 302, el reproductor conecta directo
    track_detected = Signal(int)      # sid: era una pista (Content-Length…), no un directo
    rejected = Signal(int, int)       # sid, estado HTTP: la emisora rechazó la conexión
    metadata = Signal(int, object)    # sid, {'title','name','bitrate','mime'}

    # Tiempos (s). Atributos de clase para poder acortarlos en los tests
    RETRY_DELAYS_S: Tuple[float, ...] = (2.0, 4.0, 8.0)   # reconexiones seguidas sin audio estable
    DEAD_AIR_S = 10.0         # silencio que da la conexión por muerta
    STABLE_AUDIO_S = 30.0     # audio seguido que reinicia los reintentos
    PREVIOUS_WAIT_S = 3.0     # plazo para que cierre la conexión de la sesión anterior

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[_Session] = None
        self._previous: Optional[_Session] = None   # la última con lector: se espera a que cierre
        self._next_sid = 0
        # (sid, estado) del último rechazo y (sid, 'http'|'network') del último
        # fallo de una primera conexión: por si el error de FFmpeg llega antes
        self.last_rejected: Optional[Tuple[int, int]] = None
        self.last_failure: Optional[Tuple[int, str]] = None

    @property
    def port(self) -> int:
        server = self._server
        return server.server_address[1] if server is not None else 0

    @property
    def session_id(self) -> int:
        """La sesión en curso (0 si no hay)."""
        session = self._session
        return session.sid if session is not None else 0

    def open(self, url: str, prefill_s: float, known_live: bool = True,
             budget: Optional[ConnectionBudget] = None) -> Optional[str]:
        """Nueva sesión (cierra la anterior). known_live=False: aún no se sabe
        si es un directo; las cabeceras deciden (una pista va por 302)."""
        self.close()
        if requests is None or not self._ensure_server():
            return None
        with self._lock:
            self._next_sid += 1
            session = self._session = _Session(url, prefill_s, self._next_sid, self,
                                               bool(known_live), budget, self._previous)
        return f"http://127.0.0.1:{self.port}/{session.token}"

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is None:
            return
        session.close()
        # Ya cerrada no puede arrancar su lector: si no lo tuvo, la siguiente
        # espera a la que hubiera antes
        self._previous = session if session.reader is not None else (
            session.previous or self._previous)

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

    def _report(self, session: _Session, what: str, arg: Any = None) -> None:
        """Avisa desde los hilos (la señal llega encolada al hilo de UI)."""
        if what == 'rejected':
            self.last_rejected = (session.sid, int(arg))
        if self._session is not session:
            return   # una sesión vieja: ya no le importa a nadie
        try:
            if what == 'metadata':
                self.metadata.emit(session.sid, arg)
            elif what == 'rejected':
                self.rejected.emit(session.sid, int(arg))
            elif what == 'track':
                self.track_detected.emit(session.sid)
            elif what == 'bypassed':
                self.bypassed.emit(session.sid)
        except RuntimeError:
            pass   # el servicio ya se destruyó
