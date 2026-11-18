"""Biblioteca de música personalizada (sin Qt): listas, enlaces y playlists.

Formato persistido en data['music'] (DataHandler.get_music / save_music):
    {'lists': [{'id', 'name', 'items': [Item, ...]}],
     'current': {'list': str|None, 'item': str|None},   # lo último que sonó
     'volume': 0..1, 'shuffle': bool, 'repeat': 'all'|'one'|'off',
     'focus_only': bool,
     'seeds': [str, ...]}   # ids de lo que la app ya ofreció de serie
    Item = {'id', 'kind': 'file'|'url', 'source': ruta absoluta o URL http(s),
            'title', 'live': bool|None (None = aún no se sabe),
            'mime': '' si no se sabe, 'bitrate': kbps (0 si no se sabe)}

La lista virtual LOFI_LIST_ID ('lofi') no se guarda aquí: la genera el
servicio escaneando assets/music/lofi y es de solo lectura.

Lo que la app trae de serie (las radios de core/radio_catalog.py) entra con
seed_entries como elementos normales de una lista normal. 'seeds' recuerda
qué ids se ofrecieron, se añadieran o no: así no se duplican ni vuelve lo
que el usuario borró.

Seguridad: las rutas UNC (\\\\servidor\\...) nunca se aceptan (abrirlas
puede filtrar el hash NTLM de Windows a un servidor ajeno), y una playlist
descargada de Internet solo aporta enlaces http(s), nunca rutas locales. Una
lista HLS de tu equipo (FFmpeg abre tal cual lo que diga dentro) solo vale si
todo lo que referencia es http(s) o relativo a su carpeta (local_hls_ok).
"""
import copy
import math
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname

LOFI_LIST_ID = 'lofi'
AUDIO_EXTS = {'.mp3', '.aac', '.m4a', '.ogg', '.oga', '.opus', '.flac', '.wav', '.wma'}
PLAYLIST_EXTS = {'.m3u', '.m3u8', '.pls', '.xspf'}
REPEAT_MODES = ('all', 'one', 'off')
DEFAULT_VOLUME = 0.6

# Límites: el JSON de datos no debe crecer sin control por una playlist enorme
MAX_LISTS = 200
MAX_ITEMS_PER_LIST = 2000
MAX_TITLE = 200
MAX_LIST_NAME = 100
MAX_SOURCE = 2048
MAX_PLAYLIST_ENTRIES = 2000
# Texto guardado (fuentes + títulos, en caracteres): 2000 entradas de 2 KB
# cada una cabrían en los límites de arriba y llevarían el JSON a megas
MAX_LIST_TEXT = 512 * 1024
MAX_LIBRARY_TEXT = 4 * 1024 * 1024
_MAX_ID = 64
# Ids de serie que se recuerdan: de sobra para el catálogo (lo comprueba
# tests/test_radio_catalog.py). Con la marca llena no se siembra nada más,
# porque lo que no se pudiera recordar volvería tras borrarlo
MAX_SEEDS = 256

# Tipo MIME por extensión (para el códec mostrado y los items locales)
EXT_MIME = {
    '.mp3': 'audio/mpeg', '.aac': 'audio/aac', '.m4a': 'audio/mp4',
    '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.opus': 'audio/opus',
    '.flac': 'audio/flac', '.wav': 'audio/wav', '.wma': 'audio/x-ms-wma',
}
_MIME_CODEC = {
    'audio/mpeg': 'MP3', 'audio/mp3': 'MP3', 'audio/mpeg3': 'MP3', 'audio/x-mpeg': 'MP3',
    'audio/aac': 'AAC', 'audio/aacp': 'AAC', 'audio/x-aac': 'AAC',
    'audio/mp4': 'AAC', 'audio/x-m4a': 'AAC', 'audio/m4a': 'AAC',
    'audio/ogg': 'OGG', 'application/ogg': 'OGG', 'audio/vorbis': 'OGG',
    'audio/opus': 'Opus', 'audio/flac': 'FLAC', 'audio/x-flac': 'FLAC',
    'audio/wav': 'WAV', 'audio/x-wav': 'WAV', 'audio/wave': 'WAV',
    'audio/x-ms-wma': 'WMA',
}
_EXT_CODEC = {'.mp3': 'MP3', '.aac': 'AAC', '.m4a': 'AAC', '.ogg': 'OGG', '.oga': 'OGG',
              '.opus': 'Opus', '.flac': 'FLAC', '.wav': 'WAV', '.wma': 'WMA'}

