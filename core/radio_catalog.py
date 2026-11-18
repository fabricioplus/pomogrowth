"""Radios lofi de serie: las que PomoGrowth trae ya puestas en Música.

Sin Qt (regla de core/), con el patrón de core/ambient_catalog.py: un
dataclass congelado y una tupla en el orden en que se muestran. Lo usa
DataHandler: _load siembra (también tras import_data) e import_data le pasa
a un backup sin marca la de las radios que el usuario quitó.

Contrato:
- Sembrar es solo datos: las radios entran en data['music'] como elementos
  normales (live=True, mime y bitrate, igual que una radio que el usuario
  pega) de una lista normal, «Radios lofi», con el id fijo SEED_LIST_ID. Se
  quitan, renombran y reordenan como cualquier otra. Nada se conecta a la
  red hasta que el usuario pulsa reproducir.
- data['music']['seeds'] recuerda los ids del catálogo que ya se ofrecieron,
  se añadieran o no (ya estaba esa emisora en otra lista, aunque fuera con
  la URL escrita algo distinta, o no cabía). Así no se duplican y una radio
  que el usuario borre no vuelve. Si borra la lista entera, solo una radio
  NUEVA de una versión futura la vuelve a crear. Tampoco vuelve al importar
  un backup anterior a las radios de serie (sin 'seeds'): hereda la marca de
  las que quitó (declined_radios) y recibe el resto.
- Para un usuario nuevo la lista es la única; a quien ya tenía listas se le
  añade al final. Nunca sobre un archivo de datos que no se pudo leer
  (load_error), y DataHandler.SEED_DEFAULT_RADIOS = False la apaga (así va
  toda la suite de tests: tests/conftest.py).
- Los ids son estables y no se reutilizan nunca. Cambiar la URL de una radio
  ya sembrada no llega a quien la tiene guardada: hará falta una migración
  que reemplace la fuente vieja exacta.
- Las fuentes son streams directos (Icecast/SHOUTcast) en MP3 o AAC por
  https: FFmpeg abre la fuente tal cual (no expande .pls/.m3u), y el título
  ICY y el búfer solo funcionan con esos formatos. Nada de enlaces de
  radio.garden, que son redirecciones de su servicio.
  tests/test_radio_catalog.py lo comprueba sin red.
"""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.music_library import Entry, seed_entries, station_key

SEED_LIST_ID = 'radios-lofi'     # _new_id nunca lo genera: da hex sin guiones
SEED_LIST_NAME = "Radios lofi"


@dataclass(frozen=True)
class DefaultRadio:
    id: str          # estable: identifica la radio en music['seeds']
    title: str       # con el lugar, que el item no tiene campo para él
    source: str      # URL https directa del montaje (sin .pls/.m3u ni HLS)
    mime: str        # 'audio/mpeg' | 'audio/aac': el códec que muestra la fila
    bitrate: int     # kbps (icy-br del servidor)


# En este orden se añaden. Elegidas y medidas con curl el 2026-11-18 desde
# el equipo del usuario (formato, icy-metaint y tiempo hasta el primer
# byte). bigFM redirige (302) a su CDN: se guarda la URL de antes de la
# redirección, que no lleva tokens que caduquen
DEFAULT_RADIOS: Tuple[DefaultRadio, ...] = (
    # Icecast 2.4, único montaje (web: 247lofiradio.com)
    DefaultRadio('lofi247-londres', "24/7 Lofi Radio · Londres",
                 "https://ec3.yesstreaming.net:3755/stream", 'audio/mpeg', 64),
    # aac-128 siempre es AAC; la ruta mp3-128 sirve al azar MP3 o AAC
    DefaultRadio('bigfm-lofi-focus', "bigFM LoFi Focus · Alemania",
                 "https://stream.bigfm.de/lofifocus/aac-128/", 'audio/aac', 128),
    # AzuraCast, único montaje de la estación (web: nia.nc)
    DefaultRadio('nia-lofi-numea', "NIA Radio LoFi · Numea",
                 "https://radio.nia.nc/radio/8020/lofi-hq-stream.aac", 'audio/aac', 128),
)


def apply_default_radios(music: Dict[str, Any],
                         catalog: Optional[Iterable[DefaultRadio]] = None) -> bool:
    """Añade a `music` (ya saneada con normalize_music) las radios de serie
    que nunca se le ofrecieron y las apunta en music['seeds']. True si cambió
    algo (hay que guardar). Sin `catalog` usa DEFAULT_RADIOS, leído en cada
    llamada para que los tests lo puedan sustituir."""
    radios = DEFAULT_RADIOS if catalog is None else tuple(catalog)
    return seed_entries(
        music,
        ((r.id, Entry(r.source, r.title, live=True, mime=r.mime, bitrate=r.bitrate))
         for r in radios),
        SEED_LIST_ID, SEED_LIST_NAME)


def declined_radios(music: Dict[str, Any],
                    catalog: Optional[Iterable[DefaultRadio]] = None) -> List[str]:
    """Los ids de music['seeds'] (saneada con normalize_music) cuya emisora
    ya no está en ninguna lista: las de serie que el usuario quitó (o que no
    llegaron a entrar) y las que el catálogo ya no conoce.

    Es la marca que hereda un backup anterior a las radios de serie (con
    música pero sin 'seeds') al importarlo: así recibe las radios que el
    usuario conserva y no resucita las que quitó (DataHandler.import_data)."""
    radios = {r.id: r for r in (DEFAULT_RADIOS if catalog is None else catalog)}
    present = {station_key(i['source']) for x in music['lists'] for i in x['items']}
    return [sid for sid in music['seeds']
            if sid not in radios or station_key(radios[sid].source) not in present]
