"""Resolver un enlace o ruta pegada en pistas reproducibles (bloqueante).

resolve_link() se ejecuta fuera del hilo de UI (services.workers.run_in_thread):
mira las cabeceras de la URL SIN descargar el audio (stream=True y se cierra
la respuesta), descarga como mucho 1 MB si es una playlist y decide:
  archivo de audio local → 1 entrada · playlist local → sus entradas ·
  carpeta local → sus audios (no recursivo) · playlist remota → solo sus
  enlaces http(s) · HLS → 1 stream en directo (la local, solo si no apunta
  fuera de su carpeta) · audio/* o cabeceras icy-* → 1 entrada (radio si
  hay icy-*; sin Content-Length ni icy-* aún no se sabe: un MP3 servido por
  partes también llega así) · página web → error.
Toda la petición tiene un plazo total (Deadline): un servidor que gotea no
la mantiene viva. Los mensajes de error van en español y nunca incluyen la
URL (puede llevar tokens de acceso).
"""
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlsplit

from core.music_library import (AUDIO_EXTS, EXT_MIME, MAX_LIST_NAME, PLAYLIST_CTYPES,
                                PLAYLIST_EXTS, Entry, classify_link, clean_text,
                                detect_playlist, local_hls_ok, parse_playlist,
                                playlist_name, scan_audio_folder, source_ext,
                                title_from_source)

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3 import poolmanager as _poolmanager
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
except ImportError:  # pragma: no cover - requests viene en requirements
    requests = None

USER_AGENT = 'PomoGrowth/9'
MAX_PLAYLIST_BYTES = 1024 * 1024
MSG_HLS_UNSAFE = ("Esa lista HLS apunta a rutas de red o a archivos fuera de su carpeta: "
                  "por seguridad no se reproduce")


# ------------------------------------------------------------------ plazo total
_local = threading.local()


def _shutdown(sock: Any) -> None:
    import socket
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass


class Deadline:
    """Plazo TOTAL de una petición. requests solo limita cada lectura del
    socket, así que un servidor que gotea un byte por segundo la mantendría
    viva sin fin (también mientras llegan las cabeceras). Al vencer el plazo,
    o con abort() desde otro hilo, se cortan los sockets de la petición (en
    Windows la lectura en curso se despierta con el siguiente byte o, como
    mucho, con el timeout por lectura). En el hilo de la petición:
        with Deadline(8) as dl:
            dl.session.get(...)
    """

    def __init__(self, seconds: float):
        self.seconds = max(0.1, float(seconds))
        self.end = time.monotonic() + self.seconds
        self.expired = False
        self.session = None
        self._closed = False
        self._sockets: List[Any] = []
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def __enter__(self) -> 'Deadline':
        self.end = time.monotonic() + self.seconds
        self.session = _new_session()
        _local.deadline = self
        self._timer = threading.Timer(self.seconds, self.abort)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._timer is not None:
            self._timer.cancel()
        _local.deadline = None
        with self._lock:
            self._closed = True       # no tocar sockets que urllib3 cierre o reutilice
            self._sockets.clear()
        if self.session is not None:
            self.session.close()

    def track(self, sock: Any) -> None:
        with self._lock:
            if self._closed:
                return
            if not self.expired:
                self._sockets.append(sock)
                return
        _shutdown(sock)

    def abort(self) -> None:
        """Corta la petición ya (sirve desde cualquier hilo, también antes de
        empezarla: entonces ni llega a hacerse)."""
        with self._lock:
            if self.expired:
                return
            self.expired = True
            if self._closed:
                return
            for sock in self._sockets:
                _shutdown(sock)
            self._sockets = []


def _track_socket(sock: Any) -> None:
    deadline = getattr(_local, 'deadline', None)
    if deadline is not None and sock is not None:
        deadline.track(sock)


if requests is not None:
    class _Tracked:
        """Apunta los sockets de la conexión en el Deadline del hilo: el
        recién conectado y, en https, el ya cifrado."""

        def _new_conn(self):
            sock = super()._new_conn()
            _track_socket(sock)
            return sock

        def connect(self):
            super().connect()
            _track_socket(self.sock)

    class _HTTPConnection(_Tracked, HTTPConnection):
        pass

    class _HTTPSConnection(_Tracked, HTTPSConnection):
        pass

    class _HTTPPool(HTTPConnectionPool):
        ConnectionCls = _HTTPConnection

    class _HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = _HTTPSConnection

    _POOLS = {'http': _HTTPPool, 'https': _HTTPSPool}

    class _TrackingAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = _POOLS

        def proxy_manager_for(self, proxy, **kwargs):
            manager = super().proxy_manager_for(proxy, **kwargs)
            # Un proxy SOCKS trae sus propias clases: se respetan
            if manager.pool_classes_by_scheme is _poolmanager.pool_classes_by_scheme:
                manager.pool_classes_by_scheme = _POOLS
            return manager


def _new_session():
    session = requests.Session()
    adapter = _TrackingAdapter()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

MSG_HTML = ("Ese enlace es una página web, no un audio: usa el enlace directo del "
            "stream o del archivo (.mp3, .m3u, .pls)")
