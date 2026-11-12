"""Sonidos del temporizador: avisos al iniciar, pausar, acabar un pomodoro (o
la sesión Libre) y acabar un descanso.

Los llamadores solo usan play(kind) con kind en EVENTS ('start', 'stop',
'complete', 'break'); el resto lo decide la configuración (Ajustes → «Timer y
ventana» → Sonidos, que el AppController aplica al arrancar y al guardar):

- estilos propios ('campana', 'marimba', 'suave'): WAV sintetizados por
  scripts/build_sounds.py en assets/sounds/timer/<estilo>/<evento>.wav, con
  QtMultimedia.QSoundEffect (baja latencia, sin decodificar). Los efectos se
  crean en el hilo de la GUI la primera vez que suena un estilo (los cuatro
  de ese estilo a la vez) y quedan en caché, colgados de la QApplication.
  QSoundEffect carga en segundo plano: lo que se pide antes de que esté listo
  suena al quedar listo (nunca dos veces).
- 'windows': lo de siempre (v8 y hasta la 1.0), los sonidos del sistema con
  winsound (alias SystemStart, SystemHand…); el volumen no se aplica.

Si falta el WAV, QSoundEffect no carga o se llama fuera del hilo de la GUI,
se vuelve a winsound sin romper nada. Nada de hilos con QSoundEffect: solo el
alias de winsound va en un hilo (como en v8; PlaySound puede tardar en
resolverlo).
"""
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from utils.paths import resource_base

EVENTS = ('start', 'stop', 'complete', 'break')
# (clave, texto del selector de Ajustes), en el orden del selector
STYLES = (('campana', 'Campana'), ('marimba', 'Marimba'), ('suave', 'Suave'),
          ('windows', 'Sistema de Windows'))
STYLE_KEYS = tuple(key for key, _label in STYLES)
WAV_STYLES = ('campana', 'marimba', 'suave')

DEFAULT_ENABLED = True
DEFAULT_VOLUME = 70
DEFAULT_STYLE = 'campana'

SOUNDS_DIR = resource_base() / 'assets' / 'sounds' / 'timer'

_ALIASES = {
    'start': 'SystemStart',
    'stop': 'SystemHand',
    'complete': 'SystemAsterisk',
    'break': 'SystemQuestion',
}

_config: Dict[str, Any] = {'enabled': DEFAULT_ENABLED, 'volume': DEFAULT_VOLUME,
                           'style': DEFAULT_STYLE}
# (estilo, evento) -> QSoundEffect (o None si no se pudo crear)
_effects: Dict[Tuple[str, str], Any] = {}
# Efectos pedidos antes de estar listos: id(efecto) -> (efecto, evento)
_pending: Dict[int, Tuple[Any, str]] = {}


# ---------------------------------------------------------------- ajustes
def normalize_style(style) -> str:
    return style if style in STYLE_KEYS else DEFAULT_STYLE


def normalize_volume(volume) -> int:
    if isinstance(volume, bool):
        return DEFAULT_VOLUME
    if isinstance(volume, int):  # también uno enorme, que float() no admite
        return max(0, min(100, volume))
    try:
        return max(0, min(100, int(round(float(volume)))))
    except (TypeError, ValueError, OverflowError):  # también inf o 10**400
        return DEFAULT_VOLUME


def configure(enabled: Optional[bool] = None, volume=None,
              style: Optional[str] = None) -> None:
    """Cambia lo que se pase (lo demás se queda como estaba)."""
    if enabled is not None:
        _config['enabled'] = bool(enabled)
    if volume is not None:
        _config['volume'] = normalize_volume(volume)
    if style is not None:
        _config['style'] = normalize_style(style)


def configure_from_settings(settings: Dict[str, Any]) -> None:
    """Aplica las claves timer_sound* de settings (con valores por defecto
    para datos viejos)."""
    configure(enabled=settings.get('timer_sounds_enabled', DEFAULT_ENABLED),
              volume=settings.get('timer_sound_volume', DEFAULT_VOLUME),
              style=settings.get('timer_sound_style', DEFAULT_STYLE))


def config() -> Dict[str, Any]:
    return dict(_config)


def sound_path(style: str, kind: str) -> Path:
    return SOUNDS_DIR / style / f"{kind}.wav"


def linear_volume(volume: int) -> float:
    """0-100 del deslizador → volumen lineal de QSoundEffect, con curva
    cuadrática (como oímos: al 50 % suena claramente más bajo, −12 dB, y no
    casi igual; al 70 % por defecto, −6 dB). La curva logarítmica de Qt
    (QAudio.convertVolume) dejaba el 70 % en −12 dB: demasiado flojo para un
    aviso."""
    v = normalize_volume(volume) / 100.0
    return v * v


