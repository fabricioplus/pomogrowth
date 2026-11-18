"""Metadatos ICY (Icecast/SHOUTcast) leídos en la MISMA conexión del audio.

Con «Icy-MetaData: 1» el servidor intercala en el audio, cada icy-metaint
bytes, un byte de longitud L y L×16 bytes de metadatos
(«StreamTitle='Artista - Tema';StreamUrl='';», rellenos con \\0; L = 0 si no
hay nada nuevo). Qt no expone el StreamTitle, y abrir otra conexión para
leerlo sería otra sesión con la emisora: las de pago (DI.FM…) solo admiten
una a la vez y pausan la cuenta. Por eso el proxy local
(services.stream_buffer) pide los metadatos en su única conexión y los quita
del audio con IcyDemuxer antes de dárselo al reproductor.

- parse_metaint(valor): el entero de la cabecera icy-metaint; None si no
  viene (no hay nada que quitar) y ValueError si viene y no es un entero
  entre 1 y MAX_METAINT: con un valor así no se sabe dónde van los bloques,
  y quien lo recibe no debe pasar ese audio tal cual (mezclaría metadatos).
- IcyDemuxer(metaint).feed(trozo) → (audio, [(posición, bloque)]): los
  trozos pueden partir el audio, el byte de longitud o el bloque por
  cualquier byte (el estado se conserva entre llamadas); `posición` es el
  byte del audio devuelto tras el que iba el bloque. Uno nuevo por conexión:
  cada una empieza con icy-metaint bytes de audio enteros.
- parse_stream_title(bloque) → 'Artista - Tema' o None (UTF-8 o, en las
  radios viejas, Windows-1252); has_stream_title(bloque) dice si el bloque
  trae el campo (un StreamTitle='' borra el título; un bloque sin él, no).
"""
import re
from typing import List, Optional, Tuple

from core.music_library import clean_text

MAX_METAINT = 256 * 1024
MAX_META_BLOCK = 255 * 16  # el byte de longitud ×16: 4080 como mucho
_TITLE_RE = re.compile(r"StreamTitle='(.*?)';", re.S)


def parse_metaint(value: Optional[str]) -> Optional[int]:
    """icy-metaint → bytes de audio entre bloques; None si no viene.
    ValueError si viene y no es un entero de 1 a MAX_METAINT."""
    if value is None:
        return None
    text = value.strip()
    if not (text.isascii() and text.isdigit()):
        raise ValueError("icy-metaint no es un número")
    metaint = int(text)
    if not 0 < metaint <= MAX_METAINT:
        raise ValueError("icy-metaint fuera de rango")
    return metaint


def has_stream_title(block: bytes) -> bool:
    return b"StreamTitle=" in block


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


class IcyDemuxer:
    """Separa los bloques de metadatos ICY del audio de UNA conexión."""

    def __init__(self, metaint: int):
        if not 0 < metaint <= MAX_METAINT:
            raise ValueError("icy-metaint fuera de rango")
        self.metaint = metaint
        self._audio_left = metaint    # audio que falta hasta el byte de longitud
        self._meta_left = 0           # bytes del bloque que faltan
        self._state = 'audio'         # 'audio' | 'len' | 'meta'
        self._block = bytearray()

    def feed(self, data: bytes) -> Tuple[bytes, List[Tuple[int, bytes]]]:
        audio = bytearray()
        blocks: List[Tuple[int, bytes]] = []
        i, n = 0, len(data)
        while i < n:
            if self._state == 'audio':
                take = min(self._audio_left, n - i)
                audio += data[i:i + take]
                i += take
                self._audio_left -= take
                if self._audio_left == 0:
                    self._state = 'len'
            elif self._state == 'len':
                size = data[i] * 16
                i += 1
                if size:
                    self._state, self._meta_left = 'meta', size
                    self._block = bytearray()
                else:   # L = 0: nada nuevo, vuelve el audio
                    self._state, self._audio_left = 'audio', self.metaint
            else:
                take = min(self._meta_left, n - i)
                self._block += data[i:i + take]
                i += take
                self._meta_left -= take
                if self._meta_left == 0:
                    blocks.append((len(audio), bytes(self._block)))
                    self._block = bytearray()
                    self._state, self._audio_left = 'audio', self.metaint
        return bytes(audio), blocks