MSG_NOT_AUDIO = ("Ese enlace no lleva a un audio ni a una lista de reproducción: usa "
                 "el enlace directo del stream o del archivo (.mp3, .m3u, .pls)")
MSG_EMPTY_LIST = "La lista no tiene pistas que se puedan reproducir"


@dataclass
class ResolveResult:
    entries: List[Entry] = field(default_factory=list)
    list_name: Optional[str] = None   # playlist o carpeta: nombre para una lista nueva
    error: Optional[str] = None


def _decode(raw: bytes) -> str:
    """Texto de una playlist: UTF-8 (con o sin BOM) o, si no, Windows-1252."""
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1', errors='replace')


def fix_header_text(value: Optional[str]) -> str:
    """Las cabeceras llegan como latin-1; las radios mandan UTF-8 o, las
    más viejas, Windows-1252 («Radio Café ’80s»)."""
    if not value:
        return ''
    try:
        raw = value.encode('latin-1')
    except UnicodeEncodeError:
        raw = None
    if raw is not None:
        for enc in ('utf-8', 'cp1252'):
            try:
                value = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    return clean_text(value, 200)


def parse_bitrate(value: Optional[str]) -> int:
    """icy-br: '320' o '128,128' → kbps (0 si no se entiende)."""
    digits = ''
    for ch in (value or '').strip():
        if ch.isdigit():
            digits += ch
        else:
            break
    try:
        kbps = int(digits) if digits else 0
    except ValueError:
        return 0
    return kbps if 0 < kbps <= 100000 else 0


# ------------------------------------------------------------------ locales
def _resolve_local(path_str: str) -> ResolveResult:
    path = Path(path_str)
    try:
        if path.is_dir():
            files = scan_audio_folder(path)
            if not files:
                return ResolveResult(error="Esa carpeta no tiene archivos de audio")
            entries = [Entry(source=str(p), title=title_from_source(str(p)), live=False,
                             mime=EXT_MIME.get(p.suffix.lower(), '')) for p in files]
            return ResolveResult(entries=entries,
                                 list_name=clean_text(path.name, MAX_LIST_NAME) or 'Carpeta')
        ext = path.suffix.lower()
        if ext in PLAYLIST_EXTS:
            with open(path, 'rb') as fh:
                raw = fh.read(MAX_PLAYLIST_BYTES + 1)
            if len(raw) > MAX_PLAYLIST_BYTES:
                return ResolveResult(error="La lista es demasiado grande (más de 1 MB)")
            text = _decode(raw)
            fmt = detect_playlist(text, url=str(path))
            if fmt == 'hls':
                # FFmpeg abrirá lo que diga dentro, sin el filtro de las demás listas
                if not local_hls_ok(text):
                    return ResolveResult(error=MSG_HLS_UNSAFE)
                return ResolveResult(entries=[Entry(source=str(path),
                                                    title=title_from_source(str(path)),
                                                    live=True)])
            entries = parse_playlist(text, fmt, base=str(path.parent), remote=False)
            if not entries:
                return ResolveResult(error=MSG_EMPTY_LIST)
            name = playlist_name(text, fmt) or clean_text(path.stem, MAX_LIST_NAME)
            return ResolveResult(entries=entries, list_name=name or 'Lista')
        if ext in AUDIO_EXTS:
            return ResolveResult(entries=[Entry(source=str(path),
                                                title=title_from_source(str(path)),
                                                live=False, mime=EXT_MIME.get(ext, ''))])
    except OSError:
        return ResolveResult(error="No se pudo leer ese archivo o carpeta")
    return ResolveResult(error="Ese archivo no es de audio ni una lista "
                               "(.mp3, .m4a, .flac, .m3u, .pls…)")


def local_hls_file_ok(path: str) -> bool:
    """Para un item local .m3u/.m3u8/.hls justo antes de sonar (puede venir
    de datos importados o haber cambiado desde que se añadió): ¿se le puede
    dar a FFmpeg? Una de más de 1 MB no se revisa y no se da."""
    try:
        with open(path, 'rb') as fh:
            raw = fh.read(MAX_PLAYLIST_BYTES + 1)
    except OSError:
        return True  # si no se puede leer, FFmpeg tampoco: fallará al cargar
    return len(raw) <= MAX_PLAYLIST_BYTES and local_hls_ok(_decode(raw))


# ------------------------------------------------------------------ remotos
def _network_error(exc: Exception) -> str:
    if requests is not None:
        if isinstance(exc, requests.exceptions.Timeout):
            return "El servidor tardó demasiado en responder: inténtalo de nuevo más tarde"
        if isinstance(exc, requests.exceptions.SSLError):
            return "No se pudo establecer una conexión segura con el servidor"
        if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                            requests.exceptions.InvalidSchema)):
            return "Ese enlace no es válido"
        if isinstance(exc, requests.exceptions.TooManyRedirects):
            return "El enlace redirige demasiadas veces"
    return "No se pudo conectar con el servidor: revisa el enlace y tu conexión"


