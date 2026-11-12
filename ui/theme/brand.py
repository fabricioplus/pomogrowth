"""Marca de PomoGrowth («Identidad Esmeralda»): la mascota y el logo.

Los dibujos salen de los SVG del usuario en assets/ (icono.svg y el logo
horizontal monocromo, con el texto ya en trazados), nunca de copias propias.
El verde se usa de dos maneras:
- Fuera de la app (icono del exe, del instalador, de la ventana y de la
  bandeja): el esmeralda oficial `BRAND_GREEN`, tal cual. Esos iconos los
  genera scripts/build_brand_assets.py en assets/icon.ico e icon.png, igual
  que las imágenes del asistente del instalador (installer/*.bmp).
- Dentro de la app (cabecera, splash, fichas de bienvenida y despedida): la
  mascota toma el acento del tema activo (`t.accent`) y cambia con los temas
  de la tienda. Cara blanca y ojos oscuros en los dos casos.
"""
import math
import xml.etree.ElementTree as ET
from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from utils.paths import resource_base

BRAND_GREEN = "#0BBF85"  # esmeralda oficial (cuerpo y hojas)
BRAND_INK = "#0F1720"    # ojos de la mascota
# Nombre de «icono» para icon_chip / BrandedDialog(icon=...): la mascota
MASCOT_ICON = "pomogrowth-mascot"

_ASSETS = resource_base() / "assets"
MASCOT_SVG = _ASSETS / "icono.svg"
# Logo horizontal en un color: de él solo se toma el texto (en trazados; los
# SVG con <text> usan una fuente incrustada que QtSvg ignora)
WORDMARK_SVG = _ASSETS / "pomogrowth-horizontal-mono-white.svg"
_WORDMARK_POMO = 4  # los 4 primeros trazados son «Pomo»; el resto, «Growth»

_SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG_NS)  # sin prefijos «ns0:» al volver a texto

_text_cache: dict = {}
_renderer_cache: dict = {}
_geometry_cache: dict = {}
_pixmap_cache: dict = {}


def _read(path) -> Optional[str]:
    if path not in _text_cache:
        try:
            _text_cache[path] = path.read_text(encoding="utf-8")
        except OSError:
            _text_cache[path] = None
    return _text_cache[path]


def _by_id(root: ET.Element, element_id: str) -> Optional[ET.Element]:
    return next((e for e in root.iter() if e.get("id") == element_id), None)


def _set_fill(element: Optional[ET.Element], color: str) -> None:
    """Pinta el grupo y lo que lleve color propio dentro."""
    if element is None:
        return
    for e in element.iter():
        if e is element or e.get("fill"):
            e.set("fill", color)


def _empty(element: Optional[ET.Element]) -> None:
    if element is not None:
        for child in list(element):
            element.remove(child)


def _mascot_root(color: str, detail: bool = True, eyes: bool = True) -> ET.Element:
    root = ET.fromstring(_read(MASCOT_SVG) or f'<svg xmlns="{_SVG_NS}"/>')
    _set_fill(_by_id(root, "mascot-body"), color)
    _set_fill(_by_id(root, "mascot-leaves"), color)
    if not detail:
        _empty(_by_id(root, "timer-detail"))
    if not eyes:
        _empty(_by_id(root, "mascot-eyes"))
    return root


def mascot_svg(color: str = BRAND_GREEN, *, detail: bool = True,
               eyes: bool = True) -> bytes:
    """icono.svg con el cuerpo y las hojas de `color`. Sin `detail` se quita
    el detalle del temporizador y sin `eyes`, los ojos (iconos diminutos)."""
    return ET.tostring(_mascot_root(color, detail, eyes))


def _renderer(color: str, detail: bool = True, eyes: bool = True) -> QSvgRenderer:
    key = (color, detail, eyes)
    if key not in _renderer_cache:
        _renderer_cache[key] = QSvgRenderer(QByteArray(mascot_svg(
            color, detail=detail, eyes=eyes)))
    return _renderer_cache[key]


def _geometry() -> dict:
    """Medidas del SVG original (en un renderer propio: a los de pintar se
    les cambia el recuadro visible)."""
    if not _geometry_cache:
        r = QSvgRenderer(QByteArray(mascot_svg()))
        canvas = r.viewBoxF()
        bounds = canvas
        if r.elementExists("mascot"):
            bounds = r.transformForElement("mascot").mapRect(r.boundsOnElement("mascot"))
        _geometry_cache.update(canvas=canvas, bounds=bounds,
                               eyes=r.transformForElement("mascot-eyes"))
    return _geometry_cache


def _view_box(margin: Optional[float]) -> QRectF:
    """Recuadro cuadrado del SVG que se pinta. Con `margin` (fracción del
    lado libre a cada lado) se ciñe a la mascota; con None, el lienzo del
    propio SVG (248×256, con su margen de diseño) hecho cuadrado."""
    geo = _geometry()
    box = geo["canvas"] if margin is None else geo["bounds"]
    side = max(box.width(), box.height())
    if margin is not None:
        side /= 1 - 2 * margin
    c = box.center()
    return QRectF(c.x() - side / 2, c.y() - side / 2, side, side)


def _eye_rects(view: QRectF, rect: QRectF):
    """Ojos en coordenadas del destino, ajustados a píxeles enteros (a 16 px
    miden 1,4 × 2,3 px y el suavizado los dejaba en dos manchas grises)."""
    eyes = _by_id(_mascot_root(BRAND_GREEN), "mascot-eyes")
    if eyes is None or view.width() <= 0:
        return []
    to_svg = _geometry()["eyes"]
    scale = rect.width() / view.width()
    out = []
    for e in eyes.iter(f"{{{_SVG_NS}}}rect"):
        r = to_svg.mapRect(QRectF(*(float(e.get(k, 0)) for k in
                                   ("x", "y", "width", "height"))))
        # Ancho redondeado y alto hacia arriba: el ojo sigue siendo alargado
        w, h = max(1, round(r.width() * scale)), max(2, math.ceil(r.height() * scale))
        cx = rect.x() + (r.center().x() - view.x()) * scale
        cy = rect.y() + (r.center().y() - view.y()) * scale
        out.append(QRectF(round(cx - w / 2), round(cy - h / 2), w, h))
    return out


