"""Titlebar propia de la ventana frameless, al estilo Windows 11.

Izquierda: marca (la mascota en el acento del tema) + nivel/XP/monedas
(antes una fila aparte de ~40 px).
Derecha: acciones de la app agrupadas y los botones de ventana a la misma
altura que las acciones, en una cápsula con fondo y borde sutiles que los
distingue: minimizar, modo compacto y cerrar (glifos de Segoe Fluent Icons,
el compacto con su icono; cerrar en rojo). Antes iban pegados arriba, al
estilo de Windows, y quedaban por encima del resto de iconos.
"""
from typing import Optional

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from ui.theme import brand, fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import IconButton, tinted_icon

# Glifos de Segoe Fluent Icons / MDL2 (ChromeMinimize, ChromeClose)
_GLYPH_MINIMIZE, _GLYPH_CLOSE = "", ""
# Icono (Lucide) de un botón de la cápsula, del tamaño visual de los glifos:
# a 14 px el dibujo del compacto mide ~12×10, como la «X» de cerrar (a 16
# abultaba más que ella y pesaba casi el doble)
_WINDOW_ICON_SIZE = 14
# La mascota se lee bien a este tamaño en la cabecera de 52 px
LOGO_SIZE = 26


class _WindowButton(QPushButton):
    """Botón de ventana dentro de la cápsula: glifo del sistema o, el del
    modo compacto, su icono tintado como los glifos (fg_dim; fg al pasar el
    ratón)."""

    def __init__(self, tooltip: str, *, glyph: str = "", fallback: str = "",
                 icon: Optional[str] = None, close: bool = False, parent=None):
        super().__init__(parent)
        self._icon_name = icon
        self._hovered = False
        if icon:
            self.setIconSize(QSize(_WINDOW_ICON_SIZE, _WINDOW_ICON_SIZE))
        else:
            glyph_font = fonts.glyph(7.5)
            if glyph_font is not None:
                self.setFont(glyph_font)
                self.setText(glyph)
            else:
                self.setText(fallback)
        self.setToolTip(tooltip)
        self.setFixedSize(34, 28)
        self.setCursor(Qt.ArrowCursor)
        self._close = close
        self.restyle()

    def restyle(self) -> None:
        t = tokens()
        # Sobre el fondo de la cápsula (surface), surface_hover apenas se veía
        hover_bg = "#c42b1c" if self._close else t.surface_active
        hover_fg = "#ffffff" if self._close else t.fg
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" border-radius: {t.radius_small - 1}px; color: {t.fg_dim}; padding: 0; }}"
            f"QPushButton:hover {{ background: {hover_bg}; color: {hover_fg}; }}"
            f"QPushButton:pressed {{ background: {hover_bg}; color: {hover_fg}; }}")
        self._tint()

    def _tint(self) -> None:
        """El icono no sigue el `color` de la hoja de estilos: se re-tiñe."""
        if self._icon_name:
            t = tokens()
            self.setIcon(tinted_icon(self._icon_name, t.fg if self._hovered else t.fg_dim,
                                     _WINDOW_ICON_SIZE))

    def enterEvent(self, event):
        self._hovered = True
        self._tint()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._tint()
        super().leaveEvent(event)


class _AmbientDot(QWidget):
    """Punto discreto en la esquina del botón Música: hay sonidos de ambiente
    sonando (relleno del acento) o armados a la espera del enfoque (solo el
    contorno, atenuado). No intercepta el ratón: el clic es del botón."""
    SIZE = 9

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.state = 'off'
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setVisible(False)

    def set_state(self, state: str) -> None:
        self.state = state
        self.setVisible(state in ('playing', 'waiting'))
        self.update()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.state == 'playing':
            # Borde del color de la cabecera: se despega del icono
            p.setPen(QPen(QColor(t.bg_header), 1.5))
            p.setBrush(QColor(t.accent))
        else:
            p.setPen(QPen(QColor(t.fg_muted), 1.5))
            p.setBrush(QColor(t.bg_header))
        p.drawEllipse(r)
        p.end()


