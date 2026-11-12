"""Presentación del primer arranque: tres diapositivas con ilustraciones
animadas para entender en medio minuto cómo ayuda PomoGrowth.

1. «Una tarea, toda tu atención»: temporizador, descansos y modo Libre.
2. «Tu espacio sin distracciones»: modo enfoque, música/ambiente y Asana.
3. «Mira cómo creces»: XP, niveles, monedas, racha, estadísticas y mascota.

Las ilustraciones se pintan con QPainter (sin imágenes externas: solo los
iconos SVG de assets/icons y la mascota de la marca) sobre un lienzo de
diseño de 660 × 220 que se escala al hueco disponible. Cada una corre en
bucle a ~60 fps solo mientras está a la vista (su temporizador se para al
ocultarse: al cambiar de diapositiva o al cerrar el diálogo).

La abre el controlador (`AppController.show_onboarding`): al primer arranque
de una instalación nueva y desde Ajustes → Perfil → «Ver la presentación».
Cerrarla de cualquier forma la da por vista (eso lo guarda el controlador).
"""
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import (QEasingCurve, QElapsedTimer, QPointF, QRectF,
                            QSize, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QGuiApplication,
                           QLinearGradient, QPainter, QPainterPath, QPen,
                           QPolygonF, QRadialGradient)
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from ui.dialogs.branded_dialogs import BrandedDialog
from ui.greeting import first_name, with_name
from ui.theme import brand, fonts
from ui.theme.tokens import tokens
from ui.widgets.icon_button import IconButton, tinted_icon

# Transición entre diapositivas (deslizar + fundir) y cadencia de las
# animaciones (~60 fps)
TRANSITION_MS = 260
FRAME_MS = 16
SLIDE_OFFSET = 56  # px que se desplaza la diapositiva al entrar o salir

# Ancho de la tarjeta (760 px: 712 de contenido y el padding) y alto
# preferido de la ilustración; con poca pantalla (escalado al 200 %) se encogen
CARD_W, CONTENT_W, SCENE_H, SCENE_MIN_H = 760, 712, 204, 100
# Lienzo de diseño de las ilustraciones (se escala manteniendo la proporción)
DESIGN_W, DESIGN_H = 660.0, 220.0

# El dorado de la barra de XP y de las monedas de la cabecera
# (ui/widgets/xp_bar.py): la ilustración de progreso usa los mismos
XP_GOLD = ('#f7e08a', '#d19a00')
COIN_GOLD = '#f2c94c'


@dataclass(frozen=True)
class Slide:
    key: str
    title: str
    text: str                          # `{min}`: minutos del pomodoro
    chips: Tuple[Tuple[str, str], ...]  # (icono, texto)


# Textos ajustados a lo que la app hace de verdad (ver CONTENIDO_WEB.md,
# «Qué no prometer»): ClickUp solo crea tareas; la mascota se gana
SLIDES = (
    Slide("focus", "Una tarea, toda tu atención",
          "Elige una tarea y pulsa empezar: bloques de enfoque de {min} minutos"
          " con sus descansos y un aviso sonoro al acabar cada uno. ¿Prefieres"
          " no tener límite? Usa el modo Libre.",
          (("timer", "Pomodoro de {min} min"), ("coffee", "Descansos"),
           ("infinity", "Modo Libre"))),
    Slide("space", "Tu espacio sin distracciones",
          "El modo enfoque bloquea las webs que te distraen. Pon tu música, una"
          " radio o sonidos de ambiente como lluvia u océano, y ten tus tareas"
          " de Asana en el mismo sitio (y crea tareas nuevas también en ClickUp).",
          (("shield", "Modo enfoque"), ("music", "Música y ambiente"),
           ("asana-mark", "Asana y ClickUp"))),
    Slide("grow", "Mira cómo creces",
          "Cada pomodoro suma XP y monedas: subes de nivel, alargas tu racha de"
          " días y ves tu avance en Estadísticas. Con las monedas te ganas temas"
          " y una mascota que te acompaña.",
          (("trending-up", "XP y niveles"), ("flame", "Racha de días"),
           ("estadisticas", "Estadísticas"))),
)


# ------------------------------------------------------------ curvas y color
def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _phase(x: float, start: float, duration: float) -> float:
    """Avance 0..1 de un tramo que empieza en `start` y dura `duration` s."""
    if duration <= 0:
        return 1.0 if x >= start else 0.0
    return _clamp((x - start) / duration)


def _out_cubic(v: float) -> float:
    return 1 - (1 - v) ** 3


def _in_out(v: float) -> float:
    return 4 * v ** 3 if v < 0.5 else 1 - (-2 * v + 2) ** 3 / 2


def _out_back(v: float, s: float = 1.70158) -> float:
    """Llega pasándose un poco y vuelve (el «pop» de la app)."""
    v -= 1
    return 1 + (s + 1) * v ** 3 + s * v ** 2 if v < 0 else 1.0


def _c(color, alpha: float = 1.0) -> QColor:
    """Color con su alfa multiplicado por `alpha`."""
    c = QColor(color)
    c.setAlphaF(_clamp(c.alphaF() * alpha))
    return c


def _mix(a, b, f: float) -> QColor:
    a, b, f = QColor(a), QColor(b), _clamp(f)
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * f,
                           a.greenF() + (b.greenF() - a.greenF()) * f,
                           a.blueF() + (b.blueF() - a.blueF()) * f,
                           a.alphaF() + (b.alphaF() - a.alphaF()) * f)


def _draw_icon(p: QPainter, name: str, color: str, center: QPointF,
               size: float) -> None:
    """Icono SVG tintado centrado en `center` (unidades de diseño). El
    pixmap se pide al tamaño real en pantalla, cuantizado a 4 px: nítido
    con cualquier escala y sin llenar la caché de tinted_icon."""
    m = p.worldTransform()
    scale = math.hypot(m.m11(), m.m12()) or 1.0
    px = max(8, int(round(size * scale / 4.0)) * 4)
    pm = tinted_icon(name, QColor(color).name(), px).pixmap(px, px)
    target = QRectF(center.x() - size / 2, center.y() - size / 2, size, size)
    p.drawPixmap(target, pm, QRectF(pm.rect()))


def _partial(points: List[QPointF], f: float) -> List[QPointF]:
    """Primeros `f` (0..1) de una polilínea, para trazos que se dibujan solos."""
    if f <= 0 or len(points) < 2:
        return []
    lengths = [math.dist((a.x(), a.y()), (b.x(), b.y()))
               for a, b in zip(points, points[1:])]
    goal = sum(lengths) * _clamp(f)
    out = [points[0]]
    for (a, b), seg in zip(zip(points, points[1:]), lengths):
        if goal >= seg:
            out.append(b)
            goal -= seg
            continue
        k = goal / seg if seg else 0
        out.append(QPointF(a.x() + (b.x() - a.x()) * k, a.y() + (b.y() - a.y()) * k))
        break
    return out


