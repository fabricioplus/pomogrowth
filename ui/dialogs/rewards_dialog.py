"""Centro de recompensas: tienda, inventario (equipar) y progreso.

Port de rewards_view (v8) sobre Qt. Comprar un tema y equiparlo re-tematiza
la app EN CALIENTE vía ThemeManager (sin reiniciar).

Cada artículo lleva una vista previa pintada con QPainter (sin assets ni
emojis): el tema en miniatura con su propia paleta, la mascota con su sprite
pixel, y un motivo propio para fondos, sonidos e iconos.

La música ya no vive aquí: tiene su propio diálogo (ui/dialogs/music_dialog.py,
botón «Música» de la cabecera).
"""
import math
import random

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QLinearGradient, QPainter,
                           QPainterPath, QPen, QRadialGradient)
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QTabWidget,
                               QVBoxLayout, QWidget)

from core.rewards import ShopItem
from ui.dialogs.branded_dialogs import BrandedDialog
from ui.theme import fonts
from ui.theme.tokens import THEMES, ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon
from ui.widgets.progress_ring import ProgressRing

try:  # los mismos sprites 8-bit de la mascota que acompaña al timer
    from ui.widgets.pet_widget import _SPRITES as _PET_SPRITES
except ImportError:  # pragma: no cover - sin sprites: vista previa genérica
    _PET_SPRITES = {}

CATEGORIES = [("all", "Todos"), ("theme", "Temas"), ("icon_pack", "Iconos"),
              ("sound_pack", "Sonidos"), ("background", "Fondos"),
              ("pet", "Mascotas")]
SLOT_FIELDS = {'theme': 'current_theme', 'icon_pack': 'current_icon_pack',
               'background': 'current_background', 'sound_pack': 'current_sound_pack',
               'pet': 'current_pet'}
# Pestañas (icono, título)
TABS = [("shopping-bag", "Tienda"), ("backpack", "Inventario"), ("star", "Progreso")]
CATEGORY_ICONS = {'theme': 'palette', 'icon_pack': 'sparkles',
                  'sound_pack': 'audio-lines', 'background': 'image',
                  'pet': 'paw-print'}
INVENTORY_SECTIONS = [('theme', "Temas"), ('pet', "Mascotas"),
                      ('sound_pack', "Sonidos"), ('icon_pack', "Iconos"),
                      ('background', "Fondos")]
# El tema original como artículo del inventario: siempre en propiedad, para
# poder volver a él tras probar uno de la tienda
DEFAULT_THEME = ShopItem('default', 'Clásico', 'El tema original', 0, 'theme')
GOLD = '#f2c94c'  # el dorado de las monedas de la titlebar (XpBar)
SHOP_COLUMNS = 3


def _fmt_minutes(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _qcolor(color: str, alpha: int = 255) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def _label(parent: QWidget, text: str, font, role: str = None,
           color: str = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text, parent)
    lab.setFont(font)
    if role:
        lab.setProperty("role", role)
    if color:
        lab.setStyleSheet(f"color: {color};")
    lab.setWordWrap(wrap)
    return lab


def _icon_label(parent: QWidget, name: str, color: str, size: int) -> QLabel:
    lab = QLabel(parent)
    lab.setPixmap(tinted_icon(name, color, size).pixmap(size, size))
    lab.setFixedSize(size, size)
    return lab


def _icon_tile(parent: QWidget, name: str, color: str, size: int = 32,
               icon_size: int = 16, tinted: bool = False) -> QLabel:
    """Icono en una ficha redondeada: neutra, o del color del icono al 15 %."""
    t = tokens()
    tile = QLabel(parent)
    tile.setFixedSize(size, size)
    tile.setAlignment(Qt.AlignCenter)
    tile.setPixmap(tinted_icon(name, color, icon_size).pixmap(icon_size, icon_size))
    if tinted:
        c = QColor(color)
        bg = f"rgba({c.red()}, {c.green()}, {c.blue()}, 38)"
    else:
        bg = t.surface_active
    tile.setStyleSheet(f"background: {bg}; border-radius: {t.radius_small}px;")
    return tile


def _clear_layout(layout) -> None:
    """Vacía un layout para repoblarlo sin cambiar de contenedor (así el
    scroll conserva su posición al comprar o equipar)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


class _Divider(QWidget):
    """Separador de 1 px entre filas de una tarjeta (pintado: sigue al tema)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(tokens().border))
        p.end()


