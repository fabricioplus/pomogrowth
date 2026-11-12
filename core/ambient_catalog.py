"""Catálogo de los sonidos de ambiente (lluvia, océano, ruidos…) y su mezcla.

Sin Qt (regla de core/): lo usan AmbientService (services/ambient_service.py),
el mezclador del diálogo de Música y DataHandler._load, que sanea la mezcla
guardada en settings['ambient'].

Los audios son recursos de la app (no del usuario): viven en
assets/sounds/ambient/<id>.ogg dentro de los recursos empaquetados
(utils.paths.resource_base(): _internal en el exe), con un CREDITOS.txt de
sus licencias. Un sonido sin su archivo no está disponible (la tarjeta sale
desactivada), nunca es un error.

La mezcla guardada (settings['ambient']):
- volumes:    {id: 0..100} por sonido (los que falten, su valor por defecto)
- master:     0..100, volumen general del ambiente
- focus_only: bool, solo con un pomodoro o una sesión Libre en marcha
- last_mix:   [ids] la última mezcla que sonó: «Reanudar» la recupera (al
              abrir la app no suena nada sola)
"""
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import paths

GROUP_NATURE = 'naturaleza'
GROUP_NOISE = 'ruido'
SOUND_EXT = '.ogg'
MASTER_DEFAULT = 100


@dataclass(frozen=True)
class AmbientSound:
    id: str
    name: str
    icon: str        # assets/icons/<icon>.svg (Lucide)
    group: str       # GROUP_NATURE | GROUP_NOISE
    volume: int      # volumen por defecto (0-100)
    hint: str        # una línea para el tooltip de la tarjeta


# En este orden se pintan las tarjetas. Con el general al 100 % y el sonido
# al 70 %, una grabación queda unos 9 dB por debajo de la música con su
# volumen por defecto: de fondo, sin taparla. Los ruidos arrancan algo más
# bajos (y sus archivos ya lo están: scripts/build_ambient_sounds.py) porque
# cansan antes; la chimenea, más alta, porque el limitador la deja más baja
SOUNDS: Tuple[AmbientSound, ...] = (
    AmbientSound('lluvia', "Lluvia", 'cloud-rain', GROUP_NATURE, 70,
                 "Lluvia suave y constante"),
    AmbientSound('tormenta', "Tormenta", 'cloud-lightning', GROUP_NATURE, 70,
                 "Lluvia con truenos"),
    AmbientSound('oceano', "Océano", 'waves', GROUP_NATURE, 70,
                 "Olas que rompen en la orilla"),
    AmbientSound('rio', "Río", 'droplets', GROUP_NATURE, 70,
                 "Agua que corre entre piedras"),
    AmbientSound('bosque', "Bosque", 'trees', GROUP_NATURE, 70,
                 "Pájaros y hojas al viento"),
    AmbientSound('chimenea', "Chimenea", 'flame', GROUP_NATURE, 80,
                 "Leña que crepita"),
    AmbientSound('ruido_blanco', "Ruido blanco", 'audio-waveform', GROUP_NOISE, 65,
                 "Tapa voces y ruidos"),
    AmbientSound('ruido_rosa', "Ruido rosa", 'audio-waveform', GROUP_NOISE, 65,
                 "Más suave que el blanco"),
    AmbientSound('ruido_marron', "Ruido marrón", 'audio-waveform', GROUP_NOISE, 65,
                 "Grave, como un rumor"),
)
SOUND_IDS: Tuple[str, ...] = tuple(s.id for s in SOUNDS)
_BY_ID: Dict[str, AmbientSound] = {s.id: s for s in SOUNDS}


def get(sound_id: str) -> Optional[AmbientSound]:
    return _BY_ID.get(sound_id)


def ambient_dir() -> Path:
    """Carpeta de los audios entre los recursos empaquetados. Se resuelve en
    cada llamada (paths.resource_base, no una constante del módulo): así los
    tests pueden apuntarla a tmp."""
    return paths.resource_base() / "assets" / "sounds" / "ambient"


def sound_path(sound_id: str) -> Path:
    return ambient_dir() / f"{sound_id}{SOUND_EXT}"


def is_available(sound_id: str) -> bool:
    """¿Está el archivo del sonido? (sin él la tarjeta sale desactivada)."""
    if sound_id not in _BY_ID:
        return False
    try:
        return sound_path(sound_id).is_file()
    except OSError:
        return False


# ================================================================ la mezcla
def clamp_volume(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):  # NaN, ±inf
        return default
    return int(max(0, min(100, round(value))))  # un entero enorme no desborda aquí


def default_mix() -> Dict[str, Any]:
    return {'volumes': {s.id: s.volume for s in SOUNDS}, 'master': MASTER_DEFAULT,
            'focus_only': False, 'last_mix': []}


def normalize_mix(raw: Any) -> Dict[str, Any]:
    """settings['ambient'] saneado: volúmenes 0-100 de los sonidos que
    existen (los que falten, por defecto), general 0-100, focus_only bool y
    last_mix sin repetir ni ids desconocidos, en el orden del catálogo."""
    mix = default_mix()
    if not isinstance(raw, dict):
        return mix
    volumes = raw.get('volumes')
    if isinstance(volumes, dict):
        for s in SOUNDS:
            mix['volumes'][s.id] = clamp_volume(volumes.get(s.id), s.volume)
    mix['master'] = clamp_volume(raw.get('master'), MASTER_DEFAULT)
    mix['focus_only'] = raw.get('focus_only') is True
    last = raw.get('last_mix')
    if isinstance(last, (list, tuple)):
        wanted = {x for x in last if isinstance(x, str)}
        mix['last_mix'] = [sid for sid in SOUND_IDS if sid in wanted]
    return mix


def ordered(ids) -> List[str]:
    """Los ids conocidos de `ids`, sin repetir y en el orden del catálogo."""
    wanted = set(ids)
    return [sid for sid in SOUND_IDS if sid in wanted]


def mix_gain(volume: int, master: int) -> float:
    """Amplitud (0..1) de un sonido con su volumen y el general (0-100 cada
    uno). El oído es logarítmico: con una curva cuadrática el deslizador a la
    mitad suena «a la mitad» (con la lineal, el 50 % apenas baja)."""
    level = max(0.0, min(1.0, volume / 100.0)) * max(0.0, min(1.0, master / 100.0))
    return level * level