class TitleBar(QWidget):
    minimize_requested = Signal()
    close_requested = Signal()
    compact_requested = Signal()
    stats_requested = Signal()
    settings_requested = Signal()
    music_requested = Signal()
    rewards_requested = Signal()
    focus_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        t = tokens()
        self.setFixedHeight(52)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), t.bg_header)
        self.setPalette(pal)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 10, 0)
        lay.setSpacing(4)

        # Marca: la mascota + el nombre (texto normal; el acento queda para
        # la mascota, que cambia de color con el tema)
        self.logo = QLabel()
        self.logo.setPixmap(brand.mascot_pixmap(t.accent, LOGO_SIZE))
        lay.addWidget(self.logo)
        lay.addSpacing(6)
        self.title = QLabel("PomoGrowth")
        self.title.setFont(fonts.display(fonts.MEDIUM, tabular=False))
        self.title.setStyleSheet(f"color: {t.fg}; background: transparent;")
        lay.addWidget(self.title)
        lay.addSpacing(22)

        # Hueco para la barra de nivel/XP (set_xp_widget): se queda con el
        # espacio libre hasta su tope y solo lo que sobre queda como hueco.
        # (Con factores 3:2 el hueco le quitaba sitio aunque cupiera entera.)
        self._xp_slot = QHBoxLayout()
        self._xp_slot.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self._xp_slot, 1)

        lay.addStretch(0)
        lay.addSpacing(10)  # que las monedas no queden pegadas al trofeo

        def btn(icon, tip, signal, *, enabled=True):
            b = IconButton(icon, tip, color=t.fg_dim, hover_color=t.fg, size=19)
            b.setFixedSize(38, 36)
            b.clicked.connect(signal.emit)
            b.setEnabled(enabled)
            if not enabled:
                b.setToolTip(f"{tip} — próximamente")
            lay.addWidget(b)
            return b

        # Acciones de la app, agrupadas (la música, primera: es la que más se usa)
        self.music_btn = btn("music", "Música", self.music_requested, enabled=False)
        self.rewards_btn = btn("trophy", "Recompensas", self.rewards_requested,
                               enabled=False)
        self.focus_btn = btn("target", "Modo enfoque", self.focus_requested, enabled=False)
        self.stats_btn = btn("estadisticas", "Estadísticas", self.stats_requested,
                             enabled=False)
        self.settings_btn = btn("configurar", "Configuración", self.settings_requested,
                                enabled=False)

        # Botones de ventana: cápsula centrada a la altura de las acciones,
        # con el modo compacto entre minimizar y cerrar (también achica la
        # ventana). El controller lo usa como al resto: compact_btn y
        # compact_requested
        self._window_controls = QFrame()
        self._window_controls.setObjectName("windowControls")
        controls = QHBoxLayout(self._window_controls)
        controls.setContentsMargins(3, 3, 3, 3)
        controls.setSpacing(2)
        self.min_btn = _WindowButton("Minimizar", glyph=_GLYPH_MINIMIZE, fallback="–")
        self.min_btn.clicked.connect(self.minimize_requested.emit)
        self.compact_btn = _WindowButton("Modo compacto", icon="compacto")
        self.compact_btn.clicked.connect(self.compact_requested.emit)
        # Cerrar sale de la app (el controller pregunta antes); seguir en
        # segundo plano es minimizar
        self.close_btn = _WindowButton("Salir de PomoGrowth", glyph=_GLYPH_CLOSE,
                                       fallback="✕", close=True)
        self.close_btn.clicked.connect(self.close_requested.emit)
        self._window_buttons = [self.min_btn, self.compact_btn, self.close_btn]
        for b in self._window_buttons:
            controls.addWidget(b)
        self._style_window_controls()
        lay.addSpacing(8)
        lay.addWidget(self._window_controls, 0, Qt.AlignVCenter)

        self._buttons = [self.music_btn, self.rewards_btn, self.focus_btn, self.stats_btn,
                         self.settings_btn]
        # Sonidos de ambiente: un punto en el botón Música (set_ambient_state)
        self.ambient_dot = _AmbientDot(self.music_btn)
        self.ambient_dot.move(self.music_btn.width() - _AmbientDot.SIZE - 4, 4)
        self._music_tip: Optional[str] = None
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def set_ambient_state(self, state: str, detail: str = '') -> None:
        """'playing' | 'waiting' | 'off'. El punto en el botón Música y, en su
        tooltip, una línea con lo que suena (detail)."""
        if self._music_tip is None:
            self._music_tip = self.music_btn.toolTip()
        self.ambient_dot.set_state(state)
        tip = self._music_tip
        if state == 'playing':
            tip += f"\nAmbiente: {detail}" if detail else "\nAmbiente sonando"
        elif state == 'waiting':
            tip += (f"\nAmbiente al enfocar: {detail}" if detail
                    else "\nAmbiente: sonará al enfocar")
        self.music_btn.setToolTip(tip)

    def set_xp_widget(self, widget: QWidget) -> None:
        """Integra la barra de nivel/XP/monedas en la propia titlebar."""
        self._xp = widget
        self._xp_slot.addWidget(widget)
        self._fit()

    # --- Ajuste al ancho (ventana estrecha o panel de tareas oculto) ---
    def _fit(self) -> None:
        """Cuando no cabe todo, primero se recoge la barra de XP (ella misma
        quita el texto y las monedas), después cede el nombre (queda el logo)
        y, como último recurso, la barra entera. Nada se monta encima."""
        lay = self.layout()
        sp = lay.spacing()
        xp = getattr(self, '_xp', None)
        title_w = self.title.sizeHint().width() + sp
        xp_w = xp.minimumSizeHint().width() + sp if xp is not None else 0
        # Mínimo sin nombre ni barra (los ocultos no cuentan en el layout)
        base = lay.minimumSize().width()
        if not self.title.isHidden():
            base -= title_w
        if xp is not None and not xp.isHidden():
            base -= xp_w
        w = self.width()
        if xp is not None and xp.isHidden() == (w >= base + xp_w):
            xp.setVisible(w >= base + xp_w)
        show_title = w >= base + xp_w + title_w
        if self.title.isHidden() == show_title:
            self.title.setVisible(show_title)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def event(self, e):
        handled = super().event(e)
        # Cambió el contenido (p. ej. «Nv. 10»): volver a comprobar que cabe
        if e.type() == QEvent.LayoutRequest:
            self._fit()
        return handled

    def _style_window_controls(self) -> None:
        t = tokens()
        self._window_controls.setStyleSheet(
            f"#windowControls {{ background: {t.surface}; border: 1px solid {t.border};"
            f" border-radius: {t.radius_small + 2}px; }}")

    def _retheme(self) -> None:
        t = tokens()
        pal = self.palette()
        pal.setColor(self.backgroundRole(), t.bg_header)
        self.setPalette(pal)
        self.logo.setPixmap(brand.mascot_pixmap(t.accent, LOGO_SIZE))
        self.title.setStyleSheet(f"color: {t.fg}; background: transparent;")
        self._style_window_controls()
        for b in self._buttons:
            b.set_colors(t.fg_dim, t.fg)
        self.ambient_dot.update()
        for b in self._window_buttons:
            b.restyle()

    # --- Drag nativo (conserva Aero Snap / shake de Windows) ---
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            win = self.window().windowHandle()
            if win is not None:
                win.startSystemMove()
                return
        super().mousePressEvent(event)
