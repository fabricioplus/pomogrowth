"""Bienvenida (resumen de tareas, se cierra al hacer clic en cualquier parte)
y despedida (tras ocultar la interfaz al salir: «Hasta pronto», el
repositorio de GitHub para darle una estrella, Ko-fi para donar y las redes
sociales de GrowthLab y de fabricioplus para seguirlas)."""
import re
import unicodedata
from datetime import datetime

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent,
                            QPropertyAnimation, QRectF, Qt, QTimer,
                            QVariantAnimation)
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from core.config import Config
from ui.dialogs.branded_dialogs import (SHADOW_MARGINS, BrandedDialog,
                                        icon_chip, soft_shadow)
from ui.greeting import (clean_name, first_name, greeting_text, greeting_word,
                         valid_streak, with_name)
from ui.theme import brand, fonts
from ui.theme.tokens import tokens
from ui.widgets.link_button import LinkButton

_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre")

# Líneas del resumen que se muestran como fila con icono y cifra:
# (palabra clave, icono, token de color, etiqueta en singular, en plural).
# El orden importa: «vencida» antes que «hoy»
_STAT_KINDS = (
    ("vencida", "calendar-x", "error", "Vencida", "Vencidas"),
    ("hoy", "calendar-clock", "warning", "Vence hoy", "Vencen hoy"),
    ("pendiente", "list-todo", "accent", "Pendiente", "Pendientes"),
)


def _greeting(now: datetime) -> str:
    return greeting_word(now)  # las franjas viven en ui/greeting (compartidas)


def _date_text(now: datetime) -> str:
    text = f"{_DAYS[now.weekday()]}, {now.day} de {_MONTHS[now.month - 1]}"
    return text[0].upper() + text[1:]


def _clean(line: str) -> str:
    """Quita emojis y símbolos decorativos de los extremos de la línea."""
    def deco(ch: str) -> bool:
        return ch.isspace() or unicodedata.category(ch) in ("So", "Sk", "Mn", "Cf")
    start, end = 0, len(line)
    while start < end and deco(line[start]):
        start += 1
    while end > start and deco(line[end - 1]):
        end -= 1
    return line[start:end]


def _parse_stat(line: str):
    """(icono, token, etiqueta, cifra) si la línea es un contador conocido."""
    low = line.lower()
    number = re.search(r"\d+", line)
    if not number:
        return None
    for key, icon, color_token, singular, plural in _STAT_KINDS:
        if key in low:
            n = int(number.group())
            return icon, color_token, singular if n == 1 else plural, n
    return None


