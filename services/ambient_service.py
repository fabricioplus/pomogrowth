"""Sonidos de ambiente en bucle (lluvia, océano, ruido rosa…) mezclables.

Independiente de MusicService y simultáneo con ella: cada uno tiene sus
reproductores y su volumen. El catálogo y la mezcla guardada están en
core/ambient_catalog.py; aquí solo se hace sonar.

Contrato con la interfaz (resumen; detalle en cada método):
- Señales: changed() (cambió algo de la mezcla: sonidos activos, volúmenes,
  general, «solo durante el enfoque»), state_changed(str: 'off'|'playing'|
  'waiting') y error_occurred(str, mensaje en español).
- Control: toggle(id), set_active(id, on), set_volume(id, 0..100),
  set_master(0..100), set_focus_only(on), on_focus_state(running),
  stop_all(), resume_last() («Reanudar»: la última mezcla) y shutdown().
- Lectura: active_ids(), is_playing(), state(), sound_state(id),
  available(id), volume(id), master(), focus_only(), last_mix(),
  can_resume().
- Al crear el servicio no suena nada: la última mezcla espera a «Reanudar».

Cómo suena sin cortes: cada sonido activo es un «looper» con DOS
reproductores que se relevan antes del final del archivo con un fundido
cruzado de igual potencia (XFADE_MS; coseno el que sale, seno el que entra:
la suma de potencias es constante y no hay bache de volumen). setLoops de
QMediaPlayer deja un hueco audible al volver al principio. Al activar entra
con un fundido (FADE_IN_MS) y al desactivar o parar sale con otro
(FADE_OUT_MS): nunca un corte brusco. Con «solo durante el enfoque» se pausa
y reanuda con esos mismos fundidos.

Un único temporizador (_tick) lleva fundidos, relevos y errores: cada TICK_MS
mientras algo se funde y cada STEADY_MS si solo suena (basta para ver
llegar el final con segundos de margen); parado si nada suena. Es robusto a
una duración aún desconocida (no hay relevo hasta saberla; si el archivo se
acaba antes, salta al otro reproductor), a un archivo que falla (ese sonido
se desactiva y se avisa) y a parar o reactivar durante un fundido (el nuevo
fundido parte del volumen del momento).

Los reproductores salen de una fábrica (VOICE_FACTORY o voice_factory=):
los tests usan reproductores falsos y tests/conftest.py impide crear
QMediaPlayer reales en toda la suite. Un reproductor («voz») ofrece load,
play, pause, stop, set_position, position, duration, set_gain, error,
at_end y dispose (ver _QtVoice).
"""
import math
import time
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from core import ambient_catalog as catalog

_S = QMediaPlayer.MediaStatus
# El texto de Qt llega en inglés («Could not open file»): al usuario, el
# nuestro según el tipo de error; el de Qt, solo al log
_E = QMediaPlayer.Error
ERROR_TEXT = {_E.ResourceError: "No se pudo leer el archivo",
              _E.FormatError: "Formato de audio no compatible",
              _E.AccessDeniedError: "Sin permiso para leer el archivo"}
DEFAULT_ERROR_TEXT = "No se pudo reproducir el archivo"


def error_text(error) -> str:
    return ERROR_TEXT.get(error, DEFAULT_ERROR_TEXT)


class _QtVoice:
    """Un reproductor de verdad: QMediaPlayer + QAudioOutput (sin señales
    hacia fuera: el servicio lo consulta en cada tick)."""

    def __init__(self, parent: QObject):
        self._audio = QAudioOutput(parent)
        self._audio.setVolume(0.0)
        self._player = QMediaPlayer(parent)
        self._player.setAudioOutput(self._audio)
        self._error = ''
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_status)

    def _on_error(self, error, text: str = '') -> None:
        if error != _E.NoError:
            if text:
                print(f"Ambiente: error del reproductor: {text}")
            self._error = error_text(error)

    def _on_status(self, status) -> None:
        if status == _S.InvalidMedia and not self._error:
            self._error = "El archivo no se puede reproducir"

    def load(self, path) -> None:
        self._error = ''
        self._player.setSource(QUrl.fromLocalFile(str(path)))

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def stop(self) -> None:
        self._player.stop()

    def set_position(self, ms: int) -> None:
        self._player.setPosition(int(ms))

    def position(self) -> int:
        return int(self._player.position())

    def duration(self) -> int:
        return int(self._player.duration())

    def set_gain(self, gain: float) -> None:
        self._audio.setVolume(max(0.0, min(1.0, float(gain))))

    def error(self) -> str:
        return self._error

    def at_end(self) -> bool:
        return self._player.mediaStatus() == _S.EndOfMedia

    def dispose(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())
        self._player.deleteLater()
        self._audio.deleteLater()


