"""Mezclador de sonidos de ambiente (lluvia, océano, ruidos…), al estilo Noisli.

Vive en el diálogo de Música: la entrada fija «Sonidos de ambiente» de la
columna de listas lo muestra en el hueco del panel de la lista. Vista sobre
AmbientService (services/ambient_service.py), que hace todo el trabajo:
- Rejilla de tarjetas por grupo (naturaleza y ruidos): icono + nombre; un
  clic (o Espacio/Enter) enciende o apaga el sonido. Encendida, borde y
  ficha en el acento con unas ondas que se expanden y su deslizador de
  volumen; apagada, una línea que describe el sonido. Sin su archivo, la
  tarjeta sale desactivada y el tooltip lo dice.
- Cabecera: qué suena y «Reanudar» (la última mezcla: al abrir la app no
  suena nada sola) o «Parar todo».
- Pie: volumen general y «Solo durante el enfoque» (propio del ambiente).
La barra «Ahora suena» del diálogo sigue siendo la de la música: los dos se
mezclan. Colores solo de los tokens del tema; se re-tematiza en caliente.
"""
from typing import Dict, List

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QVBoxLayout, QWidget)

from core import ambient_catalog as catalog
from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon
from ui.widgets.mini_player import ElidedLabel
from ui.widgets.toggle_switch import ToggleSwitch

AMBIENT_ID = '__ambient__'          # la entrada fija de la columna de listas
AMBIENT_NAME = "Sonidos de ambiente"
AMBIENT_ICON = "waves"
GROUP_TITLES = {catalog.GROUP_NATURE: "Naturaleza", catalog.GROUP_NOISE: "Ruidos"}
COLUMNS = 3
CARD_H = 84
CHIP = 34
ANIM_MS = 50                        # ondas de las tarjetas que suenan
ANIM_PERIOD_S = 2.4                 # una onda tarda esto en expandirse
FOCUS_TIP = ("El ambiente solo suena con un pomodoro o una sesión Libre en marcha: "
             "se pausa en los descansos y al pausar o parar el timer, y vuelve al "
             "reanudar el enfoque.")
WAITING_TEXT = "Sonará al enfocar"


def _blend(a: str, b: str, f: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * f),
                  round(ca.green() + (cb.green() - ca.green()) * f),
                  round(ca.blue() + (cb.blue() - ca.blue()) * f))


