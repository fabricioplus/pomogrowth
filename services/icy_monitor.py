"""«Qué suena ahora» en radios Icecast/SHOUTcast (metadatos ICY).

Qt no expone el StreamTitle de las radios (FFmpeg pide los metadatos y los
quita del audio), así que read_icy_info() abre una conexión aparte, lee hasta
el primer bloque de metadatos (icy-metaint bytes de audio + el bloque) y la
cierra, todo dentro de un plazo total (Deadline). IcyMonitor la repite cada
30 s en un hilo propio (no retiene el cierre de la app) mientras suena una
radio y avisa solo cuando algo cambia.
"""
import re
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from core.music_library import clean_text
from services.music_resolver import Deadline, USER_AGENT, fix_header_text, parse_bitrate

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

MAX_METAINT = 256 * 1024
MAX_META_BLOCK = 255 * 16  # el byte de longitud ×16: 4080 como mucho
_TITLE_RE = re.compile(r"StreamTitle='(.*?)';", re.S)


def parse_stream_title(block: bytes) -> Optional[str]:
    """StreamTitle='Artista - Tema'; → 'Artista - Tema' (None si no hay).
    En UTF-8 o, en las radios viejas, Windows-1252 («Don’t Stop Me Now»)."""
    block = block.rstrip(b'\0')
    for enc in ('utf-8', 'cp1252'):
        try:
            text = block.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = block.decode('latin-1', errors='replace')
    m = _TITLE_RE.search(text)
    if m:
        value = m.group(1)
    else:
        # Sin «';» final (bloque cortado): hasta la última comilla
        start = text.find("StreamTitle='")
        if start < 0:
            return None
        value = text[start + len("StreamTitle='"):]
        value = value[:value.rfind("'")] if "'" in value else value
    value = clean_text(value, 300)
    return value or None


def _read_exact(raw, n: int, deadline: Deadline) -> Optional[bytes]:
    """n bytes del audio (None si se acaba o vence el plazo). read1 devuelve
    lo que traiga cada lectura: con una radio lenta el plazo se mira a
    menudo (read(n) esperaba a reunir los n bytes)."""
    buf = bytearray()
    while len(buf) < n:
        if deadline.expired or deadline.remaining() <= 0:
            return None
        chunk = raw.read1(min(16384, n - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_icy_info(url: str, timeout: float = 8,
                  deadline: Optional[Deadline] = None) -> Dict[str, Any]:
    """{'title', 'name', 'bitrate', 'mime'} de una radio; None en lo que no
    se sepa ('mime' es el Content-Type, para mostrar el códec). Nunca lanza:
    ante cualquier fallo devuelve lo que haya podido leer. Todo en un plazo
    total de `timeout` (o el `deadline` dado, que se puede cortar con
    abort())."""
    info: Dict[str, Any] = {'title': None, 'name': None, 'bitrate': None, 'mime': None}
    dl = deadline or Deadline(timeout)
    if requests is None or dl.expired:
        return info
    try:
        headers = {'Icy-MetaData': '1', 'User-Agent': USER_AGENT}
        with dl, dl.session.get(url, stream=True, headers=headers, timeout=timeout) as resp:
            if resp.status_code >= 400:
                return info
            info['name'] = fix_header_text(resp.headers.get('icy-name')) or None
            info['bitrate'] = parse_bitrate(resp.headers.get('icy-br')) or None
            ctype = (resp.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
            if ctype.startswith('audio/') or ctype == 'application/ogg':
                info['mime'] = clean_text(ctype, 100)
            try:
                metaint = int((resp.headers.get('icy-metaint') or '').strip())
            except ValueError:
                return info
            if not 0 < metaint <= MAX_METAINT:
                return info
            raw = resp.raw
            if _read_exact(raw, metaint, dl) is None:
                return info
            length = _read_exact(raw, 1, dl)
            if not length:
                return info
            size = length[0] * 16
            if size == 0 or size > MAX_META_BLOCK:
                return info
            block = _read_exact(raw, size, dl)
            if block is not None:
                info['title'] = parse_stream_title(block)
    except Exception:  # noqa: BLE001 - red caída, cabeceras raras, etc.
        pass
    return info


class IcyMonitor(QObject):
    """Consulta periódica de los metadatos ICY de una radio."""
    info_changed = Signal(dict)

    POLL_MS = 30000
    TIMEOUT_S = 8

    def __init__(self, parent: Optional[QObject] = None, poll_ms: Optional[int] = None):
        super().__init__(parent)
        self._url: Optional[str] = None
        self._gen = 0             # invalida respuestas de una URL anterior
        self._busy = False
        self._deadline: Optional[Deadline] = None   # el de la consulta en curso
        self._last: Optional[Dict[str, Any]] = None
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms or self.POLL_MS)
        self._timer.timeout.connect(self._poll)

    @property
    def active(self) -> bool:
        return self._url is not None

    def last_info(self) -> Optional[Dict[str, Any]]:
        return dict(self._last) if self._last else None

    def start(self, url: str) -> None:
        if url == self._url and self._timer.isActive():
            return
        self.stop()
        self._url = url
        self._timer.start()
        self._poll()

    def stop(self) -> None:
        """Deja de consultar y corta la consulta en curso (si no, cada pausa
        y reanudación de la radio dejaría otra conexión abierta a la vez)."""
        self._timer.stop()
        self._url = None
        self._gen += 1
        self._busy = False
        self._last = None
        if self._deadline is not None:
            self._deadline.abort()
            self._deadline = None

    def _poll(self) -> None:
        if not self._url or self._busy:
            return
        from services.workers import run_in_thread
        self._busy = True
        gen, url = self._gen, self._url
        dl = self._deadline = Deadline(self.TIMEOUT_S)
        run_in_thread(lambda: read_icy_info(url, self.TIMEOUT_S, deadline=dl),
                      on_done=lambda info, g=gen: self._on_info(g, info),
                      on_error=lambda _e, g=gen: self._on_info(g, None))

    def _on_info(self, gen: int, info: Optional[Dict[str, Any]]) -> None:
        try:
            if gen != self._gen:
                return
            self._busy = False
            self._deadline = None
            if not isinstance(info, dict) or info == self._last:
                return
            self._last = dict(info)
            self.info_changed.emit(dict(info))
        except RuntimeError:
            pass  # el monitor ya se destruyó mientras la consulta volaba
