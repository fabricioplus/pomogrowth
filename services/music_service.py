"""Reproductor de música personalizada sobre QMediaPlayer + QAudioOutput.

Reproduce listas del usuario (archivos locales, enlaces http/https, radios
Icecast/SHOUTcast y HLS; MP3 hasta 320 kbps y lo que entienda FFmpeg) y la
lista virtual «Carpeta lofi» (assets/music/lofi junto al exe, solo lectura).
La biblioteca vive en data['music'] (core.music_library, DataHandler) y se
guarda en cada cambio; el volumen, con un retardo de ~800 ms.

Contrato con la interfaz (resumen; detalle en cada método):
- Señales: track_changed(str), playing_changed(bool), now_playing_changed(),
  state_changed(str: 'idle'|'loading'|'buffering'|'playing'|'paused'|
  'reconnecting'|'error'), error_occurred(str), library_changed(),
  position_changed(int ms), link_resolved(str list_id, int añadidos, str error).
- is_playing / playing_changed indican que la música está «encendida» (el
  usuario la quiere sonando): incluye cargando, reconectando y la espera del
  enfoque con focus_only. El detalle está en state y en current().
- current() añade a lo pactado 'station' (icy-name de la radio, '' si no hay),
  'waiting_focus' (encendida pero esperando a que arranque el enfoque) y
  'error' (con state 'error', el motivo corto, sin el título).
- focus_only: pulsar reproducir con el timer parado o en descanso arma la
  música pero NO suena (state 'paused', waiting_focus True) hasta que el
  controller avise con on_focus_state(True).
- Directo: si se corta (error, fin o sin avanzar STALL_MS) reconecta con las
  esperas de RECONNECT_DELAYS_MS (inyectables) sin saltar de item; pista
  finita: al terminar pasa a la siguiente según repeat/shuffle y, si falla,
  salta tras ERROR_SKIP_MS sin bucle infinito si fallan todas.
- Búfer adaptativo (services.stream_buffer): la primera conexión de cada
  directo es siempre directa (carga rápida). Cada corte de un directo que ya
  sonaba sube un escalón de BUFFER_STEPS_S y la reconexión pasa por el proxy
  local con esa precarga ('buffering' mientras tanto; current()['buffer_s']
  dice los segundos, 0 si va directo). Cambiar de item lo vuelve a 0; pausar
  y reanudar reconecta directo pero conserva el escalón. Sin HLS ni archivos
  locales ni pistas finitas, y sin guardarlo entre sesiones.
- «En directo» = duración 0 tras 1,5 s sonando (isSeekable() no sirve: el
  backend FFmpeg lo da True también en las radios). Solo se guarda en el item
  si lo confirman las cabeceras icy-* (o lo dijo el resolver): un MP3 servido
  sin Content-Length también dura 0 y, si acaba limpio, era una pista.
- Al pausar una radio se corta la conexión y al reanudar se reconecta (así no
  suena audio viejo del búfer ni se gasta red en pausa). Una pista por enlace
  también se desconecta, recordando la posición: al reanudar se recarga y
  salta ahí (el servidor cortaría una conexión parada en un descanso).
- Constructor: además de parent y data_handler admite lofi_dir (carpeta lofi;
  por defecto MUSIC_DIR), reconnect_delays, buffer_steps e icy_poll_ms (para
  los tests).
  Sin data_handler la biblioteca vive solo en memoria. Si data_handler.data
  se reemplaza (importar un backup, borrar datos) la biblioteca se relee.
  La pista actual y lo aprendido al sonar se guardan con retardo, como el
  volumen; shutdown() (al cerrar la app) además corta los enlaces en curso.

Compatibilidad con código anterior: tracks (rutas lofi), index, play(int)
(índice de la carpeta lofi), current_track_name(), rescan() y MUSIC_DIR.

Nunca leer de metaData() las claves FileFormat/AudioCodec/VideoCodec:
PySide6 lanza RuntimeError al convertirlas (solo claves seguras, con try).
"""
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer

from core.music_library import (EXT_MIME, HLS_EXTS, LOFI_LIST_ID, MAX_ITEMS_PER_LIST,
                                REPEAT_MODES, Entry, MusicLibrary, classify_link, codec_name,
                                scan_audio_folder, source_ext, title_from_source,
                                update_item_fields)
from services.icy_monitor import IcyMonitor
from services.music_resolver import (MSG_HLS_UNSAFE, Deadline, ResolveResult,
                                     local_hls_file_ok, resolve_link)
from services.stream_buffer import PREFILL_GRACE_S, StreamBuffer
from utils.paths import app_base

# Junto al exe cuando la app está empaquetada: el usuario suelta sus pistas ahí
MUSIC_DIR = app_base() / "assets" / "music" / "lofi"
LOFI_NAME = "Carpeta lofi"
DEFAULT_LIST_NAME = "Mis enlaces"

_S = QMediaPlayer.MediaStatus
_P = QMediaPlayer.PlaybackState


def _safe_meta(player: QMediaPlayer, key) -> Any:
    try:
        return player.metaData().value(key)
    except RuntimeError:
        return None


