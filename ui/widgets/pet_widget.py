"""Mascota pixel 8-bit: sprite animado (QPainter, sin assets) + burbuja de chat.

Los sprites son matrices de caracteres pintadas píxel a píxel — estilo consola
antigua, coherente con la regla del proyecto de no depender de emojis/PNGs.
Dos frames por mascota (idle/parpadeo) alternados por QTimer.
"""
from typing import Dict, List, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens

PIXEL = 3  # escala: 1 celda del sprite = 3x3 px reales

# Leyenda: X cuerpo · D cuerpo oscuro · O interior de oreja · E ojo ·
# W hocico/blanco · . transparente
_SPRITES: Dict[str, Dict] = {
    'pet_cat': {
        'palette': {'X': '#f59e0b', 'D': '#b45309', 'O': '#fca5a5',
                    'E': '#14532d', 'W': '#fef3c7'},
        'frames': [
            [
                "..X.....X.......",
                "..XX...XX.......",
                "..XOXXXOX.......",
                "..XXXXXXX.......",
                "..XEXXXEX.......",
                "..XXXWXXX.......",
                "...XXXXX........",
                "..XXXXXXXX...D..",
                ".XXXXXXXXXX..D..",
                ".XXXXXXXXXX.DD..",
                ".XXXXXXXXXXDD...",
                "..DX..DX........",
            ],
            [
                "..X.....X.......",
                "..XX...XX.......",
                "..XOXXXOX.......",
                "..XXXXXXX.......",
                "..XDXXXDX.......",
                "..XXXWXXX.......",
                "...XXXXX........",
                "..XXXXXXXX......",
                ".XXXXXXXXXX.D...",
                ".XXXXXXXXXX.D...",
                ".XXXXXXXXXXDD...",
                "..DX..DX........",
            ],
        ],
    },
    'pet_dog': {
        'palette': {'X': '#a1887f', 'D': '#6d4c41', 'O': '#d7ccc8',
                    'E': '#1e293b', 'W': '#3e2723'},
        'frames': [
            [
                ".DD.......DD....",
                ".DDX.....XDD....",
                ".DXXXXXXXXXD....",
                ".XXXXXXXXXXX....",
                ".XXEXXXXXEXX....",
                ".XXXXWWXXXXX....",
                "..XXXWWXXXX.....",
                "..XXXXXXXXXX.D..",
                ".XXXXXXXXXXXXD..",
                ".XXXXXXXXXXXX...",
                ".XXXXXXXXXXXX...",
                "..XX.....XX.....",
            ],
            [
                ".DD.......DD....",
                ".DDX.....XDD....",
                ".DXXXXXXXXXD....",
                ".XXXXXXXXXXX....",
                ".XXDXXXXXDXX....",
                ".XXXXWWXXXXX....",
                "..XXXWWXXXX..D..",
                "..XXXXXXXXXX.D..",
                ".XXXXXXXXXXXX...",
                ".XXXXXXXXXXXX...",
                ".XXXXXXXXXXXX...",
                "..XX.....XX.....",
            ],
        ],
    },
    'pet_rat': {
        'palette': {'X': '#90a4ae', 'D': '#546e7a', 'O': '#f8bbd0',
                    'E': '#212121', 'W': '#eceff1'},
        'frames': [
            [
                ".XX.....XX......",
                "XOOX...XOOX.....",
                "XOOXXXXXOOX.....",
                ".XXXXXXXXX......",
                ".XEXXXXXEX......",
                ".XXXXWXXXX......",
                "..XXXXXXXX......",
                ".XXXXXXXXXX.....",
                ".XXXXXXXXXXX....",
                ".XXXXXXXXXX.DDD.",
                "..XXXXXXXX.D....",
                "...XX...XX......",
            ],
            [
                ".XX.....XX......",
                "XOOX...XOOX.....",
                "XOOXXXXXOOX.....",
                ".XXXXXXXXX......",
                ".XDXXXXXDX......",
                ".XXXXWXXXX......",
                "..XXXXXXXX......",
                ".XXXXXXXXXX.....",
                ".XXXXXXXXXXX.DD.",
                ".XXXXXXXXXX.D...",
                "..XXXXXXXX.D....",
                "...XX...XX......",
            ],
        ],
    },
}


class PetSprite(QWidget):
    """Sprite 8-bit animado; clic para hablar con la mascota."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._species = 'pet_cat'
        self._frame = 0
        cols = len(_SPRITES['pet_cat']['frames'][0][0])
        rows = len(_SPRITES['pet_cat']['frames'][0])
        self.setFixedSize(cols * PIXEL, rows * PIXEL)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QTimer(self)
        self._anim.setInterval(700)
        self._anim.timeout.connect(self._next_frame)
        self._anim.start()

    def set_species(self, species: str) -> None:
        if species in _SPRITES:
            self._species = species
            self._frame = 0
            self.update()

    def _next_frame(self) -> None:
        if not self.isVisible():
            return
        self._frame = (self._frame + 1) % len(_SPRITES[self._species]['frames'])
        self.update()

    def paintEvent(self, event):
        spec = _SPRITES[self._species]
        grid: List[str] = spec['frames'][self._frame]
        palette = spec['palette']
        p = QPainter(self)
        for y, row in enumerate(grid):
            for x, ch in enumerate(row):
                color = palette.get(ch)
                if color:
                    p.fillRect(x * PIXEL, y * PIXEL, PIXEL, PIXEL, QColor(color))
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PetWidget(QWidget):
    """Fila mascota: sprite + burbuja de mensajes con autocierre."""
    advice_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        t = tokens()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(10)

        self.sprite = PetSprite(self)
        self.sprite.clicked.connect(self.advice_requested.emit)
        lay.addWidget(self.sprite, 0, Qt.AlignBottom)

        self.bubble = QLabel("", self)
        self.bubble.setFont(fonts.font(fonts.XS))
        self.bubble.setWordWrap(True)
        self.bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._style_bubble()
        self.bubble.hide()
        lay.addWidget(self.bubble, 1)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.bubble.hide)
        self._pending_ms = 0  # autocierre pospuesto hasta ser visible
        ThemeManager.instance().theme_changed.connect(self._style_bubble)

    def _style_bubble(self) -> None:
        t = tokens()
        self.bubble.setStyleSheet(
            f"background: {t.surface}; color: {t.fg}; border: 1px solid {t.border};"
            f" border-radius: 8px; padding: 6px 9px;")

    def set_species(self, species: str, name: str) -> None:
        self.sprite.set_species(species)
        self.sprite.setToolTip(f"{name} — clic para pedirle un consejo")

    def show_message(self, text: str, duration_ms: int = 14000) -> None:
        self.bubble.setText(text)
        self.bubble.show()
        # No consumir el tiempo con la ventana oculta (p. ej. el saludo de
        # arranque durante el splash del auto-sync): arrancar al mostrarse
        if self.isVisible():
            self._hide_timer.start(duration_ms)
            self._pending_ms = 0
        else:
            self._pending_ms = duration_ms

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending_ms and not self.bubble.isHidden():
            self._hide_timer.start(self._pending_ms)
            self._pending_ms = 0