def _alpha(color: str, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


def _names(ids: List[str]) -> str:
    return ", ".join(catalog.get(s).name for s in ids if catalog.get(s))


# ================================================================ tarjeta
class _CardSlider(QSlider):
    """Deslizador de una tarjeta: la rueda solo lo mueve con el foco puesto;
    sin él, la rueda sigue hasta el scroll del mezclador (si no, al bajar
    con la rueda se cambiaba el volumen del sonido que pasaba por debajo)."""

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class AmbientCard(QAbstractButton):
    """Un sonido: clic (o Espacio/Enter) lo enciende o apaga. Pintada a mano;
    el deslizador de volumen es un hijo de verdad (teclado, rueda con el foco
    puesto, arrastre)."""
    toggle_requested = Signal(str)
    volume_changed = Signal(str, int)

    def __init__(self, sound: catalog.AmbientSound, parent: QWidget):
        super().__init__(parent)
        self.sound = sound
        self.state = 'off'          # 'off' | 'playing' | 'waiting' | 'unavailable'
        self.phase = 0.0            # 0..1, avance de las ondas
        self._hover = False
        self.setObjectName(f"ambientCard_{sound.id}")
        self.setAccessibleName(sound.name)
        self.setCursor(Qt.PointingHandCursor)
        # Solo con Tab: un clic no deja el anillo de foco pintado
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedHeight(CARD_H)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.clicked.connect(lambda: self.toggle_requested.emit(self.sound.id))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12 + CHIP + 6, 12, 8)
        lay.setSpacing(0)
        self.slider = _CardSlider(Qt.Horizontal, self)
        self.slider.setFocusPolicy(Qt.StrongFocus)   # sin WheelFocus: ver _CardSlider
        self.slider.setRange(0, 100)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.setAccessibleName(f"Volumen de {sound.name}")
        self.slider.valueChanged.connect(self._on_slider)
        lay.addWidget(self.slider)
        policy = self.slider.sizePolicy()
        policy.setRetainSizeWhenHidden(True)   # la tarjeta no cambia de alto
        self.slider.setSizePolicy(policy)
        self.slider.setVisible(False)

    @property
    def active(self) -> bool:
        return self.state in ('playing', 'waiting')

    def set_state(self, state: str, volume: int, tooltip: str) -> None:
        changed = state != self.state
        self.state = state
        self.setEnabled(state != 'unavailable')
        self.setToolTip(tooltip)
        self.slider.setVisible(self.active)
        if not self.slider.isSliderDown() and self.slider.value() != volume:
            self.slider.blockSignals(True)
            self.slider.setValue(volume)
            self.slider.blockSignals(False)
        self.slider.setToolTip(f"Volumen de {self.sound.name}: {self.slider.value()} %")
        if changed:
            self.update()

    def _on_slider(self, value: int) -> None:
        self.slider.setToolTip(f"Volumen de {self.sound.name}: {value} %")
        self.volume_changed.emit(self.sound.id, value)

    def sizeHint(self) -> QSize:
        return QSize(180, CARD_H)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.isEnabled():
            # Una vez por pulsación: con Enter mantenido, la autorrepetición
            # encendía y apagaba el sonido sin parar (como Espacio, que
            # QAbstractButton ya filtra)
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def hitButton(self, pos) -> bool:
        """Encendida, la franja del deslizador no apaga el sonido: un clic
        que se queda a 2 px del deslizador no debe cortarlo."""
        if self.active and pos.y() >= 12 + CHIP + 6:
            return False
        return super().hitButton(pos)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        state = self.state
        active = self.active
        # Fondo y borde: encendida, un velo del acento y el borde de la marca
        if active:
            bg = _blend(t.surface, t.accent, 0.10 if state == 'playing' else 0.05)
            border = QColor(t.accent) if state == 'playing' else _blend(t.border, t.accent, 0.55)
            width = 1.5
        else:
            bg = QColor(t.surface_hover if self._hover and self.isEnabled() else t.surface)
            border = QColor(t.fg_muted if self._hover and self.isEnabled() else t.border)
            width = 1.0
        pen = QPen(border, width)
        if state == 'unavailable':
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(bg)
        p.drawRoundedRect(r, t.radius, t.radius)
        if self.hasFocus():
            p.setPen(QPen(_alpha(t.accent, 160), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(2, 2, -2, -2), t.radius - 2, t.radius - 2)

        # Ficha del icono (círculo) con las ondas detrás si suena
        cx, cy = 12 + CHIP / 2, 12 + CHIP / 2
        if state == 'playing':
            for k in (0.0, 0.5):
                f = (self.phase + k) % 1.0
                radius = CHIP / 2 + 1 + f * 9
                p.setPen(QPen(_alpha(t.accent, 110 * (1 - f)), 1.4))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius))
        if active:
            chip_bg = _alpha(t.accent, 46 if state == 'playing' else 26)
            icon_color = t.accent if state == 'playing' else t.fg_dim
        else:
            chip_bg = QColor(t.surface_active)
            icon_color = t.fg_muted if state == 'unavailable' else t.fg_dim
        p.setPen(Qt.NoPen)
        p.setBrush(chip_bg)
        p.drawEllipse(QRectF(cx - CHIP / 2, cy - CHIP / 2, CHIP, CHIP))
        icon = 18
        tinted_icon(self.sound.icon, icon_color, icon).paint(
            p, int(cx - icon / 2), int(cy - icon / 2), icon, icon)

        # Nombre y la línea de estado (encendida) o la descripción (apagada)
        x = 12 + CHIP + 10
        width = r.right() - 10 - x
        name_font = fonts.font(fonts.SMALL, bold=True)
        p.setFont(name_font)
        p.setPen(QColor(t.fg_muted if state == 'unavailable' else t.fg))
        p.drawText(QRectF(x, 12, width, CHIP / 2 + 1), Qt.AlignLeft | Qt.AlignBottom,
                   QFontMetrics(name_font).elidedText(self.sound.name, Qt.ElideRight,
                                                      int(width)))
        sub_font = fonts.font(fonts.XS)
        if state == 'playing':
            sub, color = f"Sonando · {self.slider.value()} %", t.accent
        elif state == 'waiting':
            sub, color = WAITING_TEXT, t.info
        elif state == 'unavailable':
            sub, color = "No disponible", t.fg_muted
        else:
            sub, color = "Clic para activar", t.fg_muted
        p.setFont(sub_font)
        p.setPen(QColor(color))
        p.drawText(QRectF(x, 12 + CHIP / 2 + 1, width, CHIP / 2), Qt.AlignLeft | Qt.AlignTop,
                   QFontMetrics(sub_font).elidedText(sub, Qt.ElideRight, int(width)))
        if not active:
            # Donde irá el deslizador: qué es el sonido
            band = QRectF(12, 12 + CHIP + 6, r.width() - 22, r.bottom() - (12 + CHIP + 6) - 4)
            p.setPen(QColor(t.fg_muted))
            p.drawText(band, Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(sub_font).elidedText(self.sound.hint, Qt.ElideRight,
                                                         int(band.width())))
        p.end()