# --------------------------------------------------------------- ilustración
class _Scene(QWidget):
    """Lienzo animado de una diapositiva: panel hundido con un leve
    resplandor del acento y la escena de `_paint`, en bucle de PERIOD s.

    El reloj corre solo mientras el widget está a la vista (showEvent →
    vuelve a empezar; hideEvent → se para). `set_time`/`advance` mueven la
    animación a mano (tests y capturas)."""
    PERIOD = 6.0
    GLOW = QPointF(DESIGN_W / 2, DESIGN_H / 2)  # centro del resplandor

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._t = 0.0
        self._last_ms = 0
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._on_frame)
        self.setMinimumHeight(SCENE_MIN_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Opaca: si no, cada fotograma repinta debajo el marco de la sombra
        # del diálogo y su QGraphicsDropShadowEffect vuelve a difuminarlo
        # (un núcleo entero de CPU); paintEvent rellena todo el rectángulo
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def sizeHint(self) -> QSize:
        return QSize(CONTENT_W, SCENE_H)

    def minimumSizeHint(self) -> QSize:
        return QSize(320, SCENE_MIN_H)

    # ---- reloj
    def restart(self) -> None:
        self._t = 0.0
        self._last_ms = 0
        self._clock.start()
        self._timer.start()
        self.update()

    def stop(self) -> None:
        self._timer.stop()

    def is_running(self) -> bool:
        return self._timer.isActive()

    def time(self) -> float:
        return self._t

    def set_time(self, seconds: float) -> None:
        self._t = max(0.0, float(seconds))
        self.update()

    def advance(self, seconds: float) -> None:
        self.set_time(self._t + seconds)

    def _on_frame(self) -> None:
        now = self._clock.elapsed()
        self._t += (now - self._last_ms) / 1000.0
        self._last_ms = now
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.restart()  # cada vez que se entra en la diapositiva, desde el principio

    def hideEvent(self, event):
        self.stop()
        super().hideEvent(event)

    # ---- pintado
    def _loop_alpha(self, x: float) -> float:
        """Entrada y salida suaves del bucle (para las escenas que se reinician)."""
        return _phase(x, 0.0, 0.3) * (1 - _phase(x, self.PERIOD - 0.5, 0.5))

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform
                         | QPainter.TextAntialiasing)
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        shape = QPainterPath()
        shape.addRoundedRect(panel, t.radius, t.radius)
        p.fillRect(self.rect(), QColor(t.surface))  # la tarjeta, bajo las esquinas
        p.fillPath(shape, QColor(t.bg))
        p.setClipPath(shape)
        scale = min(panel.width() / DESIGN_W, panel.height() / DESIGN_H)
        p.translate(panel.center())
        p.scale(scale, scale)
        p.translate(-DESIGN_W / 2, -DESIGN_H / 2)
        glow = QRadialGradient(self.GLOW, 220)
        glow.setColorAt(0.0, _c(t.accent, 0.11))
        glow.setColorAt(1.0, _c(t.accent, 0.0))
        p.fillRect(QRectF(-DESIGN_W, -DESIGN_H, DESIGN_W * 3, DESIGN_H * 3), QBrush(glow))
        p.save()
        self._paint(p, t, self._t, self._t % self.PERIOD)
        p.restore()
        p.resetTransform()
        p.setClipping(False)
        p.setOpacity(1.0)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(t.border), 1))
        p.drawPath(shape)
        p.end()

    def _paint(self, p: QPainter, t, total: float, x: float) -> None:
        """`total`: segundos desde que se mostró; `x`: posición en el bucle."""
        raise NotImplementedError