# Servicios que no dejan sacar su audio: se avisa en vez de fallar a ciegas
_UNSUPPORTED_SERVICES = (
    (('youtube.com', 'youtu.be', 'youtube-nocookie.com'), 'YouTube'),
    (('spotify.com', 'spotify.link', 'spoti.fi'), 'Spotify'),
    (('soundcloud.com', 'snd.sc'), 'SoundCloud'),
    (('deezer.com', 'deezer.page.link', 'dzr.page.link'), 'Deezer'),
    (('music.apple.com', 'itunes.apple.com'), 'Apple Music'),
    (('tidal.com',), 'Tidal'),
)

_DRIVE_RE = re.compile(r'^[A-Za-z]:[\\/]')
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]+')        # controles C0 y C1
# Marcas de dirección (bidi): U+202E pinta «oidar» como «radio»
_BIDI_RE = re.compile('[‎‏‪-‮⁦-⁩]')
_SPACES_RE = re.compile(r'\s+')

MSG_UNC = ("Las rutas de red (\\\\servidor\\...) no se admiten: copia la música "
           "a una carpeta de tu equipo.")


@dataclass
class Entry:
    """Una pista o stream encontrado al resolver un enlace o leer una playlist."""
    source: str
    title: str = ''
    live: Optional[bool] = None
    mime: str = ''
    bitrate: int = 0


@dataclass
class LinkInfo:
    kind: str          # 'url' | 'file' | 'unsupported' | 'invalid'
    normalized: str
    reason: str = ''


# ----------------------------------------------------------------- utilidades
def clean_text(value: Any, max_len: int) -> str:
    """Texto de una línea: sin caracteres de control (C0, C1) ni marcas de
    dirección, espacios compactados y recortado a max_len. Cualquier cosa
    que no sea str da ''."""
    if not isinstance(value, str):
        return ''
    text = _SPACES_RE.sub(' ', _CTRL_RE.sub(' ', _BIDI_RE.sub('', value))).strip()
    return text[:max_len].rstrip()


def is_http_url(source: str) -> bool:
    try:
        parts = urlsplit(source)
    except ValueError:
        return False
    return parts.scheme.lower() in ('http', 'https') and bool(parts.hostname)


def is_unc(source: str) -> bool:
    return isinstance(source, str) and source.startswith(('\\\\', '//'))


def is_local_abs(source: str) -> bool:
    """Ruta local absoluta y NO de red (con letra de unidad en Windows)."""
    if not isinstance(source, str) or not source or is_unc(source):
        return False
    if os.name == 'nt':
        return bool(_DRIVE_RE.match(source))
    return source.startswith('/')


def source_kind(source: str) -> Optional[str]:
    """'url' | 'file' | None (fuente no admitida)."""
    if is_http_url(source):
        return 'url'
    if is_local_abs(source):
        return 'file'
    return None


def source_ext(source: str) -> str:
    """Extensión en minúsculas del archivo al que apunta la fuente ('' si no hay)."""
    if not source:
        return ''
    if is_http_url(source):
        path = unquote(urlsplit(source).path)
    else:
        path = source
    name = path.replace('\\', '/').rsplit('/', 1)[-1]
    return os.path.splitext(name)[1].lower()


def codec_name(mime: str, source: str = '') -> str:
    """'MP3', 'AAC', 'FLAC'... deducido del tipo MIME o, si no, de la extensión."""
    base = (mime or '').split(';', 1)[0].strip().lower()
    if base in _MIME_CODEC:
        return _MIME_CODEC[base]
    return _EXT_CODEC.get(source_ext(source), '')


def _service_for_host(host: Optional[str]) -> Optional[str]:
    host = (host or '').lower().rstrip('.')
    if not host:
        return None
    for domains, name in _UNSUPPORTED_SERVICES:
        for d in domains:
            if host == d or host.endswith('.' + d):
                return name
    return None


def _unsupported_reason(service: str) -> str:
    return (f"{service} no deja reproducir su audio fuera de su app: usa el enlace "
            "directo de una radio o de un archivo (.mp3, .m3u, .pls).")