# Fábrica de reproductores: f(parent) -> voz. tests/conftest.py la cambia por
# una falsa para que ningún test cree un QMediaPlayer de verdad
VOICE_FACTORY: Callable[[QObject], object] = _QtVoice


def _smooth(p: float) -> float:
    """0..1 → 0..1 con arranque y final suaves (un fundido lineal se nota
    al empezar)."""
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


class _Looper:
    """Un sonido activo: dos voces que se relevan y su fundido general."""

    def __init__(self, sound_id: str, voices: List[object], now: float):
        self.id = sound_id
        self.voices = voices
        self.main = 0                  # la voz que suena (la otra espera el relevo)
        self.paused = True             # voces paradas (recién creado o enfoque en pausa)
        self.after: Optional[str] = None   # al acabar el fundido: 'remove' | 'pause'
        self._from = self._to = 0.0
        self._t0 = now
        self._ms = 0.0
        self.xfade_t0: Optional[float] = None
        self.xfade_ms = 0.0

    def gain(self, now: float) -> float:
        if self._ms <= 0 or now - self._t0 >= self._ms:
            return self._to
        return self._from + (self._to - self._from) * _smooth((now - self._t0) / self._ms)

    def fading(self, now: float) -> bool:
        return self._ms > 0 and now - self._t0 < self._ms

    def fade(self, now: float, to: float, ms: float, after: Optional[str] = None) -> None:
        """Fundido desde el volumen de AHORA (si se para a mitad de una
        entrada, la salida no salta al máximo antes de bajar)."""
        self._from = self.gain(now)
        self._to, self._t0, self._ms = float(to), now, float(ms)
        self.after = after

    @property
    def leaving(self) -> bool:
        return self.after == 'remove'


