"""Estado y lógica del temporizador Pomodoro (sin UI).

Extraído de pomogrowth_v8_ultra/app_ultra.py (extract_from_v8.py, una sola vez).
A partir de aquí este archivo es fuente normal de v9.
"""
import time
from collections import defaultdict
from typing import Dict, Any, List, Optional

# El bucle de la UI late cada 250 ms. Un hueco mayor entre latidos significa
# que el proceso no corrió (equipo suspendido o hibernado): ese tiempo no es
# trabajo. Holgura amplia para no confundirlo con un parón puntual de la UI.
SUSPEND_GAP_S = 60.0

# Tipos de temporizador: el Pomodoro cuenta hacia atrás con descansos; el
# Libre es un cronómetro sin tope (enfocarse el tiempo que se quiera)
TIMER_KINDS = ('pomodoro', 'free')


class PomodoroModel:
    def __init__(self):
        self.is_running = False; self.is_break_time = False
        # Modo Libre: 'free' cuenta hacia arriba sin tope; free_elapsed acumula
        # los tramos ya cerrados de la sesión (sobrevive a las pausas). Sustituye
        # al vestigio is_pomodoro_mode de v8 (siempre True)
        self.timer_kind = 'pomodoro'; self.free_elapsed = 0.0
        self.pomodoro_duration = 25*60; self.break_duration = 5*60; self.long_break_duration = 15*60; self.pomodoros_per_cycle = 4; self.pomodoro_cycle_count = 0
        self.remaining_time = self.pomodoro_duration; self.start_timestamp = 0.0
        # Último instante en que consta que el proceso corría (start o latido)
        self.last_alive = 0.0
        # Instante en que terminó el último tramo pausado (para fecharlo)
        self.last_segment_end = 0.0
        self.tasks: List[Dict[str, Any]] = []; self.active_task_id: Optional[int] = None
        self.task_times: Dict[int, float] = defaultdict(float); self.task_break_times: Dict[int, float] = defaultdict(float)
        self.last_pomodoro_task_id: Optional[int] = None
    def set_durations(self, p_min, b_min, lb_min, p_cycle): self.pomodoro_duration=p_min*60; self.break_duration=b_min*60; self.long_break_duration=lb_min*60; self.pomodoros_per_cycle=p_cycle
    def start(self, now: Optional[float] = None):
        if not self.is_running:
            self.is_running = True
            self.start_timestamp = self.last_alive = time.time() if now is None else now
    def heartbeat(self, now: Optional[float] = None) -> float:
        """Latido del bucle de la UI. Devuelve los segundos sin latir si
        superan SUSPEND_GAP_S (el equipo estuvo suspendido); 0.0 si todo va
        normal. Ante un hueco NO avanza last_alive: pause() cortará ahí."""
        if not self.is_running:
            return 0.0
        now = time.time() if now is None else now
        gap = now - self.last_alive
        if gap > SUSPEND_GAP_S:
            return gap
        if gap < 0:
            # Reloj atrasado (a mano o por sincronización): desplazar el tramo
            # para no perder el tiempo ya acreditado por los latidos
            self.start_timestamp += gap
        self.last_alive = now
        return 0.0
    def _segment_elapsed(self, now: Optional[float] = None) -> float:
        """Segundos del tramo en curso. Solo cuenta hasta el último latido si
        desde entonces pasó más de SUSPEND_GAP_S: el tiempo con el equipo
        suspendido no se acredita, aunque nadie haya llamado a heartbeat()."""
        if not self.is_running:
            return 0.0
        now = time.time() if now is None else now
        # Hueco largo (suspensión) o reloj atrasado aún sin latir: lo
        # acreditado llega hasta el último latido
        if now - self.last_alive > SUSPEND_GAP_S or now < self.last_alive:
            end = self.last_alive
        else:
            end = now
        return max(0.0, end - self.start_timestamp)
    def pause(self, now: Optional[float] = None) -> float:
        if not self.is_running: return 0.0
        elapsed = self._segment_elapsed(now)
        if self.is_free:
            # Sin tope: el cronómetro libre no tiene remaining_time que respetar
            self.free_elapsed += elapsed
        else:
            # Un tramo nunca supera lo que quedaba: al llegar a 0 la sesión se
            # completa, así que lo que exceda es tiempo que no existió
            elapsed = min(elapsed, max(0.0, self.remaining_time))
            self.remaining_time -= elapsed
        self.is_running = False
        # Fin real del tramo: con una suspensión de por medio no es «ahora»
        self.last_segment_end = self.start_timestamp + elapsed
        return elapsed
    def reset(self, keep_cycle: bool = False):
        self.is_running=False; self.is_break_time = False; self.remaining_time = self.pomodoro_duration
        self.free_elapsed = 0.0
        if not keep_cycle: self.pomodoro_cycle_count = 0
    def is_session_in_progress(self) -> bool:
        """True si el pomodoro actual ya inició su conteo (corriendo o pausado
        a mitad). En descanso devuelve False: ahí la tarea puede cambiarse."""
        if self.is_break_time:
            return False
        if self.is_free:
            return self.is_running or self.free_elapsed > 0
        return self.is_running or self.remaining_time < self.pomodoro_duration
    def get_display_time(self, now: Optional[float] = None) -> float:
        """Pomodoro: lo que queda. Libre: lo acumulado, hacia arriba."""
        if self.is_free:
            return self.free_elapsed + self._segment_elapsed(now)
        return max(0, self.remaining_time - self._segment_elapsed(now))
    def get_current_runtime(self, now: Optional[float] = None) -> float: return self._segment_elapsed(now)

    # ---- Modo Libre ----
    @property
    def is_free(self) -> bool:
        return self.timer_kind == 'free'
    def set_kind(self, kind: str) -> bool:
        """Cambia entre 'pomodoro' y 'free'. Solo con el timer parado, fuera
        del descanso y sin sesión a medias (no se pierde ni se mezcla tiempo)."""
        if kind not in TIMER_KINDS:
            return False
        if kind == self.timer_kind:
            return True
        if self.is_running or self.is_break_time or self.is_session_in_progress():
            return False
        self.timer_kind = kind
        self.free_elapsed = 0.0
        self.remaining_time = self.pomodoro_duration
        return True
    def cut_free_segment(self, at: float) -> float:
        """Modo Libre: cierra el tramo en curso en `at` sin parar el
        cronómetro (p. ej. a medianoche, para fechar cada parte en su día).
        Devuelve los segundos del tramo cerrado."""
        if not (self.is_running and self.is_free) or at <= self.start_timestamp:
            return 0.0
        elapsed = at - self.start_timestamp
        self.free_elapsed += elapsed
        self.start_timestamp = at
        self.last_segment_end = at
        return elapsed
    def stop_free(self) -> float:
        """Modo Libre: fin de la sesión (con el cronómetro ya en pausa).
        Devuelve el total acumulado y deja el contador a cero."""
        total = self.free_elapsed
        self.free_elapsed = 0.0
        return total