def _file_url_to_path(url: str) -> Optional[str]:
    """file:// → ruta local. None si apunta a otro equipo (UNC)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.netloc or '').lower()
    if host not in ('', 'localhost'):
        return None
    path = url2pathname(parts.path)
    if is_unc(path):
        return None
    return path


def _classify_path(raw: str) -> LinkInfo:
    path = os.path.expanduser(raw)
    if is_unc(path):
        return LinkInfo('invalid', '', MSG_UNC)
    if not is_local_abs(path):
        return LinkInfo('invalid', '', "Usa la ruta completa del archivo o carpeta "
                                       "(por ejemplo C:\\Música\\tema.mp3).")
    path = os.path.normpath(path)
    try:
        exists = os.path.exists(path)
    except (OSError, ValueError):
        exists = False
    if not exists:
        return LinkInfo('invalid', '', "No encuentro ese archivo o carpeta en tu equipo.")
    return LinkInfo('file', path)


# --------------------------------------------------------------- enlaces
def classify_link(text: str) -> LinkInfo:
    """Qué es lo que pegó el usuario: un enlace http(s), una ruta local
    existente, un servicio que no se puede reproducir o algo no válido."""
    raw = (text or '').strip()
    # «Copiar como ruta» de Windows lo pone entre comillas
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in '"\'':
        raw = raw[1:-1].strip()
    if raw.startswith('<') and raw.endswith('>'):
        raw = raw[1:-1].strip()
    if not raw:
        return LinkInfo('invalid', '', "Pega un enlace o la ruta de un archivo.")
    if '\n' in raw or '\r' in raw:
        return LinkInfo('invalid', '', "Pega un solo enlace cada vez.")
    if len(raw) > MAX_SOURCE:
        return LinkInfo('invalid', '', "El enlace es demasiado largo.")
    if raw.lower().startswith('spotify:'):
        return LinkInfo('unsupported', '', _unsupported_reason('Spotify'))
    if is_unc(raw):
        return LinkInfo('invalid', '', MSG_UNC)
    if _DRIVE_RE.match(raw) or raw.startswith('~') or (os.name != 'nt' and raw.startswith('/')):
        return _classify_path(raw)
    try:
        parts = urlsplit(raw)
        host = parts.hostname
    except ValueError:
        return LinkInfo('invalid', '', "Ese enlace no es válido.")
    scheme = parts.scheme.lower()
    if scheme in ('http', 'https'):
        if not host:
            return LinkInfo('invalid', '', "Ese enlace no es válido.")
        service = _service_for_host(host)
        if service:
            return LinkInfo('unsupported', '', _unsupported_reason(service))
        if any(c.isspace() for c in raw):
            return LinkInfo('invalid', '', "Ese enlace no es válido: tiene espacios.")
        return LinkInfo('url', raw)
    if scheme == 'file':
        path = _file_url_to_path(raw)
        if path is None:
            return LinkInfo('invalid', '', MSG_UNC)
        return _classify_path(path)
    if scheme:
        return LinkInfo('invalid', '', "Solo se pueden reproducir enlaces http:// o "
                                       "https:// y archivos de tu equipo.")
    # Sin esquema: «open.spotify.com/...» o «radio.com/stream.mp3»
    first = raw.split('/', 1)[0]
    if '.' in first and ' ' not in first:
        service = _service_for_host(first.split(':', 1)[0])
        if service:
            return LinkInfo('unsupported', '', _unsupported_reason(service))
        return LinkInfo('invalid', '', "Al enlace le falta http:// o https:// al principio.")
    return LinkInfo('invalid', '', "Eso no es un enlace ni la ruta de un archivo de tu equipo.")


def title_from_source(source: str) -> str:
    """Título por defecto: nombre de archivo sin extensión ni %xx; para un
    stream sin nombre de archivo, el host."""
    if not source:
        return ''
    if is_http_url(source):
        parts = urlsplit(source)
        name = unquote(parts.path).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
        stem, ext = os.path.splitext(name)
        if ext.lower() in AUDIO_EXTS | PLAYLIST_EXTS and clean_text(stem, MAX_TITLE):
            return clean_text(stem, MAX_TITLE)
        host = (parts.hostname or '').lower()
        if host.startswith('www.'):
            host = host[4:]
        return clean_text(host, MAX_TITLE)
    name = source.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
    stem, ext = os.path.splitext(name)
    return clean_text(stem if ext else name, MAX_TITLE) or clean_text(name, MAX_TITLE)


# --------------------------------------------------------------- playlists
_PLAYLIST_CTYPES = {
    'audio/x-mpegurl': 'm3u', 'audio/mpegurl': 'm3u', 'application/x-mpegurl': 'm3u',
    'application/vnd.apple.mpegurl': 'hls', 'audio/x-scpls': 'pls',
    'application/pls+xml': 'pls', 'application/xspf+xml': 'xspf',
}
PLAYLIST_CTYPES = frozenset(_PLAYLIST_CTYPES)


def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith('\ufeff') else text


def detect_playlist(text: str, url: str = '', content_type: str = '') -> Optional[str]:
    """'m3u' | 'pls' | 'xspf' | 'hls' | None. HLS es un M3U con etiquetas
    #EXT-X-…: es UN stream (FFmpeg lo reproduce tal cual), no una lista."""
    body = _strip_bom(text or '')
    head = body.lstrip()[:4096]
    if '#EXT-X-' in body[:65536].upper():
        return 'hls'
    low = head.lower()
    if low.startswith(('<!doctype html', '<html')):
        return None  # una página web (de error, de login...) no es una lista
    if low.startswith('[playlist]'):
        return 'pls'
    if low.startswith('#extm3u'):
        return 'm3u'
    if low.startswith('<?xml') or low.startswith('<playlist'):
        if '<playlist' in low and ('xspf' in low or '<tracklist' in low):
            return 'xspf'
    ctype = (content_type or '').split(';', 1)[0].strip().lower()
    by_type = _PLAYLIST_CTYPES.get(ctype)
    if by_type == 'hls':
        # Muchos servidores sirven M3U simples con el tipo de HLS
        return 'm3u'
    if by_type:
        return by_type
    ext = source_ext(url) if url else ''
    if ext in ('.m3u', '.m3u8'):
        return 'm3u'
    if ext == '.pls':
        return 'pls'
    if ext == '.xspf':
        return 'xspf'
    return None