class FocusScene(_Scene):
    """1 · Una tarea se marca como actual, el anillo se llena con la cuenta
    atrás y al completarse salta el check, suena el aviso y llega el descanso."""
    PERIOD = 7.4
    GLOW = QPointF(470, 98)
    CARDS = ((132, 84), (164, 104), (110, 70))  # anchos de las dos líneas
    RING = (470.0, 98.0, 66.0)                   # centro y radio
    FILL_AT, FILL_S, DONE_AT = 1.6, 3.0, 4.6

    def __init__(self, parent: QWidget, minutes: int = 25):
        super().__init__(parent)
        try:
            self._minutes = max(1, int(minutes))
        except (TypeError, ValueError):
            self._minutes = 25

    def _paint(self, p, t, total, x):
        alpha = self._loop_alpha(x)
        self._paint_tasks(p, t, x, alpha)
        self._paint_link(p, t, x, alpha)
        self._paint_ring(p, t, x, alpha)
        self._paint_bell(p, t, x, alpha)
        self._paint_break(p, t, x, alpha)

    def _selected(self, x: float) -> float:
        return _out_cubic(_phase(x, 0.95, 0.4))

    def _paint_tasks(self, p, t, x, alpha):
        sel_f = self._selected(x)
        for i, (w1, w2) in enumerate(self.CARDS):
            a = _out_cubic(_phase(x, 0.1 + 0.1 * i, 0.5))
            sel = sel_f if i == 1 else 0.0
            rect = QRectF(44, 30 + 56 * i + (1 - a) * 16 - 3 * sel, 248, 44)
            p.setOpacity(alpha * a)
            if sel > 0:  # halo de la tarea actual
                p.setPen(Qt.NoPen)
                p.setBrush(_c(t.accent, 0.12 * sel))
                p.drawRoundedRect(rect.adjusted(-5, -5, 5, 5), 15, 15)
            p.setBrush(QColor(t.surface))
            p.setPen(QPen(_mix(t.border, t.accent, sel), 1 + sel))
            p.drawRoundedRect(rect, 10, 10)
            cy = rect.center().y()
            dot = QPointF(rect.x() + 22, cy)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_mix(t.fg_muted, t.accent, sel), 1.6))
            p.drawEllipse(dot, 8, 8)
            p.setPen(Qt.NoPen)
            if sel > 0:
                p.setBrush(QColor(t.accent))
                p.drawEllipse(dot, 4.2 * sel, 4.2 * sel)
            p.setBrush(_c(_mix(t.fg_dim, t.fg, sel), 0.8))
            p.drawRoundedRect(QRectF(rect.x() + 40, cy - 8, w1, 7), 3.5, 3.5)
            p.setBrush(_c(t.fg_muted, 0.55))
            p.drawRoundedRect(QRectF(rect.x() + 40, cy + 3, w2, 5), 2.5, 2.5)
            right = QPointF(rect.right() - 22, cy)
            tag = t.tag_colors[(i * 3) % len(t.tag_colors)]
            p.setBrush(_c(tag, 1 - sel))
            p.drawEllipse(right, 4, 4)
            if sel > 0:  # «en curso»: botón de reproducir
                r = 11 * _out_back(sel)
                p.setBrush(QColor(t.accent))
                p.drawEllipse(right, r, r)
                k = r / 11
                p.setBrush(QColor(t.on_accent))
                p.drawPolygon(QPolygonF([right + QPointF(-3 * k, -5 * k),
                                         right + QPointF(5.5 * k, 0),
                                         right + QPointF(-3 * k, 5 * k)]))

    def _paint_link(self, p, t, x, alpha):
        """Trazo discontinuo que fluye de la tarea actual al anillo."""
        d = _out_cubic(_phase(x, 1.15, 0.45))
        if d <= 0:
            return
        cx, cy, r = self.RING
        start = QPointF(298, 105)
        end = QPointF(cx - r - 16, cy)
        path = QPainterPath(start)
        path.cubicTo(start + QPointF(44, 0), end - QPointF(44, 0), end)
        pts = [path.pointAtPercent(d * k / 24) for k in range(25)]
        pen = QPen(_c(t.accent, 0.75), 2, Qt.CustomDashLine, Qt.RoundCap)
        pen.setDashPattern([2.0, 3.0])
        pen.setDashOffset(-x * 10)
        p.setOpacity(alpha)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(QPolygonF(pts))
        if d >= 1:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawEllipse(end, 3, 3)

    def _paint_ring(self, p, t, x, alpha):
        cx, cy, r = self.RING
        appear = _phase(x, 0.25, 0.6)
        p.save()
        p.translate(cx, cy)
        s = 0.85 + 0.15 * _out_back(appear)
        p.scale(s, s)
        p.setOpacity(alpha * _clamp(appear * 2))
        box = QRectF(-r, -r, 2 * r, 2 * r)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(t.surface_active), 10, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(box)
        prog = _in_out(_phase(x, self.FILL_AT, self.FILL_S))
        if prog > 0:
            grad = QConicalGradient(0, 0, 90)
            grad.setColorAt(0.0, QColor(t.accent))
            grad.setColorAt(0.5, QColor(t.accent_grad_end))
            grad.setColorAt(1.0, QColor(t.accent))
            p.setPen(QPen(QBrush(grad), 10, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(box, 90 * 16, -int(prog * 360 * 16))
            ang = math.radians(90 - prog * 360)
            head = QPointF(r * math.cos(ang), -r * math.sin(ang))
            gone = 1 - _phase(x, self.DONE_AT, 0.25)  # completo, sin cabeza
            p.setPen(Qt.NoPen)
            p.setBrush(_c(t.accent, 0.28 * gone))
            p.drawEllipse(head, 10, 10)
            p.setBrush(_c(t.fg, 0.9 * gone))
            p.drawEllipse(head, 2.6, 2.6)
        done = _phase(x, self.DONE_AT, 0.3)
        if done < 1:  # cuenta atrás de los minutos del pomodoro
            left = round(self._minutes * 60 * (1 - prog))
            base = p.opacity()
            p.setOpacity(base * (1 - done))
            p.setPen(QColor(t.fg))
            p.setFont(fonts.display(20))
            p.drawText(QRectF(-r, -26, 2 * r, 34), Qt.AlignCenter,
                       f"{left // 60:02d}:{left % 60:02d}")
            p.setPen(QColor(t.fg_dim))
            p.setFont(fonts.font(fonts.XS))
            p.drawText(QRectF(-r, 10, 2 * r, 18), Qt.AlignCenter, "Enfoque")
            p.setOpacity(base)
        pop = _phase(x, self.DONE_AT, 0.5)
        if pop > 0:
            burst = _out_cubic(_phase(x, self.DONE_AT, 0.9))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_c(t.accent, 0.55 * (1 - burst)), 2))
            p.drawEllipse(QPointF(0, 0), r + 6 + 20 * burst, r + 6 + 20 * burst)
            p.setPen(Qt.NoPen)
            for k in range(12):  # destellos
                a = math.radians(k * 30 + 15)
                dist = r + 10 + 26 * burst
                size = 3.2 * (1 - burst) + 0.6
                p.setBrush(_c(t.accent if k % 2 else t.accent_grad_end, 1 - burst))
                p.drawEllipse(QPointF(dist * math.cos(a), dist * math.sin(a)), size, size)
            k = _out_back(pop)
            p.setBrush(QColor(t.accent))
            p.drawEllipse(QPointF(0, 0), 30 * k, 30 * k)
            check = _partial([QPointF(-12, 1), QPointF(-3, 10), QPointF(14, -9)],
                             _out_cubic(_phase(x, self.DONE_AT + 0.15, 0.35)))
            if check:
                p.setPen(QPen(QColor(t.on_accent), 5, Qt.SolidLine, Qt.RoundCap,
                              Qt.RoundJoin))
                p.drawPolyline(QPolygonF(check))
        p.restore()

    def _paint_bell(self, p, t, x, alpha):
        """La campana del aviso: discreta durante el pomodoro, suena al acabar."""
        cx, cy, r = self.RING
        center = QPointF(cx + r + 26, cy - r + 8)
        ring = _phase(x, self.DONE_AT + 0.05, 1.1)
        ringing = 0 < ring < 1
        p.save()
        p.setOpacity(alpha * _phase(x, 0.5, 0.4))
        p.translate(center)
        if ringing:
            p.rotate(16 * math.sin(ring * math.pi * 7) * (1 - ring))
            for j in range(2):  # ondas del sonido
                w = _clamp(ring * 1.6 - j * 0.3)
                if 0 < w < 1:
                    p.setPen(QPen(_c(t.warning, 0.8 * (1 - w)), 2, Qt.SolidLine, Qt.RoundCap))
                    rr = 16 + 12 * w
                    p.drawArc(QRectF(-rr, -rr, 2 * rr, 2 * rr), -40 * 16, 80 * 16)
                    p.drawArc(QRectF(-rr, -rr, 2 * rr, 2 * rr), 140 * 16, 80 * 16)
        rung = ring > 0
        _draw_icon(p, "bell-ring" if rung else "bell",
                   t.warning if rung else t.fg_muted, QPointF(0, 0), 22)
        p.restore()

    def _paint_break(self, p, t, x, alpha):
        """Tras el pomodoro, el descanso (en el color de descanso de la app)."""
        a = _out_cubic(_phase(x, self.DONE_AT + 0.8, 0.45))
        if a <= 0:
            return
        cx, cy, r = self.RING
        pill = QRectF(cx - 58, cy + r + 16 + (1 - a) * 8, 116, 26)
        p.setOpacity(alpha * a)
        p.setPen(QPen(_c(t.break_accent, 0.55), 1))
        p.setBrush(_c(t.break_accent, 0.16))
        p.drawRoundedRect(pill, 13, 13)
        _draw_icon(p, "coffee", t.break_accent, QPointF(pill.x() + 20, pill.center().y()), 15)
        p.setPen(QColor(t.break_accent))
        p.setFont(fonts.font(fonts.XS, bold=True))
        p.drawText(pill.adjusted(32, 0, -10, 0), Qt.AlignVCenter | Qt.AlignLeft, "Descanso")