class ItemPreview(QWidget):
    """Miniatura pintada de un artículo: el tema con su paleta real, la
    mascota con su sprite (con parpadeo) y un motivo para el resto."""

    def __init__(self, item: ShopItem, height: int = 92, parent=None):
        super().__init__(parent)
        self._item = item
        self._frame = 0
        self.setFixedHeight(height)
        self.setMinimumWidth(88)
        self._blink = None
        if item.category == 'pet' and item.id in _PET_SPRITES:
            # Parpadeo idle, como la mascota junto al timer
            self._blink = QTimer(self)
            self._blink.setInterval(700)
            self._blink.timeout.connect(self._next_frame)

    def showEvent(self, event):
        super().showEvent(event)
        if self._blink:
            self._blink.start()

    def hideEvent(self, event):
        if self._blink:
            self._blink.stop()
        super().hideEvent(event)

    def _next_frame(self) -> None:
        self._frame = (self._frame + 1) % len(_PET_SPRITES[self._item.id]['frames'])
        self.update()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        rad = t.radius_small
        clip = QPainterPath()
        clip.addRoundedRect(r, rad, rad)
        p.setClipPath(clip)
        cat, key = self._item.category, self._item.id
        if cat == 'theme' and key in THEMES:
            self._paint_theme(p, r, THEMES[key])
        elif cat == 'pet' and key in _PET_SPRITES:
            self._paint_pet(p, r)
        elif cat == 'background':
            self._paint_background(p, r)
        elif cat == 'sound_pack':
            self._paint_sound(p, r)
        elif cat == 'icon_pack':
            self._paint_icons(p, r)
        else:
            self._backdrop(p, r)
            icon = CATEGORY_ICONS.get(cat, 'sparkles')
            p.drawPixmap(QPointF(r.center().x() - 17, r.center().y() - 17),
                         tinted_icon(icon, self._item.preview_color, 34).pixmap(34, 34))
        # Contorno sutil: separa la miniatura de la tarjeta
        p.setClipping(False)
        p.setPen(QPen(QColor(t.border), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        p.end()

    # ---- motivos ----
    def _backdrop(self, p: QPainter, r: QRectF) -> None:
        """Fondo hundido con un halo del color del artículo."""
        p.fillRect(r, QColor(tokens().bg_header))
        glow = QRadialGradient(r.center(), max(r.width(), r.height()) * 0.55)
        glow.setColorAt(0.0, _qcolor(self._item.preview_color, 64))
        glow.setColorAt(1.0, _qcolor(self._item.preview_color, 0))
        p.fillRect(r, QBrush(glow))

    @staticmethod
    def _paint_theme(p: QPainter, r: QRectF, th) -> None:
        """La pantalla principal en miniatura con la paleta del tema: dos
        tareas (la activa con borde de acento) y el anillo del timer."""
        p.fillRect(r, QColor(th.bg))
        pad = 12.0
        split = r.left() + r.width() * 0.56
        gap = 8.0
        card_h = min(30.0, (r.height() - 2 * pad - gap) / 2)
        top = r.center().y() - (2 * card_h + gap) / 2
        for i in range(2):
            cr = QRectF(r.left() + pad, top + i * (card_h + gap),
                        split - r.left() - pad - 4, card_h)
            p.setPen(QPen(QColor(th.accent if i == 0 else th.border), 1))
            p.setBrush(QColor(th.surface_hover if i == 0 else th.surface))
            p.drawRoundedRect(cr.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            cy = cr.center().y()
            p.setPen(QPen(QColor(th.fg_muted), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cr.left() + 11, cy), 3.5, 3.5)
            p.setPen(Qt.NoPen)
            tx = cr.left() + 21
            tw = cr.right() - 8 - tx
            p.setBrush(QColor(th.fg))
            p.drawRoundedRect(QRectF(tx, cy - 5, tw * (0.9 if i == 0 else 0.7), 3.5), 1.75, 1.75)
            p.setBrush(QColor(th.fg_dim if i == 0 else th.fg_muted))
            p.drawRoundedRect(QRectF(tx, cy + 2, tw * 0.42, 3), 1.5, 1.5)
        # Anillo con el degradado del tema (misma costura que ProgressRing)
        d = r.height() - 2 * pad
        cx = split + (r.right() - split) / 2
        ring = QRectF(cx - d / 2 + 2, r.center().y() - d / 2 + 2, d - 4, d - 4)
        p.setPen(QPen(QColor(th.surface_active), 4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(ring)
        frac, seam = 0.68, 96.0
        top_pos = (90.0 - seam) / 360.0 % 1.0
        grad = QConicalGradient(ring.center(), seam)
        grad.setColorAt(0.0, QColor(th.accent_grad_end))
        grad.setColorAt(max(0.0, top_pos - frac), QColor(th.accent_grad_end))
        grad.setColorAt(top_pos, QColor(th.accent))
        grad.setColorAt(1.0, QColor(th.accent))
        arc = QPen(QBrush(grad), 4)
        arc.setCapStyle(Qt.RoundCap)
        p.setPen(arc)
        p.drawArc(ring, 90 * 16, -int(frac * 360 * 16))
        # Cifras y puntos del ciclo: enfoque, enfoque, descanso
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(th.fg))
        bw = ring.width() * 0.44
        cy = ring.center().y()
        p.drawRoundedRect(QRectF(cx - bw / 2, cy - 6, bw, 6), 3, 3)
        for k, col in enumerate((th.accent, th.accent, th.break_accent)):
            p.setBrush(QColor(col))
            p.drawEllipse(QPointF(cx - 7 + k * 7, cy + 7), 2.2, 2.2)

    def _paint_pet(self, p: QPainter, r: QRectF) -> None:
        spec = _PET_SPRITES[self._item.id]
        self._backdrop(p, r)
        frames = spec['frames']
        rows = len(frames[0])
        # Ancho real del dibujo en todos los frames (dejan columnas vacías)
        used = [x for frame in frames for row in frame
                for x, ch in enumerate(row) if ch != '.']
        x0, x1 = min(used), max(used) + 1
        scale = max(2, min(5, int(min((r.height() - 24) / rows,
                                      (r.width() - 24) / (x1 - x0)))))
        w, h = (x1 - x0) * scale, rows * scale
        ox = round(r.center().x() - w / 2) - x0 * scale
        oy = round(r.center().y() - h / 2) - 2
        p.setPen(Qt.NoPen)
        p.setBrush(_qcolor('#000000', 70))
        p.drawEllipse(QPointF(r.center().x(), oy + h + 1), w * 0.42, 3.5)
        p.setRenderHint(QPainter.Antialiasing, False)  # píxeles nítidos
        for y, row in enumerate(frames[self._frame]):
            for x, ch in enumerate(row):
                color = spec['palette'].get(ch)
                if color:
                    p.fillRect(ox + x * scale, oy + y * scale, scale, scale, QColor(color))
        p.setRenderHint(QPainter.Antialiasing, True)

    def _paint_background(self, p: QPainter, r: QRectF) -> None:
        c, key = self._item.preview_color, self._item.id
        rng = random.Random(key)  # mismo motivo en cada repintado
        p.setPen(Qt.NoPen)
        if 'stars' in key:
            sky = QLinearGradient(r.topLeft(), r.bottomLeft())
            sky.setColorAt(0.0, QColor('#060a14'))
            sky.setColorAt(1.0, QColor(c))
            p.fillRect(r, QBrush(sky))
            for _ in range(48):
                pt = QPointF(r.left() + rng.random() * r.width(),
                             r.top() + rng.random() * r.height())
                rad = rng.choice((0.6, 0.8, 1.0, 1.3))
                p.setBrush(_qcolor('#ffffff', rng.randint(80, 230)))
                p.drawEllipse(pt, rad, rad)
            for _ in range(3):  # destellos de cuatro puntas
                cx = r.left() + r.width() * rng.uniform(0.1, 0.9)
                cy = r.top() + r.height() * rng.uniform(0.15, 0.75)
                s = rng.uniform(3.5, 5.5)
                star = QPainterPath()
                star.moveTo(cx, cy - s)
                for dx, dy in ((0.25, -0.25), (s, 0), (0.25, 0.25), (0, s),
                               (-0.25, 0.25), (-s, 0), (-0.25, -0.25)):
                    star.lineTo(cx + dx, cy + dy)
                star.closeSubpath()
                p.setBrush(QColor('#fde68a'))
                p.drawPath(star)
        elif 'waves' in key:
            p.fillRect(r, QColor(tokens().bg_header))
            k = 2 * math.pi / (r.width() * 0.6)
            for i in range(3):  # de atrás hacia delante
                base = r.top() + r.height() * (0.36 + i * 0.18)
                amp, phase = 7.0 - i * 1.5, i * 1.4
                path = QPainterPath()
                path.moveTo(r.left(), r.bottom())
                x = r.left()
                while x <= r.right() + 3:
                    path.lineTo(x, base + amp * math.sin(k * (x - r.left()) + phase))
                    x += 3
                path.lineTo(r.right() + 3, r.bottom())
                path.closeSubpath()
                p.setBrush(_qcolor(c, 60 + i * 50))
                p.drawPath(path)
        else:  # partículas flotantes
            self._backdrop(p, r)
            for _ in range(34):
                pt = QPointF(r.left() + rng.random() * r.width(),
                             r.top() + rng.random() * r.height())
                rad = rng.uniform(1.0, 4.2)
                p.setBrush(_qcolor(c, rng.randint(70, 220)))
                p.drawEllipse(pt, rad, rad)

    def _paint_sound(self, p: QPainter, r: QRectF) -> None:
        """Ecualizador: ondulante (naturaleza), escalonado (8-bit) o suelto."""
        c, key = self._item.preview_color, self._item.id
        self._backdrop(p, r)
        rng = random.Random(key)
        n, gap = 23, 3.0
        bw = (r.width() * 0.72 - gap * (n - 1)) / n
        x0 = r.center().x() - (n * bw + (n - 1) * gap) / 2
        blocky = '8bit' in key
        p.setPen(Qt.NoPen)
        for i in range(n):
            env = math.sin(math.pi * (i + 0.5) / n) ** 0.7  # más alto al centro
            if blocky:
                h = round((0.3 + 0.7 * rng.random()) * env * 4) / 4
            elif 'nature' in key:
                h = env * (0.45 + 0.55 * abs(math.sin(i * 0.55 + 0.3)))
            else:
                h = env * (0.35 + 0.65 * rng.random())
            h = max(0.12, h)
            bh = h * (r.height() - 28)
            bar = QRectF(x0 + i * (bw + gap), r.center().y() - bh / 2, bw, bh)
            p.setBrush(_qcolor(c, 140 + int(110 * h)))
            radius = 0.0 if blocky else bw / 2
            p.drawRoundedRect(bar, radius, radius)

    def _paint_icons(self, p: QPainter, r: QRectF) -> None:
        """Cuatro iconos de la app con el estilo del paquete."""
        t = tokens()
        c, key = self._item.preview_color, self._item.id
        self._backdrop(p, r)
        names = ("timer", "coffee", "target", "check-circle")
        size, gap = 22, 20
        total = len(names) * size + (len(names) - 1) * gap
        x = r.center().x() - total / 2
        y = r.center().y() - size / 2
        for i, name in enumerate(names):
            color = t.tag_colors[i % len(t.tag_colors)] if 'emoji' in key else c
            center = QPointF(x + size / 2, y + size / 2)
            p.setPen(Qt.NoPen)
            if 'neon' in key:
                glow = QRadialGradient(center, size)
                glow.setColorAt(0.0, _qcolor(c, 120))
                glow.setColorAt(1.0, _qcolor(c, 0))
                p.setBrush(QBrush(glow))
                p.drawEllipse(center, size, size)
            elif 'emoji' in key:
                p.setBrush(_qcolor(color, 40))
                p.drawRoundedRect(QRectF(x - 6, y - 6, size + 12, size + 12), 8, 8)
            p.drawPixmap(QPointF(x, y), tinted_icon(name, color, size).pixmap(size, size))
            x += size + gap


class RewardsDialog(BrandedDialog):
    theme_equipped = Signal(str)

    def __init__(self, parent_window, controller):
        super().__init__(parent_window, "Recompensas", icon="trophy")
        self.controller = controller
        self.rewards = controller.rewards
        self._category = "all"
        self._live = True  # conexiones a señales globales aún activas
        host = self._lay.parentWidget()  # la tarjeta del diálogo

        # Saldo en la cabecera, a la derecha del título
        self._build_balance(host)

        # El tamaño mínimo va en las pestañas: el diálogo suma la tarjeta y
        # el margen de la sombra
        self.tabs = QTabWidget(host)
        self.tabs.setMinimumSize(820, 500)
        self.tabs.setIconSize(QSize(16, 16))
        self.tabs.setUsesScrollButtons(False)
        self._lay.addWidget(self.tabs, 1)
        builders = (self._build_shop_tab, self._build_inventory_tab,
                    self._build_progress_tab)
        for (_icon, label), build in zip(TABS, builders):
            self.tabs.addTab(build(), label)
        self.tabs.currentChanged.connect(self._retint_tabs)
        self.add_buttons([("Cerrar", "ghost", 0)])

        self._style_tabs()
        self._refresh_header()
        self._populate_shop()
        self._populate_inventory()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self.finished.connect(self._disconnect_live)

    # ================= Estilo y ciclo de vida =================
    def _style_tabs(self) -> None:
        t = tokens()
        # Área de contenido hundida (fondo de ventana): las tarjetas en
        # superficie se leen como las tareas de la pantalla principal
        self.tabs.setStyleSheet(
            f"QTabWidget::pane {{ background: {t.bg}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; top: -1px; }}"
            f" QTabBar::tab {{ padding: 8px 14px 9px 12px; }}")
        self._retint_tabs()

    def _retint_tabs(self, *_args) -> None:
        t = tokens()
        current = self.tabs.currentIndex()
        for i, (icon, _label) in enumerate(TABS[:self.tabs.count()]):
            self.tabs.setTabIcon(i, tinted_icon(icon, t.fg if i == current else t.fg_dim, 16))

    def _restyle(self) -> None:
        """Tema nuevo (p. ej. recién equipado aquí mismo): repintar todo."""
        self._style_tabs()
        self._refresh_all()

    def _disconnect_live(self, *_args) -> None:
        """Al cerrar: el diálogo sigue vivo como hijo de la ventana, así que
        suelta las señales globales para no repintarse oculto."""
        if not self._live:
            return
        self._live = False
        try:
            ThemeManager.instance().theme_changed.disconnect(self._restyle)
        except (RuntimeError, TypeError):
            pass

    def _refresh_all(self) -> None:
        self._refresh_header()
        self._populate_shop()
        self._populate_inventory()

    # ================= Cabecera: nivel y monedas =================
    def _build_balance(self, host: QWidget) -> None:
        box = QWidget(host)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.level_chip = QLabel(box)
        self.level_chip.setFont(fonts.font(fonts.XS, bold=True))
        self.level_chip.setAlignment(Qt.AlignCenter)
        self.level_chip.setFixedHeight(22)
        lay.addWidget(self.level_chip, 0, Qt.AlignVCenter)
        lay.addSpacing(10)
        self.coins_icon = QLabel(box)
        lay.addWidget(self.coins_icon, 0, Qt.AlignVCenter)
        self.coins_value = QLabel(box)
        self.coins_value.setFont(fonts.display(fonts.NORMAL))
        lay.addWidget(self.coins_value, 0, Qt.AlignVCenter)
        caption = _label(box, "monedas", fonts.font(fonts.XS), role="muted")
        lay.addWidget(caption, 0, Qt.AlignVCenter)
        header = getattr(self, '_header', None)
        if isinstance(header, QHBoxLayout):
            header.addWidget(box, 0, Qt.AlignVCenter)
        else:  # cabecera sin layout propio: el saldo va bajo el título
            self._lay.addWidget(box, 0, Qt.AlignLeft)

    def _refresh_header(self) -> None:
        t = tokens()
        s = self.rewards.get_stats()
        self.level_chip.setText(f"Nv. {s['level']}")
        self.level_chip.setStyleSheet(
            f"color: {t.accent}; background: {t.bg_header}; border: 1px solid {t.border};"
            f" border-radius: 11px; padding: 0 10px;")
        self.coins_icon.setPixmap(tinted_icon("coins", GOLD, 18).pixmap(18, 18))
        self.coins_value.setText(str(s['coins']))
        self.coins_value.setStyleSheet(f"color: {GOLD};")
        self._populate_progress()

    @staticmethod
    def _scroll(parent: QWidget) -> QScrollArea:
        scroll = QScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    # ================= Tienda =================
    def _build_shop_tab(self) -> QWidget:
        page = QWidget(self.tabs)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 6, 0)
        lay.setSpacing(12)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 10, 0)
        top.setSpacing(8)
        segment = QFrame(page)
        segment.setProperty("segmented", True)
        seg_lay = QHBoxLayout(segment)
        seg_lay.setContentsMargins(3, 3, 3, 3)
        seg_lay.setSpacing(2)
        group = QButtonGroup(segment)
        group.setExclusive(True)
        self._cat_buttons = {}
        for key, label in CATEGORIES:
            b = QPushButton(label, segment)
            b.setProperty("pill", True)
            b.setCheckable(True)
            b.setChecked(key == self._category)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_category(k))
            group.addButton(b)
            seg_lay.addWidget(b)
            self._cat_buttons[key] = b
        top.addWidget(segment)
        top.addStretch(1)
        self._shop_count = _label(page, "", fonts.font(fonts.XS), role="muted")
        top.addWidget(self._shop_count)
        lay.addLayout(top)

        self._shop_scroll = self._scroll(page)
        self._shop_body = QWidget(self._shop_scroll)
        self._shop_grid = QGridLayout(self._shop_body)
        self._shop_grid.setContentsMargins(0, 0, 10, 16)
        self._shop_grid.setSpacing(12)
        for col in range(SHOP_COLUMNS):
            self._shop_grid.setColumnStretch(col, 1)
        self._shop_scroll.setWidget(self._shop_body)
        lay.addWidget(self._shop_scroll, 1)
        return page

    def _set_category(self, key: str) -> None:
        self._category = key
        button = self._cat_buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self._populate_shop()
        self._shop_scroll.verticalScrollBar().setValue(0)

    def _is_equipped(self, item: ShopItem) -> bool:
        progress = self.rewards.progress
        if item.id == DEFAULT_THEME.id:
            owned_themes = {i.id for i in self.rewards.SHOP_ITEMS
                            if i.category == 'theme' and i.id in progress.purchased_items}
            return progress.current_theme not in owned_themes
        field = SLOT_FIELDS.get(item.category)
        return (item.id in progress.purchased_items and bool(field)
                and getattr(progress, field, None) == item.id)

    def _populate_shop(self) -> None:
        grid = self._shop_grid
        _clear_layout(grid)
        items = [i for i in self.rewards.SHOP_ITEMS
                 if self._category == "all" or i.category == self._category]
        coins = self.rewards.progress.coins
        owned = set(self.rewards.progress.purchased_items)
        for idx, item in enumerate(items):
            card = self._shop_card(self._shop_body, item, coins, item.id in owned,
                                   self._is_equipped(item))
            grid.addWidget(card, idx // SHOP_COLUMNS, idx % SHOP_COLUMNS)
        rows = (len(items) + SHOP_COLUMNS - 1) // SHOP_COLUMNS
        for r in range(grid.rowCount() + 1):
            grid.setRowStretch(r, 1 if r == rows else 0)
        self._shop_count.setText(_plural(len(items), "artículo", "artículos"))

    def _card_frame(self, parent: QWidget, equipped: bool) -> QFrame:
        """Tarjeta en superficie; la equipada, con borde de acento (estado)."""
        card = QFrame(parent)
        if equipped:
            t = tokens()
            card.setObjectName("rewardEquipped")
            card.setStyleSheet(
                f"#rewardEquipped {{ background: {t.surface}; border: 1px solid {t.accent};"
                f" border-radius: {t.radius}px; }}")
        else:
            card.setProperty("card", True)
        return card

    def _shop_card(self, parent: QWidget, item: ShopItem, coins: int,
                   owned: bool, equipped: bool) -> QFrame:
        card = self._card_frame(parent, equipped)
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(0)
        v.addWidget(ItemPreview(item, 92, card))
        v.addSpacing(12)
        text = QVBoxLayout()
        text.setContentsMargins(4, 0, 4, 0)
        text.setSpacing(2)
        text.addWidget(_label(card, item.name, fonts.font(fonts.SMALL, bold=True)))
        text.addWidget(_label(card, item.description, fonts.font(fonts.XS),
                              role="dim", wrap=True))
        v.addLayout(text)
        v.addStretch(1)
        v.addSpacing(12)
        v.addWidget(self._shop_actions(card, item, coins, owned, equipped))
        return card

    def _shop_actions(self, card: QWidget, item: ShopItem, coins: int,
                      owned: bool, equipped: bool) -> QWidget:
        """Pie de la tarjeta: precio y comprar / comprado y equipar / equipado."""
        t = tokens()
        bar = QWidget(card)
        bar.setFixedHeight(32)
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(6)
        if equipped:
            row.addWidget(_icon_label(bar, "check-circle", t.accent, 16))
            row.addWidget(_label(bar, "Equipado", fonts.font(fonts.SMALL, bold=True),
                                 role="accent"))
            row.addStretch(1)
        elif owned:
            row.addWidget(_icon_label(bar, "check", t.fg_dim, 15))
            row.addWidget(_label(bar, "Comprado", fonts.font(fonts.SMALL), role="dim"))
            row.addStretch(1)
            b = QPushButton("Equipar", bar)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, iid=item.id: self._equip(iid))
            row.addWidget(b)
        else:
            row.addWidget(_icon_label(bar, "coins", GOLD, 16))
            row.addWidget(_label(bar, str(item.price), fonts.font(fonts.SMALL, bold=True),
                                 color=GOLD))
            row.addStretch(1)
            if coins >= item.price:
                b = QPushButton("Comprar", bar)
                b.setProperty("variant", "accent")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, iid=item.id: self._buy(iid))
                row.addWidget(b)
            else:
                # Sin botón deshabilitado: cuánto falta, con un candado
                row.addWidget(_icon_label(bar, "lock", t.fg_muted, 13))
                row.addWidget(_label(bar, f"Faltan {item.price - coins}",
                                     fonts.font(fonts.XS), role="muted"))
        return bar

    def _buy(self, item_id: str) -> None:
        from ui.widgets import toast
        ok, msg = self.rewards.purchase_item(item_id)
        if ok:
            # La primera mascota se equipa sola al comprarla: "compré y no
            # pasó nada" era el resultado natural de exigir el paso extra
            item = next((i for i in self.rewards.SHOP_ITEMS if i.id == item_id), None)
            if (item and item.category == 'pet'
                    and self.rewards.progress.current_pet in ('none', '', None)):
                self.rewards.equip_item(item_id)
                self.controller.refresh_pet()
                msg += " ¡Ya te acompaña junto al timer!"
        (toast.show_success if ok else toast.show_warning)(
            self.controller.window, msg, duration=2500)
        self._refresh_all()
        self.controller.update_rewards_bar()

    # ================= Inventario =================
    def _build_inventory_tab(self) -> QWidget:
        self._inv_scroll = self._scroll(self.tabs)
        self._inv_body = QWidget(self._inv_scroll)
        self._inv_lay = QVBoxLayout(self._inv_body)
        self._inv_lay.setContentsMargins(16, 16, 16, 16)
        self._inv_lay.setSpacing(8)
        self._inv_scroll.setWidget(self._inv_body)
        return self._inv_scroll

    def _populate_inventory(self) -> None:
        lay = self._inv_lay
        _clear_layout(lay)
        owned = [i for i in self.rewards.SHOP_ITEMS
                 if i.id in self.rewards.progress.purchased_items]
        if not owned:
            lay.addStretch(1)
            lay.addWidget(self._inventory_empty(self._inv_body))
            lay.addStretch(2)
            return
        # Una sola rejilla para todas las secciones: columnas del mismo ancho
        host = QWidget(self._inv_body)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        row = 0
        for category, title in INVENTORY_SECTIONS:
            items = [i for i in owned if i.category == category]
            if not items:
                continue
            if category == 'theme':
                items.insert(0, DEFAULT_THEME)
            head = QWidget(host)
            hl = QHBoxLayout(head)
            hl.setContentsMargins(4, 14 if row else 0, 0, 0)
            hl.setSpacing(8)
            hl.addWidget(_label(head, title, fonts.font(fonts.SMALL, bold=True)))
            hl.addWidget(_label(head, str(len(items)), fonts.font(fonts.XS), role="muted"))
            hl.addStretch(1)
            grid.addWidget(head, row, 0, 1, 2)
            row += 1
            for idx, item in enumerate(items):
                grid.addWidget(self._inventory_card(host, item), row + idx // 2, idx % 2)
            row += (len(items) + 1) // 2
        lay.addWidget(host)
        lay.addStretch(1)

    def _inventory_empty(self, parent: QWidget) -> QWidget:
        t = tokens()
        box = QWidget(parent)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(_icon_tile(box, "backpack", t.fg_dim, 56, 26), 0, Qt.AlignHCenter)
        v.addSpacing(4)
        title = _label(box, "Aún no tienes artículos", fonts.font(fonts.NORMAL, bold=True))
        title.setAlignment(Qt.AlignCenter)
        v.addWidget(title)
        pomodoro = self.rewards.REWARDS.get('pomodoro_completed', {}).get('coins', 5)
        # Salto de línea explícito: un QLabel con ajuste de línea dentro de
        # un layout centrado no recibe su altura y se solapa
        msg = _label(box, f"Cada pomodoro completado te da {pomodoro} monedas.\n"
                          "Canjéalas en la tienda por temas, mascotas y más.",
                     fonts.font(fonts.SMALL), role="dim")
        msg.setAlignment(Qt.AlignCenter)
        v.addWidget(msg, 0, Qt.AlignHCenter)
        v.addSpacing(8)
        go = QPushButton("Ir a la tienda", box)
        go.setProperty("variant", "accent")
        go.setCursor(Qt.PointingHandCursor)
        go.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        v.addWidget(go, 0, Qt.AlignHCenter)
        return box

    def _inventory_card(self, parent: QWidget, item: ShopItem) -> QFrame:
        t = tokens()
        equipped = self._is_equipped(item)
        card = self._card_frame(parent, equipped)
        h = QHBoxLayout(card)
        h.setContentsMargins(10, 10, 16, 10)
        h.setSpacing(14)
        preview = ItemPreview(item, 64, card)
        preview.setFixedWidth(104)
        h.addWidget(preview)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addStretch(1)
        text.addWidget(_label(card, item.name, fonts.font(fonts.SMALL, bold=True)))
        status = "Equipado" if equipped else item.description
        text.addWidget(_label(card, status, fonts.font(fonts.XS),
                              role="accent" if equipped else "muted", wrap=True))
        text.addStretch(1)
        h.addLayout(text, 1)
        if equipped:
            h.addWidget(_icon_label(card, "check-circle", t.accent, 18))
        else:
            b = QPushButton("Equipar", card)
            b.setCursor(Qt.PointingHandCursor)
            if item.id == DEFAULT_THEME.id:
                b.clicked.connect(self._equip_default_theme)
            else:
                b.clicked.connect(lambda _=False, iid=item.id: self._equip(iid))
            h.addWidget(b)
        return card

    def _equip(self, item_id: str) -> None:
        from ui.widgets import toast
        if not self.rewards.equip_item(item_id):
            toast.show_warning(self.controller.window, "No se pudo equipar", duration=2000)
            return
        item = next((i for i in self.rewards.SHOP_ITEMS if i.id == item_id), None)
        if item and item.category == 'theme':
            self.theme_equipped.emit(item_id if item_id in THEMES else 'default')
        if item and item.category == 'pet':
            self.controller.refresh_pet()
        toast.show_success(self.controller.window,
                           f"{item.name if item else item_id} equipado", duration=2000)
        self._refresh_all()

    def _equip_default_theme(self) -> None:
        """Vuelve al tema original (main.py lo aplica al arrancar si el
        guardado es 'default')."""
        from ui.widgets import toast
        self.rewards.progress.current_theme = 'default'
        self.rewards.save()
        self.theme_equipped.emit('default')
        toast.show_success(self.controller.window, "Tema clásico equipado", duration=2000)
        self._refresh_all()

    # ================= Progreso =================
    def _build_progress_tab(self) -> QWidget:
        self._prog_scroll = self._scroll(self.tabs)
        self._prog_body = QWidget(self._prog_scroll)
        self._prog_lay = QHBoxLayout(self._prog_body)
        self._prog_lay.setContentsMargins(16, 16, 16, 16)
        self._prog_lay.setSpacing(16)
        self._prog_scroll.setWidget(self._prog_body)
        return self._prog_scroll

    def _populate_progress(self) -> None:
        if not hasattr(self, '_prog_lay'):
            return
        _clear_layout(self._prog_lay)
        body = self._prog_body
        s = self.rewards.get_stats()

        left = QWidget(body)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        head = _label(left, "Tu progreso", fonts.font(fonts.SMALL, bold=True))
        head.setContentsMargins(4, 0, 0, 0)
        lv.addWidget(head)
        lv.addWidget(self._level_card(left, s))
        lv.addSpacing(4)  # tarjeta → fichas: 12 px
        tiles = QGridLayout()
        tiles.setSpacing(12)
        days = s['streak_days']
        stats = [("tomato", "Pomodoros", str(s['total_pomodoros']), None),
                 ("repeat", "Ciclos completos", str(s.get('total_cycles', 0)), None),
                 ("clock", "Tiempo enfocado", _fmt_minutes(s['total_time_minutes']), None),
                 ("flame", "Racha actual", _plural(days, "día", "días"), None),
                 ("coins", "Monedas", str(s['coins']), GOLD),
                 ("sparkles", "Experiencia total", f"{s['xp']} XP", None)]
        for idx, (icon, caption, value, color) in enumerate(stats):
            tiles.addWidget(self._stat_tile(left, icon, caption, value, color),
                            idx // 3, idx % 3)
        lv.addLayout(tiles)
        lv.addStretch(1)
        self._prog_lay.addWidget(left, 3)

        right = QWidget(body)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        head = _label(right, "Cómo ganar más", fonts.font(fonts.SMALL, bold=True))
        head.setContentsMargins(4, 0, 0, 0)
        rv.addWidget(head)
        rv.addWidget(self._earn_card(right))
        rv.addStretch(1)
        self._prog_lay.addWidget(right, 2)

    def _level_card(self, parent: QWidget, s: dict) -> QFrame:
        level, xp = s['level'], s['xp']
        base = self.rewards.XP_PER_LEVEL.get(level, 0)
        maxed = (level + 1) not in self.rewards.XP_PER_LEVEL
        nxt = s['xp_for_next_level']
        frac = 1.0 if maxed else (xp - base) / max(1, nxt - base)

        card = QFrame(parent)
        card.setProperty("card", True)
        h = QHBoxLayout(card)
        h.setContentsMargins(20, 16, 20, 16)
        h.setSpacing(20)
        ring = ProgressRing(112, 8, card)
        ring.inner.setContentsMargins(14, 14, 14, 14)
        ring.inner.setSpacing(0)
        cap = _label(ring, "Nivel", fonts.font(fonts.XS), role="muted")
        cap.setAlignment(Qt.AlignCenter)
        num = _label(ring, str(level), fonts.display(fonts.LARGE))
        num.setAlignment(Qt.AlignCenter)
        ring.inner.addWidget(cap)
        ring.inner.addWidget(num)
        ring.set_fraction(frac, animate=False)
        h.addWidget(ring, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(4)
        text.addStretch(1)
        text.addWidget(_label(card, "Experiencia", fonts.font(fonts.XS), role="muted"))
        value = f"{xp} XP" if maxed else f"{xp} / {nxt} XP"
        text.addWidget(_label(card, value, fonts.display(fonts.MEDIUM)))
        detail = ("Has alcanzado el nivel máximo" if maxed else
                  f"Te faltan {nxt - xp} XP para el nivel {level + 1}")
        text.addWidget(_label(card, detail, fonts.font(fonts.SMALL), role="dim"))
        bonus = self.rewards.REWARDS.get('level_up', {}).get('coins', 0)
        if bonus and not maxed:
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(_icon_label(card, "coins", GOLD, 14))
            row.addWidget(_label(card, f"+{bonus} monedas al subir de nivel",
                                 fonts.font(fonts.XS), role="muted"))
            row.addStretch(1)
            text.addSpacing(4)
            text.addLayout(row)
        text.addStretch(1)
        h.addLayout(text, 1)
        return card

    @staticmethod
    def _stat_tile(parent: QWidget, icon: str, caption: str, value: str,
                   color: str = None) -> QFrame:
        t = tokens()
        tile = QFrame(parent)
        tile.setProperty("card", True)
        v = QVBoxLayout(tile)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(_icon_label(tile, icon, color or t.fg_dim, 15))
        top.addWidget(_label(tile, caption, fonts.font(fonts.XS), role="muted"))
        top.addStretch(1)
        v.addLayout(top)
        v.addWidget(_label(tile, value, fonts.display(fonts.MEDIUM), color=color))
        return tile

    def _earn_card(self, parent: QWidget) -> QFrame:
        t = tokens()
        R = self.rewards.REWARDS

        def reward(key: str) -> str:
            r = R.get(key, {})
            parts = []
            if r.get('xp'):
                parts.append(f"+{r['xp']} XP")
            if r.get('coins'):
                parts.append(f"+{r['coins']} monedas")
            return " · ".join(parts)

        rows = [("tomato", "Pomodoro completado", 'pomodoro_completed'),
                ("repeat", "Ciclo completo", 'cycle_completed'),
                ("flame", "Racha de 3 días", 'day_streak_3'),
                ("flame", "Racha de 7 días", 'day_streak_7'),
                ("flame", "Racha de 30 días", 'day_streak_30'),
                ("trophy", "Subir de nivel", 'level_up')]
        card = QFrame(parent)
        card.setProperty("card", True)
        v = QVBoxLayout(card)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(0)
        for i, (icon, title, key) in enumerate(rows):
            text = reward(key)
            if not text:
                continue
            if i:
                v.addWidget(_Divider(card))
            row = QWidget(card)
            h = QHBoxLayout(row)
            h.setContentsMargins(14, 9, 14, 9)
            h.setSpacing(12)
            h.addWidget(_icon_tile(row, icon, t.fg_dim, 30, 15))
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(_label(row, title, fonts.font(fonts.SMALL)))
            col.addWidget(_label(row, text, fonts.font(fonts.XS), role="muted"))
            h.addLayout(col, 1)
            v.addWidget(row)
        return card
