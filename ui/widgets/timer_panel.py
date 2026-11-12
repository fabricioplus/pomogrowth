"""Panel del temporizador: tarea activa, selector «Pomodoro | Libre», anillo
de progreso con el tiempo dentro, ciclo, controles y tiempo enfocado hoy. Arriba, junto al estado de la
sincronización, el mini reproductor de música (ui/widgets/mini_player)."""
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGraphicsDropShadowEffect,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.cycle_dots import CycleDots
from ui.widgets.icon_button import IconButton, tinted_icon
from ui.widgets.marquee_label import MarqueeLabel
from ui.widgets.progress_ring import ProgressRing


class TimerPanel(QWidget):
    toggle_requested = Signal()
    reset_requested = Signal()
    skip_requested = Signal()
    complete_task_requested = Signal()
    sync_requested = Signal()
    sync_toggled = Signal(bool)
    panel_toggle_requested = Signal()  # ocultar/mostrar el panel de tareas
    kind_requested = Signal(str)  # selector «Pomodoro | Libre»: 'pomodoro'/'free'

    def __init__(self, parent=None):
        super().__init__(parent)
        t = tokens()
        self._kind = 'pomodoro'
        self._is_break = False
        self._is_running = False
        self._sync_state = 'idle'
        self._pending_mode = None  # contenido a aplicar a mitad del crossfade

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 14, 28, 18)
        lay.setSpacing(0)

        # Fila superior: estado de conexión + sync (icono de estado + switch
        # para encender/apagar la sincronización de un vistazo)
        conn_row = QHBoxLayout()
        conn_row.setSpacing(8)
        # Ocultar/mostrar el panel de tareas (el «❮» de v8): un botón en cada
        # extremo y solo se ve el del lado donde está el panel
        self._panel_btns = {}
        for side in ('left', 'right'):
            b = IconButton(f"panel-{side}-close", "Ocultar el panel de tareas",
                           color=t.fg_muted, hover_color=t.fg, size=18, parent=self)
            b.clicked.connect(self.panel_toggle_requested.emit)
            self._panel_btns[side] = b
        conn_row.addWidget(self._panel_btns['left'])
        # Mini reproductor de música: en esta fila no ocupa alto, así nunca
        # empuja el anillo; solo se ve con algo cargado (show_now_playing)
        from ui.widgets.mini_player import MiniPlayer
        self.mini_player = MiniPlayer(self)
        conn_row.addWidget(self.mini_player)
        self.mini_player.setVisible(False)
        conn_row.addStretch(1)
        self.connection_label = QLabel("● Sin sincronización")
        self.connection_label.setFont(fonts.font(fonts.XS))
        self.connection_label.setProperty("role", "muted")
        conn_row.addWidget(self.connection_label)
        self.sync_btn = IconButton("sync", "Sincronizar ahora (Asana/ClickUp)",
                                   color=t.fg_muted, hover_color=t.accent, size=15,
                                   parent=self)
        self.sync_btn.setEnabled(False)
        self.sync_btn.clicked.connect(self.sync_requested.emit)
        conn_row.addWidget(self.sync_btn)
        from ui.widgets.toggle_switch import ToggleSwitch
        self.sync_toggle = ToggleSwitch("Sincronización activada/desactivada",
                                        parent=self)
        self.sync_toggle.toggled.connect(self.sync_toggled.emit)
        conn_row.addWidget(self.sync_toggle)
        conn_row.addWidget(self._panel_btns['right'])
        lay.addLayout(conn_row)
        self._panel_side, self._panel_collapsed = 'left', False
        self._panel_btns['right'].setVisible(False)

        # Línea de progreso del sync: solo visible mientras hay algo que decir
        self.sync_status = QLabel("")
        self.sync_status.setFont(fonts.font(fonts.XS))
        self.sync_status.setProperty("role", "muted")
        self.sync_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.sync_status.hide()
        lay.addWidget(self.sync_status)

        lay.addStretch(1)

        # Tarea activa: rótulo + botón completar + nombre (marquee)
        self.task_caption = QLabel("Tarea actual")
        self.task_caption.setFont(fonts.font(fonts.XS))
        self.task_caption.setProperty("role", "muted")
        self.task_caption.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.task_caption)
        lay.addSpacing(4)
        task_row = QHBoxLayout()
        task_row.setSpacing(6)
        task_row.addStretch(1)
        self.complete_btn = IconButton("circle", "Completar tarea activa",
                                       color=t.fg_dim, hover_color=t.accent, size=19)
        self.complete_btn.clicked.connect(self.complete_task_requested.emit)
        self.complete_btn.hide()
        task_row.addWidget(self.complete_btn)
        self.active_task = MarqueeLabel("Ninguna tarea seleccionada")
        self.active_task.setFont(fonts.font(fonts.NORMAL, weight=QFont.Weight.Medium))
        self.active_task.set_color(t.fg_dim)
        self.active_task.setMinimumWidth(240)
        self.active_task.setMaximumWidth(420)
        task_row.addWidget(self.active_task)
        task_row.addStretch(1)
        lay.addLayout(task_row)
        lay.addSpacing(12)
        self._build_kind_selector(lay)
        lay.addSpacing(12)

        # Bloque central (crossfade trabajo↔descanso): el anillo con el modo,
        # las cifras y los puntos del ciclo dentro
        self.center_block = QWidget()
        center = QHBoxLayout(self.center_block)
        center.setContentsMargins(0, 0, 0, 0)
        center.addStretch(1)
        self.ring = ProgressRing()
        center.addWidget(self.ring)
        center.addStretch(1)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        mode_row.addStretch(1)
        self.mode_icon = QLabel()
        self.mode_icon.setPixmap(tinted_icon("tomato", t.accent, 16).pixmap(16, 16))
        mode_row.addWidget(self.mode_icon)
        self.mode_label = QLabel("Enfoque")
        self.mode_label.setFont(fonts.font(fonts.SMALL, bold=True))
        self.mode_label.setStyleSheet(f"color: {t.accent};")
        mode_row.addWidget(self.mode_label)
        mode_row.addStretch(1)
        self.ring.inner.addLayout(mode_row)

        self.time_label = QLabel("25:00")
        self.time_label.setFont(fonts.display(fonts.TIMER_MAIN))
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setStyleSheet(f"color: {t.fg};")
        self.ring.inner.addWidget(self.time_label)

        dots_row = QHBoxLayout()
        dots_row.addStretch(1)
        self.cycle_dots = CycleDots()
        dots_row.addWidget(self.cycle_dots)
        dots_row.addStretch(1)
        self.ring.inner.addLayout(dots_row)

        lay.addWidget(self.center_block)
        self._center_fx = QGraphicsOpacityEffect(self.center_block)
        self._center_fx.setOpacity(1.0)
        self.center_block.setGraphicsEffect(self._center_fx)
        self._fade = QPropertyAnimation(self._center_fx, b"opacity", self)
        self._fade.setDuration(300)
        self._fade.setEasingCurve(QEasingCurve.InOutCubic)
        # Una sola conexión, para siempre (desconectar y reconectar en cada
        # cambio de modo daba el RuntimeWarning «Failed to disconnect»)
        self._fade.finished.connect(self._apply_pending_mode)
        lay.addSpacing(20)

        # Controles
        controls = QHBoxLayout()
        controls.setSpacing(18)
        controls.addStretch(1)
        self.reset_btn = IconButton("reset", "Reiniciar", color=t.fg_dim,
                                    hover_color=t.fg, size=18, variant="round")
        self.reset_btn.setFixedSize(44, 44)
        self.reset_btn.clicked.connect(self.reset_requested.emit)
        controls.addWidget(self.reset_btn)

        self.play_btn = QPushButton()
        self.play_btn.setObjectName("playBtn")
        self.play_btn.setFixedSize(64, 64)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setIcon(tinted_icon("play", t.on_accent, 26))
        self.play_btn.setIconSize(self.play_btn.size() * 0.40)
        self._play_glow = QGraphicsDropShadowEffect(self.play_btn)
        self._play_glow.setBlurRadius(28)
        self._play_glow.setOffset(0, 4)
        self.play_btn.setGraphicsEffect(self._play_glow)
        self.play_btn.clicked.connect(self.toggle_requested.emit)
        controls.addWidget(self.play_btn)

        self.skip_btn = IconButton("skip", "Saltar descanso", color=t.fg_dim,
                                   hover_color=t.break_accent, size=18, variant="round")
        self.skip_btn.setFixedSize(44, 44)
        self.skip_btn.clicked.connect(self.skip_requested.emit)
        self.skip_btn.hide()
        controls.addWidget(self.skip_btn)
        controls.addStretch(1)
        lay.addLayout(controls)

        lay.addStretch(1)

        # Mascota pixel (aparece al equiparla en Recompensas)
        from ui.widgets.pet_widget import PetWidget
        self.pet = PetWidget(self)
        lay.addWidget(self.pet)
        self.pet.setVisible(False)

        # Tiempo enfocado hoy: una línea discreta (antes, una tarjeta suelta
        # a 250 px de los controles)
        today_row = QHBoxLayout()
        today_row.setSpacing(6)
        today_row.addStretch(1)
        self.today_icon = QLabel()
        self.today_icon.setPixmap(tinted_icon("timer", t.fg_muted, 15).pixmap(15, 15))
        today_row.addWidget(self.today_icon)
        self.today_caption = QLabel("Enfocado hoy")
        self.today_caption.setFont(fonts.font(fonts.SMALL))
        self.today_caption.setProperty("role", "muted")
        today_row.addWidget(self.today_caption)
        self.total_today = QLabel("00:00:00")
        self.total_today.setFont(fonts.display(fonts.SMALL + 1))
        self.total_today.setStyleSheet(f"color: {t.fg};")
        today_row.addWidget(self.total_today)
        today_row.addStretch(1)
        lay.addSpacing(10)
        lay.addLayout(today_row)

        self._style_play()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    # ---- modo Libre ----
    KIND_HINT_LOCKED = "Termina o reinicia la sesión para cambiar de modo"

    def _build_kind_selector(self, lay: QVBoxLayout) -> None:
        """Control segmentado «Pomodoro | Libre» encima del anillo (el mismo
        de los periodos de Estadísticas). Libre es un cronómetro sin tope."""
        row = QHBoxLayout()
        row.addStretch(1)
        self.kind_segment = QFrame(self)
        self.kind_segment.setProperty("segmented", True)
        seg = QHBoxLayout(self.kind_segment)
        seg.setContentsMargins(3, 3, 3, 3)
        seg.setSpacing(2)
        self._kind_group = QButtonGroup(self)
        self._kind_group.setExclusive(True)
        self.kind_buttons = {}
        self._kind_tips = {
            'pomodoro': "Pomodoro: cuenta atrás con descansos",
            'free': "Libre: un contador sin límite, el tiempo que quieras"}
        for kind, text in (('pomodoro', "Pomodoro"), ('free', "Libre")):
            b = QPushButton(text, self.kind_segment)
            b.setProperty("pill", True)
            b.setCheckable(True)
            b.setChecked(kind == self._kind)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(self._kind_tips[kind])
            b.clicked.connect(lambda _=False, k=kind: self.kind_requested.emit(k))
            self._kind_group.addButton(b)
            self.kind_buttons[kind] = b
            seg.addWidget(b)
        row.addWidget(self.kind_segment)
        row.addStretch(1)
        lay.addLayout(row)

    def set_kind(self, kind: str) -> None:
        """Pomodoro o Libre: rótulo e icono del modo, sin puntos del ciclo y
        con «Terminar sesión» en lugar de «Reiniciar» en el modo Libre."""
        self._kind = 'free' if kind == 'free' else 'pomodoro'
        free = self._kind == 'free'
        self.kind_buttons[self._kind].setChecked(True)
        self.cycle_dots.setVisible(not free)
        self.reset_btn.set_icon_name("flag" if free else "reset")
        self.reset_btn.setToolTip("Terminar sesión" if free else "Reiniciar")
        self._style_mode()

    def set_kind_enabled(self, enabled: bool) -> None:
        """Con una sesión a medias o en descanso no se cambia de modo."""
        for kind, b in self.kind_buttons.items():
            b.setEnabled(enabled)
            b.setToolTip(self._kind_tips[kind] if enabled else self.KIND_HINT_LOCKED)

    # ---- estilo ----
    def _mode_accent(self) -> str:
        t = tokens()
        return t.break_accent if self._is_break else t.accent

    def _style_play(self) -> None:
        t = tokens()
        accent = self._mode_accent()
        hover = t.info if self._is_break else t.accent_hover
        self.play_btn.setStyleSheet(
            f"#playBtn {{ background: {accent}; border: none; border-radius: 32px; }}"
            f"#playBtn:hover {{ background: {hover}; }}")
        glow = QColor(accent)
        glow.setAlpha(90)
        self._play_glow.setColor(glow)
        self.play_btn.setIcon(tinted_icon("pause" if self._is_running else "play",
                                          t.on_accent, 26))

    def _style_mode(self) -> None:
        accent = self._mode_accent()
        if self._is_break:
            icon, text = "coffee", "Descanso"
        elif self._kind == 'free':
            icon, text = "infinity", "Libre"
        else:
            icon, text = "tomato", "Enfoque"
        self.mode_icon.setPixmap(tinted_icon(icon, accent, 16).pixmap(16, 16))
        self.mode_label.setText(text)
        self.mode_label.setStyleSheet(f"color: {accent};")

    def _retheme(self) -> None:
        t = tokens()
        self.time_label.setStyleSheet(f"color: {t.fg};")
        self.total_today.setStyleSheet(f"color: {t.fg};")
        self.today_icon.setPixmap(tinted_icon("timer", t.fg_muted, 15).pixmap(15, 15))
        self._style_mode()
        self._style_play()
        self.reset_btn.set_colors(t.fg_dim, t.fg)
        self.skip_btn.set_colors(t.fg_dim, t.break_accent)
        self.complete_btn.set_colors(t.fg_dim, t.accent)
        for b in self._panel_btns.values():
            b.set_colors(t.fg_muted, t.fg)
        self.set_sync_state(self._sync_state)

    # ---- panel de tareas ----
    def set_panel_toggle(self, side: str, collapsed: bool) -> None:
        """Muestra el botón del lado del panel con el icono de su estado."""
        side = side if side in self._panel_btns else 'left'
        self._panel_side, self._panel_collapsed = side, collapsed
        for s, b in self._panel_btns.items():
            b.setVisible(s == side)
        btn = self._panel_btns[side]
        btn.set_icon_name(f"panel-{side}-{'open' if collapsed else 'close'}")
        btn.setToolTip("Mostrar el panel de tareas" if collapsed
                       else "Ocultar el panel de tareas")

    # ---- sync ----
    def set_sync_state(self, state: str) -> None:
        """Colorea el icono de sync según estado: off (apagado), idle (sin
        conectar aún), syncing, online, offline."""
        t = tokens()
        colors = {'off': t.fg_muted, 'idle': t.fg_dim, 'syncing': t.warning,
                  'online': t.accent, 'offline': t.danger}
        self._sync_state = state if state in colors else 'idle'
        c = colors[self._sync_state]
        self.sync_btn.set_colors(c, t.accent if self._sync_state != 'off' else t.fg_dim)
        tips = {'off': "Sincronización desactivada — enciende el switch para activarla",
                'idle': "Sincronizar ahora (Asana/ClickUp)",
                'syncing': "Sincronizando…",
                'online': "Sincronizado — clic para sincronizar de nuevo",
                'offline': "Sin conexión — clic para reintentar"}
        self.sync_btn.setToolTip(tips[self._sync_state])

    def set_sync_status(self, text: str) -> None:
        """Muestra la línea de progreso del sync; se oculta sola si queda vacía."""
        self.sync_status.setText(text)
        self.sync_status.setVisible(bool(text.strip()))

    # ---- API para el controller ----
    def set_time_text(self, text: str) -> None:
        self.time_label.setText(text)

    def set_progress(self, fraction: float, animate: bool = True) -> None:
        """Progreso de la sesión (0..1) en el anillo."""
        self.ring.set_fraction(fraction, animate=animate)

    def set_running(self, running: bool) -> None:
        t = tokens()
        self._is_running = running
        self.play_btn.setIcon(tinted_icon("pause" if running else "play", t.on_accent, 26))

    def set_mode(self, is_break: bool) -> None:
        if is_break == self._is_break:
            return
        self._is_break = is_break
        self.ring.set_break(is_break)
        # Crossfade: fundir a 0.25, cambiar contenido, volver a 1
        self._pending_mode = is_break
        self._fade.stop()
        self._fade.setStartValue(self._center_fx.opacity())
        self._fade.setEndValue(0.25)
        self._fade.start()

    def _apply_pending_mode(self) -> None:
        if self._pending_mode is None:
            return
        self._pending_mode = None
        self._style_mode()
        self._style_play()
        self.skip_btn.setVisible(self._is_break)
        fade_in = QPropertyAnimation(self._center_fx, b"opacity", self)
        fade_in.setDuration(300)
        fade_in.setStartValue(0.25)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.InOutCubic)
        fade_in.start(QPropertyAnimation.DeleteWhenStopped)

    def set_active_task(self, name) -> None:
        t = tokens()
        if name:
            self.active_task.set_text(name)
            self.active_task.set_color(t.fg)
            self.complete_btn.show()
        else:
            self.active_task.set_text("Ninguna tarea seleccionada")
            self.active_task.set_color(t.fg_dim)
            self.complete_btn.hide()

    def set_cycle(self, filled: int, total: int) -> None:
        self.cycle_dots.set_progress(filled, total)

    def set_total_today(self, text: str) -> None:
        self.total_today.setText(text)