# Extensiones con las que FFmpeg lee un archivo local como HLS
HLS_EXTS = {'.m3u', '.m3u8', '.hls'}
_URI_ATTR_RE = re.compile(r'\bURI\s*=\s*(?:"([^"]*)"|([^,\s]+))', re.I)


def hls_refs(text: str) -> List[str]:
    """Todo lo que una lista HLS manda abrir: segmentos y variantes (líneas
    sin #) y los URI= de sus etiquetas (#EXT-X-KEY, -MAP, -MEDIA...)."""
    refs: List[str] = []
    for line in _strip_bom(text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            refs.extend(m.group(1) if m.group(1) is not None else m.group(2)
                        for m in _URI_ATTR_RE.finditer(line))
        else:
            refs.append(line)
    return refs


def local_hls_ok(text: str) -> bool:
    """¿Se puede dar a FFmpeg esta lista HLS de tu equipo? Solo si todo lo
    que referencia son enlaces http(s) o rutas relativas a su carpeta sin
    «..»: FFmpeg abre tal cual \\\\servidor\\... (filtraría el hash NTLM),
    \\\\?\\C:\\... o cualquier archivo del disco. Sin etiquetas #EXT-X- no
    es HLS (FFmpeg no la lee como tal) y no hay nada que mirar."""
    if '#EXT-X-' not in (text or '').upper():
        return True
    for ref in hls_refs(text):
        ref = ref.strip()
        if is_http_url(ref):
            continue
        parts = ref.replace('\\', '/').split('/')
        if not ref or ref.startswith(('\\', '/')) or ':' in ref or '..' in parts:
            return False
    return True


def _resolve_ref(ref: str, base: str, remote: bool) -> Optional[str]:
    """Una entrada de playlist → fuente absoluta (URL o ruta), o None si no
    se admite. Con remote=True solo salen URLs http(s)."""
    ref = (ref or '').strip()
    if not ref or len(ref) > MAX_SOURCE or is_unc(ref):
        return None
    try:
        scheme = urlsplit(ref).scheme.lower()
    except ValueError:
        return None
    if _DRIVE_RE.match(ref):
        # «C:\...»: ruta local (urlsplit la tomaría por el esquema «c:»)
        if remote:
            return None
        return os.path.normpath(ref)
    if scheme in ('http', 'https'):
        return ref if is_http_url(ref) else None
    if scheme == 'file':
        if remote:
            return None
        path = _file_url_to_path(ref)
        return os.path.normpath(path) if path and is_local_abs(path) else None
    if scheme:
        return None  # spotify:, rtsp:, javascript:...
    # Relativa: contra la URL o la carpeta base
    if is_http_url(base):
        if ref.startswith('\\'):
            return None
        joined = urljoin(base, ref)
        return joined if is_http_url(joined) and len(joined) <= MAX_SOURCE else None
    if remote or not base or not is_local_abs(base):
        return None
    if os.name != 'nt' and ref.startswith('/'):
        return os.path.normpath(ref)
    if ref.startswith(('\\', '/')):
        # «\Música\x.mp3»: relativa a la unidad de la carpeta base
        joined = os.path.join(os.path.splitdrive(base)[0] + os.sep, ref.lstrip('\\/'))
    else:
        joined = os.path.join(base, ref)
    joined = os.path.normpath(joined)
    return joined if is_local_abs(joined) else None


def _extinf_title(line: str) -> str:
    """#EXTINF:-1 tvg-name="a,b",Título → 'Título' (comas entre comillas no cuentan)."""
    body = line[len('#EXTINF:'):]
    quoted = False
    for i, ch in enumerate(body):
        if ch == '"':
            quoted = not quoted
        elif ch == ',' and not quoted:
            return clean_text(body[i + 1:], MAX_TITLE)
    return ''


def _parse_m3u(text: str) -> List[tuple]:
    out, title = [], ''
    for line in _strip_bom(text).splitlines():
        line = line.strip()
        if not line or line.startswith('<'):
            continue
        if line.startswith('#'):
            if line.upper().startswith('#EXTINF:'):
                title = _extinf_title(line)
            continue
        out.append((line, title))
        title = ''
    return out


def _parse_pls(text: str) -> List[tuple]:
    files: Dict[int, str] = {}
    titles: Dict[int, str] = {}
    for line in _strip_bom(text).splitlines():
        key, sep, value = line.strip().partition('=')
        if not sep:
            continue
        m = re.fullmatch(r'(file|title)(\d{1,6})', key.strip().lower())
        if not m:
            continue
        n = int(m.group(2))
        if m.group(1) == 'file':
            files[n] = value.strip()
        else:
            titles[n] = clean_text(value, MAX_TITLE)
    return [(files[n], titles.get(n, '')) for n in sorted(files)]


def _local(tag: Any) -> str:
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def _xspf_root(text: str):
    body = _strip_bom(text or '')
    upper = body.upper()
    # Sin DTD ni entidades: corta de raíz las bombas de expansión y XXE
    if '<!DOCTYPE' in upper or '<!ENTITY' in upper:
        return None
    try:
        return ET.fromstring(body)
    except (ET.ParseError, ValueError):
        return None


def _parse_xspf(text: str) -> List[tuple]:
    root = _xspf_root(text)
    if root is None:
        return []
    out = []
    for el in root.iter():
        if _local(el.tag) != 'track':
            continue
        location = title = creator = ''
        for child in el:
            name = _local(child.tag)
            value = (child.text or '').strip()
            if name == 'location' and not location:
                location = value
            elif name == 'title':
                title = clean_text(value, MAX_TITLE)
            elif name == 'creator':
                creator = clean_text(value, MAX_TITLE)
        if location:
            label = f"{creator} - {title}" if creator and title else (title or creator)
            out.append((location, clean_text(label, MAX_TITLE)))
    return out


def parse_playlist(text: str, fmt: Optional[str], base: str = '',
                   remote: bool = False) -> List[Entry]:
    """Entradas de un M3U/M3U8 extendido, PLS o XSPF (máx. 2000). Las rutas
    relativas se resuelven contra `base` (URL o carpeta). Con remote=True
    (playlist descargada) solo se aceptan entradas http/https. HLS no se
    expande: devuelve [] (se reproduce la URL de la playlist tal cual)."""
    if fmt == 'm3u':
        raw = _parse_m3u(text or '')
    elif fmt == 'pls':
        raw = _parse_pls(text or '')
    elif fmt == 'xspf':
        raw = _parse_xspf(text or '')
    else:
        return []
    out: List[Entry] = []
    for ref, title in raw:
        source = _resolve_ref(ref, base, remote)
        if source is None:
            continue
        out.append(Entry(source=source, title=title))
        if len(out) >= MAX_PLAYLIST_ENTRIES:
            break
    return out


def playlist_name(text: str, fmt: Optional[str]) -> str:
    """Nombre que la propia playlist declara (<title> de XSPF, #PLAYLIST: de M3U)."""
    if fmt == 'xspf':
        root = _xspf_root(text)
        if root is not None:
            for child in root:
                if _local(child.tag) == 'title':
                    return clean_text(child.text, MAX_LIST_NAME)
    elif fmt == 'm3u':
        for line in _strip_bom(text or '').splitlines()[:200]:
            if line.strip().upper().startswith('#PLAYLIST:'):
                return clean_text(line.strip()[len('#PLAYLIST:'):], MAX_LIST_NAME)
    return ''


# --------------------------------------------------------------- validación
def default_music() -> Dict[str, Any]:
    return {'lists': [], 'current': {'list': None, 'item': None},
            'volume': DEFAULT_VOLUME, 'shuffle': False, 'repeat': 'all',
            'focus_only': False, 'seeds': []}


def _new_id(used: set) -> str:
    while True:
        new = uuid.uuid4().hex[:12]
        if new not in used and new != LOFI_LIST_ID:
            used.add(new)
            return new


def source_key(source: str) -> str:
    """Clave para deduplicar: las rutas de Windows no distinguen mayúsculas."""
    if is_http_url(source):
        return source
    return os.path.normcase(os.path.normpath(source))


def item_text_size(item: Dict[str, Any]) -> int:
    """Lo que un item pesa en el JSON a efectos de MAX_*_TEXT."""
    return len(item.get('source') or '') + len(item.get('title') or '')


def _valid_item(raw: Any, used_ids: set) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    source = raw.get('source')
    if not isinstance(source, str):
        return None
    source = source.strip()
    if not source or len(source) > MAX_SOURCE:
        return None
    kind = source_kind(source)
    if kind is None:
        return None
    item_id = raw.get('id')
    if (not isinstance(item_id, str) or not item_id.strip() or len(item_id) > _MAX_ID
            or item_id in used_ids or item_id == LOFI_LIST_ID):
        item_id = _new_id(used_ids)
    else:
        used_ids.add(item_id)
    live = raw.get('live')
    bitrate = raw.get('bitrate')
    mime = raw.get('mime')
    return {
        'id': item_id,
        'kind': kind,
        'source': source,
        'title': clean_text(raw.get('title'), MAX_TITLE) or title_from_source(source)
                 or 'Sin título',
        'live': live if isinstance(live, bool) else None,
        'mime': clean_text(mime, 100).lower() if isinstance(mime, str) else '',
        'bitrate': (bitrate if isinstance(bitrate, int) and not isinstance(bitrate, bool)
                    and 0 <= bitrate <= 100000 else 0),
    }


def normalize_music(raw: Any) -> Dict[str, Any]:
    """Copia saneada de data['music']: descarta listas/items mal formados,
    aplica los límites, ids únicos (los repetidos se renuevan) y valores por
    defecto. Nunca lanza."""
    out = default_music()
    if not isinstance(raw, dict):
        return out
    lists: List[Dict[str, Any]] = []
    list_ids: set = set()
    item_ids: set = set()
    library_text = 0
    raw_lists = raw.get('lists')
    for lst in raw_lists if isinstance(raw_lists, list) else []:
        if len(lists) >= MAX_LISTS:
            break
        if not isinstance(lst, dict):
            continue
        lid = lst.get('id')
        if (not isinstance(lid, str) or not lid.strip() or len(lid) > _MAX_ID
                or lid in list_ids or lid == LOFI_LIST_ID):
            lid = _new_id(list_ids)
        else:
            list_ids.add(lid)
        items: List[Dict[str, Any]] = []
        seen: set = set()
        list_text = 0
        raw_items = lst.get('items')
        for it in raw_items if isinstance(raw_items, list) else []:
            if len(items) >= MAX_ITEMS_PER_LIST:
                break
            item = _valid_item(it, item_ids)
            if item is None:
                continue
            key = source_key(item['source'])
            if key in seen:
                continue
            size = item_text_size(item)
            if (list_text + size > MAX_LIST_TEXT
                    or library_text + size > MAX_LIBRARY_TEXT):
                break
            list_text += size
            library_text += size
            seen.add(key)
            items.append(item)
        lists.append({'id': lid, 'name': clean_text(lst.get('name'), MAX_LIST_NAME)
                      or 'Lista sin nombre', 'items': items})
    out['lists'] = lists
    vol = raw.get('volume')
    if isinstance(vol, (int, float)) and not isinstance(vol, bool) and math.isfinite(vol):
        out['volume'] = max(0.0, min(1.0, float(vol)))
    if isinstance(raw.get('shuffle'), bool):
        out['shuffle'] = raw['shuffle']
    if raw.get('repeat') in REPEAT_MODES:
        out['repeat'] = raw['repeat']
    if isinstance(raw.get('focus_only'), bool):
        out['focus_only'] = raw['focus_only']
    # La marca de lo ofrecido de serie: sin ella en el esquema se perdería en
    # el primer guardado y lo borrado volvería al abrir
    seeds = raw.get('seeds')
    if isinstance(seeds, list):
        out['seeds'] = list(dict.fromkeys(
            x for x in seeds if isinstance(x, str) and 0 < len(x) <= _MAX_ID))[:MAX_SEEDS]
    cur = raw.get('current')
    if isinstance(cur, dict):
        c_list, c_item = cur.get('list'), cur.get('item')
        if c_list == LOFI_LIST_ID:
            # Los items lofi salen del disco: aquí solo se valida la forma
            out['current'] = {'list': LOFI_LIST_ID,
                              'item': c_item if isinstance(c_item, str)
                              and 0 < len(c_item) <= 260 else None}
        else:
            found = next((x for x in lists if x['id'] == c_list), None)
            if found is not None:
                ok = isinstance(c_item, str) and any(i['id'] == c_item for i in found['items'])
                out['current'] = {'list': c_list, 'item': c_item if ok else None}
    return out


# --------------------------------------------------------------- biblioteca
class MusicLibrary:
    """Listas persistidas del usuario (la lista lofi la gestiona el servicio).
    Opera sobre una copia saneada; to_dict() devuelve lo que se guarda."""

    def __init__(self, music: Any = None):
        self.data: Dict[str, Any] = normalize_music(music)
        # Por qué el último add_entries dejó entradas fuera: '' (ninguno),
        # 'list' (lista llena) o 'library' (la música ocupa el máximo)
        self.limit_hit = ''

    # ---------- lectura ----------
    @property
    def lists(self) -> List[Dict[str, Any]]:
        return self.data['lists']

    @property
    def current(self) -> Dict[str, Optional[str]]:
        return self.data['current']

    def get_list(self, list_id: Optional[str]) -> Optional[Dict[str, Any]]:
        return next((x for x in self.data['lists'] if x['id'] == list_id), None)

    def find_item(self, list_id: Optional[str], item_id: Optional[str]) -> Optional[Dict[str, Any]]:
        lst = self.get_list(list_id)
        if lst is None:
            return None
        return next((i for i in lst['items'] if i['id'] == item_id), None)

    def _all_item_ids(self) -> set:
        return {i['id'] for lst in self.data['lists'] for i in lst['items']}

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)

    # ---------- listas ----------
    def add_list(self, name: str) -> str:
        """Crea una lista vacía y devuelve su id. ValueError si ya hay MAX_LISTS."""
        if len(self.data['lists']) >= MAX_LISTS:
            raise ValueError(f"Ya tienes {MAX_LISTS} listas: borra alguna para crear otra.")
        lid = _new_id({x['id'] for x in self.data['lists']})
        self.data['lists'].append({'id': lid, 'name': clean_text(name, MAX_LIST_NAME)
                                   or 'Lista nueva', 'items': []})
        return lid

    def rename_list(self, list_id: str, name: str) -> bool:
        lst = self.get_list(list_id)
        name = clean_text(name, MAX_LIST_NAME)
        if lst is None or not name:
            return False
        lst['name'] = name
        return True

    def delete_list(self, list_id: str) -> bool:
        lst = self.get_list(list_id)
        if lst is None:
            return False
        self.data['lists'].remove(lst)
        if self.data['current'].get('list') == list_id:
            self.data['current'] = {'list': None, 'item': None}
        return True

    def find_list_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        key = clean_text(name, MAX_LIST_NAME).lower()
        return next((x for x in self.data['lists'] if x['name'].lower() == key), None)

    # ---------- items ----------
    def add_entries(self, list_id: str, entries: Iterable[Any]) -> int:
        """Añade al final las entradas válidas que no estén ya en la lista
        (misma fuente), sin pasar de MAX_ITEMS_PER_LIST ni de los límites de
        texto (limit_hit dice si alguno dejó fuera algo). Devuelve cuántas
        se añadieron."""
        self.limit_hit = ''
        lst = self.get_list(list_id)
        if lst is None:
            return 0
        used = self._all_item_ids()
        seen = {source_key(i['source']) for i in lst['items']}
        list_text = sum(item_text_size(i) for i in lst['items'])
        library_text = sum(item_text_size(i) for x in self.data['lists'] for i in x['items'])
        added = 0
        for entry in entries or []:
            if len(lst['items']) >= MAX_ITEMS_PER_LIST:
                self.limit_hit = 'list'
                break
            raw = entry if isinstance(entry, dict) else (
                {'source': entry.source, 'title': entry.title, 'live': entry.live,
                 'mime': entry.mime, 'bitrate': entry.bitrate}
                if isinstance(entry, Entry) else None)
            if raw is None:
                continue
            raw = {k: v for k, v in raw.items() if k != 'id'}
            item = _valid_item(raw, used)
            if item is None:
                continue
            key = source_key(item['source'])
            if key in seen:
                used.discard(item['id'])
                continue
            size = item_text_size(item)
            if list_text + size > MAX_LIST_TEXT or library_text + size > MAX_LIBRARY_TEXT:
                self.limit_hit = 'list' if list_text + size > MAX_LIST_TEXT else 'library'
                break
            list_text += size
            library_text += size
            seen.add(key)
            lst['items'].append(item)
            added += 1
        return added

    def remove_item(self, list_id: str, item_id: str) -> bool:
        lst = self.get_list(list_id)
        item = self.find_item(list_id, item_id)
        if item is None:
            return False
        lst['items'].remove(item)
        cur = self.data['current']
        if cur.get('list') == list_id and cur.get('item') == item_id:
            cur['item'] = None
        return True

    def move_item(self, list_id: str, item_id: str, new_index: int) -> bool:
        lst = self.get_list(list_id)
        item = self.find_item(list_id, item_id)
        if item is None or not isinstance(new_index, int):
            return False
        items = lst['items']
        old = items.index(item)
        new_index = max(0, min(len(items) - 1, new_index))
        if new_index == old:
            return False
        items.insert(new_index, items.pop(old))
        return True

    def rename_item(self, list_id: str, item_id: str, title: str) -> bool:
        item = self.find_item(list_id, item_id)
        title = clean_text(title, MAX_TITLE)
        if item is None or not title:
            return False
        item['title'] = title
        return True

    def update_item(self, list_id: str, item_id: str, **fields) -> bool:
        """Actualiza title/live/mime/bitrate (validados). True si cambió algo."""
        item = self.find_item(list_id, item_id)
        return update_item_fields(item, **fields) if item is not None else False

    def set_current(self, list_id: Optional[str], item_id: Optional[str]) -> None:
        self.data['current'] = {'list': list_id, 'item': item_id}


