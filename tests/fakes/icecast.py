"""Emisora Icecast falsa para los tests de radio (solo 127.0.0.1).

Imita a una radio de pago tipo DI.FM: un Hub con varios «hosts» (prem1, prem2
y prem4, cada uno en su puerto efímero, como los espejos de un .pls) y un
«listen» que sirve el .pls. Todos comparten el Hub, que REGISTRA cada
petición de audio (apertura y cierre, User-Agent, Icy-MetaData, Range,
estado, bytes y cómo acabó) y cuenta las abiertas a la vez, en total y por
clave: max_active es lo que una radio de pago no admite por encima de 1. El
cierre del cliente se ve al momento (se mira el socket entre envío y envío,
y el de todas las abiertas al llegar una nueva: un servidor de verdad ya
tiene su FIN), no al siguiente fallo de escritura; una petición rechazada
también cuenta como conexión abierta mientras dura.

Rutas: /<canal>_hi?<clave> es el audio; /<canal>.pls?listen_key=<clave>, la
lista con las tres entradas (pídesela al host «listen»).

Modos (Hub.set_mode(modo, kill=False, **ajustes); valen para las conexiones
nuevas y kill=True corta además las abiertas):
- 'normal': 200 con icy-name/icy-br/icy-genre, una ráfaga inicial (burst
  bytes) y luego a tiempo real (byterate). Con «Icy-MetaData: 1», icy-metaint
  y, cada metaint bytes, un bloque «StreamTitle='Artista N - Tema N';» (N
  cambia cada title_every bloques; los de en medio van vacíos, L = 0). Con
  el ajuste metaint_header, la cabecera icy-metaint lleva ese texto (uno que
  no vale: 'abc', '0'…) aunque los bloques sigan yendo cada metaint bytes.
- 'noicy': como 'normal' pero sin ninguna cabecera icy-* ni metadatos (un
  directo servido por nginx o un CDN).
- 'reject401', 'reject403', 'reject429'… (cualquier rejectNNN): ese estado
  con una página HTML.
- 'html200': 200 text/html, la página de «cuenta en pausa».
- 'html_as_audio', 'hls_as_audio': 200 audio/mpeg con Content-Length y, de
  cuerpo, esa página o una lista HLS (solo el contenido dice lo que es).
- 'accept_close': lee la petición y cierra sin contestar.
- 'down404': 404 con HTML (un Icecast sin fuente).
- 'silent': como 'normal' hasta la ráfaga y luego se calla sin cerrar; las
  conexiones abiertas también se callan y siguen al volver a 'normal'.
- 'deaf': como 'normal' hasta la ráfaga y luego ni manda ni mira el socket:
  no se entera de que el cliente cerró ni cierra su lado (en Windows, el
  hilo que lee de ella sigue bloqueado aunque se corte su conexión).
- 'cut': cada conexión emite cut_after_s segundos y cierra limpio (también
  vale para el corte: Hub.cut() corta de golpe las abiertas).
- 'track': una pista de track_s segundos con Content-Length y Accept-Ranges
  (206 con Range), sin icy-*.
- 'chunked': la misma pista por partes (HTTP/1.1 chunked), sin tamaño ni
  icy-*, con su parte final: como la sirven un PHP o un Node.
- 'icy_v1': la línea de estado «ICY 200 OK» de SHOUTcast v1 y unas tramas;
  espera 2 s a que el cliente cierre y, si no, cierra él con
  end='server_timeout'.
Formatos (Hub.formats): el de cada conexión de audio sale de esa lista en
orden y el último se repite: 'mp3' (MPEG-1 capa III, 320 kbps, 44,1 kHz),
'aac' (ADTS AAC-LC 44,1 kHz estéreo) y 'aac22' (ADTS AAC-LC 22,05 kHz). Las
tramas son de silencio (las de AAC solo sirven para mirar sus cabeceras).

start(tls=ruta) sirve por https con ese certificado (tls_cert(): uno de
prueba que certifi no valida, como el de un antivirus que inspecciona
HTTPS). La negociación TLS se hace al aceptar: una que el cliente aborta no
llega a ser una petición y no se registra.
"""
import select
import socket
import ssl
import sysconfig
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

