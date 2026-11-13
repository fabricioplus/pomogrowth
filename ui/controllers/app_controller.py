"""Controlador central de v9: conecta PomodoroModel/DataHandler/Rewards con la UI Qt.

Equivalente Qt del AppController de v8 (fase 1: timer + tareas). El bucle de
250 ms es un QTimer; las actualizaciones de tarjetas son in-place vía el
registro del TaskListPanel (misma filosofía que la reforma de fluidez de v8).
"""
import queue
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer

from core.config import Config
from core.pomodoro_model import PomodoroModel
from core.rewards import RewardsSystem
from services import sound_service
from services.asana_service import AsanaService
from ui.dialogs import branded_dialogs as dialogs
from ui.theme.tokens import tokens
from ui.widgets import toast
from ui.widgets.task_list import TaskListPanel
from ui.widgets.timer_panel import TimerPanel
from ui.widgets.xp_bar import XpBar

# El panel de tareas se acopla a la izquierda o a la derecha del timer
# (arriba/abajo se quitaron en v9.2.2: no quedaban bien)
PANEL_POSITIONS = ('left', 'right')


def normalize_panel_position(pos) -> str:
    """Posición guardada → 'left'/'right'. Las de versiones anteriores se
    leen como su equivalente: arriba (tareas primero) = izquierda, abajo =
    derecha; cualquier otra cosa, izquierda."""
    pos = {'top': 'left', 'bottom': 'right'}.get(pos, pos)
    return pos if pos in PANEL_POSITIONS else 'left'


