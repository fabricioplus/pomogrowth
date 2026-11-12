"""Nivel, progreso de XP y monedas — compacto, para vivir en la titlebar.

Si la cabecera va justa (ventana estrecha o panel de tareas oculto) se recoge
por orden: primero el texto de XP y después las monedas, que pasan al tooltip.
Nunca se monta un elemento sobre otro.
"""
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon
from ui.widgets.progress_gradient import ProgressGradient

_GOLD = '#f2c94c'
# Ancho de la barra: mínimo legible, con monedas y con todo a la vista
_BAR_MIN, _BAR_ROOMY, _BAR_FULL = 40, 90, 160


class XpBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        t = tokens()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # Chip de nivel
        self.level_label = QLabel("Nv. 1")
        self.level_label.setFont(fonts.font(fonts.SMALL, bold=True))
        self.level_label.setAlignment(Qt.AlignCenter)
        self.level_label.setFixedHeight(26)
        self.level_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        lay.addWidget(self.level_label, 0, Qt.AlignVCenter)

        self.bar = ProgressGradient(height=8)
        # Crece con el espacio libre de la cabecera, con tope para no ser una regla
        self.bar.setMinimumWidth(_BAR_MIN)
        self.bar.setMaximumWidth(380)
        # Degradado dorado fijo: es la barra de progreso hacia el siguiente nivel
        self.bar.set_custom_colors('#f7e08a', '#d19a00')
        self.bar.set_track(t.surface_active, rounded=True)
        lay.addWidget(self.bar, 1, Qt.AlignVCenter)

        self.xp_label = QLabel("0 / 100 XP")
        self.xp_label.setFont(fonts.font(fonts.SMALL))
        self.xp_label.setProperty("role", "muted")
        lay.addWidget(self.xp_label)
        # Textos a su medida: el sitio de sobra es de la barra (y su tope, el
        # de todo el bloque)
        self.xp_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

        lay.addSpacing(6)
        self.coins_icon = QLabel()
        self.coins_icon.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        lay.addWidget(self.coins_icon)
        self.coins_label = QLabel("0")
        self.coins_label.setFont(fonts.font(fonts.SMALL, bold=True))
        self.coins_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.coins_label.setToolTip("Monedas para la tienda de recompensas")
        lay.addWidget(self.coins_label)
        self._cap_width()
        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def _retheme(self) -> None:
        t = tokens()
        if hasattr(self, 'bar'):
            self.bar.set_track(t.surface_active, rounded=True)
        self.level_label.setStyleSheet(
            f"color: {t.accent}; background: {t.surface}; border: 1px solid {t.border};"
            f" border-radius: 12px; padding: 1px 10px;")
        self.coins_icon.setPixmap(tinted_icon("coins", _GOLD, 18).pixmap(18, 18))
        self.coins_label.setStyleSheet(f"color: {_GOLD};")

    # --- Ajuste al ancho disponible ---
    def _widths(self) -> tuple:
        """(mínimo, con monedas, completo) según el contenido actual. Se mide con
        los sizeHint de cada pieza, que valen aunque esté oculta."""
        sp = self.layout().spacing()
        low = self.level_label.sizeHint().width() + sp
        coins = (6 + sp + self.coins_icon.sizeHint().width()
                 + sp + self.coins_label.sizeHint().width())
        text = sp + self.xp_label.sizeHint().width()
        return (low + _BAR_MIN, low + _BAR_ROOMY + coins,
                low + _BAR_FULL + text + coins)

    def _cap_width(self) -> None:
        # Tope del bloque = todo a la vista con la barra en su máximo; lo que
        # sobre en la cabecera queda como hueco, no dentro del bloque
        self.setMaximumWidth(self._widths()[2] - _BAR_FULL + self.bar.maximumWidth())

    def minimumSizeHint(self) -> QSize:
        return QSize(self._widths()[0], super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        # Siempre el tamaño completo: si dependiera de lo que está oculto, la
        # cabecera oscilaría entre recoger y desplegar
        return QSize(self._widths()[2], super().sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        _, with_coins, full = self._widths()
        for widget, show in ((self.xp_label, self.width() >= full),
                             (self.coins_icon, self.width() >= with_coins),
                             (self.coins_label, self.width() >= with_coins)):
            if widget.isHidden() == show:
                widget.setVisible(show)

    def update_from_stats(self, stats: dict, xp_level_base: int) -> None:
        """stats: dict de RewardsSystem.get_stats(); xp_level_base: XP acumulada
        donde empezó el nivel actual (para la fracción dentro del nivel)."""
        level = stats.get('level', 1)
        xp = stats.get('xp', 0)
        xp_next = stats.get('xp_for_next_level', xp) or xp
        coins = stats.get('coins', 0)
        self.level_label.setText(f"Nv. {level}")
        self.coins_label.setText(f"{coins}")
        span = max(1, xp_next - xp_level_base)
        self.xp_label.setText(f"{xp} / {xp_next} XP")
        self.bar.set_fraction((xp - xp_level_base) / span if xp_next > xp_level_base else 1.0)
        # Lo que se recoja cuando falte sitio sigue a mano al pasar el ratón
        self.setToolTip(f"Nivel {level} · {xp} / {xp_next} XP · {coins} monedas")
        self._cap_width()
        self.updateGeometry()
        self._fit()