KEY = "clave-de-prueba"
STATION = "DI.FM Lofi (prueba local)"
# 1 trama MPEG-1 capa III, 320 kbps, 44,1 kHz (1044 bytes, ~26 ms) de silencio
MP3_FRAME = bytes([0xFF, 0xFB, 0xE0, 0x04]) + bytes(1040)


def adts_frame(sf_index: int, length: int = 192, channels: int = 2, profile: int = 1) -> bytes:
    """Trama ADTS (cabecera de 7 bytes sin CRC) rellena de ceros."""
    header = bytes([
        0xFF, 0xF1,
        (profile << 6) | (sf_index << 2) | (channels >> 2),
        ((channels & 3) << 6) | ((length >> 11) & 0x03),
        (length >> 3) & 0xFF,
        ((length & 7) << 5) | 0x1F,
        0xFC])
    return header + bytes(length - len(header))


FORMATS = {
    'mp3': ('audio/mpeg', MP3_FRAME),
    'aac': ('audio/aac', adts_frame(4)),      # 44,1 kHz
    'aac22': ('audio/aac', adts_frame(7)),    # 22,05 kHz
}
HTML_PAUSED = (b"<!DOCTYPE html><html><head><title>Access paused</title></head><body>"
               b"<h1>Your account is currently active in more than one place</h1>"
               b"<p>One stream at a time.</p></body></html>")
HTML_404 = (b"<html><head><title>Error 404</title></head><body><b>404 - The file you "
            b"requested could not be found</b></body></html>")
HLS_LIST = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nseg1.aac\n"
_PUBLIC = ('id', 'host', 'path', 'key', 'ua', 'icy', 'range', 't_open', 't_close',
           'status', 'bytes', 'end', 'format')


def tls_cert() -> Optional[str]:
    """Certificado de prueba con su clave (el que trae CPython en Lib/test,
    para «localhost»): certifi no lo valida. None si esta instalación de
    Python no trae la suite de tests."""
    stdlib = Path(sysconfig.get_paths()['stdlib'])
    for rel in ('test/certdata/keycert.pem', 'test/keycert.pem'):
        if (stdlib / rel).is_file():
            return str(stdlib / rel)
    return None


def peer_gone(sock: Any) -> bool:
    """¿Cerró el cliente? Sin esperar: tras la petición, lo único que puede
    mandar es el cierre (FIN o RST; con TLS, que no deja mirar sin leer, su
    aviso de cierre)."""
    try:
        readable, _w, _x = select.select([sock], [], [], 0)
        if not readable:
            return False
        return isinstance(sock, ssl.SSLSocket) or sock.recv(1, socket.MSG_PEEK) == b''
    except (OSError, ValueError):
        return True