class WelcomeDialog(BrandedDialog):
    """Se cierra con clic en CUALQUIER parte (dentro o fuera), Escape o Enter.
    Con `user_name`, el saludo lleva el nombre («Buenos días, Fabricio»); con
    `streak` (actual, mejor), el contador de la racha de uso junto al título."""

    def __init__(self, parent_window, summary_lines, user_name: str = "",
                 streak=None):
        now = datetime.now()
        t = tokens()
        super().__init__(parent_window, _greeting(now), accent=t.accent,
                         icon=brand.MASCOT_ICON, subtitle=_date_text(now))
        self._card.setMinimumWidth(420)
        self.streak_badge = self._personalize(now, user_name, streak)
        name_prefix = f"{clean_name(user_name)}, " if clean_name(user_name) else None
        stats, notes = [], []
        for raw in summary_lines:
            # «Fabricio, tienes 5 tareas pendientes»: el contador se lee sin
            # el nombre (un «Hoyos» o un «Ana2» no deben cambiar la fila).
            # El nombre se quita ANTES de _clean: si empieza por emoji
            # («🦊 Ana2»), _clean se lo comería y el prefijo ya no casaría
            line = str(raw).strip()
            named = bool(name_prefix) and line.startswith(name_prefix)
            if named:
                line = line[len(name_prefix):]
            line = _clean(line)
            if not line:
                continue
            if named:
                line = line[:1].upper() + line[1:]
            stat = _parse_stat(line)
            if stat:
                stats.append(stat)
            else:
                notes.append(line)

        if stats:
            self._lay.addWidget(self._stats_list(stats))
        for note in notes:
            self.add_message(note)

        # Pie: pista a la izquierda, acción principal a la derecha
        foot = QHBoxLayout()
        foot.setSpacing(12)
        hint = QLabel("Haz clic en cualquier parte para continuar", self._card)
        hint.setFont(fonts.font(fonts.XS))
        hint.setProperty("role", "muted")
        foot.addWidget(hint, 1, Qt.AlignVCenter)
        start = QPushButton("Empezar", self._card)
        start.setProperty("variant", "accent")
        start.setCursor(Qt.PointingHandCursor)
        start.setMinimumWidth(96)
        start.setDefault(True)
        start.clicked.connect(self.accept)
        foot.addWidget(start)
        self._lay.addSpacing(8)
        self._lay.addLayout(foot)

        if self._overlay is not None:
            self._overlay.clicked.connect(self.accept)

    def _personalize(self, now: datetime, user_name: str, streak):
        """Nombre en el título y racha de uso a la derecha de la cabecera.
        Devuelve el contador (o None si no hay racha que mostrar)."""
        if clean_name(user_name):
            self._title_label.setTextFormat(Qt.RichText)
            self._title_label.setText(greeting_text(now, user_name, tokens().accent))
        streak = valid_streak(streak)
        if streak is None:
            return None
        from ui.widgets.streak_badge import StreakBadge
        badge = StreakBadge(*streak, parent=self._card)
        self._header.addWidget(badge, 0, Qt.AlignTop)
        return badge

    def _stats_list(self, stats) -> QFrame:
        """Filas «icono · etiqueta · cifra» sobre un carril hundido."""
        t = tokens()
        box = QFrame(self._card)
        box.setObjectName("welcomeStats")
        box.setStyleSheet(f"#welcomeStats {{ background: {t.bg}; border: 1px solid {t.border};"
                          f" border-radius: {t.radius_small + 2}px; }}"
                          f"#welcomeSep {{ background: {t.border}; border: none; }}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(0)
        for i, (icon, color_token, label_text, number) in enumerate(stats):
            if i:
                sep = QFrame(box)
                sep.setObjectName("welcomeSep")
                sep.setFixedHeight(1)
                lay.addWidget(sep)
            row = QWidget(box)
            h = QHBoxLayout(row)
            h.setContentsMargins(12, 8, 16, 8)
            h.setSpacing(12)
            h.addWidget(icon_chip(icon, getattr(t, color_token), size=28,
                                  icon_size=16, parent=row))
            label = QLabel(label_text, row)
            label.setFont(fonts.font(fonts.SMALL))
            h.addWidget(label, 1)
            value = QLabel(str(number), row)
            value.setFont(fonts.display(fonts.NORMAL))
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            h.addWidget(value)
            lay.addWidget(row)
        return box

    def mousePressEvent(self, event):
        self.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.accept()
            return
        super().keyPressEvent(event)


# Redes de la despedida (las de Config.SOCIAL_LINKS): nombre y color de
# marca, con su icono brand-<red>.svg (Simple Icons). TikTok no tiene un
# color propio que se lea sobre fondo oscuro: va con el del texto
_NETWORKS = {
    "facebook": ("Facebook", "#1877F2"),
    "instagram": ("Instagram", "#E4405F"),
    "youtube": ("YouTube", "#FF0000"),
    "tiktok": ("TikTok", None),
    "linkedin": ("LinkedIn", "#0A66C2"),
}
# Contraste mínimo de un icono (WCAG 1.4.11) para pasar al color de marca al
# pasar el ratón; por debajo, el acento del tema. Con los temas de hoy no
# llega a pasar: el más justo, el azul de LinkedIn, da 3,0-3,4 sobre el carril
_MIN_ICON_CONTRAST = 3.0
FAREWELL_MS = 8000          # cuenta de la despedida (se pausa con el ratón encima)
FADE_IN_MS, FADE_OUT_MS = 220, 260
LINK_CLOSE_MS = 600         # tras abrir un enlace, lo que tarda en irse
CARD_WIDTH = 440


def _luminance(color: str) -> float:
    def channel(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    c = QColor(color)
    return (0.2126 * channel(c.red()) + 0.7152 * channel(c.green())
            + 0.0722 * channel(c.blue()))


def contrast(a: str, b: str) -> float:
    """Relación de contraste WCAG entre dos colores (de 1 a 21)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def network_hover_color(network: str, background: str) -> str:
    """Color de la red al pasar el ratón: el de su marca si se lee sobre
    `background`; si no, el acento."""
    t = tokens()
    color = _NETWORKS[network][1] or t.fg
    return color if contrast(color, background) >= _MIN_ICON_CONTRAST else t.accent


class _CountdownBar(QWidget):
    """Barra fina con lo que le queda a la despedida: se vacía hacia la
    izquierda y, en pausa (ratón encima), se apaga."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(3)
        self._value = 1.0
        self._paused = False

    def set_value(self, value) -> None:
        self._value = max(0.0, min(1.0, float(value)))
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        r = QRectF(self.rect())
        radius = r.height() / 2
        p.setBrush(QColor(t.border))
        p.drawRoundedRect(r, radius, radius)
        if self._value > 0:
            p.setBrush(QColor(t.fg_muted if self._paused else t.accent))
            width = max(r.height(), r.width() * self._value)
            p.drawRoundedRect(QRectF(r.x(), r.y(), width, r.height()), radius, radius)
        p.end()


class FarewellWindow(QWidget):
    """Despedida: la interfaz ya está oculta cuando aparece. La mascota con
    «Hasta pronto» y, debajo, el repositorio de GitHub (Config.GITHUB_URL,
    con «Dar estrella»), Ko-fi para donar (Config.KOFI_URL) y las redes de
    GrowthLab y de fabricioplus (Config.SOCIAL_LINKS) para seguirlas. Entra con fade y, tras una cuenta
    de `duration_ms` (la barra fina del pie, en pausa con el ratón sobre la
    tarjeta), sale con fade y llama a `on_done`. Esc, «Cerrar» o abrir un
    enlace la cierran antes. `on_done` se llama UNA sola vez pase lo que
    pase (también si alguien cierra la ventana)."""

    def __init__(self, on_done, duration_ms: int = FAREWELL_MS, user_name: str = ""):
        super().__init__()
        t = tokens()
        self._on_done = on_done
        self._closing = False   # saliendo (fade de salida en marcha)
        self._done = False      # on_done ya llamado
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)  # para Esc
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*SHADOW_MARGINS)

        card = QFrame(self)
        card.setObjectName("farewellCard")
        card.setFixedWidth(CARD_WIDTH)
        card.setStyleSheet(f"#farewellCard {{ background: {t.surface}; border: 1px solid"
                           f" {t.border}; border-radius: {t.radius}px; }}")
        soft_shadow(card)
        self._card = card
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(0)

        # Saludo: la mascota, «Hasta pronto» y el mensaje (como hasta la 9.3.1)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(icon_chip(brand.MASCOT_ICON, t.accent, size=40, icon_size=24,
                                 parent=card))
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = QLabel("Hasta pronto", card)
        title.setFont(fonts.display(fonts.MEDIUM, tabular=False))
        if first_name(user_name):  # solo el primer nombre: la tarjeta es de ancho fijo
            title.setTextFormat(Qt.RichText)
            title.setText(with_name("Hasta pronto", first_name(user_name), t.accent))
        self.title_label = title
        texts.addWidget(title)
        msg = QLabel("Buen trabajo hoy. PomoGrowth te espera.", card)
        msg.setFont(fonts.font(fonts.SMALL))
        msg.setProperty("role", "dim")
        texts.addWidget(msg)
        head.addLayout(texts, 1)
        lay.addLayout(head)

        # Redes: la invitación (pregunta y petición, dos líneas sin ajuste:
        # un texto con wordWrap descuadraba el alto de la ventana fija) y una
        # fila por cuenta con sus cinco iconos
        lay.addSpacing(18)
        question = QLabel("¿Disfrutas de PomoGrowth?", card)
        question.setFont(fonts.font(fonts.SMALL, bold=True))
        lay.addWidget(question)
        lay.addSpacing(2)
        invite = QLabel("Puedes darnos tu apoyo.", card)
        invite.setFont(fonts.font(fonts.SMALL))
        invite.setProperty("role", "dim")
        lay.addWidget(invite)
        lay.addSpacing(12)
        self.links = []  # los LinkButton de las redes, en el orden de Config.SOCIAL_LINKS
        lay.addWidget(self._social_box(card))

        # Pie: la cuenta (barra fina) y «Cerrar»
        lay.addSpacing(12)
        foot = QHBoxLayout()
        foot.setSpacing(12)
        self._bar = _CountdownBar(card)
        foot.addWidget(self._bar, 1, Qt.AlignVCenter)
        self.close_link = LinkButton("Cerrar", color="fg_muted", hover="fg", parent=card)
        self.close_link.clicked.connect(self.close_now)
        foot.addWidget(self.close_link, 0, Qt.AlignVCenter)
        lay.addLayout(foot)
        outer.addWidget(card)
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2,
                      geo.center().y() - self.height() // 2)

        # Cuenta atrás que se puede pausar (QVariantAnimation: pause/resume)
        self._countdown = QVariantAnimation(self)
        self._countdown.setDuration(max(1, duration_ms))
        self._countdown.setStartValue(1.0)
        self._countdown.setEndValue(0.0)
        self._countdown.valueChanged.connect(self._bar.set_value)
        self._countdown.finished.connect(self.close_now)
        # Ratón sobre la tarjeta = pausa. Si aparece justo bajo el cursor (el
        # «Salir» de la pregunta suele quedar ahí) no pausa hasta que el ratón
        # se mueva: quien suelta el ratón no se queda con ella delante
        self._hover_armed = not self._cursor_on_card()
        card.setMouseTracking(True)
        card.installEventFilter(self)

        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(FADE_IN_MS)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        fade.start(QPropertyAnimation.DeleteWhenStopped)
        self._countdown.start()

    def _social_box(self, parent) -> QFrame:
        """Sobre un carril hundido (como el resumen de la bienvenida): el
        repositorio de GitHub, Ko-fi y, debajo, una fila «@cuenta · iconos»
        por cuenta."""
        t = tokens()
        box = QFrame(parent)
        box.setObjectName("farewellSocial")
        box.setStyleSheet(f"#farewellSocial {{ background: {t.bg}; border: 1px solid {t.border};"
                          f" border-radius: {t.radius_small + 2}px; }}"
                          f"#farewellSep {{ background: {t.border}; border: none; }}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(0)

        def separator():
            sep = QFrame(box)
            sep.setObjectName("farewellSep")
            sep.setFixedHeight(1)
            lay.addWidget(sep)

        lay.addWidget(self._repo_row(box))
        separator()
        lay.addWidget(self._kofi_row(box))
        for handle, links in Config.SOCIAL_LINKS:
            separator()
            row = QWidget(box)
            h = QHBoxLayout(row)
            h.setContentsMargins(14, 4, 8, 4)
            h.setSpacing(2)
            label = QLabel(handle, row)
            label.setFont(fonts.font(fonts.SMALL))
            h.addWidget(label, 1)
            for network, url in links:
                name = _NETWORKS[network][0]
                link = LinkButton(url=url, icon=f"brand-{network}", icon_size=16,
                                  color="fg_dim", hover=network_hover_color(network, t.bg),
                                  tooltip=f"{name} · {handle}", chip=True, parent=row)
                link.opened.connect(self._on_link_opened)
                h.addWidget(link)
                self.links.append(link)
            lay.addWidget(row)
        return box

    def _repo_row(self, parent) -> QWidget:
        """«fabricioplus/pomogrowth» (ir al repositorio) y «Dar estrella»:
        los dos abren Config.GITHUB_URL, donde se califica con la estrella."""
        url = Config.GITHUB_URL
        row, self.repo_links = self._link_row(
            parent, url.removeprefix("https://github.com/"), url, "brand-github",
            f"Ver el repositorio en GitHub · {url}",
            "Dar estrella", "star", "Califica PomoGrowth con una estrella en GitHub")
        return row

    def _kofi_row(self, parent) -> QWidget:
        """«Apóyanos en Ko-fi» y «Donar»: los dos abren Config.KOFI_URL. Va en
        su fila: junto a «Dar estrella» no cabía con margen."""
        url = Config.KOFI_URL
        tip = f"Invítanos un café en Ko-fi · {url.removeprefix('https://')}"
        row, self.kofi_links = self._link_row(
            parent, "Apóyanos en Ko-fi", url, "brand-kofi", tip, "Donar", "coffee", tip)
        return row

    def _link_row(self, parent, text, url, icon, tooltip, pill_text, pill_icon,
                  pill_tooltip):
        """Fila del carril: un enlace con su logo a la izquierda y una
        cápsula (la llamada a la acción) a la derecha, a la misma URL."""
        row = QWidget(parent)
        row.setMinimumHeight(38)  # el alto de las filas de las cuentas
        h = QHBoxLayout(row)
        h.setContentsMargins(14, 4, 14, 4)
        h.setSpacing(8)
        link = LinkButton(text, url, icon=icon, icon_size=16, color="fg", hover="accent",
                          tooltip=tooltip, parent=row)
        link.setFont(fonts.font(fonts.SMALL))
        h.addWidget(link, 0, Qt.AlignVCenter)
        h.addStretch(1)
        pill = LinkButton(pill_text, url, icon=pill_icon, icon_size=13,
                          color="accent", hover="accent_hover", tooltip=pill_tooltip,
                          pill=True, parent=row)
        h.addWidget(pill, 0, Qt.AlignVCenter)
        for button in (link, pill):
            button.opened.connect(self._on_link_opened)
        return row, [link, pill]

    # --- Cuenta y pausa ---
    def _cursor_on_card(self) -> bool:
        return self._card.rect().contains(self._card.mapFromGlobal(QCursor.pos()))

    def is_paused(self) -> bool:
        return self._countdown.state() == QAbstractAnimation.Paused

    def _set_paused(self, paused: bool) -> None:
        state = self._countdown.state()
        if paused and state == QAbstractAnimation.Running:
            self._countdown.pause()
        elif not paused and state == QAbstractAnimation.Paused:
            self._countdown.resume()
        self._bar.set_paused(self.is_paused())

    def eventFilter(self, obj, event):
        if obj is self._card and not self._closing:
            kind = event.type()
            if kind == QEvent.MouseMove:
                self._hover_armed = True  # el ratón se movió: ya cuenta
            if kind in (QEvent.Enter, QEvent.MouseMove) and self._hover_armed:
                self._set_paused(True)
            elif kind == QEvent.Leave:
                self._hover_armed = True
                self._set_paused(False)
        return super().eventFilter(obj, event)

    # --- Salida ---
    def _on_link_opened(self, _url: str) -> None:
        # El navegador ya se está abriendo: la despedida se va poco después
        QTimer.singleShot(LINK_CLOSE_MS, self.close_now)

    def close_now(self) -> None:
        """Fin de la cuenta, Esc, «Cerrar» o un enlace abierto: sale con fade
        y llama a on_done (solo la primera vez)."""
        if self._closing:
            return
        self._closing = True
        self._countdown.stop()
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(FADE_OUT_MS)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.InCubic)
        fade.start(QPropertyAnimation.DeleteWhenStopped)
        QTimer.singleShot(FADE_OUT_MS, self._finish)

    def _finish(self) -> None:
        if self._done:
            return
        self._done = self._closing = True
        self._countdown.stop()
        self.hide()
        self._on_done()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.close_now()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # Cerrada desde fuera (Alt+F4 sobre ella, un script): sin ella la app
        # se quedaría sin interfaz, así que también llama a on_done
        super().closeEvent(event)
        self._finish()
