"""Formato del audio de un directo: ¿se pueden empalmar dos conexiones?

El proxy local (services.stream_buffer) empalma en la misma conexión con el
reproductor el audio de una reconexión al origen. Algunos CDN (bigFM, por
ejemplo) cambian de formato de una conexión a otra: MP3 128 ↔ AAC (ADTS) 64,
o AAC-LC 44,1 kHz ↔ HE-AAC 22,05 kHz. FFmpeg eligió el decodificador con la
primera y no lo cambia a mitad: empalmar ese audio lo rompe. Por eso solo se
empalma una reconexión del mismo formato que la primera conexión de la
sesión. El formato es:
- el Content-Type normalizado (normalize_ctype: minúsculas, sin parámetros
  y con los sinónimos de MP3 y AAC unificados), y
- la firma de la primera trama válida (una cabecera con otra igual justo
  detrás, a la distancia que dice su longitud): en MPEG audio (MP1/MP2/MP3)
  versión, capa y frecuencia; en ADTS (AAC) perfil, frecuencia y canales.
FormatProbe mira el audio a trozos hasta dar con esa trama o hasta
PROBE_LIMIT bytes. Si no la encuentra (otro formato, audio raro), la firma
queda en None y same_format decide solo por el Content-Type.
"""
from typing import Optional, Tuple

PROBE_LIMIT = 16 * 1024

Signature = Tuple  # ('mpeg', versión, capa, frecuencia) o ('adts', perfil, frecuencia, canales)

_CTYPE_ALIASES = {
    'audio/mp3': 'audio/mpeg', 'audio/mpeg3': 'audio/mpeg', 'audio/x-mpeg': 'audio/mpeg',
    'audio/x-mp3': 'audio/mpeg', 'audio/x-mpeg-3': 'audio/mpeg', 'audio/mpg': 'audio/mpeg',
    'audio/x-aac': 'audio/aac', 'audio/x-aacp': 'audio/aacp',
}

# kbps por índice (0 = libre, 15 = no válido)
_BITRATES = {
    (1, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
# Bits de versión: 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5 (1 no vale)
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def normalize_ctype(ctype: Optional[str]) -> str:
    base = (ctype or '').split(';', 1)[0].strip().lower()
    return _CTYPE_ALIASES.get(base, base)


def _mpeg_frame(data: bytes, i: int) -> Optional[Tuple[Signature, int]]:
    """Cabecera MPEG audio en data[i:]: (firma, longitud de la trama) o None."""
    if i + 4 > len(data) or data[i] != 0xFF or data[i + 1] & 0xE0 != 0xE0:
        return None
    b1, b2 = data[i + 1], data[i + 2]
    version = (b1 >> 3) & 3
    layer_bits = (b1 >> 1) & 3            # 3 = capa I, 2 = II, 1 = III, 0 = no vale
    br_index, sr_index = b2 >> 4, (b2 >> 2) & 3
    if version == 1 or layer_bits == 0 or br_index in (0, 15) or sr_index == 3:
        return None
    layer = 4 - layer_bits
    rate = _RATES[version][sr_index]
    kbps = _BITRATES[(1 if version == 3 else 2, layer)][br_index]
    padding = (b2 >> 1) & 1
    if layer == 1:
        length = (12 * kbps * 1000 // rate + padding) * 4
    elif layer == 3 and version != 3:
        length = 72 * kbps * 1000 // rate + padding
    else:
        length = 144 * kbps * 1000 // rate + padding
    return ('mpeg', version, layer, rate), length


def _adts_frame(data: bytes, i: int) -> Optional[Tuple[Signature, int]]:
    """Cabecera ADTS en data[i:]: (firma, longitud de la trama) o None."""
    if i + 7 > len(data) or data[i] != 0xFF or data[i + 1] & 0xF6 != 0xF0:
        return None
    b1, b2, b3, b4, b5 = data[i + 1:i + 6]
    profile, sf_index = b2 >> 6, (b2 >> 2) & 0x0F
    channels = ((b2 & 1) << 2) | (b3 >> 6)
    length = ((b3 & 0x03) << 11) | (b4 << 3) | (b5 >> 5)
    header = 7 if b1 & 1 else 9
    if sf_index > 12 or length < header:
        return None
    return ('adts', profile, sf_index, channels), length


def find_signature(data: bytes) -> Optional[Signature]:
    """La firma de la primera trama válida de `data` (None si no hay una
    confirmada por la siguiente)."""
    start = 0
    while True:
        i = data.find(b'\xff', start)
        if i < 0 or i + 4 > len(data):
            return None
        for parse in (_adts_frame, _mpeg_frame):
            frame = parse(data, i)
            if frame is None:
                continue
            sig, length = frame
            following = parse(data, i + length)
            if following is not None and following[0] == sig:
                return sig
        start = i + 1


class FormatProbe:
    """Busca la firma del audio de una conexión a medida que llega."""

    def __init__(self, limit: int = PROBE_LIMIT):
        self.limit = limit
        self.signature: Optional[Signature] = None
        self.done = False
        self._seen = bytearray()

    def feed(self, data: bytes) -> bool:
        """Añade audio; True cuando ya hay veredicto (firma o límite)."""
        if self.done or not data:
            return self.done
        self._seen += data[:max(0, self.limit - len(self._seen))]
        self.signature = find_signature(bytes(self._seen))
        if self.signature is not None or len(self._seen) >= self.limit:
            self.done = True
            self._seen = bytearray()
        return self.done


def same_format(a: Tuple[str, Optional[Signature]],
                b: Tuple[str, Optional[Signature]]) -> bool:
    """¿(Content-Type, firma) compatibles? Sin firma en alguna, manda el tipo."""
    if a[0] != b[0]:
        return False
    if a[1] is None or b[1] is None:
        return True
    return a[1] == b[1]