class Hub:
    """Estado y registro compartidos por todos los hosts de la emisora."""

    def __init__(self, burst: int = 64 * 1024, byterate: int = 40000, metaint: int = 16000,
                 title_every: int = 1, cut_after_s: float = 1.0, track_s: float = 8.0):
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.burst, self.byterate, self.metaint = burst, byterate, metaint
        self.title_every, self.cut_after_s, self.track_s = title_every, cut_after_s, track_s
        self.metaint_header: Optional[str] = None   # icy-metaint que se anuncia (None = metaint)
        self.scheme = 'http'
        self.mode = 'normal'
        self.formats: List[str] = ['mp3']
        self.records: List[Dict[str, Any]] = []
        self.active: Dict[int, Dict[str, Any]] = {}
        self.max_active = 0
        self.max_active_by_key: Dict[str, int] = {}
        self.stopping = threading.Event()
        self.servers: Dict[str, ThreadingHTTPServer] = {}
        self._next_id = 0
        self._audio_conns = 0

    # ------------------------------------------------------------ uso desde el test
    def now(self) -> float:
        return time.monotonic() - self.t0

    def url(self, host: str = 'prem2', key: str = KEY, channel: str = 'lofi') -> str:
        port = self.servers[host].server_address[1]
        return f"{self.scheme}://127.0.0.1:{port}/{channel}_hi?{key}"

    def pls_url(self, key: str = KEY, channel: str = 'lofi') -> str:
        port = self.servers['listen'].server_address[1]
        return f"{self.scheme}://127.0.0.1:{port}/premium_high/{channel}.pls?listen_key={key}"

    def set_mode(self, mode: str, kill: bool = False, **settings: Any) -> None:
        with self.lock:
            self.mode = mode
            for name, value in settings.items():
                setattr(self, name, value)
            victims = list(self.active.values()) if kill else []
        for rec in victims:
            self._kill(rec, 'server_cut')

    def cut(self) -> None:
        """Corte del origen: cierra de golpe las conexiones abiertas."""
        with self.lock:
            victims = list(self.active.values())
        for rec in victims:
            self._kill(rec, 'server_cut')

    def streams(self, since: float = 0.0) -> List[Dict[str, Any]]:
        """Peticiones de audio (aceptadas o no) abiertas desde `since`."""
        with self.lock:
            return [{k: r[k] for k in _PUBLIC} for r in self.records if r['t_open'] >= since]

    def active_count(self) -> int:
        with self.lock:
            return len(self.active)

    def close(self) -> None:
        self.stopping.set()
        with self.lock:
            victims = list(self.active.values())
        for rec in victims:
            self._kill(rec, 'server_stop')
        for srv in self.servers.values():
            srv.shutdown()
            srv.server_close()

    # ------------------------------------------------------------ registro
    def _open(self, handler: 'Handler', key: str) -> Dict[str, Any]:
        h = handler.headers
        with self.lock:
            # Un servidor de verdad ya tiene el cierre de la anterior (su FIN
            # llega antes que la conexión nueva): se apunta antes de contar
            for old in list(self.active.values()):
                if peer_gone(old['sock']):
                    old['t_close'] = old['t_close'] or self.now()
                    old['end'] = old['end'] or 'client_closed'
                    self.active.pop(old['id'], None)
            self._next_id += 1
            rec = {'id': self._next_id, 'host': handler.server.name,
                   'path': urlsplit(handler.path).path, 'key': key,
                   'ua': h.get('User-Agent') or '', 'icy': h.get('Icy-MetaData') or '',
                   'range': h.get('Range') or '', 't_open': self.now(), 't_close': None,
                   'status': None, 'bytes': 0, 'end': None, 'format': None,
                   'kill': threading.Event(), 'sock': handler.connection}
            self.records.append(rec)
            self.active[rec['id']] = rec
            self.max_active = max(self.max_active, len(self.active))
            same = sum(1 for r in self.active.values() if r['key'] == key)
            self.max_active_by_key[key] = max(self.max_active_by_key.get(key, 0), same)
        return rec

    def _close(self, rec: Dict[str, Any], end: str) -> None:
        with self.lock:
            if rec['t_close'] is None:
                rec['t_close'] = self.now()
                rec['end'] = rec['end'] or end
            self.active.pop(rec['id'], None)

    def _kill(self, rec: Dict[str, Any], why: str) -> None:
        if rec['kill'].is_set():
            return
        rec['end'] = why
        rec['kill'].set()
        try:
            rec['sock'].shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _next_format(self) -> str:
        with self.lock:
            fmt = self.formats[min(self._audio_conns, len(self.formats) - 1)]
            self._audio_conns += 1
        return fmt


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "Icecast 2.4.0-kh15"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802 - nombre de la API de http.server
        hub: Hub = self.server.hub
        parts = urlsplit(self.path)
        if parts.path.endswith('.pls'):
            self._playlist(hub, parts.query.split('listen_key=', 1)[-1])
        elif parts.path.endswith('_hi'):
            rec = hub._open(self, parts.query)
            try:
                self._audio(hub, rec)
            except OSError:
                pass
            finally:
                hub._close(rec, 'client_closed')
        else:
            self._send(404, HTML_404, "text/html")

    # ------------------------------------------------------------ respuestas
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _playlist(self, hub: Hub, key: str) -> None:
        channel = urlsplit(self.path).path.rsplit('/', 1)[-1][:-len('.pls')]
        lines = ["[playlist]", "NumberOfEntries=3"]
        for n, host in enumerate(('prem2', 'prem4', 'prem1'), 1):
            lines += [f"File{n}={hub.url(host, key, channel)}",
                      f"Title{n}=DI.FM (prueba) - {channel}", f"Length{n}=-1"]
        lines.append("Version=2")
        self._send(200, ("\n".join(lines) + "\n").encode(), "audio/x-scpls")

    def _audio(self, hub: Hub, rec: Dict[str, Any]) -> None:
        mode = hub.mode
        if mode.startswith('reject'):
            rec['status'] = int(mode[-3:])
            self._send(rec['status'], HTML_PAUSED, "text/html; charset=utf-8")
        elif mode == 'html200':
            rec['status'] = 200
            self._send(200, HTML_PAUSED, "text/html; charset=utf-8")
        elif mode in ('html_as_audio', 'hls_as_audio'):
            rec['status'] = 200
            self._send(200, HTML_PAUSED if mode == 'html_as_audio' else HLS_LIST, "audio/mpeg")
        elif mode == 'down404':
            rec['status'] = 404
            self._send(404, HTML_404, "text/html")
        elif mode == 'accept_close':
            rec['status'] = 0
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
        elif mode == 'track':
            self._track(hub, rec)
        elif mode == 'chunked':
            self._chunked(hub, rec)
        elif mode == 'icy_v1':
            rec['status'] = 200
            self.wfile.write(b"ICY 200 OK\r\nicy-name: Vieja\r\nContent-Type: audio/mpeg\r\n\r\n"
                             + MP3_FRAME * 8)
            self._wait_gone(hub, rec, 2.0)
        else:
            self._stream(hub, rec, cut_after=hub.cut_after_s if mode == 'cut' else None)

    def _gone(self) -> bool:
        return peer_gone(self.connection)

    def _wait_gone(self, hub: Hub, rec: Dict[str, Any], seconds: float) -> bool:
        """Espera a que el cliente cierre, como mucho `seconds`. True si cerró
        (o lo cortó kill/close); si vence el plazo, el falso cierra él y el
        registro lo dice (end='server_timeout'), no «client_closed»."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if rec['kill'].is_set() or hub.stopping.is_set() or self._gone():
                return True
            time.sleep(0.01)
        rec['end'] = rec['end'] or 'server_timeout'
        return False

    def _stream(self, hub: Hub, rec: Dict[str, Any], cut_after=None) -> None:
        fmt = rec['format'] = hub._next_format()
        ctype, frame = FORMATS[fmt]
        plain = hub.mode == 'noicy'
        icy = self.headers.get("Icy-MetaData") == "1" and not plain
        rec['status'] = 200
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if not plain:
            self.send_header("icy-name", STATION)
            self.send_header("icy-genre", "Lofi")
            self.send_header("icy-br", "320")
            self.send_header("icy-pub", "0")
        self.send_header("Cache-Control", "no-cache, no-store")
        if icy:
            self.send_header("icy-metaint", hub.metaint_header or str(hub.metaint))
        self.end_headers()
        source = frame * max(1, 65536 // len(frame))
        start = time.monotonic()
        paused = 0.0                    # tiempo callado: no cuenta para el ritmo
        silent_from = None
        sent = since_meta = blocks = pos = 0
        while True:
            if hub.mode == 'deaf' and sent >= hub.burst:
                # Ni manda ni mira el socket: no ve el cierre del cliente
                while not (rec['kill'].is_set() or hub.stopping.is_set()):
                    rec['kill'].wait(0.05)
                return
            if rec['kill'].is_set() or hub.stopping.is_set() or self._gone():
                return
            now = time.monotonic()
            if cut_after is not None and now - start >= cut_after:
                rec['end'] = 'served'
                return
            if hub.mode == 'silent' and sent >= hub.burst:
                silent_from = silent_from or now
                time.sleep(0.01)
                continue
            if silent_from is not None:
                paused += now - silent_from
                silent_from = None
            target = hub.burst + (now - start - paused) * hub.byterate
            if sent >= target:
                time.sleep(0.01)
                continue
            n = int(min(target - sent, 8192)) or 1
            chunk = (source[pos:] + source)[:n]
            pos = (pos + n) % len(source)
            out = bytearray()
            i = 0
            while i < len(chunk):
                room = hub.metaint - since_meta if icy else len(chunk) - i
                part = chunk[i:i + room]
                out += part
                i += len(part)
                since_meta += len(part)
                if icy and since_meta == hub.metaint:
                    since_meta = 0
                    if blocks % hub.title_every == 0:
                        song = blocks // hub.title_every + 1
                        meta = f"StreamTitle='Artista {song} - Tema {song}';".encode()
                        meta += bytes((-len(meta)) % 16)
                        out += bytes([len(meta) // 16]) + meta
                    else:
                        out += b'\x00'
                    blocks += 1
            self.wfile.write(out)
            rec['bytes'] += len(out)
            sent += len(chunk)

    def _track(self, hub: Hub, rec: Dict[str, Any]) -> None:
        body = MP3_FRAME * max(1, int(hub.track_s / 0.026))
        rng = self.headers.get("Range") or ''
        start = 0
        if rng.startswith("bytes=") and rng[6:].split('-')[0].isdigit():
            start = min(int(rng[6:].split('-')[0]), len(body))
        rec['status'] = 206 if start else 200
        self.send_response(rec['status'])
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        for i in range(start, len(body), 16384):
            if rec['kill'].is_set() or hub.stopping.is_set() or self._gone():
                return
            part = body[i:i + 16384]
            self.wfile.write(part)
            rec['bytes'] += len(part)
        rec['end'] = 'served'


    def _chunked(self, hub: Hub, rec: Dict[str, Any]) -> None:
        body = MP3_FRAME * max(1, int(hub.track_s / 0.026))
        self.protocol_version = "HTTP/1.1"
        rec['status'] = 200
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(0, len(body), 8192):
            if rec['kill'].is_set() or hub.stopping.is_set() or self._gone():
                return
            part = body[i:i + 8192]
            self.wfile.write(b'%x\r\n%s\r\n' % (len(part), part))
            rec['bytes'] += len(part)
        self.wfile.write(b'0\r\n\r\n')
        self.close_connection = True
        rec['end'] = 'served'


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def start(tls: Optional[str] = None, **settings: Any) -> Hub:
    """Arranca los cuatro hosts (puertos efímeros) con un Hub común; con tls
    (certificado y clave en un .pem), por https."""
    hub = Hub(**settings)
    context = None
    if tls:
        hub.scheme = 'https'
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls)
    for name in ('listen', 'prem1', 'prem2', 'prem4'):
        srv = _Server(("127.0.0.1", 0), Handler)
        if context is not None:
            srv.socket = context.wrap_socket(srv.socket, server_side=True)
        srv.hub = hub
        srv.name = name
        hub.servers[name] = srv
        threading.Thread(target=srv.serve_forever, args=(0.1,), daemon=True,
                         name=f"icecast-falso-{name}").start()
    return hub