class MusicService(QObject):
    track_changed = Signal(str)          # título del item actual ('' si ninguno)
    playing_changed = Signal(bool)       # música encendida / apagada
    now_playing_changed = Signal()       # algo de current() cambió
    state_changed = Signal(str)
    error_occurred = Signal(str)         # mensaje en español
    library_changed = Signal()
    position_changed = Signal('qint64')  # ms (64 bits: una radio puede sonar días)
    link_resolved = Signal(str, int, str)

    # Tiempos (ms). Atributos de clase para poder acortarlos en los tests
    RECONNECT_DELAYS_MS: Tuple[int, ...] = (2000, 5000, 10000, 20000, 30000)
    ERROR_SKIP_MS = 1000
    STALL_MS = 15000          # directo que deja de avanzar → reconectar
    LOAD_TIMEOUT_MS = 30000   # no empieza a sonar (o finita atascada) → fallo
    STABLE_MS = 15000         # sonando este tiempo seguido → se reinician los intentos
    VOLUME_SAVE_MS = 800
    PREV_RESTART_MS = 3000    # «anterior» con la pista avanzada: vuelve al inicio
    WATCHDOG_MS = 1000
    LINK_TIMEOUT_S = 10
    # Búfer adaptativo: segundos de precarga tras el 1.º, 2.º, 3.º… corte (tope el último)
    BUFFER_STEPS_S: Tuple[float, ...] = (5, 10, 15, 20)
    LIVE_STALLED_MS = 5000    # directo atascado (StalledMedia) sin avanzar → corte

    def __init__(self, parent: Optional[QObject] = None, data_handler=None,
                 lofi_dir: Optional[Path] = None,
                 reconnect_delays: Optional[Iterable[int]] = None,
                 icy_poll_ms: Optional[int] = None,
                 buffer_steps: Optional[Iterable[float]] = None):
        super().__init__(parent)
        self._dh = data_handler
        self._dh_ref = None
        self._lib_obj = MusicLibrary(None)
        self._reload_library(apply_volume=False)
        if reconnect_delays is not None:
            self.RECONNECT_DELAYS_MS = tuple(int(x) for x in reconnect_delays)
        if buffer_steps is not None:
            self.BUFFER_STEPS_S = tuple(buffer_steps)
        self._lofi_dir = Path(lofi_dir) if lofi_dir else MUSIC_DIR
        self._lofi_items: List[Dict[str, Any]] = []
        self.tracks: List[Path] = []

        self._audio = QAudioOutput(self)
        self._audio.setVolume(self.volume())
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_media_info)
        self._player.seekableChanged.connect(self._on_media_info)
        self._player.metaDataChanged.connect(self._on_metadata)

        self._icy = IcyMonitor(self, poll_ms=icy_poll_ms)
        self._icy.info_changed.connect(self._on_icy)
        self._init_buffer()

        # Estado de reproducción
        self._want = False            # el usuario quiere música (armada)
        self._state = 'idle'
        self._active = False          # el reproductor debería estar sonando
        self._loaded_key: Optional[Tuple[str, str, str]] = None
        cur = self._lib.current
        self._cur_list: Optional[str] = cur.get('list')
        self._cur_item: Optional[str] = cur.get('item')
        self._snapshot: Optional[Dict[str, Any]] = None
        self._focus_running = False
        self._attempts = 0            # reconexiones seguidas
        self._errors_in_row = 0       # pistas finitas fallidas seguidas
        self._played = False          # avanzó alguna vez desde que se eligió
        self._progressed = False      # avanzó desde el último setSource
        self._live_runtime: Optional[bool] = None
        self._icy_confirmed = False   # cabeceras icy-* vistas: directo seguro
        self._runtime_bitrate = 0
        self._icy_info: Dict[str, Any] = {}
        self._last_pos = -1
        self._last_progress = 0.0
        self._load_started = 0.0
        self._shuffle_list: Optional[str] = None
        self._shuffle_order: List[str] = []
        self._pending_event: Optional[Tuple[str, str, str]] = None
        self._error_text = ''         # motivo corto del último error (current()['error'])
        # Pista por enlace desconectada en pausa: (clave cargada, posición, duración)
        self._resume_at: Optional[Tuple[Tuple[str, str, str], int, int]] = None
        self._seek_pending: Optional[int] = None
        self._seek_duration = 0       # la duración que se sabía al recargar para saltar
        self._link_deadlines: set = set()   # enlaces resolviéndose (shutdown los corta)

        self._reconnect_timer = self._single_shot(self._do_reconnect)
        self._skip_timer = self._single_shot(self._skip_after_error)
        self._stable_timer = self._single_shot(self._on_stable)
        self._volume_timer = self._single_shot(self._persist)
        self._event_timer = self._single_shot(self._dispatch_event)
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(self.WATCHDOG_MS)
        self._watchdog.timeout.connect(self._check_watchdog)

        self.rescan_lofi()
        self._snapshot = self._lookup(self._cur_list, self._cur_item)

    def _single_shot(self, slot) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(slot)
        return timer

    # ================================================================ biblioteca
    @property
    def _lib(self) -> MusicLibrary:
        # Importar un backup o borrar los datos reemplazan data_handler.data:
        # se relee la biblioteca para no pisar lo importado con una copia vieja
        if self._dh is not None and self._dh.data is not self._dh_ref:
            self._reload_library()
        return self._lib_obj

    def _reload_library(self, apply_volume: bool = True) -> None:
        if self._dh is None:
            return
        self._dh_ref = self._dh.data
        try:
            self._lib_obj = MusicLibrary(self._dh.get_music())
        except Exception as e:  # noqa: BLE001 - datos raros: biblioteca vacía
            print(f"Música: no se pudo leer la biblioteca: {e}")
            self._lib_obj = MusicLibrary(None)
        if apply_volume and hasattr(self, '_audio'):
            self._audio.setVolume(self._lib_obj.data['volume'])

    def _persist(self) -> None:
        self._volume_timer.stop()
        if self._dh is None:
            return
        try:
            self._dh.save_music(self._lib.to_dict())
            self._dh_ref = self._dh.data
        except Exception as e:  # noqa: BLE001
            print(f"Música: no se pudo guardar la biblioteca: {e}")

    def _save_soon(self) -> None:
        """Guardado con retardo para lo que cambia a menudo (volumen, pista
        actual, lo aprendido al sonar): no reescribir el JSON entero en el
        hilo de UI a cada cambio de pista. stop() guarda lo pendiente."""
        if self._dh is not None:
            self._volume_timer.start(self.VOLUME_SAVE_MS)

    def _items_of(self, list_id: Optional[str]) -> List[Dict[str, Any]]:
        if list_id == LOFI_LIST_ID:
            return self._lofi_items
        lst = self._lib.get_list(list_id)
        return lst['items'] if lst else []

    def _lookup(self, list_id: Optional[str], item_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not item_id:
            return None
        return next((i for i in self._items_of(list_id) if i['id'] == item_id), None)

    def _current_item(self) -> Optional[Dict[str, Any]]:
        item = self._lookup(self._cur_list, self._cur_item)
        if item is not None:
            self._snapshot = item
            return item
        # Borrado de la lista (o de la carpeta lofi) mientras sonaba
        return self._snapshot if self._loaded_key and self._snapshot else None

    # ================================================================ lectura
    def lists(self) -> List[Dict[str, Any]]:
        out = [{'id': LOFI_LIST_ID, 'name': LOFI_NAME, 'count': len(self._lofi_items),
                'readonly': True}]
        for lst in self._lib.lists:
            out.append({'id': lst['id'], 'name': lst['name'], 'count': len(lst['items']),
                        'readonly': False})
        return out

    def loose_target_name(self) -> str:
        """A qué lista van los archivos sueltos y enlaces que no piden una
        (lo que haría `_target_list(None, None)`, sin crear nada): la lista
        propia que suena o «Mis enlaces»."""
        cur = self._cur_list
        info = self._lib.get_list(cur) if cur and cur != LOFI_LIST_ID else None
        return info['name'] if info is not None else DEFAULT_LIST_NAME

    def items(self, list_id: str) -> List[Dict[str, Any]]:
        return [dict(i) for i in self._items_of(list_id)]

    def current(self) -> Optional[Dict[str, Any]]:
        item = self._current_item()
        if item is None:
            return None
        items = self._items_of(self._cur_list)
        index = next((n for n, i in enumerate(items) if i['id'] == item['id']), -1)
        live = self._is_live()
        loaded = self._loaded_key is not None
        station = (self._icy_info.get('name') or '') if live else ''
        if live:
            song = self._icy_info.get('title') or ''
            if song:
                subtitle = song
            elif station and station != item['title']:
                subtitle = station
            else:
                subtitle = 'En directo'
        else:
            subtitle = f"Pista {index + 1} de {len(items)}" if index >= 0 else ''
        bitrate = (self._runtime_bitrate or self._icy_info.get('bitrate')
                   or item.get('bitrate') or 0)
        saved = self._saved_spot(item)   # pista por enlace en pausa: sin conexión
        if loaded:
            position = int(self._player.position() if self._seek_pending is None
                           else self._seek_pending)
            duration = int(self._player.duration()) if not live else 0
        else:
            position, duration = (saved[1], saved[2]) if saved else (0, 0)
        return {
            'list_id': self._cur_list, 'item_id': item['id'], 'title': item['title'],
            'subtitle': subtitle, 'station': station, 'live': live,
            'bitrate_kbps': int(bitrate), 'codec': codec_name(item.get('mime', ''),
                                                              item['source']),
            'index': index, 'count': len(items),
            'position_ms': position, 'duration_ms': duration,
            'seekable': bool(loaded and not live and self._player.isSeekable())
                        or bool(saved and saved[2] > 0),
            'state': self._state, 'waiting_focus': self._want and self._gated(),
            'error': self._error_text if self._state == 'error' else '',
            'buffer_s': self._buffer_s,
        }

    def _saved_spot(self, item: Dict[str, Any]) -> Optional[Tuple[Any, int, int]]:
        """La posición recordada de `item` si está desconectado en pausa."""
        saved = self._resume_at
        if (saved is None or self._loaded_key is not None
                or saved[0] != (self._cur_list, item['id'], item['source'])):
            return None
        return saved

    @property
    def is_playing(self) -> bool:
        return self._want

    @property
    def state(self) -> str:
        return self._state

    def volume(self) -> float:
        return float(self._lib.data['volume'])

    def shuffle(self) -> bool:
        return bool(self._lib.data['shuffle'])

    def repeat(self) -> str:
        return self._lib.data['repeat']

    def focus_only(self) -> bool:
        return bool(self._lib.data['focus_only'])

    def muted(self) -> bool:
        return self._audio.isMuted()

    # ---------- compatibilidad ----------
    @property
    def index(self) -> int:
        if self._cur_list != LOFI_LIST_ID:
            return -1
        return next((n for n, i in enumerate(self._lofi_items) if i['id'] == self._cur_item), -1)

    def current_track_name(self) -> str:
        item = self._current_item()
        return item['title'] if item else ''

    def rescan(self) -> int:
        return self.rescan_lofi()

    # ================================================================ control
    def play(self, list_id=None, item_id: Optional[str] = None) -> None:
        """Sin argumentos: reanuda lo actual o empieza la lista actual. Con
        list_id (y opcionalmente item_id): empieza ahí. Un int es el índice
        en la carpeta lofi (compatibilidad)."""
        if isinstance(list_id, int) and not isinstance(list_id, bool):
            if not self._lofi_items:
                self.track_changed.emit("")
                return
            list_id, item_id = LOFI_LIST_ID, self._lofi_items[list_id % len(self._lofi_items)]['id']
        if list_id is None and item_id is None:
            if self._current_item() is not None:
                self.resume()
                return
            list_id = self._default_list()
            if list_id is None:
                self.track_changed.emit("")
                self.error_occurred.emit("No hay música en tus listas: añade un enlace, "
                                         "un archivo o una carpeta")
                return
        if list_id is None:
            list_id = self._cur_list
        if not self._items_of(list_id):
            self.error_occurred.emit("Esa lista está vacía")
            return
        if item_id is None or self._lookup(list_id, item_id) is None:
            item_id = self._first_item_id(list_id)
        self._errors_in_row = 0
        same = (list_id == self._cur_list and item_id == self._cur_item
                and self._loaded_key is not None)
        if same and self._want and self._state not in ('error', 'idle'):
            return  # ya está sonando (o cargando) justo eso
        if same and self._state == 'paused':
            self.resume()
            return
        self._play_item(list_id, item_id)

    def resume(self) -> None:
        if self._current_item() is None:
            self.play()
            return
        if self._want and self._state in ('loading', 'buffering', 'playing', 'reconnecting'):
            return
        self._errors_in_row = 0
        self._set_want(True)
        if self._gated():
            self._halt()
            self._set_state('paused')
            return
        self._start_sound()

    def pause(self) -> None:
        """Pausa del usuario: con focus_only también desarma la música hasta
        que vuelva a pulsar reproducir."""
        self._set_want(False)
        if self._current_item() is None:
            return
        self._halt()
        self._set_state('paused')

    def toggle_pause(self) -> None:
        if self._want:
            self.pause()
        else:
            self.resume()

    def stop(self) -> None:
        self._set_want(False)
        self._halt()
        self._unload()
        self._resume_at = None
        self._set_state('idle')
        if self._volume_timer.isActive():
            self._persist()

    def shutdown(self) -> None:
        """Al cerrar la app: para y corta los enlaces que se estén resolviendo
        (sus hilos no retienen el proceso, pero así no siguen en la red)."""
        for deadline in list(self._link_deadlines):
            deadline.abort()
        self._link_deadlines.clear()
        self.stop()
        self._icy.stop()
        self._stream_buffer.shutdown()

    def next(self) -> None:
        nid = self._neighbor(+1, auto=False)
        if nid is not None:
            self._go_to(nid)

    def prev(self) -> None:
        if (self._loaded_key and not self._is_live() and self._want
                and self._player.position() > self.PREV_RESTART_MS):
            self._player.setPosition(0)
            return
        nid = self._neighbor(-1, auto=False)
        if nid is not None:
            self._go_to(nid)

    def _go_to(self, item_id: str) -> None:
        """Siguiente/anterior a mano. Con un solo elemento el vecino es el
        mismo: una radio encendida no se corta (sería reconectarla para nada)
        y una pista cargada vuelve al principio."""
        if item_id == self._cur_item and self._current_item() is not None:
            if self._is_live() and self._want:
                return
            if self._loaded_key is not None and not self._is_live():
                self._player.setPosition(0)
                return
        self._errors_in_row = 0
        self._play_item(self._cur_list, item_id)

    def seek(self, ms: int) -> None:
        if self._loaded_key and not self._is_live() and self._player.isSeekable():
            self._player.setPosition(max(0, int(ms)))
            return
        item = self._current_item()
        saved = self._saved_spot(item) if item is not None else None
        if saved and saved[2] > 0:
            # En pausa y desconectada: se apunta para cuando se recargue
            pos = max(0, min(int(ms), saved[2]))
            self._resume_at = (saved[0], pos, saved[2])
            self.position_changed.emit(pos)
            self.now_playing_changed.emit()

    def set_volume(self, fraction: float) -> None:
        try:
            value = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            return
        self._audio.setVolume(value)
        if value != self._lib.data['volume']:
            self._lib.data['volume'] = value
            self._volume_timer.start(self.VOLUME_SAVE_MS)

    def set_muted(self, muted: bool) -> None:
        self._audio.setMuted(bool(muted))

    def set_shuffle(self, on: bool) -> None:
        on = bool(on)
        if on != self.shuffle():
            self._lib.data['shuffle'] = on
            self._shuffle_order = []
            self._persist()
            self.now_playing_changed.emit()

    def set_repeat(self, mode: str) -> None:
        if mode in REPEAT_MODES and mode != self.repeat():
            self._lib.data['repeat'] = mode
            self._persist()
            self.now_playing_changed.emit()

    def set_focus_only(self, on: bool) -> None:
        on = bool(on)
        if on == self.focus_only():
            return
        self._lib.data['focus_only'] = on
        self._persist()
        if self._want and self._current_item() is not None:
            if self._gated():
                self._halt()
                self._set_state('paused')
            elif self._state == 'paused':
                self._start_sound()
        self.now_playing_changed.emit()

    def on_focus_state(self, focus_running: bool) -> None:
        """El controller avisa en cada arranque/pausa/fin/cambio de modo del
        timer. Con focus_only y la música armada: enfoque en marcha → suena;
        descanso, pausa o parado → en pausa (sigue armada)."""
        running = bool(focus_running)
        if running == self._focus_running:
            return
        self._focus_running = running
        if not self.focus_only() or not self._want or self._current_item() is None:
            return
        if running:
            if not self._active and self._state == 'paused':
                self._start_sound()
        else:
            self._halt()
            self._set_state('paused')
        self.now_playing_changed.emit()

    # ================================================================ biblioteca (escritura)
    def create_list(self, name: str) -> str:
        try:
            lid = self._lib.add_list(name)
        except ValueError as e:
            self.error_occurred.emit(str(e))
            return ''
        self._persist()
        self.library_changed.emit()
        return lid

    def rename_list(self, list_id: str, name: str) -> bool:
        if list_id == LOFI_LIST_ID or not self._lib.rename_list(list_id, name):
            return False
        self._persist()
        self.library_changed.emit()
        return True

    def delete_list(self, list_id: str) -> bool:
        if list_id == LOFI_LIST_ID or self._lib.get_list(list_id) is None:
            return False
        was_current = list_id == self._cur_list
        if was_current:
            self.stop()
        self._lib.delete_list(list_id)
        if was_current:
            self._clear_current()
        self._persist()
        self.library_changed.emit()
        return True

    def add_entries(self, list_id: str, entries: Iterable[Any]) -> int:
        if list_id == LOFI_LIST_ID:
            return 0
        added = self._lib.add_entries(list_id, entries)
        if added:
            self._persist()
            self.library_changed.emit()
            if list_id == self._cur_list:
                self.now_playing_changed.emit()
        return added

    def remove_item(self, list_id: str, item_id: str) -> bool:
        if list_id == LOFI_LIST_ID or self._lib.find_item(list_id, item_id) is None:
            return False
        is_current = list_id == self._cur_list and item_id == self._cur_item
        nxt = self._neighbor(+1, auto=False) if is_current and self._want else None
        self._lib.remove_item(list_id, item_id)
        self._shuffle_order = [i for i in self._shuffle_order if i != item_id]
        if is_current:
            if nxt and nxt != item_id and self._lookup(list_id, nxt) is not None:
                self._play_item(list_id, nxt)
            else:
                self.stop()
                self._cur_item = None
                self._snapshot = None
                self._lib.set_current(list_id, None)
                self.track_changed.emit("")
        self._persist()
        self.library_changed.emit()
        self.now_playing_changed.emit()
        return True

    def move_item(self, list_id: str, item_id: str, new_index: int) -> bool:
        if list_id == LOFI_LIST_ID or not self._lib.move_item(list_id, item_id, new_index):
            return False
        self._persist()
        self.library_changed.emit()
        if list_id == self._cur_list:
            self.now_playing_changed.emit()
        return True

    def rename_item(self, list_id: str, item_id: str, title: str) -> bool:
        if list_id == LOFI_LIST_ID or not self._lib.rename_item(list_id, item_id, title):
            return False
        self._persist()
        self.library_changed.emit()
        if list_id == self._cur_list and item_id == self._cur_item:
            self.track_changed.emit(self.current_track_name())
            self.now_playing_changed.emit()
        return True

    def add_link(self, text: str, list_id: Optional[str] = None) -> None:
        """Resuelve el enlace en segundo plano y emite link_resolved(lista,
        añadidos, error). Sin list_id: una playlist crea su propia lista; un
        audio suelto va a la lista actual o a «Mis enlaces». Va en un hilo
        propio con plazo total (2×LINK_TIMEOUT_S): un servidor lento no
        ocupa los hilos de Asana ni retiene el cierre de la app."""
        from services.workers import run_in_thread
        timeout = self.LINK_TIMEOUT_S
        deadline = Deadline(max(timeout, 1) * 2)
        self._link_deadlines.add(deadline)
        run_in_thread(lambda: resolve_link(text, timeout, deadline=deadline),
                      on_done=lambda res: self._on_link_done(res, list_id, deadline),
                      on_error=lambda _e: self._on_link_done(
                          ResolveResult(error="No se pudo abrir ese enlace"), list_id,
                          deadline))

    def _on_link_done(self, res: ResolveResult, list_id: Optional[str],
                      deadline: Optional[Deadline] = None) -> None:
        self._link_deadlines.discard(deadline)
        try:
            if res.error:
                self.link_resolved.emit(list_id or '', 0, res.error)
                return
            added, target, error = self._add_resolved(res, list_id)
            if added:
                self._persist()
                self.library_changed.emit()
                if target == self._cur_list:
                    self.now_playing_changed.emit()
            self.link_resolved.emit(target or list_id or '', added, error)
        except RuntimeError:
            pass  # el servicio se destruyó mientras se resolvía

    def _add_resolved(self, res: ResolveResult, list_id: Optional[str]
                      ) -> Tuple[int, Optional[str], str]:
        """Añade lo resuelto a su lista (sin guardar). → (añadidos, lista, error)."""
        named = res.list_name if list_id is None else None
        target, created, error = self._target_list(list_id, named)
        if error:
            return 0, None, error
        added = self._lib.add_entries(target, res.entries)
        if added:
            return added, target, ''
        limit = self._lib.limit_hit
        if created:
            self._lib.delete_list(target)
            target = None
        lst = self._lib.get_list(target) if target else None
        if lst is not None and len(lst['items']) >= MAX_ITEMS_PER_LIST:
            return 0, target, f"La lista está llena (máximo {MAX_ITEMS_PER_LIST} pistas)"
        if limit == 'list':
            return 0, target, "La lista está llena: crea otra para añadir más"
        if limit == 'library':
            return 0, target, ("Tu música ya ocupa el máximo: borra alguna lista para "
                               "añadir más")
        return 0, target, "Eso ya estaba en la lista"

    def _target_list(self, list_id: Optional[str], new_name: Optional[str]
                     ) -> Tuple[Optional[str], bool, str]:
        if list_id:
            if list_id == LOFI_LIST_ID:
                return None, False, ("La carpeta lofi es de solo lectura: añade la música "
                                     "a otra lista")
            if self._lib.get_list(list_id) is None:
                return None, False, "Esa lista ya no existe"
            return list_id, False, ''
        try:
            if new_name:
                return self._lib.add_list(new_name), True, ''
            cur = self._cur_list
            if cur and cur != LOFI_LIST_ID and self._lib.get_list(cur) is not None:
                return cur, False, ''
            existing = self._lib.find_list_by_name(DEFAULT_LIST_NAME)
            if existing is not None:
                return existing['id'], False, ''
            return self._lib.add_list(DEFAULT_LIST_NAME), True, ''
        except ValueError as e:
            return None, False, str(e)

    def add_paths(self, paths: Iterable[Any], list_id: Optional[str] = None) -> int:
        """Archivos, carpetas o playlists locales (soltados o elegidos). Sin
        list_id, cada carpeta/playlist crea su lista y los archivos sueltos van
        a la lista actual o a «Mis enlaces». Devuelve cuántas pistas se
        añadieron; si no se añadió ninguna, emite error_occurred."""
        total, first_error = 0, ''
        loose: List[Entry] = []
        for p in paths or []:
            info = classify_link(str(p))
            if info.kind != 'file':
                first_error = first_error or (info.reason or "Eso no es un archivo de tu equipo")
                continue
            res = resolve_link(info.normalized)  # local: sin red
            if res.error:
                first_error = first_error or res.error
                continue
            if res.list_name and list_id is None:
                added, _target, error = self._add_resolved(res, None)
                total += added
                if error and not added:
                    first_error = first_error or error
            else:
                loose.extend(res.entries)
        if loose:
            added, _target, error = self._add_resolved(ResolveResult(entries=loose), list_id)
            total += added
            if error and not added:
                first_error = first_error or error
        if total:
            self._persist()
            self.library_changed.emit()
            self.now_playing_changed.emit()
        elif first_error:
            self.error_occurred.emit(first_error)
        return total

    def rescan_lofi(self) -> int:
        try:
            self._lofi_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        files = scan_audio_folder(self._lofi_dir)
        self._lofi_items = [{
            'id': p.name, 'kind': 'file', 'source': str(p),
            'title': title_from_source(str(p)) or p.name, 'live': False,
            'mime': EXT_MIME.get(p.suffix.lower(), ''), 'bitrate': 0} for p in files]
        self.tracks = files
        if self._cur_list == LOFI_LIST_ID:
            self._shuffle_order = []
        self.library_changed.emit()
        return len(files)

    @property
    def lofi_dir(self) -> Path:
        return self._lofi_dir

    # ================================================================ internos: selección
    def _default_list(self) -> Optional[str]:
        if self._items_of(self._cur_list):
            return self._cur_list
        if self._lofi_items:
            return LOFI_LIST_ID
        return next((x['id'] for x in self._lib.lists if x['items']), None)

    def _first_item_id(self, list_id: str) -> Optional[str]:
        items = self._items_of(list_id)
        if not items:
            return None
        if self.shuffle():
            order = self._new_shuffle([i['id'] for i in items])
            self._shuffle_list, self._shuffle_order = list_id, order
            return order[0]
        return items[0]['id']

    def _new_shuffle(self, ids: List[str], first: Optional[str] = None,
                     avoid: Optional[str] = None) -> List[str]:
        order = list(ids)
        random.shuffle(order)
        if first in order:
            order.remove(first)
            order.insert(0, first)
        elif avoid is not None and len(order) > 1 and order[0] == avoid:
            swap = random.randrange(1, len(order))
            order[0], order[swap] = order[swap], order[0]
        return order

    def _play_order(self, ids: List[str]) -> List[str]:
        if not self.shuffle():
            return ids
        if self._shuffle_list != self._cur_list or set(self._shuffle_order) != set(ids):
            self._shuffle_list = self._cur_list
            self._shuffle_order = self._new_shuffle(ids, first=self._cur_item)
        return self._shuffle_order

    def _neighbor(self, step: int, auto: bool, from_error: bool = False) -> Optional[str]:
        """Item siguiente (+1) o anterior (-1) de la lista actual según
        shuffle/repeat. auto: fin natural de la pista (respeta repeat)."""
        items = self._items_of(self._cur_list)
        if not items:
            return None
        ids = [i['id'] for i in items]
        if auto and not from_error and self.repeat() == 'one' and self._cur_item in ids:
            return self._cur_item
        order = self._play_order(ids)
        if self._cur_item not in order:
            return order[0] if step > 0 else order[-1]
        pos = order.index(self._cur_item) + step
        if 0 <= pos < len(order):
            return order[pos]
        if auto and self.repeat() == 'off':
            return None
        if self.shuffle() and pos >= len(order):
            self._shuffle_order = self._new_shuffle(ids, avoid=self._cur_item)
            return self._shuffle_order[0]
        return order[pos % len(order)]

    def _select(self, list_id: str, item_id: str) -> None:
        changed = (list_id, item_id) != (self._cur_list, self._cur_item)
        self._cur_list, self._cur_item = list_id, item_id
        self._snapshot = self._lookup(list_id, item_id)
        if changed:
            self._played = False
            self._live_runtime = None
            self._icy_confirmed = False
            self._runtime_bitrate = 0
            self._icy_info = {}
            self._attempts = 0
            self._resume_at = None
            self._buffer_level, self._buffer_skip = 0, False   # otra emisora: directo
        self._lib.set_current(list_id, item_id)
        self._save_soon()
        self.track_changed.emit(self.current_track_name())
        self.now_playing_changed.emit()

    def _clear_current(self) -> None:
        self._cur_list = self._cur_item = None
        self._snapshot = None
        self._lib.set_current(None, None)
        self.track_changed.emit("")
        self.now_playing_changed.emit()

    def _play_item(self, list_id: str, item_id: str) -> None:
        self._halt()
        self._unload()
        self._resume_at = None    # empieza desde el principio
        self._select(list_id, item_id)
        self._set_want(True)
        if self._gated():
            self._set_state('paused')
        else:
            self._load_current()

    # ================================================================ internos: reproducción
    def _gated(self) -> bool:
        return self.focus_only() and not self._focus_running

    def _set_want(self, want: bool) -> None:
        if want != self._want:
            self._want = want
            self.playing_changed.emit(want)

    def _set_state(self, state: str) -> None:
        if state != 'error':
            self._error_text = ''
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
            self.now_playing_changed.emit()

    def _is_live(self) -> bool:
        item = self._current_item()
        if item is None or item['kind'] != 'url':
            return False
        if self._live_runtime is not None:
            return self._live_runtime
        return item.get('live') is True

    def _treat_as_live(self) -> bool:
        """¿Un corte de este item se arregla reconectando? Un item de vida
        desconocida que nunca llegó a sonar se trata como pista (se salta)."""
        item = self._current_item()
        if item is None or item['kind'] != 'url':
            return False
        if self._live_runtime is not None:
            return self._live_runtime
        if item.get('live') is not None:
            return bool(item['live'])
        return self._played and self._player.duration() <= 0

    def _stop_timers(self) -> None:
        for timer in (self._reconnect_timer, self._skip_timer, self._stable_timer,
                      self._event_timer):
            timer.stop()
        self._pending_event = None
        self._watchdog.stop()

    def _halt(self) -> None:
        """Deja de sonar sin cambiar la intención del usuario. Una radio se
        desconecta (al volver se reconecta); un archivo se pausa. Una pista
        por enlace también se desconecta, recordando por dónde iba: el
        servidor cortaría una conexión parada (pausa larga, descanso) y al
        reanudar se daría por fallida y saltaría a la siguiente."""
        self._active = False
        self._stop_timers()
        self._icy.stop()
        self._stream_buffer.close()
        if self._loaded_key is None:
            return
        item = self._current_item()
        resumable = self._progressed or self._seek_pending is not None
        if self._is_live():
            self._unload()
        elif item is not None and item['kind'] == 'url' and resumable:
            # Si aún no había saltado tras la recarga anterior, vale ese punto
            pos = (self._seek_pending if self._seek_pending is not None
                   else int(self._player.position()))
            spot = (self._loaded_key, pos,
                    max(0, int(self._player.duration())) or self._seek_duration)
            self._unload()
            self._resume_at = spot
        elif not self._progressed:
            self._unload()
        else:
            self._player.pause()

    def _unload(self) -> None:
        self._active = False
        self._seek_pending = None
        self._player.stop()
        self._player.setSource(QUrl())
        self._loaded_key = None
        self._stream_buffer.close()
        self._buffer_s = 0

    def _start_sound(self) -> None:
        item = self._current_item()
        if item is None:
            return
        key = (self._cur_list, item['id'], item['source'])
        if (self._loaded_key == key and not self._is_live()
                and self._player.playbackState() == _P.PausedState):
            self._active = True
            self._last_progress = time.monotonic()
            self._player.play()
            self._set_state('playing')
            self._watchdog.start()
            return
        self._load_current()

    def _load_current(self, reconnect: bool = False) -> None:
        item = self._current_item()
        if item is None:
            self._set_state('idle')
            return
        self._stop_timers()
        if not reconnect:
            self._attempts = 0
        if item['kind'] == 'file' and not os.path.isfile(item['source']):
            self._unload()
            self._active = True
            self._handle_failure(f"No se encuentra el archivo «{item['title']}»",
                                 "No se encuentra el archivo")
            return
        if (item['kind'] == 'file' and source_ext(item['source']) in HLS_EXTS
                and not local_hls_file_ok(item['source'])):
            # Una lista HLS local llega a FFmpeg tal cual: puede venir de datos
            # importados o haber cambiado desde que se añadió
            self._unload()
            self._active = True
            self._handle_failure(MSG_HLS_UNSAFE)
            return
        url = (QUrl(item['source']) if item['kind'] == 'url'
               else QUrl.fromLocalFile(item['source']))
        url = self._buffered_url(item, reconnect) or url
        self._active = True
        self._progressed = False
        self._last_pos = -1
        self._stalled_at = None
        now = time.monotonic()
        self._last_progress = self._load_started = now
        self._set_state('buffering' if self._buffer_s
                        else 'reconnecting' if reconnect else 'loading')
        self._player.setSource(QUrl())
        self._active = True
        key = (self._cur_list, item['id'], item['source'])
        resume, self._resume_at = self._resume_at, None
        self._seek_pending = (resume[1] if resume and resume[0] == key and resume[1] > 0
                              and resume[2] > 0 else None)
        self._seek_duration = resume[2] if self._seek_pending is not None else 0
        self._loaded_key = key
        self._player.setSource(url)
        if not self._active:
            return  # falló al instante (lo gestiona _dispatch_event)
        self._player.play()
        self._watchdog.start(self.WATCHDOG_MS)
        if self._is_live():
            self._icy.start(item['source'])
        self.now_playing_changed.emit()

    def _mark_playing(self) -> None:
        self._errors_in_row = 0
        if self._state != 'playing':
            self._set_state('playing')
            if self._attempts and not self._stable_timer.isActive():
                self._stable_timer.start(self.STABLE_MS)

    def _on_stable(self) -> None:
        if self._state == 'playing':
            self._attempts = 0

    def _detect_live(self) -> None:
        item = self._current_item()
        if item is None:
            return
        # Duración 0 tras 1,5 s sonando = directo. isSeekable() no sirve: el
        # backend FFmpeg lo da True también en las radios. Un item que ya se
        # sabe pista (live False) sigue siéndolo aunque se sirva sin tamaño
        live = (item['kind'] == 'url' and self._player.duration() <= 0
                and item.get('live') is not False)
        self._live_runtime = live
        if not live and item.get('live') is not False:
            self._update_current_item(live=False)
        elif live and self._icy_confirmed and item.get('live') is not True:
            # Duración 0 a secas no basta para guardarlo: un MP3 servido sin
            # Content-Length también la da (lo confirma _on_icy o el resolver)
            self._update_current_item(live=True)
        if live and self._active and not self._icy.active:
            self._icy.start(item['source'])
        elif not live and self._icy.active:
            self._icy.stop()
        self.now_playing_changed.emit()

    def _live_confirmed(self) -> bool:
        """¿Directo de verdad (cabeceras icy-* o lo dijo el resolver), no solo
        por durar 0?"""
        item = self._current_item()
        return self._icy_confirmed or bool(item and item.get('live') is True)

    def _update_current_item(self, **fields) -> None:
        item = self._lookup(self._cur_list, self._cur_item)
        if item is None:
            return
        if self._cur_list == LOFI_LIST_ID:
            update_item_fields(item, **fields)   # en memoria: la carpeta manda
        elif self._lib.update_item(self._cur_list, self._cur_item, **fields):
            self._save_soon()

    # ---------- señales del reproductor ----------
    def _on_status(self, status) -> None:
        if not self._active:
            return
        if status in (_S.LoadedMedia, _S.BufferedMedia):
            self._apply_pending_seek()
        self._stalled_at = time.monotonic() if status == _S.StalledMedia else None
        if status == _S.EndOfMedia:
            self._queue_event('end', '')
        elif status == _S.InvalidMedia:
            self._queue_event('fail', '')
        elif status == _S.StalledMedia:
            if self._state == 'playing':
                self._set_state('buffering')
        elif status == _S.BufferedMedia:
            if self._player.playbackState() == _P.PlayingState and self._progressed:
                self._mark_playing()

    def _on_error(self, error, _message: str = '') -> None:
        if not self._active or error == QMediaPlayer.Error.NoError:
            return
        self._queue_event('fail', '')

    def _queue_event(self, kind: str, message: str, short: str = '') -> None:
        """Fin o fallo: se atiende fuera del manejador de la señal (reabrir el
        reproductor dentro de su propia señal es frágil). _active=False evita
        contar dos veces el mismo fallo (errorOccurred + InvalidMedia)."""
        self._active = False
        self._watchdog.stop()
        if self._pending_event is None:
            self._pending_event = (kind, message, short)
            self._event_timer.start(0)

    def _dispatch_event(self) -> None:
        event, self._pending_event = self._pending_event, None
        if event is None or not self._want:
            return
        kind, message, short = event
        if kind == 'end':
            self._on_end()
        else:
            self._on_failure(message, short)

    def _handle_failure(self, message: str, short: str = '') -> None:
        """message: el aviso completo; short: sin el título, para «Ahora
        suena», que ya lo muestra (por defecto, message)."""
        self._queue_event('fail', message, short)

    def _on_end(self) -> None:
        if self._treat_as_live():
            # Un directo confirmado (también por el búfer, ya cortado): su fin es otro corte
            if self._live_confirmed():
                self._schedule_reconnect()
                return
            # «Directo» solo por durar 0 y acabó limpio: era una pista servida
            # sin Content-Length (chunked). Fin de pista, y se recuerda
            self._live_runtime = False
            self._update_current_item(live=False)
        nid = self._neighbor(+1, auto=True)
        if nid is None:  # repeat 'off' al final de la lista
            self._unload()
            self._set_want(False)
            self._set_state('idle')
            return
        if nid == self._cur_item:
            self._load_current()
        else:
            self._play_item(self._cur_list, nid)

    def _on_failure(self, message: str = '', short: str = '') -> None:
        item = self._current_item()
        if item is None:
            self._give_up("No se pudo reproducir")
            return
        if self._treat_as_live():
            self._schedule_reconnect()
            return
        self._unload()
        self._errors_in_row += 1
        count = len(self._items_of(self._cur_list))
        reason = message or f"No se pudo reproducir «{item['title']}»"
        short = short or message or "No se pudo reproducir"
        if self._errors_in_row >= max(1, count):
            if count <= 1:
                self._give_up(reason, short)
            else:
                self._give_up("No se pudo reproducir ninguna pista de la lista")
            return
        self._error_text = f"{short}: paso a la siguiente"
        self._set_state('error')
        self.now_playing_changed.emit()
        self.error_occurred.emit(f"{reason}: paso a la siguiente")
        self._skip_timer.start(self.ERROR_SKIP_MS)

    def _skip_after_error(self) -> None:
        if not self._want:
            return
        nid = self._neighbor(+1, auto=True, from_error=True)
        if nid is None:
            self._give_up("No quedan más pistas que reproducir")
            return
        self._play_item(self._cur_list, nid)

    def _give_up(self, message: str, short: str = '') -> None:
        self._stop_timers()
        self._icy.stop()
        self._unload()
        self._set_want(False)
        self._error_text = short or message
        self._set_state('error')
        self.now_playing_changed.emit()
        self.error_occurred.emit(message)

    def _schedule_reconnect(self) -> None:
        self._bump_buffer()
        self._stop_timers()
        self._unload()
        delays = tuple(self.RECONNECT_DELAYS_MS)
        if self._attempts >= len(delays):
            self._give_up("Se perdió la conexión con la emisora y no se pudo recuperar")
            return
        delay = delays[self._attempts]
        self._attempts += 1
        self._set_state('reconnecting')
        self._reconnect_timer.start(max(0, int(delay)))

    def _do_reconnect(self) -> None:
        if self._want and not self._gated() and self._current_item() is not None:
            self._load_current(reconnect=True)

    def _check_watchdog(self) -> None:
        if not self._active:
            self._watchdog.stop()
            return
        now = time.monotonic()
        if not self._progressed:
            # La precarga del búfer también cuenta como carga
            if now - self._load_started > self.LOAD_TIMEOUT_MS / 1000 + self._buffer_s:
                self._handle_failure("La emisora o el archivo no responde")
            return
        limit = self.STALL_MS if self._treat_as_live() else self.LOAD_TIMEOUT_MS
        if self._stalled_at is not None and self._treat_as_live():
            limit = min(limit, self.LIVE_STALLED_MS)   # atascado: no esperar los 15 s
        if (self._player.playbackState() == _P.PlayingState
                and now - self._last_progress > limit / 1000):
            self._handle_failure("La reproducción se quedó atascada")

    def _on_position(self, pos: int) -> None:
        # Recargando tras una pausa: la barra se queda donde iba hasta el salto
        self.position_changed.emit(pos if self._seek_pending is None else self._seek_pending)
        if not self._active:
            return
        if self._seek_pending is not None and pos >= 1500:
            self._seek_pending = None   # suena sin poder saltar: sigue desde ahí
        if pos > 0 and pos != self._last_pos:
            self._last_progress = time.monotonic()
            self._stalled_at = None
            self._progressed = True
            self._played = True
            if self._player.playbackState() == _P.PlayingState:
                self._mark_playing()
            if self._live_runtime is None and pos >= 1500:
                self._detect_live()
        self._last_pos = pos

    def _on_media_info(self, *_args) -> None:
        if self._loaded_key is not None:
            self._apply_pending_seek()
            self.now_playing_changed.emit()

    def _apply_pending_seek(self) -> None:
        """Pista por enlace recargada tras una pausa: vuelve a donde iba en
        cuanto se conoce su duración (sin soporte de rangos en el servidor,
        FFmpeg no salta y sigue desde el principio)."""
        pos = self._seek_pending
        # Antes de LoadedMedia el backend FFmpeg ignora el salto
        if (pos is None or self._loaded_key is None or self._player.duration() <= 0
                or not self._player.isSeekable()
                or self._player.mediaStatus() not in (_S.LoadedMedia, _S.BufferingMedia,
                                                      _S.BufferedMedia)):
            return   # se reintenta con LoadedMedia / seekableChanged
        self._seek_pending = None
        self._player.setPosition(min(pos, max(0, self._player.duration() - 1000)))

    def _on_metadata(self) -> None:
        bps = _safe_meta(self._player, QMediaMetaData.AudioBitRate)
        if isinstance(bps, int) and not isinstance(bps, bool) and bps > 0:
            kbps = int(round(bps / 1000))
            if kbps != self._runtime_bitrate:
                self._runtime_bitrate = kbps
                self._update_current_item(bitrate=kbps)
                self.now_playing_changed.emit()

    def _on_icy(self, info: Dict[str, Any]) -> None:
        if not any(info.get(k) for k in ('title', 'name', 'bitrate')):
            return  # consulta fallida: se conserva lo último que se supo
        self._icy_info = dict(info)
        # Cabeceras o metadatos ICY: es una radio de verdad, y se guarda
        self._icy_confirmed = True
        item = self._current_item()
        if item is not None and self._live_runtime and item.get('live') is not True:
            self._update_current_item(live=True)
        if item is not None and not item.get('mime') and info.get('mime'):
            self._update_current_item(mime=info['mime'])   # para mostrar el códec
        self.now_playing_changed.emit()

    # ---------- búfer adaptativo de los directos ----------
    def _init_buffer(self) -> None:
        self._buffer_level = 0        # escalón de BUFFER_STEPS_S (0 = conexión directa)
        self._buffer_s = 0            # precarga de la conexión en curso (0 = directa)
        self._buffer_skip = False     # el intento por el búfer falló: el siguiente, directo
        self._stalled_at: Optional[float] = None   # StalledMedia sin avanzar desde
        self._stream_buffer = StreamBuffer(self)
        self._stream_buffer.bypassed.connect(self._on_buffer_bypassed)
        self._net_timeout_default = self._player.playbackOptions().networkTimeout()

    def _buffer_eligible(self, item: Optional[Dict[str, Any]]) -> bool:
        """¿Directo por enlace que se puede almacenar? HLS no: FFmpeg ya guarda
        segmentos enteros y un proxy de bytes no reescribe la lista. Solo un
        directo confirmado: durar 0 no basta (una pista servida sin
        Content-Length también lo da) y el proxy la repetiría sin fin."""
        return (item is not None and item['kind'] == 'url' and self._treat_as_live()
                and self._live_confirmed()
                and source_ext(item['source']) not in HLS_EXTS)

    def _bump_buffer(self) -> None:
        """Un corte de un directo que ya sonaba sube un escalón (hasta el tope).
        Los reintentos que no llegan a sonar no suben; si el que falla iba por
        el búfer, el siguiente va directo (por si el proxy es el problema)."""
        if self._progressed and self._buffer_eligible(self._current_item()):
            self._buffer_level = min(self._buffer_level + 1, len(self.BUFFER_STEPS_S))
        self._buffer_skip = bool(self._buffer_s and not self._progressed)

    def _buffered_url(self, item: Dict[str, Any], reconnect: bool) -> Optional[QUrl]:
        """La URL del proxy local si toca búfer (solo al reconectar tras un
        corte); None = conexión directa. Fija self._buffer_s."""
        self._buffer_s = 0
        url = None
        skip, self._buffer_skip = self._buffer_skip, False
        if reconnect and not skip and self._buffer_level > 0 and self._buffer_eligible(item):
            steps = self.BUFFER_STEPS_S
            seconds = steps[min(self._buffer_level, len(steps)) - 1]
            local = self._stream_buffer.open(item['source'], seconds)
            if local is not None:   # si el proxy no abre, se conecta directo
                self._buffer_s = seconds
                url = QUrl(local)
        else:
            self._stream_buffer.close()
        # El proxy retiene las cabeceras hasta tener la precarga: que FFmpeg
        # no dé la conexión por muerta mientras tanto
        extra = (self._buffer_s + PREFILL_GRACE_S) * 1000 if self._buffer_s else 0
        timeout = int(self._net_timeout_default + extra)
        opts = self._player.playbackOptions()
        if opts.networkTimeout() != timeout:
            opts.setNetworkTimeout(timeout)
            self._player.setPlaybackOptions(opts)
        return url

    def _on_buffer_bypassed(self, sid: int) -> None:
        """El origen no se dejó almacenar (HLS, error…): el reproductor sigue
        la redirección y conecta directo."""
        if sid != self._stream_buffer.session_id or not self._buffer_s:
            return
        self._buffer_s = 0
        # El watchdog ya no cuenta la precarga: la conexión directa empieza
        # ahora y tiene su LOAD_TIMEOUT entero
        self._load_started = self._last_progress = time.monotonic()
        if self._state == 'buffering':
            self._set_state('reconnecting')
        self.now_playing_changed.emit()