class AmbientService(QObject):
    changed = Signal()           # cambió la mezcla (activos, volúmenes, enfoque…)
    state_changed = Signal(str)  # 'off' | 'playing' | 'waiting' (para la cabecera)
    error_occurred = Signal(str)

    # Tiempos (ms). Atributos de clase para poder acortarlos en los tests
    FADE_IN_MS = 1000
    FADE_OUT_MS = 800
    XFADE_MS = 3000
    MIN_XFADE_MS = 250       # relevo mínimo si el tick llega tarde al final
    TICK_MS = 40             # con fundidos o relevos en marcha
    STEADY_MS = 200          # solo sonando: vigilar el final del archivo
    SAVE_MS = 800            # volúmenes: guardado con retardo, como la música

    def __init__(self, parent: Optional[QObject] = None, data_handler=None,
                 voice_factory: Optional[Callable[[QObject], object]] = None,
                 clock: Optional[Callable[[], float]] = None):
        """clock: segundos monótonos (time.monotonic); los tests pasan uno falso."""
        super().__init__(parent)
        self._dh = data_handler
        self._dh_ref = None
        self._factory = voice_factory or VOICE_FACTORY
        self._clock = clock or time.monotonic
        self._mix_data = catalog.default_mix()
        self._reload_mix()
        self._loopers: Dict[str, _Looper] = {}
        self._broken: Dict[str, str] = {}   # id → motivo (falló en esta sesión)
        self._focus_running = False
        self._state = 'off'
        self._ticker = QTimer(self)
        self._ticker.timeout.connect(self._tick)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._persist)

    def _now(self) -> float:
        return self._clock() * 1000.0

    # ================================================================ mezcla guardada
    @property
    def _mix(self) -> dict:
        # Importar un backup o borrar los datos reemplazan data_handler.data:
        # se relee la mezcla para no pisarla con una copia vieja
        if self._dh is not None and self._dh.data is not self._dh_ref:
            self._reload_mix()
        return self._mix_data

    def _reload_mix(self) -> None:
        if self._dh is None:
            return
        self._dh_ref = self._dh.data
        try:
            mix = catalog.normalize_mix((self._dh.load_settings() or {}).get('ambient'))
        except Exception as e:  # noqa: BLE001 - datos raros: la mezcla por defecto
            print(f"Ambiente: no se pudo leer la mezcla: {e}")
            mix = catalog.default_mix()
        self._mix_data = mix
        if getattr(self, '_loopers', None):   # (en __init__ aún no existen)
            # Datos importados con sonidos encendidos: «solo durante el
            # enfoque» y los volúmenes pueden haber cambiado. Se reaplican
            # fuera de este getter (no emitir señales desde dentro)
            QTimer.singleShot(0, self, self._resync)

    def _resync(self) -> None:
        self._apply_gate()
        self._apply_all()
        self._emit()

    def _persist(self) -> None:
        self._save_timer.stop()
        if self._dh is None:
            return
        try:
            settings = self._dh.load_settings()
            settings['ambient'] = catalog.normalize_mix(self._mix)
            self._dh.save_settings(settings)
            self._dh_ref = self._dh.data
        except Exception as e:  # noqa: BLE001
            print(f"Ambiente: no se pudo guardar la mezcla: {e}")

    def _save_soon(self) -> None:
        """Lo que cambia a menudo (deslizadores): no reescribir el JSON entero
        a cada paso. stop_all() y shutdown() guardan lo pendiente."""
        if self._dh is not None:
            self._save_timer.start(self.SAVE_MS)

    def _remember_mix(self) -> None:
        """La última mezcla que sonó (para «Reanudar»): se apunta mientras
        haya algo activo; al quedarse en nada se conserva la anterior."""
        active = self.active_ids()
        if active:
            self._mix['last_mix'] = active

    # ================================================================ lectura
    def available(self, sound_id: str) -> bool:
        """Con su archivo y sin haber fallado en esta sesión."""
        return sound_id not in self._broken and catalog.is_available(sound_id)

    def unavailable_reason(self, sound_id: str) -> str:
        if sound_id in self._broken:
            return f"No se pudo reproducir: {self._broken[sound_id]}"
        if not catalog.is_available(sound_id):
            return "No disponible: falta su archivo de sonido"
        return ''

    def active_ids(self) -> List[str]:
        """Los sonidos encendidos (sin los que se están apagando), en el
        orden del catálogo."""
        return catalog.ordered(sid for sid, lp in self._loopers.items() if not lp.leaving)

    def is_active(self, sound_id: str) -> bool:
        lp = self._loopers.get(sound_id)
        return lp is not None and not lp.leaving

    def _gated(self) -> bool:
        return bool(self._mix['focus_only']) and not self._focus_running

    def state(self) -> str:
        if not self.active_ids():
            return 'off'
        return 'waiting' if self._gated() else 'playing'

    def is_playing(self) -> bool:
        return self.state() == 'playing'

    def sound_state(self, sound_id: str) -> str:
        """'unavailable' | 'off' | 'playing' | 'waiting' (activo, esperando
        a que arranque el enfoque)."""
        if self.is_active(sound_id):
            return 'waiting' if self._gated() else 'playing'
        return 'off' if self.available(sound_id) else 'unavailable'

    def volume(self, sound_id: str) -> int:
        sound = catalog.get(sound_id)
        return int(self._mix['volumes'].get(sound_id, sound.volume if sound else 50))

    def master(self) -> int:
        return int(self._mix['master'])

    def focus_only(self) -> bool:
        return bool(self._mix['focus_only'])

    def last_mix(self) -> List[str]:
        return list(self._mix['last_mix'])

    def can_resume(self) -> bool:
        """«Reanudar»: nada suena y la última mezcla tiene algo disponible."""
        return not self.active_ids() and any(self.available(s) for s in self.last_mix())

    def gain_of(self, sound_id: str) -> float:
        """Amplitud objetivo de un sonido (sin fundidos): volumen × general."""
        return catalog.mix_gain(self.volume(sound_id), self.master())

    # ================================================================ control
    def toggle(self, sound_id: str) -> bool:
        """Enciende o apaga un sonido. Devuelve si quedó encendido."""
        self.set_active(sound_id, not self.is_active(sound_id))
        return self.is_active(sound_id)

    def set_active(self, sound_id: str, on: bool) -> bool:
        """Devuelve False si no se pudo (sonido desconocido o sin archivo:
        eso no es un error, la tarjeta ya sale desactivada)."""
        if catalog.get(sound_id) is None:
            return False
        if on:
            if not self._switch_on(sound_id):
                return False
        else:
            self._switch_off(sound_id)
        self._remember_mix()
        self._persist()
        self._emit()
        return True

    def _switch_on(self, sound_id: str) -> bool:
        lp = self._loopers.get(sound_id)
        if lp is not None and not lp.leaving:
            return True
        if not self.available(sound_id):
            return False
        now = self._now()
        if lp is None:
            lp = self._create(sound_id, now)
            if lp is None:
                return False
        lp.after = None
        if self._gated():
            # Armado: sonará al arrancar el enfoque. Si estaba apagándose,
            # termina de bajar y se queda en pausa
            if not lp.paused:
                lp.fade(now, 0.0, self.FADE_OUT_MS, after='pause')
                self._kick()
        else:
            self._fade_in(lp, now)
        return True

    def _switch_off(self, sound_id: str) -> None:
        lp = self._loopers.get(sound_id)
        if lp is None or lp.leaving:
            return
        if lp.paused:
            self._remove(lp)      # no suena: nada que fundir
        else:
            lp.fade(self._now(), 0.0, self.FADE_OUT_MS, after='remove')
            self._kick()

    def _create(self, sound_id: str, now: float) -> Optional[_Looper]:
        path = catalog.sound_path(sound_id)
        voices: List[object] = []
        try:
            for _ in range(2):
                voice = self._factory(self)
                voices.append(voice)
                voice.set_gain(0.0)
                voice.load(path)
        except Exception as e:  # noqa: BLE001 - el backend de audio no arrancó
            for voice in voices:
                self._dispose(voice)
            self._fail_id(sound_id, str(e) or "No se pudo abrir el audio")
            return None
        lp = _Looper(sound_id, voices, now)
        self._loopers[sound_id] = lp
        return lp

    def _fade_in(self, lp: _Looper, now: float) -> None:
        if lp.paused:
            lp.voices[lp.main].play()
            lp.paused = False
        lp.fade(now, 1.0, self.FADE_IN_MS)
        self._kick()

    def set_volume(self, sound_id: str, value) -> None:
        sound = catalog.get(sound_id)
        if sound is None:
            return
        value = catalog.clamp_volume(value, self.volume(sound_id))
        if value == self.volume(sound_id):
            return
        self._mix['volumes'][sound_id] = value
        self._apply_all()
        self._save_soon()
        self.changed.emit()

    def set_master(self, value) -> None:
        value = catalog.clamp_volume(value, self.master())
        if value == self.master():
            return
        self._mix['master'] = value
        self._apply_all()
        self._save_soon()
        self.changed.emit()

    def set_focus_only(self, on: bool) -> None:
        on = bool(on)
        if on == self.focus_only():
            return
        self._mix['focus_only'] = on
        self._persist()
        self._apply_gate()
        self._emit()

    def on_focus_state(self, focus_running: bool) -> None:
        """El controller avisa en cada arranque/pausa/fin/cambio del timer
        (como a la música). Con focus_only: enfoque en marcha → suena;
        descanso, pausa o parado → se pausa con un fundido (sigue activo)."""
        running = bool(focus_running)
        if running == self._focus_running:
            return
        self._focus_running = running
        if self.focus_only():
            self._apply_gate()
            self._emit()

    def _apply_gate(self) -> None:
        now = self._now()
        gated = self._gated()
        for lp in self._loopers.values():
            if lp.leaving:
                continue
            if gated:
                if not lp.paused and lp.after != 'pause':
                    lp.fade(now, 0.0, self.FADE_OUT_MS, after='pause')
                    self._kick()
            elif lp.paused or lp.after == 'pause':
                lp.after = None
                self._fade_in(lp, now)

    def stop_all(self) -> None:
        """«Parar todo»: todos salen con su fundido. La mezcla queda
        recordada para «Reanudar»."""
        self._remember_mix()
        for lp in list(self._loopers.values()):
            if lp.leaving:
                continue
            if lp.paused:
                self._remove(lp)
            else:
                lp.fade(self._now(), 0.0, self.FADE_OUT_MS, after='remove')
                self._kick()
        self._persist()
        self._emit()

    def resume_last(self) -> int:
        """«Reanudar»: enciende la última mezcla (lo que siga disponible).
        Devuelve cuántos sonidos encendió."""
        count = 0
        for sound_id in self.last_mix():
            if self.available(sound_id) and self._switch_on(sound_id):
                count += 1
        if count:
            self._remember_mix()
            self._persist()
            self._emit()
        return count

    def shutdown(self) -> None:
        """Al cerrar la app: para todo con su fundido (la despedida deja
        tiempo de sobra) y guarda lo pendiente."""
        self.stop_all()

    # ================================================================ motor
    def _kick(self) -> None:
        """Hay un fundido o un relevo: tick rápido."""
        if not self._ticker.isActive() or self._ticker.interval() != self.TICK_MS:
            self._ticker.start(self.TICK_MS)

    def _tick(self) -> None:
        now = self._now()
        for lp in list(self._loopers.values()):
            error = next((v.error() for v in lp.voices if v.error()), '')
            if error:
                self._fail(lp, error)
                continue
            if lp.paused:
                continue
            if lp.after and not lp.fading(now):
                if lp.after == 'remove':
                    self._remove(lp)
                    self._emit()
                    continue
                if lp.after == 'pause':
                    self._pause(lp)
                    continue
            self._loop(lp, now)
            self._apply(lp, now)
        self._schedule(now)

    def _schedule(self, now: float) -> None:
        sounding = [lp for lp in self._loopers.values() if not lp.paused]
        if not sounding:
            self._ticker.stop()
            return
        busy = any(lp.fading(now) or lp.after or lp.xfade_t0 is not None for lp in sounding)
        interval = self.TICK_MS if busy else self.STEADY_MS
        if not self._ticker.isActive() or self._ticker.interval() != interval:
            self._ticker.start(interval)

    def _loop(self, lp: _Looper, now: float) -> None:
        """El relevo: antes del final del archivo entra la otra voz desde el
        principio y las dos se cruzan con igual potencia."""
        main = lp.voices[lp.main]
        other = lp.voices[1 - lp.main]
        if lp.xfade_t0 is not None:
            if now - lp.xfade_t0 >= lp.xfade_ms:
                self._finish_xfade(lp)
            return
        if main.at_end():
            # Se acabó sin relevo (duración desconocida o un tick muy tarde):
            # la otra entra ya, sin fundido
            other.set_position(0)
            other.play()
            main.stop()
            lp.main = 1 - lp.main
            return
        duration = main.duration()
        if duration <= 0:
            return          # aún no se sabe: sin relevo hasta saberla
        xfade = min(self.XFADE_MS, duration / 3)
        left = duration - main.position()
        if left <= xfade:
            other.set_position(0)
            other.play()
            lp.xfade_t0 = now
            lp.xfade_ms = max(self.MIN_XFADE_MS, min(xfade, left))

    def _finish_xfade(self, lp: _Looper) -> None:
        if lp.xfade_t0 is None:
            return
        lp.voices[lp.main].stop()
        lp.voices[lp.main].set_gain(0.0)
        lp.main = 1 - lp.main
        lp.xfade_t0 = None

    def _apply(self, lp: _Looper, now: float) -> None:
        if lp.paused:
            return
        base = self.gain_of(lp.id) * lp.gain(now)
        main = lp.voices[lp.main]
        other = lp.voices[1 - lp.main]
        if lp.xfade_t0 is not None:
            p = max(0.0, min(1.0, (now - lp.xfade_t0) / lp.xfade_ms))
            main.set_gain(base * math.cos(p * math.pi / 2))
            other.set_gain(base * math.sin(p * math.pi / 2))
        else:
            main.set_gain(base)
            other.set_gain(0.0)

    def _apply_all(self) -> None:
        now = self._now()
        for lp in self._loopers.values():
            self._apply(lp, now)

    def _pause(self, lp: _Looper) -> None:
        """Fin del fundido de salida por el enfoque: las voces en pausa (al
        reanudar siguen donde iban)."""
        self._finish_xfade(lp)
        for voice in lp.voices:
            voice.set_gain(0.0)
        lp.voices[lp.main].pause()
        lp.paused = True
        lp.after = None

    def _remove(self, lp: _Looper) -> None:
        if self._loopers.get(lp.id) is lp:
            del self._loopers[lp.id]
        for voice in lp.voices:
            self._dispose(voice)

    @staticmethod
    def _dispose(voice) -> None:
        try:
            voice.set_gain(0.0)
            voice.dispose()
        except Exception as e:  # noqa: BLE001
            print(f"Ambiente: no se pudo soltar un reproductor: {e}")

    def _fail(self, lp: _Looper, error: str) -> None:
        self._remove(lp)
        self._fail_id(lp.id, error)

    def _fail_id(self, sound_id: str, error: str) -> None:
        """Un archivo que no suena: ese sonido se apaga, sale de la última
        mezcla y queda no disponible en esta sesión; se avisa una vez."""
        self._broken[sound_id] = error
        if sound_id in self._mix['last_mix']:
            self._mix['last_mix'] = [s for s in self._mix['last_mix'] if s != sound_id]
            self._persist()
        sound = catalog.get(sound_id)
        name = sound.name if sound else sound_id
        print(f"Ambiente: «{name}» falló: {error}")
        self.error_occurred.emit(f"No se pudo reproducir «{name}»: {error}")
        self._emit()

    def _emit(self) -> None:
        self.changed.emit()
        state = self.state()
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