class SpaceScene(_Scene):
    """2 · El escudo del modo enfoque rechaza las webs que distraen (rebotan),
    llueve en la tarjeta de ambiente junto al ecualizador de la música y una
    tarea de Asana se sincroniza."""
    PERIOD = 6.0
    GLOW = QPointF(200, 110)
    SHIELD = QPointF(200, 110)
    AURA = 74.0
    # Webs que distraen: (icono, color, altura de salida)
    DISTRACTIONS = (("play", "error", 54), ("image", "warning", 110),
                    ("globe", "info", 166))
    TRIP_S = 2.0  # cada distracción: llega, rebota y se va

    def _paint(self, p, t, total, x):
        self._paint_shield(p, t, total, x)
        self._paint_distractions(p, t, total, x)
        self._paint_ambient(p, t, total, x)
        self._paint_sync(p, t, total, x)

    def _paint_shield(self, p, t, total, x):
        intro = _out_back(_phase(total, 0.0, 0.7))
        breathe = 1 + 0.025 * math.sin(2 * math.pi * x / 3.0)
        p.save()
        p.translate(self.SHIELD)
        p.setOpacity(_clamp(intro))
        p.scale(intro * breathe, intro * breathe)
        p.setPen(Qt.NoPen)
        p.setBrush(_c(t.accent, 0.07))
        p.drawEllipse(QPointF(0, 0), self.AURA, self.AURA)
        p.setBrush(Qt.NoBrush)
        for j in range(2):  # ondas del escudo
            w = (x / 2.0 + j * 0.5) % 1.0
            p.setPen(QPen(_c(t.accent, 0.35 * (1 - w)), 1.5))
            rr = 56 + 30 * w
            p.drawEllipse(QPointF(0, 0), rr, rr)
        shield = QPainterPath(QPointF(0, -54))
        shield.cubicTo(16, -44, 32, -40, 46, -40)
        shield.lineTo(46, -2)
        shield.cubicTo(46, 28, 24, 46, 0, 56)
        shield.cubicTo(-24, 46, -46, 28, -46, -2)
        shield.lineTo(-46, -40)
        shield.cubicTo(-32, -40, -16, -44, 0, -54)
        grad = QLinearGradient(0, -54, 0, 56)
        grad.setColorAt(0.0, QColor(t.accent_hover))
        grad.setColorAt(1.0, QColor(t.accent_dim))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(_c(t.accent_hover, 0.9), 1.5))
        p.drawPath(shield)
        p.setPen(QPen(QColor(t.on_accent), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(QPolygonF([QPointF(-16, 2), QPointF(-5, 13), QPointF(17, -11)]))
        p.restore()

    def _paint_distractions(self, p, t, total, x):
        hit_x = self.SHIELD.x() - self.AURA - 17
        for k, (icon, token, y0) in enumerate(self.DISTRACTIONS):
            delay = k * self.TRIP_S / 3
            if total < delay + 0.4:  # la primera vuelta sale escalonada
                continue
            u = ((x - delay) % self.TRIP_S) / self.TRIP_S
            y_hit = self.SHIELD.y() + (y0 - self.SHIELD.y()) * 0.45
            side = -1 if y0 < self.SHIELD.y() else 1
            if u < 0.42:  # llega acelerando
                v = (u / 0.42) ** 2
                pos = QPointF(-24 + (hit_x + 24) * v, y0 + (y_hit - y0) * v)
                rot, a, s = 0.0, _clamp(u / 0.06), 1.0
            else:  # rebota y se apaga
                v = _out_cubic((u - 0.42) / 0.58)
                pos = QPointF(hit_x - 74 * v, y_hit + side * 34 * v)
                rot, a, s = 40 * side * v, 1 - v, 1 - 0.25 * v
            color = getattr(t, token)
            p.save()
            p.translate(pos)
            p.rotate(rot)
            p.scale(s, s)
            p.setOpacity(a)
            p.setBrush(_c(color, 0.16))
            p.setPen(QPen(_c(color, 0.6), 1.2))
            p.drawRoundedRect(QRectF(-17, -17, 34, 34), 9, 9)
            _draw_icon(p, icon, color, QPointF(0, 0), 18)
            p.restore()
            if 0.42 <= u < 0.8:  # el choque contra el escudo
                w = (u - 0.42) / 0.38
                ang = math.degrees(math.atan2(-(y_hit - self.SHIELD.y()),
                                              hit_x - self.SHIELD.x()))
                rr = self.AURA + 2 + 8 * w
                p.setOpacity(1 - w)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(t.accent), 3, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(QRectF(self.SHIELD.x() - rr, self.SHIELD.y() - rr, 2 * rr, 2 * rr),
                          int((ang - 24) * 16), 48 * 16)
        p.setOpacity(1.0)

    def _card(self, p, t, rect: QRectF, intro: float) -> QRectF:
        rect = rect.translated(24 * (1 - intro), 0)
        p.setOpacity(intro)
        p.setBrush(QColor(t.surface))
        p.setPen(QPen(QColor(t.border), 1))
        p.drawRoundedRect(rect, 12, 12)
        return rect

    def _paint_ambient(self, p, t, total, x):
        """Lluvia de ambiente + ecualizador de la música."""
        card = self._card(p, t, QRectF(330, 24, 300, 76),
                          _out_cubic(_phase(total, 0.15, 0.6)))
        tile = QRectF(card.x() + 12, card.y() + 10, 66, 56)
        p.setPen(Qt.NoPen)
        p.setBrush(_c(t.info, 0.14))
        p.drawRoundedRect(tile, 10, 10)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(tile, 10, 10)
        p.setClipPath(clip, Qt.IntersectClip)
        for i in range(7):  # gotas: 6 caídas por bucle, cada una a su ritmo
            v = (x + i * 0.37) % 1.0
            dx = tile.x() + 10 + i * 7.6
            y = tile.y() + 22 + v * 40
            p.setPen(QPen(_c(t.info, 0.85 * (1 - 0.6 * v)), 2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(dx, y), QPointF(dx - 1.5, y + 6))
        p.restore()
        _draw_icon(p, "cloud-rain", t.info, QPointF(tile.center().x(), tile.y() + 16), 26)
        p.setPen(Qt.NoPen)
        p.setBrush(_c(t.fg_dim, 0.8))
        p.drawRoundedRect(QRectF(card.x() + 94, card.y() + 24, 80, 7), 3.5, 3.5)
        p.setBrush(_c(t.fg_muted, 0.55))
        p.drawRoundedRect(QRectF(card.x() + 94, card.y() + 38, 52, 5), 2.5, 2.5)
        _draw_icon(p, "music", t.accent, QPointF(card.x() + 196, card.center().y()), 20)
        base = card.bottom() - 18
        for i, (k, phase) in enumerate(((3, 0.0), (4, 1.3), (5, 2.1), (6, 0.7),
                                        (4, 2.8), (5, 1.9), (3, 0.4))):
            h = 8 + 28 * (0.5 + 0.5 * math.sin(2 * math.pi * k * x / self.PERIOD + phase))
            bar = QRectF(card.x() + 214 + i * 11, base - h, 6, h)
            grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
            grad.setColorAt(0.0, QColor(t.accent))
            grad.setColorAt(1.0, QColor(t.accent_grad_end))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(bar, 3, 3)
        p.setOpacity(1.0)

    def _paint_sync(self, p, t, total, x):
        """Una tarea de Asana que se sincroniza. Sin marca de ClickUp: con
        ClickUp la app solo crea tareas, no sincroniza (CONTENIDO_WEB.md,
        «Qué no prometer»)."""
        card = self._card(p, t, QRectF(330, 120, 300, 76),
                          _out_cubic(_phase(total, 0.3, 0.6)))
        base_alpha = p.opacity()
        chip = QRectF(card.x() + 12, card.y() + 10, 56, 56)
        p.setPen(Qt.NoPen)
        p.setBrush(_c(t.asana, 0.16))
        p.drawRoundedRect(chip, 10, 10)
        _draw_icon(p, "asana-mark", t.asana, chip.center(), 28)
        p.setBrush(_c(t.fg_dim, 0.8))
        p.drawRoundedRect(QRectF(card.x() + 86, card.y() + 24, 104, 7), 3.5, 3.5)
        p.setBrush(_c(t.fg_muted, 0.55))
        p.drawRoundedRect(QRectF(card.x() + 86, card.y() + 38, 70, 5), 2.5, 2.5)
        s = (x % 3.0) / 3.0
        spot = QPointF(card.right() - 30, card.center().y())
        if s < 0.55:  # sincronizando
            p.save()
            p.translate(spot)
            p.rotate(s / 0.55 * 540)
            _draw_icon(p, "sync", t.fg_dim, QPointF(0, 0), 22)
            p.restore()
        else:  # al día
            pop = _out_back(_phase(s, 0.55, 0.12))
            flash = 1 - _phase(s, 0.55, 0.4)
            if flash > 0:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(_c(t.accent, 0.6 * flash), 1.5))
                p.drawRoundedRect(card, 12, 12)
            p.save()
            p.translate(spot)
            p.scale(pop, pop)
            _draw_icon(p, "check-circle", t.accent, QPointF(0, 0), 24)
            p.restore()
        p.setOpacity(base_alpha)


class GrowScene(_Scene):
    """3 · La barra de XP se llena pomodoro a pomodoro hasta subir de nivel,
    las monedas saltan al contador, la racha se enciende, la barra de hoy de
    las estadísticas crece y la mascota lo celebra."""
    PERIOD = 7.2
    GLOW = QPointF(250, 70)
    GAINS = (0.7, 1.5, 2.3)       # cada pomodoro: +25 XP y +5 monedas
    LEVEL_UP = 3.1
    BAR = QRectF(104, 54, 296, 20)
    BADGE = QPointF(64, 64)
    STREAK_TILE = QRectF(40, 116, 150, 80)
    COIN_TILE = QRectF(206, 116, 150, 80)
    STATS = (0.38, 0.62, 0.5, 0.8)  # días anteriores; el último es hoy
    MASCOT = QRectF(566, 126, 68, 68)

    def _paint(self, p, t, total, x):
        alpha = self._loop_alpha(x)
        self._paint_xp(p, t, x, alpha)
        self._paint_tiles(p, t, x, alpha)
        self._paint_coins(p, t, x, alpha)
        self._paint_stats(p, t, x, alpha)
        self._paint_mascot(p, t, x, alpha)

    def _gained(self, x: float) -> float:
        """Pomodoros ya sumados (con su animación): 0..3."""
        return sum(_out_cubic(_phase(x, g, 0.45)) for g in self.GAINS)

    def _paint_xp(self, p, t, x, alpha):
        lu = _phase(x, self.LEVEL_UP, 0.5)
        drain = _in_out(_phase(x, self.LEVEL_UP + 0.25, 0.45))
        fill = 0.22 + 0.26 * self._gained(x)
        fill = fill * (1 - drain) + 0.1 * drain
        p.setOpacity(alpha * _out_cubic(_phase(x, 0.0, 0.4)))
        # Badge de nivel
        level = 4 if lu > 0 else 3
        pop = 1 + 0.25 * math.sin(math.pi * lu) if 0 < lu < 1 else 1.0
        p.save()
        p.translate(self.BADGE)
        p.scale(pop, pop)
        grad = QLinearGradient(0, -24, 0, 24)
        grad.setColorAt(0.0, QColor(XP_GOLD[0]))
        grad.setColorAt(1.0, QColor(XP_GOLD[1]))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(0, 0), 24, 24)
        p.setPen(QColor(t.on_accent))
        p.setFont(fonts.display(fonts.NORMAL + 2))
        p.drawText(QRectF(-24, -24, 48, 48), Qt.AlignCenter, str(level))
        p.restore()
        # Barra
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.surface_active))
        p.drawRoundedRect(self.BAR, 10, 10)
        bar = QRectF(self.BAR.x(), self.BAR.y(), max(20.0, self.BAR.width() * fill),
                     self.BAR.height())
        grad = QLinearGradient(bar.topLeft(), bar.topRight())
        grad.setColorAt(0.0, QColor(XP_GOLD[0]))
        grad.setColorAt(1.0, QColor(XP_GOLD[1]))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(bar, 10, 10)
        flash = (1 - drain) * _phase(x, self.LEVEL_UP - 0.15, 0.15)
        if flash > 0:  # la barra llena destella antes de vaciarse
            p.setBrush(_c(t.fg, 0.45 * flash))
            p.drawRoundedRect(self.BAR, 10, 10)
        # «+25 XP» de cada pomodoro, subiendo desde el final de lo ganado
        p.setFont(fonts.font(fonts.SMALL, bold=True))
        for i, g in enumerate(self.GAINS):
            v = _phase(x, g, 0.9)
            if 0 < v < 1:
                p.setOpacity(alpha * (1 - v) * _clamp(v * 6))
                p.setPen(QColor(XP_GOLD[0]))
                end_x = self.BAR.x() + self.BAR.width() * (0.22 + 0.26 * (i + 1))
                y = self.BAR.y() - 8 - 22 * _out_cubic(v)
                # Un poco a la derecha: la moneda sale del mismo punto hacia la izquierda
                p.drawText(QRectF(end_x - 6, y - 10, 80, 20), Qt.AlignCenter, "+25 XP")
        # Subida de nivel: destellos y la cápsula «¡Nivel 4!»
        if lu > 0:
            burst = _out_cubic(_phase(x, self.LEVEL_UP, 0.9))
            p.setOpacity(alpha * (1 - burst))
            p.setPen(Qt.NoPen)
            for k in range(10):
                a = math.radians(k * 36)
                dist = 28 + 30 * burst
                p.setBrush(QColor(XP_GOLD[0] if k % 2 else t.accent))
                p.drawEllipse(self.BADGE + QPointF(dist * math.cos(a), dist * math.sin(a)),
                              2.8, 2.8)
            pill_in = _out_back(_phase(x, self.LEVEL_UP + 0.05, 0.45))
            pill = QRectF(self.BAR.center().x() - 58, 14, 116, 28)
            p.save()
            p.translate(pill.center())
            p.scale(pill_in, pill_in)
            p.translate(-pill.center())
            p.setOpacity(alpha * _clamp(pill_in))
            p.setBrush(QColor(t.accent))
            p.drawRoundedRect(pill, 14, 14)
            _draw_icon(p, "sparkles", t.on_accent, QPointF(pill.x() + 20, pill.center().y()), 15)
            p.setPen(QColor(t.on_accent))
            p.setFont(fonts.font(fonts.SMALL, bold=True))
            p.drawText(pill.adjusted(32, 0, -8, 0), Qt.AlignCenter, "¡Nivel 4!")
            p.restore()
        p.setOpacity(1.0)

    def _tile(self, p, t, rect, icon, color, number: str, label: str, alpha,
              bump: float = 0.0, flicker: float = 0.0):
        p.setOpacity(alpha)
        p.setBrush(QColor(t.surface))
        p.setPen(QPen(QColor(t.border), 1))
        p.drawRoundedRect(rect, 12, 12)
        center = QPointF(rect.x() + 32, rect.center().y())
        glow = QRadialGradient(center, 26)
        glow.setColorAt(0.0, _c(color, 0.30 + 0.12 * flicker))
        glow.setColorAt(1.0, _c(color, 0.0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(center, 26, 26)
        p.save()
        p.translate(center)
        p.rotate(3 * flicker)
        p.scale(1 - 0.03 * flicker, 1 + 0.07 * flicker)
        _draw_icon(p, icon, color, QPointF(0, 0), 30)
        p.restore()
        p.save()
        num = QRectF(rect.x() + 60, rect.y() + 14, rect.width() - 66, 30)
        if bump > 0:
            k = 1 + 0.22 * math.sin(math.pi * bump)
            p.translate(num.x(), num.center().y())
            p.scale(k, k)
            p.translate(-num.x(), -num.center().y())
        p.setPen(QColor(t.fg))
        p.setFont(fonts.display(fonts.MEDIUM + 2))
        p.drawText(num, Qt.AlignVCenter | Qt.AlignLeft, number)
        p.restore()
        p.setPen(QColor(t.fg_dim))
        p.setFont(fonts.font(fonts.XS))
        p.drawText(QRectF(rect.x() + 60, rect.y() + 44, rect.width() - 64, 20),
                   Qt.AlignVCenter | Qt.AlignLeft, label)

    def _landed(self, x: float) -> int:
        return sum(1 for g in self.GAINS if _phase(x, g + 0.1, 0.7) >= 1)

    def _paint_tiles(self, p, t, x, alpha):
        a = alpha * _out_cubic(_phase(x, 0.15, 0.5))
        # Racha: el primer pomodoro del día la alarga
        first = self.GAINS[0] + 0.3
        streak_bump = _phase(x, first, 0.35)
        flicker = (math.sin(2 * math.pi * 12 * x / self.PERIOD)
                   + 0.5 * math.sin(2 * math.pi * 19 * x / self.PERIOD)) / 1.5
        self._tile(p, t, self.STREAK_TILE, "flame", t.warning,
                   "7" if x >= first else "6", "días seguidos", a,
                   bump=streak_bump if streak_bump < 1 else 0.0, flicker=flicker)
        # Monedas: +5 por pomodoro y +50 al subir de nivel
        landed = self._landed(x)
        coins = 120 + 5 * landed + (50 if x >= self.LEVEL_UP + 0.3 else 0)
        bump = 0.0
        for start in [g + 0.8 for g in self.GAINS] + [self.LEVEL_UP + 0.3]:
            v = _phase(x, start, 0.3)
            if 0 < v < 1:
                bump = v
        self._tile(p, t, self.COIN_TILE, "coins", COIN_GOLD, str(coins), "monedas", a,
                   bump=bump)
        p.setOpacity(1.0)

    def _paint_coins(self, p, t, x, alpha):
        """Una moneda por pomodoro salta de la barra al contador."""
        end = QPointF(self.COIN_TILE.x() + 32, self.COIN_TILE.center().y())
        for i, g in enumerate(self.GAINS):
            u = _phase(x, g + 0.1, 0.7)
            if not 0 < u < 1:
                continue
            start = QPointF(self.BAR.x() + self.BAR.width() * (0.22 + 0.26 * (i + 1)),
                            self.BAR.center().y())
            pos = QPointF(start.x() + (end.x() - start.x()) * u,
                          start.y() + (end.y() - start.y()) * u - 70 * math.sin(math.pi * u))
            spin = max(0.25, abs(math.cos(u * math.pi * 2.5)))
            p.save()
            p.translate(pos)
            p.scale(spin, 1.0)
            p.setOpacity(alpha * (1 - _phase(u, 0.85, 0.15)))
            p.setPen(QPen(QColor(XP_GOLD[1]), 1.5))
            p.setBrush(QColor(COIN_GOLD))
            p.drawEllipse(QPointF(0, 0), 9, 9)
            p.setPen(QPen(_c(XP_GOLD[0], 0.9), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(0, 0), 5, 5)
            p.restore()

    def _paint_stats(self, p, t, x, alpha):
        """Barras de las estadísticas: la de hoy crece con cada pomodoro."""
        base, max_h, w, gap = 198.0, 96.0, 22.0, 12.0
        left = 386.0
        p.setOpacity(alpha)
        p.setPen(QPen(QColor(t.border), 1.5))
        p.drawLine(QPointF(left - 6, base + 1), QPointF(left + 5 * w + 4 * gap + 6, base + 1))
        p.setPen(Qt.NoPen)
        today = 0.22 + 0.26 * self._gained(x)
        for i, target in enumerate(self.STATS + (today,)):
            grow = _out_back(_phase(x, 0.2 + 0.12 * i, 0.7))
            h = max(0.0, max_h * target * grow)
            if h < 1:
                continue
            bar = QRectF(left + i * (w + gap), base - h, w, h)
            if i == len(self.STATS):  # hoy, con el degradado del progreso
                grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                grad.setColorAt(0.0, QColor(t.accent))
                grad.setColorAt(1.0, QColor(t.accent_grad_end))
                p.setBrush(QBrush(grad))
            else:
                p.setBrush(_c(t.accent, 0.32))
            p.drawRoundedRect(bar, 5, 5)
        p.setOpacity(1.0)

    def _paint_mascot(self, p, t, x, alpha):
        """La mascota de la marca, en el acento del tema: flota y salta al subir de nivel."""
        bob = 3 * math.sin(2 * math.pi * 4 * x / self.PERIOD)
        hop = 18 * math.sin(math.pi * _phase(x, self.LEVEL_UP, 0.5))
        rect = self.MASCOT.translated(0, bob - hop)
        p.setOpacity(alpha * _out_cubic(_phase(x, 0.4, 0.5)))
        p.setPen(Qt.NoPen)
        p.setBrush(_c(t.bg_header, 0.9))
        shadow = 22 * (1 - hop / 40)
        p.drawEllipse(QPointF(self.MASCOT.center().x(), self.MASCOT.bottom() + 4), shadow, 4)
        brand.paint_mascot(p, rect, t.accent)
        p.setOpacity(1.0)


SCENES = {"focus": FocusScene, "space": SpaceScene, "grow": GrowScene}


# ------------------------------------------------------------------- piezas
class _Chip(QFrame):
    """Cápsula con icono y texto: una función de la diapositiva."""

    def __init__(self, icon: str, text: str, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("onboardingChip")
        self._icon_name = icon
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 12, 4)
        lay.setSpacing(6)
        self._icon = QLabel(self)
        self._icon.setFixedSize(14, 14)
        self.label = QLabel(text, self)
        self.label.setFont(fonts.font(fonts.XS))
        lay.addWidget(self._icon)
        lay.addWidget(self.label)
        self.restyle()

    def restyle(self) -> None:
        t = tokens()
        self.setStyleSheet(f"#onboardingChip {{ background: {t.bg}; border: 1px solid"
                           f" {t.border}; border-radius: 12px; }}"
                           f" #onboardingChip QLabel {{ background: transparent;"
                           f" color: {t.fg_dim}; }}")
        self._icon.setPixmap(tinted_icon(self._icon_name, t.accent, 14).pixmap(14, 14))


class SlidePage(QWidget):
    """Una diapositiva: ilustración arriba, «Paso n de 3», título, texto y
    las cápsulas de funciones."""

    def __init__(self, slide: Slide, index: int, total: int, parent: QWidget,
                 minutes: int = 25):
        super().__init__(parent)
        self.slide = slide
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        scene_cls = SCENES[slide.key]
        self.scene = (scene_cls(self, minutes) if scene_cls is FocusScene
                      else scene_cls(self))
        lay.addWidget(self.scene, 1)
        lay.addSpacing(18)
        self.kicker = QLabel(f"Paso {index + 1} de {total}", self)
        self.kicker.setFont(fonts.font(fonts.XS, bold=True))
        lay.addWidget(self.kicker)
        lay.addSpacing(2)
        self.title = QLabel(slide.title, self)
        self.title.setFont(fonts.display(fonts.LARGE, tabular=False))
        lay.addWidget(self.title)
        lay.addSpacing(6)
        self.text = QLabel(slide.text.format(min=minutes), self)
        self.text.setFont(fonts.font(fonts.SMALL + 1))
        self.text.setWordWrap(True)
        self.text.setProperty("role", "dim")
        lay.addWidget(self.text)
        lay.addSpacing(14)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.chips = [_Chip(icon, text.format(min=minutes), self)
                      for icon, text in slide.chips]
        for chip in self.chips:
            chips.addWidget(chip)
        chips.addStretch(1)
        lay.addLayout(chips)
        self.restyle()

    def restyle(self) -> None:
        self.kicker.setStyleSheet(f"color: {tokens().accent};")
        for chip in self.chips:
            chip.restyle()
        self.scene.update()


class _Stage(QWidget):
    """Contenedor de las diapositivas: una a la vista; al cambiar, la
    saliente se desliza y se funde mientras entra la nueva (TRANSITION_MS)."""
    changed = Signal(int)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.pages: List[SlidePage] = []
        self.index = 0
        self._leaving: Optional[QWidget] = None
        self._direction = 1
        self._anim: Optional[QVariantAnimation] = None
        # Alto según el ancho (los textos con ajuste de línea): el layout de
        # la tarjeta lo pregunta a heightForWidth
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def add_page(self, page: SlidePage) -> None:
        self.pages.append(page)
        page.setGeometry(self.rect())
        page.setVisible(len(self.pages) == 1)
        self.updateGeometry()

    def heightForWidth(self, width: int) -> int:
        return max((pg.heightForWidth(width) for pg in self.pages), default=-1)

    def sizeHint(self) -> QSize:
        height = self.heightForWidth(CONTENT_W)
        if height < 0:
            height = max((pg.sizeHint().height() for pg in self.pages), default=380)
        return QSize(CONTENT_W, height)

    def minimumSizeHint(self) -> QSize:
        # El ancho mínimo lo pone la tarjeta: aquí las cápsulas y los textos
        # (que se ajustan) no deben ensanchar el diálogo
        height = max((pg.minimumSizeHint().height() for pg in self.pages), default=240)
        return QSize(360, height)

    @property
    def current(self) -> SlidePage:
        return self.pages[self.index]

    def in_transition(self) -> bool:
        return self._anim is not None

    def go_to(self, index: int, animate: bool = True) -> bool:
        if not 0 <= index < len(self.pages) or index == self.index:
            return False
        self.finish()  # una transición a medias termina al instante
        self._direction = 1 if index > self.index else -1
        self._leaving = self.pages[self.index]
        self.index = index
        page = self.pages[index]
        page.setGeometry(self.rect())
        page.show()  # su ilustración empieza desde el principio
        page.raise_()
        self.changed.emit(index)
        if not animate or not self.isVisible():
            self._end()
            return True
        for w in (self._leaving, page):
            w.setGraphicsEffect(QGraphicsOpacityEffect(w))
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(TRANSITION_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(self._apply)
        anim.finished.connect(self._end)
        self._anim = anim
        self._apply(0.0)
        anim.start()
        return True

    def _apply(self, value) -> None:
        if self._leaving is None:
            return
        v = float(value)
        r = self.rect()
        d = self._direction * SLIDE_OFFSET
        self._leaving.setGeometry(r.translated(int(round(-d * v)), 0))
        self.current.setGeometry(r.translated(int(round(d * (1 - v))), 0))
        for w, opacity in ((self._leaving, 1 - v), (self.current, v)):
            effect = w.graphicsEffect()
            if isinstance(effect, QGraphicsOpacityEffect):
                effect.setOpacity(opacity)

    def _end(self) -> None:
        anim, self._anim = self._anim, None
        if anim is not None:
            anim.stop()
            anim.deleteLater()
        leaving, self._leaving = self._leaving, None
        if leaving is not None:
            leaving.hide()  # para su ilustración
            leaving.setGraphicsEffect(None)
        self.current.setGraphicsEffect(None)
        self.current.setGeometry(self.rect())

    def finish(self) -> None:
        if self._anim is not None or self._leaving is not None:
            self._end()

    def stop_all(self) -> None:
        self.finish()
        for page in self.pages:
            page.scene.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._anim is None:
            for page in self.pages:
                page.setGeometry(self.rect())


class _Dots(QWidget):
    """Indicador de diapositivas: la actual es una cápsula de acento que se
    desliza hasta la siguiente; un clic en un punto salta a esa."""
    clicked = Signal(int)
    DOT, PILL, GAP, H = 8.0, 24.0, 8.0, 20

    def __init__(self, count: int, parent: QWidget):
        super().__init__(parent)
        self._count = count
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(TRANSITION_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        width = (count - 1) * (self.DOT + self.GAP) + self.PILL
        self.setFixedSize(int(math.ceil(width)) + 8, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Diapositivas")

    def _set_pos(self, value) -> None:
        self._pos = float(value)
        self.update()

    def set_current(self, index: int, animate: bool = True) -> None:
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(float(index))
            self._anim.start()
        else:
            self._set_pos(float(index))
        self.setToolTip(f"Diapositiva {index + 1} de {self._count}")

    def stop(self) -> None:
        self._anim.stop()

    def dot_rects(self) -> List[QRectF]:
        rects, x = [], 4.0
        for i in range(self._count):
            weight = max(0.0, 1 - abs(self._pos - i))
            w = self.DOT + (self.PILL - self.DOT) * weight
            rects.append(QRectF(x, (self.H - self.DOT) / 2, w, self.DOT))
            x += w + self.GAP
        return rects

    def dot_center(self, index: int) -> QPointF:
        return self.dot_rects()[index].center()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        for i, rect in enumerate(self.dot_rects()):
            weight = max(0.0, 1 - abs(self._pos - i))
            p.setBrush(_mix(_c(t.fg_muted, 0.6), QColor(t.accent), weight))
            p.drawRoundedRect(rect, self.DOT / 2, self.DOT / 2)
        p.end()

    def mousePressEvent(self, event):
        x = event.position().x()
        rects = self.dot_rects()
        nearest = min(range(len(rects)), key=lambda i: abs(rects[i].center().x() - x))
        self.clicked.emit(nearest)


# ------------------------------------------------------------------- diálogo
class OnboardingDialog(BrandedDialog):
    """Las tres diapositivas. Navegación: puntos, «Atrás», «Siguiente» (en
    la última, «¡Empezar!»), ←/→, Intro (avanza o empieza) y Esc (= saltar).
    `outcome` dice cómo se cerró: 'start', 'skip' o 'close' (la X u otro
    cierre). `done(1)` solo con «¡Empezar!»."""

    def __init__(self, parent_window, user_name: str = "", pomodoro_min: int = 25):
        t = tokens()
        name = first_name(user_name)
        super().__init__(parent_window, self._title_for(name), accent=t.accent,
                         icon=brand.MASCOT_ICON,
                         subtitle="Así te ayuda PomoGrowth, en tres pasos")
        self._name = name
        self.outcome: Optional[str] = None
        self.setFocusPolicy(Qt.StrongFocus)
        if self._name:
            self._title_label.setTextFormat(Qt.RichText)
        # Ancho de la tarjeta acotado a la pantalla (escalados altos)
        screen = (parent_window.screen() if parent_window is not None
                  else QGuiApplication.primaryScreen())
        avail_w = screen.availableGeometry().width() if screen else 1280
        self._card.setMinimumWidth(max(420, min(CARD_W, avail_w - 120)))

        self.close_btn = IconButton("close", "Cerrar", color=t.fg_muted,
                                    hover_color=t.fg, size=16, parent=self._card)
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.clicked.connect(lambda: self.finish('close'))
        self._header.addWidget(self.close_btn, 0, Qt.AlignTop)

        self.stage = _Stage(self._card)
        try:
            minutes = max(1, int(pomodoro_min))
        except (TypeError, ValueError):
            minutes = 25
        for i, slide in enumerate(SLIDES):
            self.stage.add_page(SlidePage(slide, i, len(SLIDES), self.stage, minutes))
        self._lay.addWidget(self.stage, 1)
        self.stage.changed.connect(self._sync_controls)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.skip_btn = self._nav_button("Saltar", "ghost", lambda: self.finish('skip'))
        foot.addWidget(self.skip_btn)
        foot.addStretch(1)
        self.dots = _Dots(len(SLIDES), self._card)
        self.dots.clicked.connect(self.go_to)
        foot.addWidget(self.dots, 0, Qt.AlignVCenter)
        foot.addStretch(1)
        self.back_btn = self._nav_button("Atrás", "ghost", self.back)
        policy = self.back_btn.sizePolicy()
        policy.setRetainSizeWhenHidden(True)  # la fila no baila en la primera
        self.back_btn.setSizePolicy(policy)
        foot.addWidget(self.back_btn)
        self.next_btn = self._nav_button("Siguiente", "accent", self.next)
        self.next_btn.setMinimumWidth(124)
        # Icono a la derecha del texto (la flecha o el cohete)
        self.next_btn.setLayoutDirection(Qt.RightToLeft)
        foot.addWidget(self.next_btn)
        self._lay.addSpacing(16)
        self._lay.addLayout(foot)

        self._built = True
        self._sync_controls(0)
        self._restyle_content()

    # ---- piezas
    @staticmethod
    def _title_for(name: str) -> str:
        """«Hola, <nombre>» (el nombre en el acento) o, sin él, la bienvenida."""
        if name:
            return with_name("Hola", name, tokens().accent)
        return "Te damos la bienvenida"

    def _nav_button(self, text: str, variant: str, handler) -> QPushButton:
        b = QPushButton(text, self._card)
        b.setProperty("variant", variant)
        b.setCursor(Qt.PointingHandCursor)
        b.setMinimumWidth(88)
        b.setAutoDefault(False)
        b.setFocusPolicy(Qt.NoFocus)  # el teclado lo lleva el diálogo (←/→/Intro/Esc)
        b.clicked.connect(handler)
        return b

    @property
    def index(self) -> int:
        return self.stage.index

    @property
    def pages(self) -> List[SlidePage]:
        return self.stage.pages

    def is_last(self) -> bool:
        return self.index == len(SLIDES) - 1

    def adjustSize(self) -> None:
        """El tamaño pedido, acotado solo al área útil de la pantalla.

        `_animate_in` llama aquí; el adjustSize de Qt limita las ventanas a
        2/3 de la pantalla y con 1080p al 150 % dejaba la ilustración a
        menos de la mitad aunque sobraba sitio."""
        parent = self.parentWidget()
        screen = (parent or self).screen()
        hint = self.sizeHint()
        if screen is not None:
            hint = hint.boundedTo(screen.availableGeometry().size())
        self.resize(hint.expandedTo(self.minimumSize()))

    # ---- navegación
    def go_to(self, index: int) -> bool:
        return self.stage.go_to(index)

    def next(self) -> None:
        if self.is_last():
            self.finish('start')
        else:
            self.go_to(self.index + 1)

    def back(self) -> None:
        self.go_to(self.index - 1)

    def finish(self, outcome: str) -> None:
        self.outcome = outcome
        self.done(1 if outcome == 'start' else 0)

    def _sync_controls(self, index: int) -> None:
        self.dots.set_current(index)
        self.back_btn.setVisible(index > 0)
        last = index == len(SLIDES) - 1
        self.next_btn.setText(" ¡Empezar!" if last else " Siguiente")
        self._tint_next()

    def _tint_next(self) -> None:
        t = tokens()
        self.next_btn.setIcon(tinted_icon("rocket" if self.is_last() else "chevron-right",
                                          t.on_accent, 16))
        self.next_btn.setIconSize(QSize(16, 16))

    # ---- tema
    def _retheme(self) -> None:
        if getattr(self, '_built', False):
            self._accent_override = tokens().accent  # la ficha sigue al tema
        super()._retheme()
        if getattr(self, '_built', False):
            self._restyle_content()

    def _restyle_content(self) -> None:
        t = tokens()
        if self._name:
            self._title_label.setText(self._title_for(self._name))
        self.close_btn.set_colors(t.fg_muted, t.fg)
        for page in self.pages:
            page.restyle()
        self._tint_next()
        self.dots.update()

    # ---- ciclo de vida
    def showEvent(self, event):
        super().showEvent(event)
        self.setFocus(Qt.OtherFocusReason)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Right:
            if not self.is_last():
                self.next()
        elif key == Qt.Key_Left:
            self.back()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.next()
        elif key == Qt.Key_Escape:
            self.finish('skip')
        else:
            super().keyPressEvent(event)

    def done(self, result: int) -> None:
        if self.outcome is None:
            self.outcome = 'close'
        # Ninguna animación sigue viva tras cerrar (ni la transición ni las
        # ilustraciones ni los puntos)
        self.stage.stop_all()
        self.dots.stop()
        super().done(result)

    def running_timers(self) -> int:
        """Cuántos relojes de animación siguen en marcha (para los tests)."""
        return (sum(1 for pg in self.pages if pg.scene.is_running())
                + int(self.stage.in_transition()))
