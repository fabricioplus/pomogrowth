"""Splash de arranque con barra de progreso determinada.

Se muestra ANTES de renderizar la principal cuando hay auto-sync configurado
(la petición original del usuario en v8, portada a v9).
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui.dialogs.branded_dialogs import SHADOW_MARGINS, soft_shadow
from ui.theme import brand, fonts
from ui.theme.tokens import tokens
from ui.widgets.progress_gradient import ProgressGradient

LOGO_HEIGHT = 44  # alto del logo (mascota + «PomoGrowth»)


class SplashWindow(QWidget):
    def __init__(self):
        super().__init__()
        t = tokens()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("PomoGrowth")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*SHADOW_MARGINS)

        card = QFrame(self)
        card.setObjectName("splashCard")
        card.setStyleSheet(f"#splashCard {{ background: {t.surface}; border: 1px solid"
                           f" {t.border}; border-radius: {t.radius}px; }}")
        soft_shadow(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(28, 26, 28, 22)
        lay.setSpacing(0)

        # Marca: el logo de la casa, con la mascota y «Growth» en el acento
        # del tema y «Pomo» en el color del texto (como en la titlebar)
        logo = QLabel(card)
        logo.setPixmap(brand.logo_pixmap(LOGO_HEIGHT, t.accent, t.fg))
        lay.addWidget(logo, 0, Qt.AlignLeft)
        lay.addSpacing(20)

        self.bar = ProgressGradient(height=6, parent=card)
        self.bar.set_track(t.bg_header, rounded=True)
        lay.addWidget(self.bar)
        lay.addSpacing(10)

        status = QHBoxLayout()
        status.setSpacing(12)
        self.label = QLabel("Preparando tu espacio de trabajo…", card)
        self.label.setFont(fonts.font(fonts.XS))
        self.label.setProperty("role", "dim")
        status.addWidget(self.label, 1)
        self.percent = QLabel("", card)
        self.percent.setFont(fonts.display(fonts.XS))
        self.percent.setProperty("role", "muted")
        self.percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        status.addWidget(self.percent)
        lay.addLayout(status)

        outer.addWidget(card)
        card.setFixedWidth(400)
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

        # Centrar en pantalla
        screen = self.screen() or None
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2,
                      geo.center().y() - self.height() // 2)

    def set_progress(self, fraction: float, text: str = "") -> None:
        self.bar.set_fraction(fraction, animate=True)
        self.percent.setText(f"{round(max(0.0, min(1.0, fraction)) * 100)} %")
        if text:
            self.label.setText(text)
