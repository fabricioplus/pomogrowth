"""Enlaces discretos que abren una web en el navegador del sistema.

`open_url` es el ÚNICO sitio que llama a QDesktopServices.openUrl (los tests
lo parchean ahí). `LinkButton`: texto pequeño atenuado con icono opcional,
o solo el icono (las redes de la despedida); al pasar el ratón toma el color
de `hover`. Las URLs viven en core/config.py.
"""
from typing import Optional

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QPushButton

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon


def open_url(url: str) -> bool:
    """Abre la URL en el navegador predeterminado."""
    return QDesktopServices.openUrl(QUrl(url))


def _color(value: str) -> str:
    """'#rrggbb' tal cual; si no, el token del tema con ese nombre."""
    return value if value.startswith("#") else getattr(tokens(), value)


class LinkButton(QPushButton):
    """Enlace: `color` en reposo y `hover` al pasar el ratón (tokens del tema
    o '#rrggbb'). Con `url`, el clic la abre y emite `opened`; sin ella es un
    botón de texto más (p. ej. «Cerrar»). Con `chip`, solo el icono, en un
    cuadrado con aire alrededor (más fácil de acertar). Con `pill`, icono y
    texto dentro de una cápsula con borde (una llamada a la acción, como
    «Dar estrella»): el borde también toma el color de `hover`. Nunca coge el foco
    ni es botón por defecto: Enter en un diálogo no abre la web. Se
    re-tematiza con `theme_changed`."""
    opened = Signal(str)
    PILL_HEIGHT = 26

    def __init__(self, text: str = "", url: Optional[str] = None, *,
                 icon: Optional[str] = None, icon_size: int = 14,
                 color: str = "fg_muted", hover: str = "accent",
                 tooltip: Optional[str] = None, chip: bool = False,
                 pill: bool = False, parent=None):
        # Con icono y texto, un espacio delante: Fusion los pega
        super().__init__(f" {text}" if icon and text else text, parent)
        self.url = url
        self._icon_name = icon
        self._icon_size = icon_size
        self._color, self._hover = color, hover
        self._chip = chip
        self._pill = pill
        self._hovered = False
        self.setFont(fonts.font(fonts.XS))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAutoDefault(False)
        if icon:
            self.setIconSize(QSize(icon_size, icon_size))
        if chip:
            self.setFixedSize(icon_size + 14, icon_size + 14)
        if pill:
            self.setFixedHeight(self.PILL_HEIGHT)
        tip = tooltip if tooltip is not None else url
        if tip:
            self.setToolTip(tip)
        self.clicked.connect(self._open)
        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def _open(self) -> None:
        if self.url:
            open_url(self.url)
            self.opened.emit(self.url)

    def _retheme(self) -> None:
        rest, hover = _color(self._color), _color(self._hover)
        if self._chip:
            # Sin fondo al pasar el ratón: solo cambia el color del icono (un
            # fondo teñido le quitaba contraste al azul de LinkedIn)
            self.setStyleSheet("QPushButton { background: transparent; border: none;"
                               " padding: 0; }")
        elif self._pill:
            # Radio < mitad del alto fijo: con la mitad justa Qt deforma las esquinas
            self.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {tokens().border};"
                f" border-radius: {self.PILL_HEIGHT // 2 - 1}px; padding: 0 10px;"
                f" color: {rest}; }}"
                f"QPushButton:hover, QPushButton:pressed {{ color: {hover};"
                f" border-color: {hover}; }}")
        else:
            # font-weight: el QSS global da 500 a los botones; un enlace va normal
            self.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; padding: 2px 0;"
                f" color: {rest}; font-weight: 400; text-align: left; }}"
                f"QPushButton:hover, QPushButton:pressed {{ color: {hover}; }}")
        self._tint()

    def _tint(self) -> None:
        """El icono no sigue el `color` de la hoja de estilos: se re-tiñe."""
        if self._icon_name:
            color = _color(self._hover if self._hovered else self._color)
            self.setIcon(tinted_icon(self._icon_name, color, self._icon_size))

    def enterEvent(self, event):
        self._hovered = True
        self._tint()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._tint()
        super().leaveEvent(event)