def is_icy_status_line(exc: Exception) -> bool:
    """Los SHOUTcast v1 contestan «ICY 200 OK», que http.client rechaza
    (BadStatusLine). FFmpeg sí lo entiende: es una radio en directo."""
    seen, cur = set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if 'ICY 200' in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__ or (cur.args[0] if cur.args and
                                                   isinstance(cur.args[0], Exception)
                                                   else None)
    return False


def _read_limited(resp, limit: int, deadline: Deadline) -> Optional[bytes]:
    """Cuerpo de una playlist: None si pasa de `limit` bytes o del plazo (un
    stream de audio servido con tipo de playlist no se descarga entero).
    read1 devuelve lo que llegue en cada lectura: el plazo se mira aunque el
    servidor gotee."""
    data = bytearray()
    while True:
        if deadline.expired or deadline.remaining() <= 0:
            return None
        chunk = resp.raw.read1(16384, decode_content=True)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            return None
    return None if deadline.expired else bytes(data)


def _resolve_url(url: str, timeout: float,
                 deadline: Optional[Deadline] = None) -> ResolveResult:
    if requests is None:
        return ResolveResult(error="Falta el módulo requests para abrir enlaces")
    headers = {'Icy-MetaData': '1', 'User-Agent': USER_AGENT}
    dl = deadline or Deadline(max(timeout, 1) * 2)
    if dl.expired:
        return ResolveResult(error="Se canceló la comprobación del enlace")
    try:
        with dl, dl.session.get(url, stream=True, headers=headers, timeout=timeout,
                                allow_redirects=True) as resp:
            if resp.status_code >= 400:
                return ResolveResult(error=f"El servidor respondió con un error "
                                           f"(HTTP {resp.status_code})")
            ctype = (resp.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
            final_url = resp.url or url
            exts = {source_ext(url), source_ext(final_url)}
            icy = {k.lower(): v for k, v in resp.headers.items() if k.lower().startswith('icy-')}
            audio_type = ctype.startswith('audio/') or ctype == 'application/ogg'
            if ctype in ('text/html', 'application/xhtml+xml'):
                return ResolveResult(error=MSG_HTML)
            # Playlist por su tipo, o por la extensión si el servidor no dice
            # que es audio (un .m3u servido como audio/mpeg es un stream)
            if ctype in PLAYLIST_CTYPES or (exts & PLAYLIST_EXTS and not icy
                                            and not audio_type):
                raw = _read_limited(resp, MAX_PLAYLIST_BYTES, dl)
                if raw is None:
                    return ResolveResult(error="La lista es demasiado grande o no termina de "
                                               "descargarse")
                text = _decode(raw)
                fmt = detect_playlist(text, url=final_url, content_type=ctype)
                if fmt == 'hls':
                    return ResolveResult(entries=[Entry(source=url,
                                                        title=title_from_source(url),
                                                        live=True, mime=ctype)])
                entries = parse_playlist(text, fmt, base=final_url, remote=True)
                if not entries:
                    return ResolveResult(error=MSG_EMPTY_LIST)
                name = playlist_name(text, fmt) or clean_text(title_from_source(url),
                                                              MAX_LIST_NAME)
                return ResolveResult(entries=entries, list_name=name or 'Lista')
            is_audio = (audio_type or bool(icy)
                        or (ctype in ('', 'application/octet-stream', 'binary/octet-stream')
                            and exts & AUDIO_EXTS))
            if is_audio:
                name = fix_header_text(icy.get('icy-name'))
                # Directo solo con icy-*; sin tamaño ni icy-* puede ser un MP3
                # servido por partes (chunked): se sabrá al sonar
                live = True if icy else (False if resp.headers.get('Content-Length') else None)
                mime = ctype if ctype not in ('application/octet-stream',
                                              'binary/octet-stream') else ''
                if not mime:
                    mime = next((EXT_MIME[e] for e in exts if e in EXT_MIME), '')
                return ResolveResult(entries=[Entry(
                    source=url, title=name or title_from_source(url), live=live,
                    mime=mime, bitrate=parse_bitrate(icy.get('icy-br')))])
            return ResolveResult(error=MSG_NOT_AUDIO)
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de red → mensaje
        if is_icy_status_line(exc):
            return ResolveResult(entries=[Entry(source=url, title=title_from_source(url),
                                                live=True)])
        host = urlsplit(url).hostname or '?'
        print(f"Música: no se pudo resolver un enlace de {host}: {type(exc).__name__}")
        if dl.expired:
            return ResolveResult(error="El servidor tardó demasiado en responder: "
                                       "inténtalo de nuevo más tarde")
        return ResolveResult(error=_network_error(exc))


def resolve_link(text: str, timeout: float = 10,
                 deadline: Optional[Deadline] = None) -> ResolveResult:
    """Enlace o ruta pegada → entradas reproducibles (o un error en español).
    Un enlace tiene un plazo total de 2×timeout (o el `deadline` dado, que
    se puede cortar con abort() desde otro hilo)."""
    info = classify_link(text)
    if info.kind in ('unsupported', 'invalid'):
        return ResolveResult(error=info.reason)
    if info.kind == 'file':
        return _resolve_local(info.normalized)
    return _resolve_url(info.normalized, timeout, deadline)
