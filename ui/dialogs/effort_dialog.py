"""Popup «¿Cuánto esfuerzo te costó?» al completar una tarea: cinco estrellas
(1 = muy fácil, 5 = muy duro) con vista previa al pasar el ratón.

Rápido de colocar: un clic en una estrella o las teclas 1-5 guardan y
cierran al instante; «Omitir», Esc o un clic en el velo cierran sin guardar.
No es modal (popup(), no exec()): ni el timer ni los tests se quedan
esperando. El resultado llega por finished(int): 1..5, o 0 si se omitió.
"""
from typing import List

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from core.effort_stats import EFFORT_LABELS
from ui.dialogs.branded_dialogs import BrandedDialog
from ui.theme import fonts
from ui.theme.tokens import tokens
from ui.widgets.icon_button import tinted_icon

STAR_PX = 30
_KEYS = {Qt.Key_1: 1, Qt.Key_2: 2, Qt.Key_3: 3, Qt.Key_4: 4, Qt.Key_5: 5}


class _StarButton(QToolButton):
    """Estrella n de la fila: avisa al entrar el ratón (vista previa)."""
    hovered = Signal(int)

    def __init__(self, value: int, parent: QWidget):
        super().__init__(parent)
        self.value = value
        self.setIconSize(QSize(STAR_PX, STAR_PX))
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRaise(True)
        self.setToolTip(f"{value} · {EFFORT_LABELS[value]}")
        self.setAccessibleName(f"Esfuerzo {value} de 5: {EFFORT_LABELS[value]}")
        self.setStyleSheet("QToolButton { background: transparent; border: none;"
                           " padding: 2px; }")

    def enterEvent(self, event):
        self.hovered.emit(self.value)
        super().enterEvent(event)


class _StarRow(QWidget):
    """Cinco estrellas; las de la izquierda de la que está bajo el ratón se
    rellenan. Un clic emite rated(n)."""
    rated = Signal(int)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._preview = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.stars: List[_StarButton] = []
        for n in range(1, 6):
            b = _StarButton(n, self)
            b.hovered.connect(self.set_preview)
            b.clicked.connect(lambda _=False, v=n: self.rated.emit(v))
            lay.addWidget(b)
            self.stars.append(b)
        self._paint()

    @property
    def preview(self) -> int:
        return self._preview

    def set_preview(self, value: int) -> None:
        if value != self._preview:
            self._preview = value
            self._paint()

    def leaveEvent(self, event):
        self.set_preview(0)
        super().leaveEvent(event)

    def _paint(self) -> None:
        t = tokens()
        # Ámbar (el color de aviso del tema): las estrellas se leen como tales
        for b in self.stars:
            filled = b.value <= self._preview
            b.setIcon(tinted_icon("star-fill" if filled else "star",
                                  t.warning if filled else t.fg_muted, STAR_PX))


class EffortDialog(BrandedDialog):
    def __init__(self, parent_window, task_name: str):
        shown = task_name if len(task_name) <= 60 else task_name[:59].rstrip() + "…"
        super().__init__(parent_window, "¿Cuánto esfuerzo te costó?", icon="star",
                         subtitle=shown or None)
        self.rating = 0

        # Estrellas centradas con las etiquetas de los extremos debajo
        block = QWidget(self._card)
        block_lay = QVBoxLayout(block)
        block_lay.setContentsMargins(0, 0, 0, 0)
        block_lay.setSpacing(2)
        self.stars = _StarRow(block)
        self.stars.rated.connect(self._choose)
        block_lay.addWidget(self.stars)
        ends = QHBoxLayout()
        ends.setContentsMargins(2, 0, 2, 0)
        for i, text in enumerate((EFFORT_LABELS[1], EFFORT_LABELS[5])):
            label = QLabel(text, block)
            label.setFont(fonts.font(fonts.XS))
            label.setProperty("role", "muted")
            if i:
                ends.addStretch(1)
            ends.addWidget(label)
        block_lay.addLayout(ends)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(block)
        row.addStretch(1)
        self._lay.addLayout(row)

        self.skip_button = self.add_buttons([("Omitir", "ghost", 0)])[0]
        self.skip_button.setToolTip("Cerrar sin valorar (Esc)")
        # Un clic fuera, en el velo de la ventana, también omite
        if self._overlay is not None:
            self._overlay.clicked.connect(self.reject)
        self.setFocus()

    def _choose(self, value: int) -> None:
        if self.rating:  # ya valorada (doble clic, tecla tras clic)
            return
        self.rating = value
        self.done(value)

    def keyPressEvent(self, event):
        value = _KEYS.get(event.key())
        if value:
            self._choose(value)
            return
        super().keyPressEvent(event)
