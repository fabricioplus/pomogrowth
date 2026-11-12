"""Botón de icono SVG con tinte por estado (reemplaza los emoji/PNG de v8)."""
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QToolButton

from ui.theme.brand import device_pixel_ratio
from utils.paths import resource_base

ICONS_DIR = resource_base() / "assets" / "icons"

_icon_cache: dict = {}


def tinted_icon(name: str, color: str, size: int = 18) -> QIcon:
    """Renderiza assets/icons/<name>.svg tintado del color dado, a la
    resolución real de la pantalla (a 110 % un pixmap de 18 px reescalado
    se veía borroso)."""
    dpr = device_pixel_ratio()
    key = (name, color, size, dpr)
    if key in _icon_cache:
        return _icon_cache[key]
    path = ICONS_DIR / f"{name}.svg"
    px = max(1, round(size * dpr))
    pm = QPixmap(px, px)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    if path.exists():
        renderer = QSvgRenderer(str(path))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p, QRectF(0, 0, size, size))
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(QRectF(0, 0, size, size), QColor(color))
        p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon


class IconButton(QToolButton):
    """QToolButton de icono con tinte normal/hover y tooltip."""

    def __init__(self, icon_name: str, tooltip: str = "", *, color: str,
                 hover_color: str, size: int = 18, variant: Optional[str] = None,
                 parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._color = color
        self._hover_color = hover_color
        self._size = size
        self.setIcon(tinted_icon(icon_name, color, size))
        self.setIconSize(QSize(size, size))
        self.setCursor(Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        if variant:
            self.setProperty("variant", variant)

    def set_colors(self, color: str, hover_color: str) -> None:
        self._color, self._hover_color = color, hover_color
        self.setIcon(tinted_icon(self._icon_name, color, self._size))

    def set_icon_name(self, icon_name: str) -> None:
        self._icon_name = icon_name
        self.setIcon(tinted_icon(icon_name, self._color, self._size))

    def enterEvent(self, event):
        self.setIcon(tinted_icon(self._icon_name, self._hover_color, self._size))
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setIcon(tinted_icon(self._icon_name, self._color, self._size))
        super().leaveEvent(event)