# ================================================================ el mezclador
class AmbientMixer(QFrame):
    """Panel del mezclador. refresh() lo pone al día con el servicio (lo
    llama él mismo con changed); disconnect_live() suelta sus señales."""

    def __init__(self, ambient, parent: QWidget):
        super().__init__(parent)
        self.ambient = ambient
        self._live = True
        self.setObjectName("ambientPanel")
        self.cards: Dict[str, AmbientCard] = {}
        self._group_labels: List[QLabel] = []

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        # Cabecera: título, qué suena y «Reanudar»/«Parar todo»
        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel(AMBIENT_NAME, self)
        self.title.setFont(fonts.display(fonts.MEDIUM, tabular=False))
        titles.addWidget(self.title)
        self.meta = ElidedLabel('', self)
        self.meta.setFont(fonts.font(fonts.XS))
        titles.addWidget(self.meta)
        head.addLayout(titles, 1)
        self.main_btn = QPushButton(self)
        self.main_btn.setCursor(Qt.PointingHandCursor)
        self.main_btn.setAutoDefault(False)
        self.main_btn.setIconSize(QSize(14, 14))
        self.main_btn.setMinimumWidth(124)
        self.main_btn.clicked.connect(self._main_action)
        head.addWidget(self.main_btn, 0, Qt.AlignVCenter)
        v.addLayout(head)

        # Tarjetas por grupo, dentro de un scroll sin marco (el diálogo a su
        # tamaño mínimo no tiene alto para las tres filas)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.NoFocus)   # sin parada de Tab invisible
        page = QWidget(self.scroll)
        page.setObjectName("ambientPage")
        self.scroll.setWidget(page)
        grid = QVBoxLayout(page)
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setSpacing(6)
        for group in (catalog.GROUP_NATURE, catalog.GROUP_NOISE):
            label = QLabel(GROUP_TITLES[group], page)
            label.setFont(fonts.font(fonts.XS, bold=True))
            self._group_labels.append(label)
            grid.addWidget(label)
            cells = QGridLayout()
            cells.setHorizontalSpacing(10)
            cells.setVerticalSpacing(10)
            sounds = [s for s in catalog.SOUNDS if s.group == group]
            for n, sound in enumerate(sounds):
                card = AmbientCard(sound, page)
                card.toggle_requested.connect(self._toggle)
                card.volume_changed.connect(ambient.set_volume)
                card.installEventFilter(self)
                card.slider.installEventFilter(self)
                self.cards[sound.id] = card
                cells.addWidget(card, n // COLUMNS, n % COLUMNS)
            for col in range(COLUMNS):
                cells.setColumnStretch(col, 1)
            grid.addLayout(cells)
            grid.addSpacing(4)
        grid.addStretch(1)
        v.addWidget(self.scroll, 1)

        # Pie: volumen general y «solo durante el enfoque»
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.master_icon = QLabel(self)
        self.master_icon.setFixedSize(16, 16)
        foot.addWidget(self.master_icon)
        self.master_label = QLabel("Volumen general", self)
        self.master_label.setFont(fonts.font(fonts.XS))
        foot.addWidget(self.master_label)
        self.master = QSlider(Qt.Horizontal, self)
        self.master.setRange(0, 100)
        self.master.setFixedWidth(140)
        self.master.setCursor(Qt.PointingHandCursor)
        self.master.setAccessibleName("Volumen general del ambiente")
        self.master.setValue(ambient.master())
        self.master.valueChanged.connect(self._on_master)
        foot.addWidget(self.master)
        foot.addStretch(1)
        self.focus_label = QLabel("Solo durante el enfoque", self)
        self.focus_label.setFont(fonts.font(fonts.XS))
        self.focus_label.setToolTip(FOCUS_TIP)
        foot.addWidget(self.focus_label)
        self.focus_toggle = ToggleSwitch(FOCUS_TIP, parent=self)
        self.focus_toggle.setChecked(ambient.focus_only())
        self.focus_toggle.toggled.connect(ambient.set_focus_only)
        foot.addWidget(self.focus_toggle)
        v.addLayout(foot)

        self._anim = QTimer(self)
        self._anim.setInterval(ANIM_MS)
        self._anim.timeout.connect(self._advance)
        self._connections = [(ambient.changed, self.refresh),
                             (ThemeManager.instance().theme_changed, self.restyle)]
        for signal, slot in self._connections:
            signal.connect(slot)
        self.restyle()

    # ---------- acciones ----------
    def _toggle(self, sound_id: str) -> None:
        self.ambient.toggle(sound_id)

    def _main_action(self) -> None:
        if self.ambient.active_ids():
            self.ambient.stop_all()
        else:
            self.ambient.resume_last()

    def _on_master(self, value: int) -> None:
        self.ambient.set_master(value)
        self.master.setToolTip(f"Volumen general: {value} %")

    # ---------- estado ----------
    def refresh(self) -> None:
        if not self._live:
            return
        t = tokens()
        amb = self.ambient
        for sound_id, card in self.cards.items():
            state = amb.sound_state(sound_id)
            card.set_state(state, amb.volume(sound_id), self._card_tip(sound_id, state))
        active = amb.active_ids()
        state = amb.state()
        if not any(amb.available(s.id) for s in catalog.SOUNDS):
            meta, color = "No se encontraron los audios de ambiente", t.warning
        elif state == 'playing':
            meta, color = f"Sonando: {_names(active)}", t.accent
        elif state == 'waiting':
            meta, color = f"{WAITING_TEXT}: {_names(active)}", t.info
        else:
            meta, color = "Elige uno o varios y mézclalos con tu música", t.fg_muted
        self.meta.set_full_text(meta)
        self.meta.setToolTip(meta if self.meta.is_elided() else '')
        self.meta.setStyleSheet(f"color: {color};")
        stop = bool(active)
        self.main_btn.setText("Parar todo" if stop else "Reanudar")
        self.main_btn.setProperty("variant", "ghost" if stop else "accent")
        self.main_btn.setEnabled(stop or amb.can_resume())
        self.main_btn.style().unpolish(self.main_btn)
        self.main_btn.style().polish(self.main_btn)
        if stop:
            self.main_btn.setIcon(tinted_icon("pause", t.fg_dim, 14))
            self.main_btn.setToolTip("Apagar todos los sonidos (con un fundido)")
        else:
            enabled = self.main_btn.isEnabled()
            self.main_btn.setIcon(tinted_icon("play", t.on_accent if enabled else t.fg_muted, 14))
            last = [s for s in amb.last_mix() if amb.available(s)]
            self.main_btn.setToolTip(f"Volver a la última mezcla: {_names(last)}" if last
                                     else "Activa algún sonido: después podrás reanudar "
                                          "esa mezcla")
        if not self.master.isSliderDown() and self.master.value() != amb.master():
            self.master.blockSignals(True)
            self.master.setValue(amb.master())
            self.master.blockSignals(False)
        self.master.setToolTip(f"Volumen general: {self.master.value()} %")
        if self.focus_toggle.isChecked() != amb.focus_only():
            self.focus_toggle.blockSignals(True)
            self.focus_toggle.setChecked(amb.focus_only())
            self.focus_toggle.blockSignals(False)
        self._sync_anim()

    def _card_tip(self, sound_id: str, state: str) -> str:
        sound = catalog.get(sound_id)
        if state == 'unavailable':
            return f"{sound.name}: {self.ambient.unavailable_reason(sound_id).lower()}"
        if state == 'playing':
            return f"{sound.name}: sonando. Clic para apagarlo"
        if state == 'waiting':
            return f"{sound.name}: {WAITING_TEXT.lower()}. Clic para apagarlo"
        return f"{sound.name}: {sound.hint.lower()}. Clic para activarlo"

    def _sync_anim(self) -> None:
        want = self.isVisible() and any(c.state == 'playing' for c in self.cards.values())
        if want and not self._anim.isActive():
            self._anim.start()
        elif not want and self._anim.isActive():
            self._anim.stop()

    def _advance(self) -> None:
        step = ANIM_MS / 1000.0 / ANIM_PERIOD_S
        for card in self.cards.values():
            if card.state == 'playing':
                card.phase = (card.phase + step) % 1.0
                card.update()

    def eventFilter(self, obj, event):
        # Con el teclado (Tab o Shift+Tab desde fuera del scroll), la tarjeta
        # que coge el foco se desplaza a la vista
        if event.type() == QEvent.FocusIn:
            card = obj if isinstance(obj, AmbientCard) else obj.parentWidget()
            self.scroll.ensureWidgetVisible(card, 0, 8)
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._anim.stop()

    def restyle(self) -> None:
        if not self._live:
            return
        t = tokens()
        self.setStyleSheet(
            f"#ambientPanel {{ background: {t.bg}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; }}"
            f"#ambientPage {{ background: transparent; }}")
        self.scroll.viewport().setStyleSheet("background: transparent;")
        self.title.setStyleSheet(f"color: {t.fg};")
        for label in self._group_labels:
            label.setStyleSheet(f"color: {t.fg_muted};")
        self.master_icon.setPixmap(tinted_icon("volume-2", t.fg_dim, 16).pixmap(16, 16))
        self.master_label.setStyleSheet(f"color: {t.fg_dim};")
        self.focus_label.setStyleSheet(f"color: {t.fg_dim};")
        for card in self.cards.values():
            card.update()
        self.refresh()

    def focus_chain(self) -> List[QWidget]:
        """Orden de Tab (el visual): «Reanudar», cada tarjeta con su
        deslizador, volumen general y el interruptor del enfoque."""
        chain: List[QWidget] = [self.main_btn]
        for card in self.cards.values():
            chain += [card, card.slider]
        return chain + [self.master, self.focus_toggle]

    def disconnect_live(self) -> None:
        if not self._live:
            return
        self._live = False
        self._anim.stop()
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