def paint_mascot(painter: QPainter, rect: QRectF, color: str = BRAND_GREEN,
                 margin: Optional[float] = 0.0, *, detail: bool = True,
                 pixel_eyes: bool = False) -> None:
    """Pinta la mascota centrada en el cuadrado `rect`. `margin`: ver
    _view_box. `pixel_eyes` (iconos de hasta 35 px): ojos ajustados a píxeles."""
    view = _view_box(margin)
    renderer = _renderer(color, detail, eyes=not pixel_eyes)
    renderer.setViewBox(view)
    renderer.render(painter, rect)
    if pixel_eyes:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        for eye in _eye_rects(view, rect):
            painter.fillRect(eye, QColor(BRAND_INK))
        painter.restore()


def device_pixel_ratio() -> float:
    """Escala de la pantalla más densa: los pixmaps de la marca y los iconos
    tintados (icon_button.tinted_icon) se pintan a esa resolución."""
    app = QGuiApplication.instance()
    screens = app.screens() if app else []
    return max((s.devicePixelRatio() for s in screens), default=1.0)


def mascot_pixmap(color: str, size: int, margin: float = 0.0) -> QPixmap:
    """La mascota de `color` en un cuadrado de `size` px lógicos, a la
    resolución real de la pantalla (como tinted_icon)."""
    dpr = device_pixel_ratio()
    key = ("mascot", color, size, margin, dpr)
    if key not in _pixmap_cache:
        px = max(1, round(size * dpr))
        pm = QPixmap(px, px)
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        paint_mascot(p, QRectF(0, 0, size, size), color, margin)
        p.end()
        _pixmap_cache[key] = pm
    return _pixmap_cache[key]


def _logo_svg(color: str, pomo_color: str, growth_color: str) -> Optional[bytes]:
    """Logo horizontal en color: el texto del monocromo («Pomo» y «Growth»
    con su color cada uno) y, en el hueco de su mascota, la de icono.svg
    (el monocromo la trae con la cara recortada: se vería el fondo)."""
    text = _read(WORDMARK_SVG)
    if text is None:
        return None
    root = ET.fromstring(text)
    slot = _by_id(root, "mascot")
    mascot = _by_id(_mascot_root(color), "mascot")
    if slot is not None and mascot is not None:
        _empty(slot)
        slot.extend(list(mascot))
    wordmark = _by_id(root, "wordmark")
    if wordmark is not None:
        for i, path in enumerate(wordmark.iter(f"{{{_SVG_NS}}}path")):
            path.set("fill", pomo_color if i < _WORDMARK_POMO else growth_color)
    return ET.tostring(root)


def _wordmark_renderer(pomo_color: str, growth_color: str) -> QSvgRenderer:
    key = ("wordmark", pomo_color, growth_color)
    if key not in _renderer_cache:
        data = _logo_svg(growth_color, pomo_color, growth_color)
        _renderer_cache[key] = QSvgRenderer(QByteArray(data or b""))
    return _renderer_cache[key]


def wordmark_ratio() -> float:
    """Ancho entre alto del texto «PomoGrowth» del logo (0 sin su SVG)."""
    r = _wordmark_renderer("#FFFFFF", BRAND_GREEN)
    if not r.elementExists("wordmark"):
        return 0.0
    box = r.boundsOnElement("wordmark")
    return box.width() / box.height() if box.height() > 0 else 0.0


def paint_wordmark(painter: QPainter, rect: QRectF, pomo_color: str,
                   growth_color: str = BRAND_GREEN) -> None:
    """Pinta solo el texto «PomoGrowth» del logo (en trazados) en `rect`, que
    debe tener la proporción de wordmark_ratio(). Lo usa el script de las
    imágenes del instalador, que lo compone en vertical bajo la mascota."""
    r = _wordmark_renderer(pomo_color, growth_color)
    if r.elementExists("wordmark"):
        r.render(painter, "wordmark", rect)


def logo_pixmap(height: int, color: str, pomo_color: str,
                growth_color: Optional[str] = None) -> QPixmap:
    """Logo horizontal (mascota + «PomoGrowth») de `height` px lógicos de
    alto, ceñido al dibujo. «Growth» va del color de la mascota salvo que se
    indique otro. Sin el SVG del logo, solo la mascota."""
    growth_color = growth_color or color
    dpr = device_pixel_ratio()
    key = ("logo", height, color, pomo_color, growth_color, dpr)
    if key in _pixmap_cache:
        return _pixmap_cache[key]
    data = _logo_svg(color, pomo_color, growth_color)
    renderer = QSvgRenderer(QByteArray(data or b""))
    box = QRectF()
    for element_id in ("mascot", "wordmark"):
        if renderer.elementExists(element_id):
            box = box.united(renderer.transformForElement(element_id).mapRect(
                renderer.boundsOnElement(element_id)))
    if box.isEmpty():
        box = renderer.viewBoxF()
    if box.isEmpty():
        return mascot_pixmap(color, height)
    renderer.setViewBox(box)
    width = height * box.width() / box.height()
    pm = QPixmap(max(1, round(width * dpr)), max(1, round(height * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p, QRectF(0, 0, pm.width() / dpr, pm.height() / dpr))
    p.end()
    _pixmap_cache[key] = pm
    return pm