def update_item_fields(item: Dict[str, Any], **fields) -> bool:
    """Aplica los campos admitidos a un item (también a los lofi, en memoria)."""
    changed = False
    for key, value in fields.items():
        if key == 'title':
            value = clean_text(value, MAX_TITLE)
            if not value:
                continue
        elif key == 'live':
            if value is not None and not isinstance(value, bool):
                continue
        elif key == 'mime':
            if not isinstance(value, str):
                continue
            value = clean_text(value, 100).lower()
        elif key == 'bitrate':
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100000:
                continue
        else:
            continue
        if item.get(key) != value:
            item[key] = value
            changed = True
    return changed


def seed_entries(music: Dict[str, Any], seeds: Iterable[Tuple[str, Entry]],
                 list_id: str, list_name: str) -> bool:
    """Siembra en `music` (ya saneada con normalize_music) las entradas de
    serie (id de catálogo, Entry) cuyo id no está en music['seeds'].

    Cada id se apunta aunque su entrada no se añada (su URL ya estaba en
    alguna lista, o no cabe en los límites): así nada se duplica ni vuelve
    después de que el usuario lo borre. Las que entran van a la lista
    `list_id` (aunque el usuario la haya renombrado) o, si ya no existe, a
    una nueva con ese id y `list_name` al final de las listas, que nunca se
    crea vacía. Respeta MAX_LISTS, MAX_ITEMS_PER_LIST, MAX_LIST_TEXT y
    MAX_LIBRARY_TEXT. True si cambió algo (hay que guardar).
    """
    offered = music.setdefault('seeds', [])
    # Un id que normalize_music no conservaría tampoco se siembra: volvería
    # en cada arranque
    pending = [(sid, entry) for sid, entry in seeds
               if isinstance(sid, str) and 0 < len(sid) <= _MAX_ID and sid not in offered]
    if not pending:
        return False
    lists = music['lists']
    present = {source_key(i['source']) for x in lists for i in x['items']}
    item_ids = {i['id'] for x in lists for i in x['items']}
    library_text = sum(item_text_size(i) for x in lists for i in x['items'])
    target = next((x for x in lists if x['id'] == list_id), None)
    changed = False
    for sid, entry in pending:
        if sid in offered:
            continue   # repetido en el propio catálogo
        if len(offered) >= MAX_SEEDS:
            break      # lo que no se puede recordar no se siembra
        offered.append(sid)
        changed = True
        item = _valid_item({'source': entry.source, 'title': entry.title,
                            'live': entry.live, 'mime': entry.mime,
                            'bitrate': entry.bitrate}, item_ids)
        if item is None:
            continue
        key = source_key(item['source'])
        if key in present:
            continue
        size = item_text_size(item)
        if library_text + size > MAX_LIBRARY_TEXT:
            continue
        if target is None:
            if len(lists) >= MAX_LISTS:
                continue
            target = {'id': list_id, 'name': clean_text(list_name, MAX_LIST_NAME),
                      'items': []}
            lists.append(target)
        elif (len(target['items']) >= MAX_ITEMS_PER_LIST
              or sum(item_text_size(i) for i in target['items']) + size > MAX_LIST_TEXT):
            continue
        target['items'].append(item)
        library_text += size
        present.add(key)
    return changed


def scan_audio_folder(folder: Path) -> List[Path]:
    """Archivos de audio de una carpeta (no recursivo), en orden alfabético."""
    try:
        files = [p for p in Path(folder).iterdir()
                 if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name.lower())