class AppController(QObject):
    def __init__(self, window, data_handler):
        super().__init__(window)
        self.window = window
        self.data_handler = data_handler
        self.model = PomodoroModel()
        self.rewards = RewardsSystem()
        self.task_filter = 'pending'
        self.task_sort_mode = 'manual'
        self._session_total = self.model.pomodoro_duration
        self._today_cache = (None, 0.0)  # (clave, segundos base)

        settings = self.data_handler.load_settings()
        self.model.set_durations(
            settings.get('pomodoro_min', 25), settings.get('break_min', 5),
            settings.get('long_break_min', 15), settings.get('pomodoros_per_cycle', 4))
        self.model.reset()
        self._session_total = self.model.pomodoro_duration
        # Sonidos del temporizador: estilo, volumen y on/off de Ajustes
        sound_service.configure_from_settings(settings)
        # Al ingresar, la vista arranca SIEMPRE en Pendientes ordenadas por
        # Fecha (pedido explícito); los cambios en sesión siguen funcionando.
        self.task_sort_mode = 'due'
        # Tareas fijas completadas ayer vuelven a Pendientes
        try:
            self.data_handler.reopen_recurring_tasks()
        except Exception as e:
            print(f"No se pudieron reabrir tareas fijas: {e}")
        self._last_seen_date = date.today()
        self._mark_active_day()  # racha de uso: hoy se abrió la app
        # Con un plan ya hecho hoy (p. ej. tras reabrir la app), arrancar en «Hoy»
        if self.data_handler.get_day_plan():
            self.task_filter = 'today'

        # --- construir la UI dentro de la ventana ---
        self.xp_bar = XpBar()
        window.title_bar.set_xp_widget(self.xp_bar)

        self.tasks_panel = TaskListPanel()
        self.timer_panel = TimerPanel()
        window.splitter.addWidget(self.tasks_panel)
        window.splitter.addWidget(self.timer_panel)
        # Al redimensionar la ventana solo crece el panel de tareas: el timer
        # mantiene su ancho (stretch 0). El usuario aún puede mover el divisor.
        window.splitter.setStretchFactor(0, 1)
        window.splitter.setStretchFactor(1, 0)
        window.splitter.setSizes([440, 640])

        # --- señales ---
        tp = self.timer_panel
        tp.toggle_requested.connect(self.toggle_timer)
        tp.reset_requested.connect(self.reset_timer)
        tp.skip_requested.connect(self.skip_break)
        tp.complete_task_requested.connect(self.toggle_active_task_completion)
        tp.panel_toggle_requested.connect(self.toggle_task_panel)

        lp = self.tasks_panel
        lp.filter_changed.connect(self.set_task_filter)
        lp.sort_changed.connect(self.set_task_sort)
        lp.task_selected.connect(self.select_task)
        lp.task_toggle.connect(self.toggle_task_completion)
        lp.task_edit.connect(self.edit_task)
        lp.task_note.connect(self.note_task)
        lp.task_delete.connect(self.delete_task)
        lp.task_move_up.connect(lambda tid: self.move_task(tid, -1))
        lp.task_move_down.connect(lambda tid: self.move_task(tid, +1))
        lp.order_changed.connect(self.reorder_visible_tasks)
        lp.task_added.connect(self.add_task)
        lp.position_cycle_requested.connect(self.cycle_panel_position)
        lp.plan_requested.connect(self.open_day_plan)
        # Filtros avanzados (orden): recordar si el usuario los dejó abiertos
        lp.set_advanced_open(bool(settings.get('advanced_filters_open', False)),
                             animate=False)
        lp.advanced_toggled.connect(self._on_advanced_filters_toggled)

        # --- Asana (fase 2) — controles de sync junto a "● Asana conectado" ---
        tp.sync_requested.connect(self.sync_asana)
        self.asana = AsanaService(self.data_handler, self)
        self.asana.status.connect(tp.set_sync_status)
        self.asana.connection_state.connect(self._on_connection_state)
        self.asana.finished.connect(self._on_sync_finished)
        self.asana.error.connect(lambda msg: dialogs.info(self.window, "Asana", msg))
        # Cola de cambios hacia Asana: solo se envía con el sync permitido
        self.asana.push_allowed = self._asana_sync_allowed
        self.asana.pushed.connect(self._on_asana_pushed)
        # Un sync diferido (esperaba a un envío) también marca «sincronizando»
        self.asana.sync_started.connect(
            lambda: self.sync_enabled and self.timer_panel.set_sync_state('syncing'))
        tp.sync_btn.setEnabled(True)
        self.sync_enabled = bool(settings.get('sync_enabled', True))
        tp.sync_toggle.setChecked(self.sync_enabled)
        tp.set_sync_state('idle' if self.sync_enabled else 'off')
        # Conectar DESPUÉS del estado inicial: setChecked emite toggled
        tp.sync_toggled.connect(self._on_sync_toggled)

        # --- Settings (fase 2) ---
        window.title_bar.settings_btn.setEnabled(True)
        window.title_bar.settings_btn.setToolTip("Configuración")
        window.title_bar.settings_requested.connect(self.open_settings)

        # --- Estadísticas (fase 3) ---
        window.title_bar.stats_btn.setEnabled(True)
        window.title_bar.stats_btn.setToolTip("Estadísticas")
        window.title_bar.stats_requested.connect(self.open_stats)

        # --- Música personalizada: botón de la cabecera, mini reproductor del
        # panel del timer y avisos de error (el diálogo muestra los suyos) ---
        from services.music_service import MusicService
        self.music = MusicService(self, data_handler=self.data_handler)
        self._music_dialog = None
        window.title_bar.music_btn.setEnabled(True)
        window.title_bar.music_btn.setToolTip("Música: radios, enlaces y archivos")
        window.title_bar.music_requested.connect(self.open_music)
        mini = tp.mini_player
        mini.toggle_requested.connect(self.music.toggle_pause)
        mini.next_requested.connect(self.music.next)
        mini.open_requested.connect(self.open_music)
        self.music.now_playing_changed.connect(self._refresh_mini_player)
        self.music.playing_changed.connect(self._refresh_mini_player)
        self.music.error_occurred.connect(self._on_music_error)

        # --- Sonidos de ambiente: su mezclador vive en el diálogo de Música y
        # suenan a la vez que ella; el punto del botón Música dice si suenan.
        # Al arrancar no suena nada (la última mezcla espera a «Reanudar») ---
        from services.ambient_service import AmbientService
        self.ambient = AmbientService(self, data_handler=self.data_handler)
        self.ambient.state_changed.connect(self._refresh_ambient_indicator)
        self.ambient.changed.connect(self._refresh_ambient_indicator)
        self.ambient.error_occurred.connect(self._on_music_error)
        self._refresh_ambient_indicator()

        # --- Recompensas (fase 4) ---
        window.title_bar.rewards_btn.setEnabled(True)
        window.title_bar.rewards_btn.setToolTip("Recompensas")
        window.title_bar.rewards_requested.connect(self.open_rewards)

        # --- Fase 5: enfoque, tray, compacto, cierre ---
        self._is_closing = False
        self._welcome_shown = False
        from core.focus_mode import FocusMode
        self.focus_mode = FocusMode()
        self._focus_expire_warned = ''
        self._cleanup_stale_focus_block()
        window.title_bar.focus_btn.setEnabled(True)
        window.title_bar.focus_btn.setToolTip("Modo enfoque (bloqueo de webs)")
        window.title_bar.focus_requested.connect(self.open_focus)

        self.compact_window = None
        window.title_bar.compact_requested.connect(self.show_compact)

        self._setup_tray()
        # La X, Alt+F4 y «Cerrar» del tray salen de la app, siempre preguntando
        # antes (hasta la 9.3.1 la X ocultaba en la bandeja)
        self._quit_dialog = None
        self._quit_asking = False
        self._onboarding_dialog = None  # la presentación, mientras está abierta
        window.title_bar.close_requested.disconnect()
        window.title_bar.close_requested.connect(self.confirm_quit)
        window.close_guard = self._guard_window_close

        # --- Webhook entrante (según Ajustes: solo este equipo, red local con
        # clave o apagado) + posición del panel ---
        self.webhook_queue = queue.Queue()
        self.webhook_server = None
        self._webhook_config = ('off', '')  # (modo, clave) del servidor en marcha
        self._start_webhook_server()
        self._webhook_timer = QTimer(self)
        self._webhook_timer.setInterval(500)
        self._webhook_timer.timeout.connect(self._process_webhook_queue)
        self._webhook_timer.start()
        Config.N8N_WEBHOOK_URL = settings.get('n8n_webhook_url', '')

        self.panel_position = normalize_panel_position(
            settings.get('task_panel_position', 'left'))
        self._apply_panel_position()
        self._width_before_collapse = settings.get('window_width_expanded')
        if settings.get('task_panel_collapsed'):
            self.tasks_panel.hide()  # el ancho guardado ya era el plegado
        self.timer_panel.set_panel_toggle(self.panel_position,
                                          bool(settings.get('task_panel_collapsed')))
        # El auto-sync al arrancar lo orquesta launch() (splash previo a la UI)

        # --- Mascota pixel (comprable en la tienda; consejos con IA) ---
        self.timer_panel.pet.advice_requested.connect(self.request_pet_advice)
        self.refresh_pet()

        # --- Crear también en Asana/ClickUp desde el alta (botones del logo) ---
        from ui.controllers.task_destinations import TaskDestinations
        self.destinations = TaskDestinations(self)

        # --- estado inicial ---
        self.tasks_panel.add_panel.set_plan_hint(self.task_filter == 'today')
        # Ctrl+N: escribir una tarea nueva desde cualquier sitio de la ventana
        from PySide6.QtGui import QKeySequence, QShortcut
        self._new_task_shortcut = QShortcut(QKeySequence("Ctrl+N"), window)
        self._new_task_shortcut.activated.connect(self.tasks_panel.add_panel.focus_input)
        self.refresh_task_list()
        self.update_rewards_bar()
        self.timer_panel.set_cycle(self.model.pomodoro_cycle_count,
                                   self.model.pomodoros_per_cycle)
        # Modo Libre: selector «Pomodoro | Libre» y el último modo elegido
        tp.kind_requested.connect(self.set_timer_kind)
        self.set_timer_kind(settings.get('timer_kind', 'pomodoro'), save=False)
        self._update_timer_display()
        self._update_total_today()

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(250)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

    # ================= Timer =================
    def _tick(self) -> None:
        # Cambio de día con la app abierta: reabrir tareas fijas completadas
        today = date.today()
        if today != self._last_seen_date:
            self._last_seen_date = today
            self._mark_active_day(today)
            if self.task_filter == 'today':
                # El plan de ayer ya no vale: volver a Pendientes
                self.task_filter = 'pending'
                self.refresh_task_list()
            try:
                if self.data_handler.reopen_recurring_tasks():
                    self.refresh_task_list()
                    self._refresh_counts()
            except Exception as e:
                print(f"No se pudieron reabrir tareas fijas: {e}")
        # Los comodines del modo enfoque valen solo para el día en curso. Se
        # mira en cada tick (es barato): si el hosts no se dejó reescribir,
        # el núcleo lo reintenta cada minuto
        try:
            self.focus_mode.expire_session_allowances(today)
        except Exception as e:
            print(f"No se pudieron retirar los comodines del enfoque: {e}")
        self._warn_focus_expire()
        if not self.model.is_running:
            return
        # Antes que nada: tras suspender el equipo, el reloj de pared salta y
        # el pomodoro se daría por completado con el tiempo de la suspensión
        gap = self.model.heartbeat()
        if gap:
            self._pause_after_suspend(gap)
            return
        if self.model.is_free:
            self._split_free_at_midnight()
        self._update_timer_display()
        self._update_total_today()
        self._sync_compact()
        # El modo Libre no tiene fin: solo el Pomodoro se completa al llegar a 0
        if self.model.get_display_time() <= 0 and not self.model.is_free:
            self.complete_pomodoro_session()

    def _pause_after_suspend(self, gap: float) -> None:
        """El proceso estuvo `gap` s sin correr con el timer en marcha (equipo
        suspendido o hibernado): pausar en el último latido, de modo que ese
        tiempo no cuente como trabajo ni complete el pomodoro."""
        was_break = self.model.is_break_time
        elapsed = self.model.pause()  # corta en el último latido
        # Fechado en el corte, no al despertar: una suspensión nocturna no
        # debe pasar ese trabajo al día siguiente
        self._log_and_refresh(elapsed, "break" if was_break else "pomodoro",
                              ended_at=self.model.last_segment_end)
        self.timer_panel.set_running(False)
        self._update_timer_display()
        self._sync_compact()
        self._sync_music_focus()
        minutes = max(1, round(gap / 60))
        what = ("el descanso" if was_break
                else "la sesión libre" if self.model.is_free else "el pomodoro")
        msg = (f"El equipo estuvo suspendido ~{minutes} min: pausé {what} "
               "y ese tiempo no se contó.")
        # Notificación de Windows siempre: al despertar suele estar delante la
        # pantalla de bloqueo y un toast de 6 s pasaría sin verse; la
        # notificación queda en el centro de notificaciones
        self.tray.showMessage("PomoGrowth — timer en pausa", msg,
                              self.tray.icon(), 6000)
        main_shown = self.window.isVisible() and not self.window.isMinimized()
        if main_shown or (self.compact_window and self.compact_window.isVisible()):
            toast.show_warning(self._feedback_window(), msg, duration=6000)

    def _sync_music_focus(self) -> None:
        """La música «solo durante el enfoque» sigue al timer: suena con un
        pomodoro en marcha y se pausa en descansos, pausas o con el timer parado."""
        self.music.on_focus_state(self.model.is_running and not self.model.is_break_time)
        # El ambiente tiene su propio «solo durante el enfoque»
        self.ambient.on_focus_state(self.model.is_running and not self.model.is_break_time)

    @staticmethod
    def _clock_text(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _timer_fraction(self, shown: float) -> Optional[float]:
        """Progreso del anillo: lo consumido de la sesión; en el modo Libre
        (sin total), una vuelta por hora."""
        if self.model.is_free:
            return (shown % 3600) / 3600
        if self._session_total > 0:
            return 1.0 - shown / self._session_total
        return None

    def _ring_animates(self, shown: float) -> bool:
        # Al cerrar cada vuelta del modo Libre el anillo salta a 0 sin animar
        # el retroceso
        return self.model.is_running and not (self.model.is_free and shown % 3600 < 1)

    def _update_timer_display(self) -> None:
        shown = self.model.get_display_time()
        self.timer_panel.set_time_text(self._clock_text(shown))
        fraction = self._timer_fraction(shown)
        if fraction is not None:
            animate = self._ring_animates(shown)
            self.window.progress_bar.set_fraction(fraction, animate=animate)
            self.timer_panel.set_progress(fraction, animate=animate)

    def toggle_timer(self) -> None:
        if not self.model.active_task_id and not self.model.is_break_time:
            dialogs.info(self._feedback_window(), "Sin tarea activa",
                         "Selecciona una tarea de la lista para empezar a trabajar.")
            return
        active = self._task_by_id(self.model.active_task_id)
        if active and active.get('completed') and not self.model.is_break_time:
            if dialogs.confirm(self.window, "Tarea completada",
                               f"«{active.get('name', '')}» ya está completada.\n"
                               "¿Quieres reabrirla para seguir trabajando?",
                               ok_text="Reabrir"):
                self.toggle_task_completion(active['id'])
            return
        if self.model.is_running:
            elapsed = self.model.pause()
            # is_break_time decide el tipo (el is_pomodoro_mode de v8, siempre
            # True, registraba descansos como trabajo; ya no existe)
            self._log_and_refresh(elapsed, "break" if self.model.is_break_time else "pomodoro",
                                  ended_at=self.model.last_segment_end)
            sound_service.play("stop")
        else:
            self.model.start()
            sound_service.play("start")
        self.timer_panel.set_running(self.model.is_running)
        self._sync_compact()
        self._sync_music_focus()
        self._sync_kind_selector()

    def reset_timer(self, keep_cycle: bool = False) -> None:
        if self.model.is_free:
            # Modo Libre: el botón de reinicio es «Terminar sesión» (también al
            # completar o borrar la tarea activa con la sesión a medias)
            self.finish_free_session()
            return
        if self.model.is_running:
            elapsed = self.model.pause()
            self._log_and_refresh(elapsed, "break" if self.model.is_break_time else "pomodoro",
                                  ended_at=self.model.last_segment_end)
        self.model.reset(keep_cycle)
        self._session_total = self.model.pomodoro_duration
        self.timer_panel.set_running(False)
        self.timer_panel.set_mode(False)
        self.window.progress_bar.set_break_mode(False)
        self.timer_panel.set_cycle(self.model.pomodoro_cycle_count,
                                   self.model.pomodoros_per_cycle)
        self._update_timer_display()
        self._sync_compact()
        self._sync_music_focus()
        self._sync_kind_selector()

    def skip_break(self) -> None:
        if not self.model.is_break_time:
            return
        if self.model.is_running:
            # Registrar el descanso parcial: saltarlo no debe borrarlo de stats
            elapsed = self.model.pause()
            self._log_and_refresh(elapsed, "break", ended_at=self.model.last_segment_end)
        self.model.is_break_time = False
        self.model.remaining_time = self.model.pomodoro_duration
        self.model.is_running = False
        self._session_total = self.model.pomodoro_duration
        self.timer_panel.set_running(False)
        self.timer_panel.set_mode(False)
        self.window.progress_bar.set_break_mode(False)
        self._update_timer_display()
        self._sync_compact()
        self._sync_music_focus()
        self._sync_kind_selector()

    def complete_pomodoro_session(self) -> None:
        """Fin de pomodoro o descanso. Portado de v8 (no bloqueante)."""
        # Solo el tramo final va al chunk: las pausas intermedias ya
        # registraron el suyo en toggle_timer (antes se sumaba la duración
        # completa encima y el tiempo enfocado quedaba inflado).
        final_segment = self.model.pause()
        segment_end = self.model.last_segment_end
        if not self.model.is_break_time:
            # ---- POMODORO COMPLETADO ----
            self.model.pomodoro_cycle_count += 1
            self.model.last_pomodoro_task_id = self.model.active_task_id
            duration = self.model.pomodoro_duration
            is_long = self.model.pomodoro_cycle_count >= self.model.pomodoros_per_cycle
            if is_long:
                self.model.remaining_time = self.model.long_break_duration
                self.model.pomodoro_cycle_count = 0
            else:
                self.model.remaining_time = self.model.break_duration
            self.model.is_break_time = True
            self._session_total = self.model.remaining_time

            self.timer_panel.set_running(False)
            self.timer_panel.set_mode(True)
            self.window.progress_bar.set_break_mode(True)
            self.timer_panel.set_cycle(self.model.pomodoro_cycle_count,
                                       self.model.pomodoros_per_cycle)
            self._update_timer_display()
            # La compacta también muestra el descanso y sus botones al instante
            self._sync_compact()
            self._sync_music_focus()
            self._sync_kind_selector()

            QTimer.singleShot(50, lambda: self._log_and_refresh(
                final_segment, "pomodoro", cycle_completed=is_long, ended_at=segment_end))
            QTimer.singleShot(100, lambda: sound_service.play("complete"))

            reward_info = None
            cycle_info = None
            try:
                reward_info = self.rewards.add_pomodoro(int(duration / 60))
                if is_long:
                    cycle_info = self.rewards.add_cycle()
                self.update_rewards_bar()
            except Exception as e:
                print(f"Error otorgando recompensas: {e}")

            task = self._task_by_id(self.model.last_pomodoro_task_id)
            # Con fecha y hora: si sale tarde (sin red) no se confunde con
            # el día del envío, y cada comentario es único (la comprobación
            # anti-duplicados compara el texto exacto)
            ended = datetime.now()
            self._queue_asana_session_comment(
                task, f"🍅 Sesión Pomodoro completada el {ended:%d/%m} a las "
                      f"{ended:%H:%M}. Tiempo enfocado: {int(duration / 60)} minutos.")
            if task:
                self.send_data_to_n8n({
                    "event": "pomodoro_completed",
                    "task_name": task.get('name'),
                    "task_id": task.get('id'),
                    "asana_id": task.get('asana_id'),
                    "duration_seconds": duration,
                    "timestamp": datetime.now().isoformat(),
                })

            def notify():
                win = self._feedback_window()
                # El nivel puede subir por el pomodoro O por el bonus del ciclo
                level_info = None
                if reward_info and reward_info.get('level_up'):
                    level_info = reward_info
                elif cycle_info and cycle_info.get('level_up'):
                    level_info = cycle_info
                if level_info:
                    toast.show_success(
                        win,
                        f"🎉 ¡NIVEL {level_info.get('current_level', 1)}!  "
                        f"+{level_info.get('coins_gained', 0)} 🪙  "
                        f"+{level_info.get('xp_gained', 0)} XP", duration=4000)
                elif is_long:
                    bonus = (f" (+{cycle_info.get('xp_gained', 0)} XP, "
                             f"+{cycle_info.get('coins_gained', 0)} 🪙)" if cycle_info else "")
                    toast.show_success(win,
                                       f"¡Excelente! Ciclo completado{bonus} — descanso largo",
                                       duration=3500)
                else:
                    toast.show_success(win,
                                       "¡Buen trabajo! Pomodoro completado — descanso corto",
                                       duration=3500)
                self._pet_react('cycle' if is_long else 'pomodoro',
                                level_info or reward_info)
            QTimer.singleShot(400, notify)
        else:
            # ---- DESCANSO COMPLETADO ----
            self.model.is_break_time = False
            self.model.remaining_time = self.model.pomodoro_duration
            self._session_total = self.model.pomodoro_duration
            self.timer_panel.set_running(False)
            self.timer_panel.set_mode(False)
            self.window.progress_bar.set_break_mode(False)
            self._update_timer_display()
            self._sync_compact()
            self._sync_music_focus()
            self._sync_kind_selector()
            QTimer.singleShot(50, lambda: self._log_and_refresh(final_segment, "break",
                                                                ended_at=segment_end))
            QTimer.singleShot(100, lambda: sound_service.play("break"))
            QTimer.singleShot(400, lambda: toast.show_info(
                self._feedback_window(),
                "Descanso terminado — ¡a por el siguiente Pomodoro!", duration=3000))

    def _feedback_window(self):
        """Ventana visible donde anclar toasts/diálogos: en modo compacto los
        avisos deben salir junto a la mini ventana, no sobre la principal oculta."""
        if (not self.window.isVisible() and self.compact_window
                and self.compact_window.isVisible()):
            return self.compact_window
        return self.window

    def _log_and_refresh(self, elapsed: float, chunk_type: str,
                         cycle_completed: bool = False,
                         ended_at: Optional[float] = None) -> None:
        task_id = (self.model.last_pomodoro_task_id if chunk_type == 'break'
                   else self.model.active_task_id)
        project_name = None
        try:
            task = self._task_by_id(task_id)
            if task and task.get('source') == 'asana':
                projects = task.get('projects') or []
                if projects and isinstance(projects, list):
                    project_name = (projects[0] or {}).get('name')
        except Exception:
            project_name = None
        if elapsed > 1 and task_id is not None:
            # Modo Libre: tiempo enfocado (type 'pomodoro') marcado mode 'free'
            mode = 'free' if chunk_type == 'pomodoro' and self.model.is_free else None
            self.data_handler.log_time_chunk(elapsed, task_id, chunk_type, project_name,
                                             cycle_completed=cycle_completed,
                                             ended_at=ended_at, mode=mode)
            self._today_cache = (None, 0.0)
            if chunk_type == 'pomodoro':
                total = sum(c.get('duration', 0) for c in self.data_handler.get_time_chunks()
                            if c.get('task_id') == task_id
                            and c.get('type', 'pomodoro') == 'pomodoro')
                self.tasks_panel.update_card_time(task_id, total)
            self._update_total_today()

    def _update_total_today(self) -> None:
        chunks = self.data_handler.get_time_chunks()
        key = (len(chunks), date.today())
        if self._today_cache[0] != key:
            base = 0.0
            today = date.today()
            for c in chunks:
                if c.get('type', 'pomodoro') != 'pomodoro' or 'timestamp' not in c:
                    continue
                try:
                    if datetime.fromisoformat(c['timestamp']).date() == today:
                        base += c.get('duration', 0)
                except ValueError:
                    continue
            self._today_cache = (key, base)
        total = self._today_cache[1]
        # El tramo en curso, de un pomodoro o del modo Libre (los anteriores
        # de la sesión ya están en los chunks)
        if self.model.is_running and not self.model.is_break_time:
            total += self.model.get_current_runtime()
        m, s = divmod(int(total), 60)
        h, m = divmod(m, 60)
        self.timer_panel.set_total_today(f"{h:02d}:{m:02d}:{s:02d}")

    # ================= Modo Libre =================
    def set_timer_kind(self, kind: str, save: bool = True) -> None:
        """Selector «Pomodoro | Libre». Solo con el timer parado, fuera del
        descanso y sin sesión a medias (si no, el modelo lo rechaza y el
        selector vuelve al modo vigente). El último modo se recuerda."""
        changed = self.model.set_kind(kind)
        self.timer_panel.set_kind(self.model.timer_kind)
        self._sync_kind_selector()
        if not changed:
            return
        self._session_total = self.model.pomodoro_duration
        self._update_timer_display()
        self._sync_compact()
        settings = self.data_handler.load_settings()
        if save and settings.get('timer_kind') != self.model.timer_kind:
            settings['timer_kind'] = self.model.timer_kind
            self.data_handler.save_settings(settings)

    def _sync_kind_selector(self) -> None:
        """Con una sesión a medias o en descanso el modo no se cambia."""
        self.timer_panel.set_kind_enabled(not self.model.is_break_time
                                          and not self.model.is_session_in_progress())

    def _split_free_at_midnight(self) -> None:
        """Modo Libre: una sesión que cruza la medianoche se parte en el
        cambio de día, para que cada parte cuente en su día (un tramo se fecha
        por su final). Se llama tras el latido: con una suspensión de por medio
        ya se pausó en el último latido y aquí no se llega."""
        today = date.fromtimestamp(time.time())
        while True:
            start_day = date.fromtimestamp(self.model.start_timestamp)
            if start_day >= today:
                return
            midnight = datetime.combine(start_day + timedelta(days=1),
                                        datetime.min.time()).timestamp()
            elapsed = self.model.cut_free_segment(midnight)
            if not elapsed:
                return
            # Fechado un instante antes de medianoche: cuenta en el día que acaba
            self._log_and_refresh(elapsed, "pomodoro", ended_at=midnight - 0.001)

    def _queue_asana_session_comment(self, task: Optional[Dict[str, Any]],
                                     comment: str) -> None:
        """Comentario de fin de sesión (pomodoro o Libre) en una tarea de
        Asana. Solo con el sync permitido (switch + integración + conexión):
        sin red sí se encola; con la integración apagada no se acumulan.
        Con el alta en Asana pendiente, espera detrás de ella (gid
        provisional 'local-<id>', que el enlace cambia por el real)."""
        if not task or task.get('asana_deleted') or not self._asana_sync_allowed():
            return
        if task.get('source') == 'asana' and task.get('asana_id'):
            gid = task['asana_id']
        elif self.data_handler.has_pending_asana_create(task.get('id')):
            gid = f"local-{task['id']}"
        else:
            return
        # A la cola persistente: sin red no se pierde, se reintenta
        self.data_handler.queue_asana_comment(gid, comment)
        self.asana.push_pending()

    def finish_free_session(self, silent: bool = False) -> None:
        """«Terminar sesión» del modo Libre: registra el último tramo y deja
        el contador a cero. Desde 5 min da 1 XP por minuto y 1 moneda cada
        5 min, suma a la racha, comenta en Asana y avisa con toast, sonido y
        mascota; con menos solo queda el tiempo registrado. Sin descansos ni
        ciclos. `silent`: al salir de la app, sin avisos."""
        if not self.model.is_free:
            return
        if self.model.is_running:
            elapsed = self.model.pause()
            self._log_and_refresh(elapsed, "pomodoro", ended_at=self.model.last_segment_end)
        total = self.model.stop_free()
        if not silent:
            self.timer_panel.set_running(False)
            self._update_timer_display()
            self._sync_compact()
            self._sync_music_focus()
            self._sync_kind_selector()
        if total < 1:
            return
        minutes = int(total // 60)
        threshold = self.rewards.FREE_SESSION_MIN_MINUTES
        if minutes < threshold:
            if not silent:
                toast.show_info(self._feedback_window(),
                                f"Sesión libre de {self._clock_text(total)} registrada. "
                                f"Desde {threshold} min suma XP y monedas.", duration=3200)
            return
        reward_info = None
        try:
            reward_info = self.rewards.add_free_session(minutes)
            self.update_rewards_bar()
        except Exception as e:
            print(f"Error otorgando recompensas: {e}")
        task = self._task_by_id(self.model.active_task_id)
        ended = datetime.now()
        self._queue_asana_session_comment(
            task, f"⏱️ Sesión libre: {minutes} min (terminada el {ended:%d/%m} "
                  f"a las {ended:%H:%M}).")
        if task:
            self.send_data_to_n8n({
                "event": "free_session_completed",
                "task_name": task.get('name'),
                "task_id": task.get('id'),
                "asana_id": task.get('asana_id'),
                "duration_seconds": int(total),
                "timestamp": ended.isoformat(),
            })
        if silent:
            return
        QTimer.singleShot(100, lambda: sound_service.play("complete"))

        def notify():
            info = reward_info or {}
            if info.get('level_up'):
                text = (f"🎉 ¡NIVEL {info.get('current_level', 1)}!  "
                        f"+{info.get('coins_gained', 0)} 🪙  +{info.get('xp_gained', 0)} XP")
            else:
                text = (f"¡Buen trabajo! Sesión libre de {minutes} min — "
                        f"+{info.get('xp_gained', 0)} XP, +{info.get('coins_gained', 0)} 🪙")
            toast.show_success(self._feedback_window(), text, duration=3500)
            self._pet_react('free_session', reward_info)
        QTimer.singleShot(400, notify)

    # ================= Asana / sync =================
    def _on_sync_toggled(self, enabled: bool) -> None:
        """Switch junto al título TAREAS: enciende/apaga la sincronización."""
        self.sync_enabled = bool(enabled)
        settings = self.data_handler.load_settings()
        settings['sync_enabled'] = self.sync_enabled
        self.data_handler.save_settings(settings)
        if self.sync_enabled:
            self.timer_panel.set_sync_state('idle')
            toast.show_info(self.window, "Sincronización activada", duration=1800)
            if self.asana.is_configured():
                self.sync_asana()
        else:
            # Cancelar cualquier sync en vuelo: apagar el switch no debe
            # terminar en un toast de éxito con la lista recién rellenada
            self.asana.cancel()
            self.timer_panel.set_sync_state('off')
            toast.show_info(self.window, "Sincronización desactivada", duration=1800)

    def sync_asana(self) -> None:
        if not self.sync_enabled:
            toast.show_warning(self.window,
                               "La sincronización está apagada — enciende el switch "
                               "junto a «Asana conectado»", duration=2500)
            return
        # Mismo criterio que el auto-sync de launch(): la integración debe
        # estar activada en ⚙ Configuración → Asana
        settings = self.data_handler.load_settings()
        if not self._asana_integration_active(settings):
            toast.show_warning(self.window,
                               "La integración con Asana está desactivada en "
                               "⚙ Configuración → Asana", duration=2500)
            return
        if not self.asana.is_configured():
            dialogs.info(self.window, "Asana",
                         "Configura primero la conexión con Asana en ⚙ Configuración\n"
                         "(API Token, Assignee GID y workspace/proyectos).")
            return
        self.timer_panel.set_sync_state('syncing')
        self.asana.sync()

    def _on_sync_finished(self, count: int) -> None:
        self.timer_panel.set_sync_state('online' if self.sync_enabled else 'off')
        self._notify_legacy_resynced()
        # Autolimpiar la línea de estado pasados unos segundos (con el panel
        # como contexto: si se destruye antes, Qt cancela la llamada)
        QTimer.singleShot(5000, self.timer_panel,
                          lambda: (not self.asana.in_progress)
                          and self.timer_panel.set_sync_status(""))
        if count > 0:
            # Si la tarea activa quedó completada por el sync (p. ej. cerrada
            # en Asana) y no hay sesión en curso, soltar la selección.
            active = self._task_by_id(self.model.active_task_id)
            if (active and active.get('completed')
                    and not self.model.is_session_in_progress()):
                old = self.model.active_task_id
                self.model.active_task_id = None
                self.tasks_panel.set_active_card(old, None)
                self.timer_panel.set_active_task(None)
            # Mantener el filtro/orden actual (por defecto: Pendientes + Fecha)
            self.refresh_task_list()
            # En el arranque con splash la principal aún no es visible: el
            # toast saldría flotando solo y pisaría a la bienvenida
            if self.window.isVisible():
                toast.show_success(self.window, f"Asana sincronizado: {count} tareas",
                                   duration=2500)

    def _notify_legacy_resynced(self) -> None:
        """Primer sync tras actualizar: avisa de las tareas que tenían la marca
        heredada y cuyo estado cambió para coincidir con Asana (p. ej. un envío
        de v9.2.1 que falló sin que nadie lo viera)."""
        names = self.data_handler.legacy_resynced
        if not names:
            return
        self.data_handler.legacy_resynced = []
        first = names[0][:50]
        more = f" y {len(names) - 1} más" if len(names) > 1 else ""
        # Notificación de Windows: también se ve con el splash del arranque
        self.tray.showMessage(
            "PomoGrowth — estado tomado de Asana",
            f"«{first}»{more} cambió de estado al sincronizar: en Asana estaba "
            "distinta. Revísala si la habías completado o reabierto en la app.",
            self.tray.icon(), 8000)

    @staticmethod
    def _asana_integration_active(settings: Dict[str, Any]) -> bool:
        """Casilla «Integración con Asana activada». Sin la clave (datos
        viejos) cuenta como activa; una lista vacía es que se desmarcó (antes
        `[] or ['asana']` la daba por activa)."""
        active = settings.get('active_integrations')
        return 'asana' in (['asana'] if active is None else active)

    def _asana_sync_allowed(self) -> bool:
        """Mismo criterio que sync_asana, sin avisos: switch encendido,
        integración activa y conexión configurada."""
        if not self.sync_enabled:
            return False
        settings = self.data_handler.load_settings()
        return self._asana_integration_active(settings) and self.asana.is_configured()

    def _on_asana_pushed(self, sent: int, pending: int, dropped: int = 0) -> None:
        """Resultado de un envío de la cola hacia Asana."""
        if self._is_closing:
            return
        if (sent and self.sync_enabled and not self.asana.busy
                and not self.asana.sync_pending and not self.asana.retry_scheduled):
            self.timer_panel.set_sync_state('online')
        visible = self.window.isVisible() or (self.compact_window
                                              and self.compact_window.isVisible())
        # Altas rechazadas: aviso propio con su nombre (no son «tarea borrada»)
        rejected = list(self.data_handler.asana_create_rejections)
        self.data_handler.asana_create_rejections.clear()
        others = dropped - len(rejected)
        if rejected and visible:
            names = ", ".join(f"{name or 'Sin nombre'} ({detail})" if detail
                              else (name or 'Sin nombre') for name, detail in rejected)
            toast.show_warning(self._feedback_window(),
                               f"No se pudo crear en Asana: {names}", duration=4500)
        if others > 0 and visible:
            # Toast además de la línea de estado: dentro de un sync, el estado
            # final del sync pisaría el aviso y el cambio se revertiría sin más
            toast.show_warning(self._feedback_window(),
                               f"Asana rechazó {others} cambio(s) de la cola "
                               "(sin permiso o tarea borrada); se descartó",
                               duration=4500)
        if not pending:
            # Con cambios aún en cola, el aviso «⏳ … se reintentará» se queda
            # (también si un fallo posterior lo puso antes de este borrado)
            QTimer.singleShot(5000, self.timer_panel,
                              lambda: (not self.asana.busy)
                              and not self.data_handler.has_pending_asana_ops()
                              and self.timer_panel.set_sync_status(""))

    def _on_connection_state(self, online: bool) -> None:
        t = tokens()
        if online:
            self.timer_panel.connection_label.setText("● Asana conectado")
            self.timer_panel.connection_label.setStyleSheet(f"color: {t.accent};")
        else:
            self.timer_panel.connection_label.setText("● Sin conexión con Asana")
            self.timer_panel.connection_label.setStyleSheet(f"color: {t.danger};")
            self.timer_panel.set_sync_state('offline' if self.sync_enabled else 'off')

    # ================= Settings =================
    def open_settings(self, tab: str = '') -> None:
        """`tab`: texto de la pestaña con la que abrir (p. ej. "ClickUp")."""
        from ui.dialogs.settings_dialog import TABS, SettingsDialog
        dlg = SettingsDialog(self.window, self)
        labels = [label for _icon, label in TABS]
        if tab in labels:
            dlg.tabs.setCurrentIndex(labels.index(tab))
        dlg.exec()

    def open_stats(self) -> None:
        from ui.dialogs.stats_dialog import StatsDialog  # import perezoso (matplotlib)
        StatsDialog(self.window, self).exec()

    def open_rewards(self) -> None:
        from ui.dialogs.rewards_dialog import RewardsDialog
        dlg = RewardsDialog(self.window, self)
        dlg.theme_equipped.connect(self.apply_theme)
        dlg.exec()

    # ================= Música =================
    def open_music(self) -> None:
        """Diálogo de Música (una sola instancia: si ya está abierto, al frente)."""
        if self._music_dialog is not None:
            self._music_dialog.raise_()
            self._music_dialog.activateWindow()
            return
        from ui.dialogs.music_dialog import MusicDialog
        dlg = MusicDialog(self.window, self.music, ambient=self.ambient)
        self._music_dialog = dlg
        try:
            dlg.exec()
        finally:
            self._music_dialog = None
            dlg.deleteLater()

    def _refresh_mini_player(self, *_args) -> None:
        self.timer_panel.mini_player.show_now_playing(self.music.current(),
                                                     self.music.is_playing)

    def _on_music_error(self, message: str) -> None:
        """Errores de la música con el diálogo cerrado (p. ej. una radio que no
        vuelve): un aviso breve si hay una ventana a la vista."""
        if self._music_dialog is not None or self._is_closing:
            return
        win = self._feedback_window()
        if win.isVisible() and not win.isMinimized():
            toast.show_warning(win, message, duration=4000)

    def _refresh_ambient_indicator(self, *_args) -> None:
        """El punto del botón Música (y su tooltip): ambiente sonando o
        armado a la espera del enfoque."""
        from core.ambient_catalog import get as ambient_sound
        names = ", ".join(ambient_sound(s).name for s in self.ambient.active_ids())
        self.window.title_bar.set_ambient_state(self.ambient.state(), names)

    def _music_available(self) -> bool:
        return (self.music.is_playing or self.music.current() is not None
                or any(x['count'] for x in self.music.lists()))

    def _sync_tray_music(self) -> None:
        action = self._music_action
        action.setText("Pausar la música" if self.music.is_playing else "Reproducir la música")
        action.setEnabled(self._music_available())

    def _toggle_music_from_tray(self) -> None:
        if self._music_available():
            self.music.toggle_pause()

    def apply_theme(self, theme_id: str) -> None:
        """Re-tematiza la app en caliente (QSS global + widgets con estilos propios)."""
        from PySide6.QtWidgets import QApplication
        from ui.theme import fonts as theme_fonts
        from ui.theme.qss import build_qss
        from ui.theme.tokens import ThemeManager
        manager = ThemeManager.instance()
        if not manager.set_theme(theme_id):
            return
        QApplication.instance().setStyleSheet(
            build_qss(manager.current, theme_fonts.ui_family()))
        # Las tarjetas cachean colores: reconstruir la lista con el tema nuevo
        self.refresh_task_list()
        self.tasks_panel.set_active_card(None, self.model.active_task_id)

    def apply_settings(self, s: Dict[str, Any]) -> None:
        """Aplica en caliente lo guardado desde el diálogo de configuración."""
        # Decidir ANTES de mutar duraciones: solo re-sembrar el contador si el
        # timer está ocioso en un pomodoro fresco — guardar configuración con
        # una sesión pausada no debe borrar el tiempo restante ni el candado.
        # En el modo Libre no hay cuenta atrás que re-sembrar
        was_idle_fresh = (not self.model.is_free
                          and not self.model.is_running
                          and not self.model.is_break_time
                          and self.model.remaining_time == self.model.pomodoro_duration)
        self.model.set_durations(s.get('pomodoro_min', 25), s.get('break_min', 5),
                                 s.get('long_break_min', 15),
                                 s.get('pomodoros_per_cycle', 4))
        if was_idle_fresh:
            self.model.remaining_time = self.model.pomodoro_duration
            self._session_total = self.model.remaining_time
            self._update_timer_display()
        self.timer_panel.set_cycle(self.model.pomodoro_cycle_count,
                                   self.model.pomodoros_per_cycle)
        # Ventana
        win = self.window
        wants_top = bool(s.get('always_on_top', True))
        if bool(win.windowFlags() & Qt.WindowStaysOnTopHint) != wants_top:
            visible = win.isVisible()
            win.setWindowFlag(Qt.WindowStaysOnTopHint, wants_top)
            if visible:
                win.show()  # re-aplicar flags requiere re-mostrar
        win.set_base_opacity(s.get('window_opacity', 1.0))
        Config.N8N_WEBHOOK_URL = s.get('n8n_webhook_url', '')
        # Sonidos del temporizador (sección «Sonidos» de Ajustes)
        sound_service.configure_from_settings(s)
        toast.show_success(self.window, "Configuración guardada", duration=1800)
        self._restart_webhook_server_if_changed(s)  # «Recibir tareas»
        if self._asana_sync_allowed():
            # Con la conexión recién configurada, enviar lo que esperaba en cola
            self.asana.push_pending()
        else:
            # Integración desactivada o sin configurar: nada de syncs pendientes
            # ni el icono atascado en «sincronizando»
            self.asana.cancel()
            self.timer_panel.set_sync_state('idle' if self.sync_enabled else 'off')
        self.destinations.refresh()  # botones de Asana/ClickUp del alta

    # ================= Webhook / n8n =================
    @staticmethod
    def _webhook_settings(s: Dict[str, Any]) -> tuple:
        """(modo, clave) de «Recibir tareas», saneados."""
        from services.webhook_server import normalize_key, normalize_mode
        return normalize_mode(s.get('webhook_mode')), normalize_key(s.get('webhook_key'))

    def _start_webhook_server(self, s: Optional[Dict[str, Any]] = None) -> bool:
        """Arranca el servidor de webhooks según Ajustes: 'local' (127.0.0.1),
        'lan' (0.0.0.0 con clave) u 'off' (ninguno). False si el puerto no se
        pudo abrir (p. ej. lo usa otro programa)."""
        from services.webhook_server import WebhookServer, new_key
        s = self.data_handler.load_settings() if s is None else s
        mode, key = self._webhook_settings(s)
        if mode == 'lan' and not key:
            # 'lan' sin clave (un JSON editado a mano): se crea y se guarda
            key = s['webhook_key'] = new_key()
            self.data_handler.save_settings(s)
        self._webhook_config = (mode, key)
        self.webhook_server = None
        if mode == 'off':
            return True
        try:
            server = WebhookServer(self.webhook_queue, Config.WEBHOOK_PORT, mode=mode, key=key)
            server.start()
        except OSError as e:
            print(f"Webhook server no disponible (puerto {Config.WEBHOOK_PORT}): {e}")
            return False
        self.webhook_server = server
        return True

    def _stop_webhook_server(self) -> None:
        server, self.webhook_server = self.webhook_server, None
        if server is not None:
            try:
                server.stop()
            except OSError as e:
                print(f"Al parar el servidor de webhooks: {e}")

    def _restart_webhook_server_if_changed(self, s: Dict[str, Any]) -> None:
        """Al guardar Ajustes: si cambió el modo o la clave, para el servidor
        y lo vuelve a arrancar con lo nuevo (en el mismo puerto)."""
        if self._webhook_settings(s) == self._webhook_config:
            return
        self._stop_webhook_server()
        if not self._start_webhook_server(s):
            toast.show_warning(self.window, f"No se pudo abrir el puerto {Config.WEBHOOK_PORT} "
                                            "para recibir tareas: quizá lo usa otro programa",
                               duration=4000)

    def _process_webhook_queue(self) -> None:
        """Tareas recibidas por webhook (con el sync activado): SIEMPRE locales,
        con solo su nombre y sus etiquetas (services/webhook_server.task_fields).
        Lo demás que traiga el JSON (source, asana_id…) se descarta: un envío
        no puede enlazar una tarea a Asana, donde la app escribiría con tu
        token. Un elemento raro se salta y se siguen procesando los demás."""
        if not self.sync_enabled:
            return
        from services.webhook_server import task_fields
        added = 0
        while True:
            try:
                data = self.webhook_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fields = task_fields(data)
                if fields is None:
                    continue
                name, tags = fields
                self.data_handler.add_task(name=name, tags=tags, source='local')
                added += 1
            except Exception as e:
                print(f"Tarea del webhook descartada: {e}")
        if added:
            self.refresh_task_list()
            toast.show_info(self.window, "Tarea recibida por webhook" if added == 1
                            else f"{added} tareas recibidas por webhook", duration=2000)

    def send_data_to_n8n(self, payload: Dict[str, Any]) -> None:
        url = Config.N8N_WEBHOOK_URL
        if not url:
            return

        def _post():
            try:
                import requests
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code != 200:
                    print(f"Error al enviar datos a n8n: {r.status_code}")
            except Exception as e:
                print(f"Error de conexión al enviar datos a n8n: {e}")
        threading.Thread(target=_post, daemon=True).start()

    # ================= Posición del panel =================
    def cycle_panel_position(self) -> None:
        if not self.tasks_panel.isVisibleTo(self.window):
            self.toggle_task_panel()  # reposicionar algo invisible confunde
        idx = PANEL_POSITIONS.index(self.panel_position)
        self.panel_position = PANEL_POSITIONS[(idx + 1) % len(PANEL_POSITIONS)]
        self._apply_panel_position()
        settings = self.data_handler.load_settings()
        settings['task_panel_position'] = self.panel_position
        self.data_handler.save_settings(settings)

    def _apply_panel_position(self, window_width: Optional[int] = None) -> None:
        """Coloca el panel de tareas a su lado y deja el timer en TIMER_WIDTH.
        `window_width`: el ancho que va a tener la ventana (tras un
        setGeometry, width() aún puede dar el anterior)."""
        from ui.windows.main_window import RESIZE_MARGIN, TASKS_MIN_WIDTH, TIMER_WIDTH
        pos = self.panel_position
        splitter = self.window.splitter
        tasks_first = pos == 'left'
        if (splitter.indexOf(self.tasks_panel) == 0) != tasks_first:
            splitter.insertWidget(0 if tasks_first else 1, self.tasks_panel)
        # Reafirmar tras reordenar: el espacio extra siempre va al panel de
        # tareas; el timer conserva su tamaño al redimensionar la ventana
        splitter.setStretchFactor(splitter.indexOf(self.tasks_panel), 1)
        splitter.setStretchFactor(splitter.indexOf(self.timer_panel), 0)
        # Antes se repartía 440:640 en proporción (el timer, ~59 % de la
        # ventana): su ancho cambiaba al plegar y desplegar las tareas.
        # Mínimos explícitos: el de las tareas por su contenido (con el orden
        # sin compactar, ~540 px) quitaba sitio al timer al estrechar
        self.timer_panel.setMinimumWidth(TIMER_WIDTH)
        self.tasks_panel.setMinimumWidth(TASKS_MIN_WIDTH)
        width = window_width or self.window.width()
        tasks = max(1, width - 2 * RESIZE_MARGIN - splitter.handleWidth() - TIMER_WIDTH)
        splitter.setSizes([tasks, TIMER_WIDTH] if tasks_first else [TIMER_WIDTH, tasks])
        self.tasks_panel.position_btn.set_icon_name(f"panel-{pos}")
        self.timer_panel.set_panel_toggle(pos, not self.tasks_panel.isVisibleTo(self.window))

    def toggle_task_panel(self) -> None:
        """Oculta o muestra el panel de tareas (el «❮» de v8). Plegada, la
        ventana encaja justo en el timer (siempre el mismo ancho, el mínimo);
        al desplegar crece por el lado de las tareas con el timer quieto en su
        sitio y sin ensancharlo. El estado se recuerda entre sesiones."""
        from ui.windows.main_window import MIN_WIDTH, MIN_WIDTH_COLLAPSED
        win = self.window
        geo = win.geometry()
        screen = win.screen()
        area = screen.availableGeometry() if screen is not None else None
        collapse = self.tasks_panel.isVisibleTo(win)
        if collapse:
            self._width_before_collapse = geo.width()
            self.tasks_panel.hide()
            win.setMinimumWidth(MIN_WIDTH_COLLAPSED)
            if not win.isMaximized():
                new_w = MIN_WIDTH_COLLAPSED
                # Con el panel a la izquierda, el timer no se mueve
                x = geo.x() + (geo.width() - new_w if self.panel_position == 'left' else 0)
                win.setGeometry(x, geo.y(), new_w, geo.height())
        else:
            self.tasks_panel.show()
            win.setMinimumWidth(MIN_WIDTH)
            new_w = None
            if not win.isMaximized():
                new_w = max(MIN_WIDTH, int(self._width_before_collapse or 1100), geo.width())
                grow = new_w - geo.width()
                x = geo.x() - (grow if self.panel_position == 'left' else 0)
                if area is not None:  # que no se salga de la pantalla
                    x = max(area.left(), min(x, area.right() - new_w + 1))
                win.setGeometry(x, geo.y(), new_w, geo.height())
            self._apply_panel_position(new_w)
        self.timer_panel.set_panel_toggle(self.panel_position, collapse)
        settings = self.data_handler.load_settings()
        settings['task_panel_collapsed'] = collapse
        if self._width_before_collapse:
            settings['window_width_expanded'] = int(self._width_before_collapse)
        self.data_handler.save_settings(settings)
        self._save_window_geometry()

    # ================= Fase 5: arranque =================
    def launch(self) -> None:
        """Arranque: con auto-sync configurado, splash ANTES de renderizar la
        principal; sin él, ventana directa + bienvenida."""
        settings = self.data_handler.load_settings()
        wants_auto = (settings.get('asana_auto_sync_on_start')
                      and settings.get('sync_enabled', True)
                      and self._asana_integration_active(settings)
                      and self.asana.is_configured())
        if not wants_auto:
            self.window.show()
            QTimer.singleShot(700, self._show_welcome)
            # Cambios que quedaron en cola de la sesión anterior (sin red al
            # cerrar): sin auto-sync, enviarlos igualmente al arrancar
            QTimer.singleShot(3000, self.asana.push_pending)
            return
        from ui.windows.splash import SplashWindow
        self._splash = SplashWindow()
        self._splash.show()
        self.asana.progress.connect(self._splash.set_progress)
        self.asana.finished.connect(self._end_splash)
        self.asana.error.connect(lambda _msg: self._end_splash(-1))
        # Failsafe: si el sync se cuelga, mostrar la app igualmente a los 45 s
        QTimer.singleShot(45000, self._end_splash)
        QTimer.singleShot(250, self.asana.sync)

    def _end_splash(self, _count: int = 0) -> None:
        splash = getattr(self, '_splash', None)
        if splash is None:
            return
        self._splash = None
        try:
            self.asana.progress.disconnect(splash.set_progress)
        except (RuntimeError, TypeError):
            pass
        splash.close()
        splash.deleteLater()
        if not self.window.isVisible():
            self.window.show()
        QTimer.singleShot(400, self._show_welcome)

    def _welcome_lines(self) -> List[str]:
        tasks = [t for t in self.data_handler.get_tasks() if not t.get('completed')]
        today = date.today()
        overdue = due_today = 0
        from core.day_plan import due_local_date
        for t in tasks:
            d = due_local_date(t)  # fecha LOCAL (Asana manda UTC)
            if d is None:
                continue
            if d < today:
                overdue += 1
            elif d == today:
                due_today += 1
        name = self._user_name()
        lines = [f"{name}, tienes {len(tasks)} tareas pendientes." if name
                 else f"Tienes {len(tasks)} tareas pendientes."]
        if due_today:
            lines.append(f"{due_today} vence{'n' if due_today > 1 else ''} hoy.")
        if overdue:
            lines.append(f"{overdue} está{'n' if overdue > 1 else ''} vencida"
                         f"{'s' if overdue > 1 else ''}.")
        lines.append("Elige una tarea y arranca tu primer pomodoro.")
        return lines

    def _show_welcome(self) -> None:
        if self._welcome_shown or self._is_closing:
            return
        self._welcome_shown = True
        self._ask_user_name_if_needed()  # antes del plan o la bienvenida
        if self._is_closing:
            return
        # Presentación del primer arranque: tras el nombre (así saluda con
        # él) y antes del plan o la bienvenida
        self._show_onboarding_if_needed()
        if self._is_closing:
            return
        # Cada arranque, mientras hoy no haya plan: planificar el día en vez
        # de la bienvenida (su resumen ya cuenta pendientes y vencidas). Se
        # llega aquí tras el sync del arranque, con las fechas al día
        settings = self.data_handler.load_settings()
        if (settings.get('day_plan_on_start', True)
                and not self.data_handler.has_day_plan_today()
                and any(not t.get('completed') for t in self.data_handler.get_tasks())):
            self.open_day_plan()
            return
        from ui.windows.welcome_farewell import WelcomeDialog
        WelcomeDialog(self.window, self._welcome_lines(), user_name=self._user_name(),
                      streak=self._usage_streak()).exec()

    # ================= Nombre y racha de uso =================
    def _user_name(self) -> str:
        from ui.greeting import clean_name
        return clean_name(self.data_handler.load_settings().get('user_name', ''))

    def _usage_streak(self):
        """(actual, mejor) de la racha de días seguidos usando la app."""
        try:
            return self.data_handler.get_usage_streak()
        except Exception as e:
            print(f"No se pudo calcular la racha de uso: {e}")
            return None

    def _mark_active_day(self, today: Optional[date] = None) -> None:
        try:
            self.data_handler.mark_active_day(today)
        except Exception as e:
            print(f"No se pudo apuntar el día de uso: {e}")

    def _ask_user_name_if_needed(self) -> None:
        """Primer arranque (también para quien ya usaba la app): si no hay
        nombre y nunca se preguntó, «¿Cómo te llamas?». Guardar u Omitir:
        no se vuelve a preguntar (se cambia en Configuración → Perfil)."""
        settings = self.data_handler.load_settings()
        if self._user_name() or settings.get('user_name_asked'):
            return
        name = dialogs.ask_user_name(self._feedback_window())
        settings = self.data_handler.load_settings()
        if name:
            settings['user_name'] = name
        settings['user_name_asked'] = True
        self.data_handler.save_settings(settings)

    # ================= Presentación (onboarding) =================
    def _show_onboarding_if_needed(self) -> None:
        """Las tres diapositivas del primer arranque, una sola vez: solo en
        una instalación nueva (`onboarding_seen` False, ver DataHandler._load),
        con la app a la vista y no en el reinicio como administrador."""
        import sys
        from services.singleton import ELEVATED_RESTART_FLAG
        settings = self.data_handler.load_settings()
        if settings.get('onboarding_seen', False) or self.data_handler.load_error:
            return  # ya vista, o datos que no se pudieron leer (no es nueva)
        if ELEVATED_RESTART_FLAG in sys.argv:
            return  # se relanzó desde el modo enfoque: no es el primer vistazo
        win = self._feedback_window()
        if not win.isVisible() or win.isMinimized():
            return  # en segundo plano: queda para el próximo arranque
        self.show_onboarding()

    def show_onboarding(self, parent=None) -> None:
        """Abre la presentación (también desde Ajustes → «Ver la
        presentación»). Cerrarla de cualquier forma la marca como vista."""
        from ui.dialogs.onboarding_dialog import OnboardingDialog
        settings = self.data_handler.load_settings()
        dlg = OnboardingDialog(parent or self._feedback_window(),
                               user_name=self._user_name(),
                               pomodoro_min=settings.get('pomodoro_min', 25))
        self._onboarding_dialog = dlg
        try:
            dlg.exec()
        finally:
            self._onboarding_dialog = None
            dlg.deleteLater()
            settings = self.data_handler.load_settings()
            if not settings.get('onboarding_seen'):
                settings['onboarding_seen'] = True
                self.data_handler.save_settings(settings)

    # ================= Plan de hoy =================
    def open_day_plan(self) -> None:
        """Elegir las tareas de hoy, con sugerencias priorizadas por
        vencimiento (core/day_plan). «Ahora no» no guarda nada: se vuelve a
        ofrecer en el próximo arranque del día."""
        from core import day_plan
        from ui.dialogs.day_plan_dialog import pick_day_plan
        tasks = self.data_handler.get_tasks()
        suggestions = day_plan.suggest(tasks, self.data_handler.get_time_chunks())
        if self.data_handler.has_day_plan_today():
            preselected = self.data_handler.get_day_plan()
        else:
            preselected = day_plan.default_selection(suggestions)
        ids = pick_day_plan(self._feedback_window(), tasks, suggestions, preselected,
                            user_name=self._user_name(), streak=self._usage_streak())
        if ids is None:
            return
        # Las del plan que ya se completaron hoy se conservan al replanificar
        done = {t['id'] for t in tasks if t.get('completed')}
        kept = [i for i in self.data_handler.get_day_plan() if i in done and i not in ids]
        self.data_handler.set_day_plan(list(ids) + kept)
        if ids:
            self.set_task_filter('today')
            toast.show_success(self._feedback_window(),
                               f"Plan de hoy: {len(ids)} tarea{'s' if len(ids) != 1 else ''}",
                               duration=2200)
        elif self.task_filter == 'today':
            self.refresh_task_list()

    # ================= Fase 5: tray / cierre =================
    def _setup_tray(self) -> None:
        from PySide6.QtGui import QAction, QIcon
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon
        from utils.paths import app_icon_path
        self.tray = QSystemTrayIcon(QIcon(str(app_icon_path())), self)
        self.tray.setToolTip("PomoGrowth")
        menu = QMenu()
        show_action = QAction("Mostrar PomoGrowth", menu)
        show_action.triggered.connect(self.show_from_tray)
        menu.addAction(show_action)
        self._topmost_action = QAction("Siempre visible", menu)
        self._topmost_action.setCheckable(True)
        self._topmost_action.setChecked(
            bool(self.data_handler.load_settings().get('always_on_top', True)))
        self._topmost_action.triggered.connect(self._toggle_topmost_from_tray)
        menu.addAction(self._topmost_action)
        # Música: el texto (reproducir/pausar) se ajusta al abrir el menú
        self._music_action = QAction("Reproducir la música", menu)
        self._music_action.triggered.connect(self._toggle_music_from_tray)
        menu.addAction(self._music_action)
        menu.aboutToShow.connect(self._sync_tray_music)
        self._sync_tray_music()
        menu.addSeparator()
        quit_action = QAction("Cerrar", menu)
        quit_action.triggered.connect(self.confirm_quit)  # pregunta, como la X
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_from_tray()
            if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _save_window_geometry(self) -> None:
        """Recordar dónde estaba la ventana (se restaura al arrancar)."""
        if self.window.isVisible() and not self.window.isMinimized():
            settings = self.data_handler.load_settings()
            settings['window_geometry'] = self.window.geometry_settings()
            self.data_handler.save_settings(settings)

    def hide_to_tray(self) -> None:
        """Ocultar en la bandeja con el timer en marcha. Desde la 9.4 la X
        sale de la app (confirm_quit); esto queda para llevarla a la bandeja
        desde el código."""
        self._save_window_geometry()
        self.window.fade_hide()
        if self.compact_window and self.compact_window.isVisible():
            self.compact_window.hide()
        self.tray.showMessage("PomoGrowth sigue aquí",
                              "El temporizador continúa en segundo plano. "
                              "Doble clic en el icono para volver.",
                              self.tray.icon(), 2500)

    def show_from_tray(self) -> None:
        self.window.fade_show()

    def _toggle_topmost_from_tray(self, checked: bool) -> None:
        settings = self.data_handler.load_settings()
        settings['always_on_top'] = bool(checked)
        self.data_handler.save_settings(settings)
        self.apply_settings(settings)

    def confirm_quit(self) -> None:
        """La X de la cabecera, Alt+F4 y «Cerrar» de la bandeja: salir del
        todo, preguntando siempre antes. Una sola pregunta a la vez y ninguna
        si ya se está cerrando; «Cancelar» o Esc no tocan nada. Con la ventana
        minimizada («Cerrar» de la bandeja o de la barra de tareas), la
        restaura antes para que la pregunta salga sobre ella."""
        if self._is_closing or self._quit_asking:
            return
        self._quit_asking = True
        try:
            if self.window.isMinimized():
                self.show_from_tray()
            leave = self._ask_quit()
        finally:
            self._quit_asking = False
        if leave and not self._is_closing:
            self.quit_with_farewell()

    def _ask_quit(self) -> bool:
        """La pregunta de salir: diálogo de la casa con «Cancelar» y «Salir»
        (Enter sale, Esc cancela). Con un pomodoro o un descanso en marcha
        avisa de que el tiempo en curso se guarda (quit_with_farewell lo
        registra)."""
        if self.model.is_running:
            kind = ("un descanso" if self.model.is_break_time
                    else "una sesión libre" if self.model.is_free else "un pomodoro")
            message = (f"Tienes {kind} en marcha: el tiempo que llevas se guarda"
                       " y PomoGrowth se cierra por completo.")
        else:
            message = ("PomoGrowth se cerrará por completo. Si solo quieres"
                       " apartarlo, minimízalo.")
        dlg = dialogs.BrandedDialog(self._feedback_window(),
                                    "¿Estás seguro de salir del programa?",
                                    icon="circle-help")
        dlg.add_message(message)
        _cancel, leave = dlg.add_buttons([("Cancelar", "ghost", 0), ("Salir", "accent", 1)])
        leave.setDefault(True)
        self._quit_dialog = dlg
        try:
            return dlg.exec() == 1
        finally:
            self._quit_dialog = None
            dlg.deleteLater()

    def _guard_window_close(self) -> bool:
        """Alt+F4 o «Cerrar ventana» de la barra de tareas: la misma pregunta
        que la X, y la ventana no se cierra por su cuenta (salir es
        quit_with_farewell). En pleno cierre no se pregunta nada."""
        if self._is_closing:
            return True
        self.confirm_quit()
        return False

    def quit_with_farewell(self, *, farewell: bool = True) -> None:
        """Cierre real: ocultar TODO de inmediato, despedida y salir. Sin
        `farewell` (reinicio como administrador) sale sin la despedida."""
        if self._is_closing:
            return
        self._is_closing = True
        self._save_window_geometry()
        self._tick_timer.stop()
        # Sin reintentos ni envíos nuevos: la cola espera en el JSON
        self.asana.shutdown()
        self._webhook_timer.stop()
        self._stop_webhook_server()  # el puerto, libre para la siguiente instancia
        if self.model.is_free:
            # Modo Libre: cerrar la sesión con su tramo y sus recompensas (el
            # comentario de Asana espera en la cola al siguiente arranque)
            try:
                self.finish_free_session(silent=True)
            except Exception as e:
                print(f"No se pudo cerrar la sesión libre al salir: {e}")
        if self.model.is_running:
            elapsed = self.model.pause()
            try:
                self._log_and_refresh(elapsed,
                                      "break" if self.model.is_break_time else "pomodoro",
                                      ended_at=self.model.last_segment_end)
            except Exception:
                pass
        if self._music_dialog is not None:
            self._music_dialog.reject()
        if self._quit_dialog is not None:  # salir con la pregunta abierta: la cierra
            self._quit_dialog.reject()
        if getattr(self, '_effort_dialog', None) is not None:  # como omitido
            self._effort_dialog.reject()
        if getattr(self, '_onboarding_dialog', None) is not None:  # no se queda animándose
            self._onboarding_dialog.reject()
        self.music.shutdown()
        ambient_fading = self.ambient.is_playing()
        self.ambient.shutdown()   # sale con su fundido mientras se despide
        self.window.hide()
        if self.compact_window:
            self.compact_window.hide()
        self.tray.hide()
        try:
            self.focus_mode.cleanup_system_blocks()
        except Exception as e:
            print(f"Limpieza de enfoque al salir: {e}")
        from PySide6.QtWidgets import QApplication
        if not farewell:
            # Sin despedida (p. ej. reiniciar como administrador): se espera
            # al fundido del ambiente para no cortarlo en seco
            if ambient_fading:
                QTimer.singleShot(self.ambient.FADE_OUT_MS + 50,
                                  QApplication.instance().quit)
            else:
                QApplication.instance().quit()
            return
        from ui.windows.welcome_farewell import FarewellWindow
        name = self._user_name()
        extra = {'user_name': name} if name else {}  # sin nombre, la de siempre
        self._farewell = FarewellWindow(QApplication.instance().quit, **extra)

    # ================= Fase 5: enfoque =================
    def _cleanup_stale_focus_block(self) -> None:
        """Si un cierre abrupto dejó el hosts bloqueando, repararlo al arrancar."""
        try:
            status = self.focus_mode.get_status()
            if not status.get('active') and self.focus_mode._hosts_has_block():
                if self.focus_mode.cleanup_system_blocks():
                    print("Bloqueo de enfoque huérfano limpiado al arrancar")
        except Exception as e:
            print(f"No se pudo verificar bloqueo huérfano: {e}")

    def _warn_focus_expire(self) -> None:
        """Un aviso (una vez por fallo) si los comodines caducaron pero el
        hosts no se dejó reescribir: esos sitios siguen libres."""
        error = getattr(self.focus_mode, 'expire_error', '')
        if error == self._focus_expire_warned:
            return
        self._focus_expire_warned = error
        win = self._feedback_window()
        if error and win.isVisible() and not win.isMinimized():
            toast.show_warning(win, "Los comodines del enfoque caducaron, pero no se pudo "
                               f"volver a bloquear esos sitios: {error}", duration=6000)

    def open_focus(self) -> None:
        from ui.dialogs.focus_dialog import FocusDialog
        dlg = FocusDialog(self.window, self.focus_mode)
        dlg.state_changed.connect(self._on_focus_state)
        # «Reiniciar como administrador»: la instancia elevada ya se abre
        dlg.elevated_restart.connect(lambda: self.quit_with_farewell(farewell=False))
        dlg.session_warning = self._admin_restart_warning
        self._on_focus_state(bool(self.focus_mode.get_status().get('active')))
        dlg.exec()

    def _admin_restart_warning(self) -> str:
        """Qué corta «Reiniciar como administrador» (quit_with_farewell sin
        despedida): el aviso para confirmarlo, o '' si no hay sesión en marcha."""
        m = self.model
        question = " ¿Reiniciar PomoGrowth como administrador?"
        if m.is_free:
            if not (m.is_running or m.free_elapsed > 0):
                return ''
            return ("Tienes una sesión libre en marcha: se termina y se guarda, con sus "
                    f"recompensas si llega a {self.rewards.FREE_SESSION_MIN_MINUTES} min."
                    + question)
        if m.is_break_time:
            if not (m.is_running or m.remaining_time < self._session_total):
                return ''
            noun = "descanso"
        elif m.is_session_in_progress():
            noun = "pomodoro"
        else:
            return ''
        return (f"Tienes un {noun} en marcha: el tiempo que llevas se guarda, pero el "
                f"{noun} no se completa." + question)

    def _on_focus_state(self, active: bool) -> None:
        t = tokens()
        btn = self.window.title_bar.focus_btn
        btn.set_colors(t.accent if active else t.fg_dim,
                       t.accent if active else t.fg)

    # ================= Fase 5: modo compacto =================
    def show_compact(self) -> None:
        if self.compact_window is None:
            from ui.windows.compact_window import CompactWindow
            self.compact_window = CompactWindow()
            self.compact_window.toggle_requested.connect(self.toggle_timer)
            self.compact_window.skip_requested.connect(self.skip_break)
            self.compact_window.expand_requested.connect(self.show_normal)
        self._save_window_geometry()
        geo = self.window.geometry()
        self.compact_window.move(geo.right() - self.compact_window.width() - 20,
                                 geo.top() + 20)
        self.window.hide()
        self.compact_window.show()
        # Sincronizar DESPUÉS de show(): el guard isVisible() de _sync_compact
        # convertía el sync pre-show en no-op y la mini abría con '25:00/TRABAJO'
        self._sync_compact()

    def show_normal(self) -> None:
        if self.compact_window:
            self.compact_window.hide()
        self.window.fade_show()

    def _sync_compact(self) -> None:
        if not (self.compact_window and self.compact_window.isVisible()):
            return
        shown = self.model.get_display_time()
        task = self._task_by_id(self.model.active_task_id)
        self.compact_window.set_state(self._clock_text(shown), self.model.is_running,
                                      task.get('name') if task else None,
                                      self.model.is_break_time, free=self.model.is_free)
        fraction = self._timer_fraction(shown)
        if fraction is not None:
            self.compact_window.set_progress(fraction, animate=self._ring_animates(shown))

    # ================= Recompensas =================
    def update_rewards_bar(self) -> None:
        stats = self.rewards.get_stats()
        base = self.rewards.XP_PER_LEVEL.get(stats.get('level', 1), 0)
        self.xp_bar.update_from_stats(stats, base)

    # ================= Mascota pixel =================
    def refresh_pet(self) -> None:
        """Muestra/oculta la mascota según el slot equipado en Recompensas."""
        from services import pet_service
        pet_id = self.rewards.progress.current_pet
        owned = pet_id in self.rewards.progress.purchased_items
        if pet_id in pet_service.PET_NAMES and owned:
            self.timer_panel.pet.set_species(pet_id, pet_service.PET_NAMES[pet_id])
            self.timer_panel.pet.setVisible(True)
            greeting = pet_service.local_message('greeting', pet_id)
            if greeting:
                self.timer_panel.pet.show_message(greeting, duration_ms=8000)
        else:
            self.timer_panel.pet.setVisible(False)

    def _pet_react(self, event: str, reward_info=None) -> None:
        """Reacción local (sin red) de la mascota a un evento de progreso."""
        from services import pet_service
        pet = self.timer_panel.pet
        if not pet.isVisible():
            return
        if reward_info and reward_info.get('level_up'):
            event = 'level_up'
        msg = pet_service.local_message(event, self.rewards.progress.current_pet)
        if msg:
            pet.show_message(msg)

    def request_pet_advice(self) -> None:
        """Clic en la mascota: consejo con el proveedor de IA configurado
        (Claude, ChatGPT o endpoint propio); sin configurar, frase local."""
        from services import pet_service
        pet = self.timer_panel.pet
        species = self.rewards.progress.current_pet
        config = pet_service.provider_config(self.data_handler.load_settings())
        if not pet_service.is_configured(config):
            msg = pet_service.local_message('greeting', species)
            pet.show_message((msg or "") +
                             "\n(Configura un proveedor de IA en ⚙ Configuración → "
                             "Mascota IA — Claude, ChatGPT o tu endpoint de "
                             "ngrok — para pedirme consejos personalizados)")
            return
        pending = sum(1 for t in self.data_handler.get_tasks() if not t.get('completed'))
        today_min = int(self._today_cache[1] / 60) if self._today_cache[0] else 0
        context = pet_service.build_context(self.rewards.get_stats(), pending, today_min)
        pet.show_message("…")

        def ok(text):
            pet.show_message(str(text), duration_ms=25000)

        def fail(e):
            pet.show_message(f"No pude conectar con mi cerebro IA: {e}", duration_ms=8000)

        pet_service.request_advice(config, species, context, ok, fail)

    # ================= Tareas =================
    def _task_by_id(self, task_id) -> Optional[Dict[str, Any]]:
        if task_id is None:
            return None
        return next((t for t in self.data_handler.get_tasks()
                     if t.get('id') == task_id), None)

    @staticmethod
    def _task_project_name(task: Dict[str, Any]) -> str:
        try:
            projects = task.get('projects') or []
            if projects and isinstance(projects, list):
                name = (projects[0] or {}).get('name')
                if name:
                    return str(name)
            for tag in task.get('tags') or []:
                tag_l = str(tag).lower()
                if tag_l.startswith(('proyecto:', 'project:')):
                    return tag.split(':', 1)[1].strip()
        except Exception:
            pass
        return ''

    @staticmethod
    def _task_created_key(task: Dict[str, Any]) -> float:
        """Epoch de creación. Numérico y normalizado: comparar strings ISO
        mezclaba naive local (tareas manuales) con UTC 'Z' (Asana) y ordenaba
        mal las tareas de las últimas horas del día."""
        created = task.get('created_at')
        if created:
            try:
                dt = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
                return dt.timestamp()  # naive → hora local; aware → correcto
            except ValueError:
                pass
        try:
            # Los ids son timestamps en ms: fecha de creación implícita
            return task['id'] / 1000.0
        except (KeyError, TypeError):
            return float('-inf')

    def _sort_tasks_for_view(self, tasks: List[Dict]) -> List[Dict]:
        mode = self.task_sort_mode
        if mode == 'project':
            return sorted(tasks, key=lambda t: ((self._task_project_name(t) or '￿').lower(),
                                                (t.get('name') or '').lower()))
        if mode == 'due':
            return sorted(tasks, key=lambda t: (t.get('due') is None, t.get('due') or '',
                                                (t.get('name') or '').lower()))
        if mode == 'name':
            return sorted(tasks, key=lambda t: (t.get('name') or '').lower())
        if mode == 'created':
            # Más recientes arriba; sin fecha derivable, al final
            return sorted(tasks, key=self._task_created_key, reverse=True)
        return tasks

    def refresh_task_list(self) -> None:
        all_tasks = self.data_handler.get_tasks()
        plan = self.data_handler.get_day_plan() if self.task_filter == 'today' else []
        planned = set(plan)
        shown = [t for t in all_tasks
                 if self.task_filter == 'all'
                 or (self.task_filter == 'today' and t.get('id') in planned)
                 or (self.task_filter == 'pending' and not t.get('completed'))
                 or (self.task_filter == 'completed' and t.get('completed'))
                 or (self.task_filter == 'recurring' and t.get('recurring'))]
        if self.task_filter == 'today' and self.task_sort_mode == 'manual':
            # El orden en que se eligieron, con las ya hechas al final (tachadas)
            shown.sort(key=lambda t: (bool(t.get('completed')), plan.index(t['id'])))
        else:
            shown = self._sort_tasks_for_view(shown)
        times = defaultdict(float)
        for c in self.data_handler.get_time_chunks():
            if c.get('type', 'pomodoro') == 'pomodoro':
                times[c.get('task_id')] += c.get('duration', 0)
        self.tasks_panel.set_tasks(shown, self.model.active_task_id,
                                   times, self.task_filter, self.task_sort_mode)
        self.tasks_panel.add_panel.set_known_tags(self.data_handler.get_all_tags())
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        tasks = self.data_handler.get_tasks()
        pending = sum(1 for t in tasks if not t.get('completed'))
        self.tasks_panel.set_counter(pending, len(tasks))

    def _on_advanced_filters_toggled(self, open_: bool) -> None:
        settings = self.data_handler.load_settings()
        settings['advanced_filters_open'] = bool(open_)
        self.data_handler.save_settings(settings)

    def set_task_filter(self, key: str) -> None:
        self.task_filter = key
        # En «Hoy», lo que se añade entra en el plan: que el campo lo diga
        self.tasks_panel.add_panel.set_plan_hint(key == 'today')
        self.refresh_task_list()

    def set_task_sort(self, mode: str) -> None:
        if mode not in ('manual', 'project', 'due', 'name', 'created'):
            mode = 'manual'
        self.task_sort_mode = mode
        settings = self.data_handler.load_settings()
        settings['task_sort_mode'] = mode
        self.data_handler.save_settings(settings)
        self.refresh_task_list()

    def select_task(self, task_id: int) -> None:
        if self.model.active_task_id == task_id:
            return
        # Con un pomodoro en curso (corriendo o pausado a mitad) la tarea del
        # timer queda BLOQUEADA: revisar la lista no debe cambiar el foco.
        # Se desbloquea al terminar el pomodoro, al reiniciar o sin tarea activa.
        if self.model.is_session_in_progress() and self.model.active_task_id is not None:
            toast.show_warning(self.window,
                               "Sesión libre en curso — la tarea activa está bloqueada "
                               "(termina la sesión para cambiarla)" if self.model.is_free
                               else "Pomodoro en curso — la tarea activa está bloqueada "
                               "(termina o reinicia el timer para cambiarla)",
                               duration=2600)
            return
        old = self.model.active_task_id
        self.model.active_task_id = task_id
        self.tasks_panel.set_active_card(old, task_id)
        task = self._task_by_id(task_id)
        self.timer_panel.set_active_task(task.get('name') if task else None)

    def toggle_active_task_completion(self) -> None:
        if self.model.active_task_id is not None:
            self.toggle_task_completion(self.model.active_task_id)

    def toggle_task_completion(self, task_id: int) -> None:
        task = self._task_by_id(task_id)
        if not task:
            return
        now_completed = not task.get('completed')
        is_active = now_completed and self.model.active_task_id == task_id
        if is_active and self.model.is_session_in_progress():
            # Cerrar la sesión ANTES de marcarla completada: el último tramo
            # queda fechado antes del completado y cuenta en el esfuerzo de
            # este ciclo, no también en el siguiente de una tarea fija.
            # Preservar el progreso del ciclo: completar la tarea a mitad
            # del 4º pomodoro no debe borrar los 3 puntos ya ganados
            self.reset_timer(keep_cycle=True)
        # En tareas de Asana, update_task_status encola el cambio (persistido,
        # también con el sync apagado): el sync no lo revierte y se envía en
        # cuanto se pueda
        self.data_handler.update_task_status(task_id, now_completed)
        if task.get('source') == 'asana' and task.get('asana_id'):
            self.asana.push_pending()
        if is_active:
            self.model.active_task_id = None
            self.tasks_panel.set_active_card(task_id, None)
            self.timer_panel.set_active_task(None)

        hides_it = ((self.task_filter == 'pending' and now_completed)
                    or (self.task_filter == 'completed' and not now_completed))
        if hides_it:
            self.tasks_panel.remove_card(task_id)
        else:
            self.tasks_panel.update_card_completion(task_id, now_completed)
        self._refresh_counts()
        if now_completed and self.effort_prompt_enabled():
            # La pregunta de esfuerzo SUSTITUYE al toast «Tarea completada»
            self._ask_effort(task)
        elif now_completed:
            from core.data_handler import recurring_cycle
            cycle = recurring_cycle(task)
            if cycle:
                comeback = {'daily': "vuelve mañana",
                            'weekly': "vuelve en una semana",
                            'monthly': "vuelve el próximo mes",
                            'quarterly': "vuelve el próximo trimestre"}[cycle]
                toast.show_success(self.window,
                                   f"Tarea fija completada: {task.get('name', '')[:36]} "
                                   f"— {comeback}", duration=2600)
            else:
                toast.show_success(self.window,
                                   f"Tarea completada: {task.get('name', '')[:40]}",
                                   duration=2200)

    # ---------- valoración de esfuerzo ----------
    # Preguntar el esfuerzo al completar (ajuste 'ask_effort_on_complete');
    # los tests lo apagan por defecto (conftest)
    ASK_EFFORT_DEFAULT = True

    def effort_prompt_enabled(self) -> bool:
        return bool(self.data_handler.load_settings().get(
            'ask_effort_on_complete', self.ASK_EFFORT_DEFAULT))

    def _ask_effort(self, task: Dict[str, Any]) -> None:
        """Popup no modal de esfuerzo (1-5) de una tarea recién completada por
        el usuario. Si ya había uno abierto (otra tarea), se cierra como
        omitido. Solo lo llama toggle_task_completion: ni el merge de Asana,
        ni las marcas de completadas fuera de la app, ni la reapertura de las
        fijas preguntan nada."""
        from ui.dialogs.effort_dialog import EffortDialog
        previous = getattr(self, '_effort_dialog', None)
        if previous is not None:
            previous.reject()
        task_id = task.get('id')
        dlg = EffortDialog(self._feedback_window(), task.get('name', ''))
        dlg.finished.connect(lambda result, d=dlg: self._on_effort_finished(d, task_id, result))
        self._effort_dialog = dlg
        dlg.popup()

    def _on_effort_finished(self, dlg, task_id, result: int) -> None:
        if getattr(self, '_effort_dialog', None) is dlg:
            self._effort_dialog = None
        dlg.deleteLater()
        task = self._task_by_id(task_id)
        if not 1 <= result <= 5 or not task or not task.get('completed'):
            return  # omitido, o la tarea ya no está completada
        self.data_handler.add_effort_rating(task_id, result,
                                            project=self._task_project_name(task))
        self.tasks_panel.update_card_effort(task_id, result)

    def add_task(self, name: str, tags: List[str], recurring: str = '',
                 due: str = '', targets: Optional[Dict[str, Any]] = None) -> None:
        """targets: crear también en Asana/ClickUp (ver TaskAddPanel)."""
        targets = targets or {}
        new_id = self.data_handler.add_task(
            name, tags, source='local', recurring=recurring or False, due=due or None,
            asana_create=self.destinations.asana_payload(targets, recurring))
        if self.task_filter == 'today':
            # Viendo el plan de hoy, lo nuevo es para hoy (si no, no se vería)
            self.data_handler.set_day_plan(self.data_handler.get_day_plan() + [new_id])
        self.refresh_task_list()
        # Con el orden por fecha las tareas sin due caen al fondo: llevar la
        # tarjeta a la vista y confirmar el alta para que no parezca perdida
        self.tasks_panel.scroll_to_task(new_id)
        toast.show_success(self.window, f"Tarea añadida: {name[:40]}", duration=1800)
        self.destinations.after_add(new_id, name, due, targets)

    def delete_task(self, task_id: int) -> None:
        task = self._task_by_id(task_id)
        if not task:
            return
        if not dialogs.confirm(self.window, "Eliminar tarea",
                               f"¿Eliminar «{task.get('name', '')}»?\n"
                               "El tiempo registrado se conserva en las estadísticas.",
                               ok_text="Eliminar", danger=True):
            return
        if self.model.active_task_id == task_id:
            if self.model.is_session_in_progress():
                # Cerrar la sesión ANTES de borrar: registra el tramo en curso
                # en la tarea (como promete el diálogo) y evita que el candado
                # quede abierto con un pomodoro corriendo sin dueño
                self.reset_timer(keep_cycle=True)
            self.model.active_task_id = None
            self.timer_panel.set_active_task(None)
        self.data_handler.delete_task(task_id)
        self.tasks_panel.remove_card(task_id)
        self._refresh_counts()

    def edit_task(self, task_id: int) -> None:
        task = self._task_by_id(task_id)
        if not task:
            return
        from core.data_handler import recurring_cycle
        # El selector de repetición solo aplica a tareas locales: el ciclo de
        # vida de las de Asana lo gobierna Asana (también el de una que se
        # está creando allí: al enlazarse sería de Asana y no podría repetirse)
        creating = self.data_handler.has_pending_asana_create(task_id)
        is_local = task.get('source') != 'asana' and not creating
        current_cycle = (recurring_cycle(task) or '') if is_local else None
        hint = None if is_local else (
            "Se está creando en Asana: el nombre viaja con el alta y no puede "
            "repetirse. Las etiquetas que añadas aquí son solo de PomoGrowth."
            if creating else
            "El nombre también se cambia en Asana. Las etiquetas que añadas "
            "aquí son solo de PomoGrowth.")
        shown_name = task.get('name', '')
        result = dialogs.edit_task(self.window, shown_name,
                                   task.get('tags') or [], recurring=current_cycle,
                                   hint=hint)
        if result is None:
            return
        name, tags, cycle = result
        if name == shown_name.strip():
            # Nombre sin tocar: conservar el vigente (un sync pudo traer un
            # renombrado de Asana con el diálogo abierto; no revertirlo)
            name = task.get('name', name)
        # En tareas de Asana, un nombre nuevo se encola hacia Asana
        if cycle is None:
            self.data_handler.update_task_name_and_tags(task_id, name, tags)
        else:
            self.data_handler.update_task_name_and_tags(task_id, name, tags,
                                                        recurring=cycle)
        if not is_local:
            self.asana.push_pending()
        if self.model.active_task_id == task_id:
            self.timer_panel.set_active_task(name)
        self.refresh_task_list()

    def note_task(self, task_id: int) -> None:
        task = self._task_by_id(task_id)
        if not task:
            return
        note = dialogs.note_task(self.window, task.get('name', ''),
                                 task.get('notes', '') or '')
        if note is None:
            return
        task['notes'] = note
        self.data_handler._save()
        toast.show_info(self.window, "Nota guardada", duration=1500)

    def move_task(self, task_id: int, direction: int) -> None:
        if self.task_sort_mode != 'manual':
            return
        if self.tasks_panel.move_card(task_id, direction):
            self.reorder_visible_tasks(self.tasks_panel.visible_order())

    def reorder_visible_tasks(self, ordered_ids: List[int]) -> None:
        """Portado de v8: reordena solo las visibles dentro del array global."""
        try:
            tasks = self.data_handler.get_tasks()
            id2task = {t.get('id'): t for t in tasks}
            if any(tid not in id2task for tid in ordered_ids):
                self.refresh_task_list()
                return
            wanted = set(ordered_ids)
            visible_positions = [i for i, t in enumerate(tasks) if t.get('id') in wanted]
            if len(visible_positions) != len(ordered_ids):
                self.refresh_task_list()
                return
            for pos, tid in zip(visible_positions, ordered_ids):
                tasks[pos] = id2task[tid]
            self.data_handler._save()
        except Exception as e:
            print(f"Error aplicando el reordenamiento: {e}")
            self.refresh_task_list()