# ---------------------------------------------------------------- reproducir
def play(kind: str) -> None:
    """Aviso del temporizador con la configuración actual (no bloquea)."""
    if not _config['enabled']:
        return
    _play(_config['style'], kind, _config['volume'])


def preview(style: str, kind: str = 'complete', volume=None) -> None:
    """Botón «Probar» de Ajustes: suena aunque los sonidos estén apagados o
    aún no se haya guardado nada."""
    _play(normalize_style(style), kind,
          _config['volume'] if volume is None else normalize_volume(volume))


def _play(style: str, kind: str, volume: int) -> None:
    if kind not in EVENTS:
        kind = 'complete'
    if style == 'windows':  # al volumen del sistema: winsound no admite otro
        _play_winsound(kind)
        return
    if volume <= 0:
        return
    if not _play_effect(style, kind, volume):
        _play_winsound(kind)


def _play_winsound(kind: str) -> None:
    """Sonido del sistema (el de siempre): estilo «Sistema de Windows» y
    vuelta atrás si un WAV no se puede reproducir."""
    if sys.platform != "win32":
        return

    def _run():
        try:
            import winsound
            winsound.PlaySound(_ALIASES.get(kind, 'SystemAsterisk'),
                               winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def _gui_thread_app():
    """La QApplication si estamos en su hilo; si no, None (QSoundEffect no
    se crea ni se toca fuera del hilo de la GUI)."""
    try:
        from PySide6.QtCore import QCoreApplication, QThread
    except Exception:
        return None
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() is not app.thread():
        return None
    return app


def _new_effect(path: Path, parent):
    """Crea un QSoundEffect cargando `path` (los tests lo sustituyen)."""
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QSoundEffect
    effect = QSoundEffect(parent)
    _follow_default_output(effect)  # antes de cargar: al tocar, solo si cambió
    effect.setSource(QUrl.fromLocalFile(str(path)))
    return effect


def _alive(effect) -> bool:
    """El objeto de C++ sigue vivo (se borra con la QApplication)."""
    try:
        effect.status()
        return True
    except RuntimeError:
        return False


def _load_style(style: str, app) -> None:
    """Crea los efectos de los cuatro eventos de un estilo (precarga)."""
    for kind in EVENTS:
        key = (style, kind)
        effect = _effects.get(key)
        if effect is not None and _alive(effect):
            continue
        path = sound_path(style, kind)
        effect = None
        if path.is_file():
            try:
                effect = _new_effect(path, app)
                effect.statusChanged.connect(
                    lambda e=effect: _on_status_changed(e))
            except Exception as exc:
                print(f"No se pudo cargar el sonido {path.name} ({style}): {exc}")
                effect = None
        _effects[key] = effect


def _play_effect(style: str, kind: str, volume: int) -> bool:
    """Reproduce el WAV con QSoundEffect. False si hay que volver a winsound."""
    if style not in WAV_STYLES or not sound_path(style, kind).is_file():
        return False
    app = _gui_thread_app()
    if app is None:
        return False
    try:
        effect = _effects.get((style, kind))
        if effect is None or not _alive(effect):
            _load_style(style, app)
            effect = _effects.get((style, kind))
        if effect is None:
            return False
        from PySide6.QtMultimedia import QSoundEffect
        status = effect.status()
        if status == QSoundEffect.Status.Error:
            return False
        _follow_default_output(effect)
        effect.setVolume(linear_volume(volume))
        if status == QSoundEffect.Status.Ready:
            _pending.pop(id(effect), None)
            effect.play()
        else:
            # Aún cargando: suena al quedar listo (statusChanged)
            _pending[id(effect)] = (effect, kind)
        return True
    except Exception as exc:
        print(f"No se pudo reproducir el sonido {kind} ({style}): {exc}")
        return False


def _follow_default_output(effect) -> None:
    """Salida explícita = la predeterminada de ahora. Si después se conectan
    unos auriculares o se cambia la salida en Windows, el aviso va a la
    nueva (sin depender de cómo resuelva Qt la salida «nula» por defecto)."""
    try:
        from PySide6.QtMultimedia import QMediaDevices
        default = QMediaDevices.defaultAudioOutput()
        if not default.isNull() and effect.audioDevice() != default:
            effect.setAudioDevice(default)
    except Exception:
        pass


def _on_status_changed(effect) -> None:
    entry = _pending.get(id(effect))
    if entry is None or entry[0] is not effect:
        return
    try:
        from PySide6.QtMultimedia import QSoundEffect
        status = effect.status()
        if status == QSoundEffect.Status.Ready:
            _pending.pop(id(effect), None)
            effect.play()
        elif status == QSoundEffect.Status.Error:
            _pending.pop(id(effect), None)
            _play_winsound(entry[1])
    except RuntimeError:
        _pending.pop(id(effect), None)


def _reset_cache() -> None:
    """Olvida los efectos creados (tests)."""
    _effects.clear()
    _pending.clear()
