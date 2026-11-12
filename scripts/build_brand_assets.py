"""Genera los iconos del sistema a partir de la mascota (assets/icono.svg).

Uso: python scripts/build_brand_assets.py   (desde la raíz del repositorio; no necesita display)
Salida:
- assets/icon.ico: exe, instalador, ventana/barra de tareas y bandeja. Un PNG
  por tamaño (16 a 256), cada uno renderizado aparte con QSvgRenderer: un
  único 256 reducido por Windows se ve borroso a 16 px. Con los tamaños de
  la bandeja y del icono grande que pide Windows a 100, 110, 125 y 150 %
  (16/32, 18/35, 20/40 y 24/48; a 110 % son 106 ppp) y los de la barra de
  tareas a 100 y 110 % (24 y ~26): sin una entrada exacta, Qt y Windows
  reescalan la de al lado y los ojos ajustados se vuelven a emborronar.
- assets/icon.png: 256 px (el mismo dibujo que la entrada de 256 del .ico).
- installer/wizard_*.bmp y wizard_small_*.bmp: imágenes del asistente de Inno
  Setup (installer/PomoGrowth.iss), una por escala de pantalla, BMP de 24
  bits. La grande, la mascota sobre «PomoGrowth» («Pomo» blanco, «Growth»
  esmeralda) en el fondo BRAND_INK; la pequeña, la mascota sobre blanco.

Siempre el esmeralda oficial (ui/theme/brand.BRAND_GREEN), cara blanca y ojos
#0F1720. Desde 32 px, la mascota tal cual se diseñó, con su margen. Hasta
35 px, simplificada para que se lea la cara:
- de 16 a 26 px, margen de medio píxel en vez del de diseño (~10 %, que a
  16 px se comía casi 2 px por lado);
- hasta 35 px, ojos ajustados a píxeles enteros (suavizados eran dos manchas
  grises; a 32 px, 1 px oscuro con un halo);
- de 16 a 20 px, sin el detalle del temporizador (quedaba como un borrón
  blanco sobre el verde). A 24 px ya se lee y se conserva.
"""
import os
import struct
import sys
from pathlib import Path

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402

from ui.theme import brand  # noqa: E402

ICO_SIZES = (16, 18, 20, 24, 26, 32, 35, 40, 48, 64, 96, 128, 256)
SMALL_SIZES = (16, 18, 20, 24, 26)               # medio píxel de margen
PIXEL_EYES_SIZES = (16, 18, 20, 24, 26, 32, 35)  # ojos ajustados a píxeles
NO_DETAIL_SIZES = (16, 18, 20)                   # sin el detalle del temporizador
PNG_SIZE = 256

INSTALLER = V9 / "installer"
# Asistente de Inno Setup 6: un tamaño por escala (100, 125, 150, 175, 200,
# 225 y 250 %); Setup usa el que mejor encaja con los DPI de la pantalla
WIZARD_SIZES = ((164, 314), (192, 386), (246, 459), (273, 556),
                (328, 604), (355, 700), (410, 797))
WIZARD_SMALL_SIZES = ((55, 55), (64, 68), (83, 80), (92, 97),
                      (110, 106), (119, 123), (138, 140))
WHITE = "#FFFFFF"


def render(size: int) -> QImage:
    small = size in SMALL_SIZES
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    brand.paint_mascot(p, QRectF(0, 0, size, size), brand.BRAND_GREEN,
                       margin=0.5 / size if small else None,
                       detail=size not in NO_DETAIL_SIZES,
                       pixel_eyes=size in PIXEL_EYES_SIZES)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path: Path, images) -> None:
    """ICO con entradas PNG (Windows Vista+): cabecera, directorio y datos."""
    pngs = [(img.width(), png_bytes(img)) for img in images]
    offset = 6 + 16 * len(pngs)
    directory, blobs = b"", b""
    for size, png in pngs:
        dim = 0 if size >= 256 else size  # 0 significa 256
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png),
                                 offset + len(blobs))
        blobs += png
    path.write_bytes(struct.pack("<HHH", 0, 1, len(pngs)) + directory + blobs)


def render_wizard(width: int, height: int) -> QImage:
    """Panel izquierdo de la bienvenida y del final: la mascota sobre el
    texto, centrados un poco por encima de la mitad. Todo en fracciones del
    ancho: las siete escalas no tienen la misma proporción."""
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(QColor(brand.BRAND_INK))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    mascot, text_w = width * 0.54, width * 0.80
    ratio = brand.wordmark_ratio()
    text_h = text_w / ratio if ratio else 0.0  # sin el SVG del logo, solo la mascota
    gap = width * 0.12 if text_h else 0.0
    top = round(height * 0.46 - (mascot + gap + text_h) / 2)
    brand.paint_mascot(p, QRectF(round((width - mascot) / 2), top, mascot, mascot),
                       brand.BRAND_GREEN)
    if text_h:
        brand.paint_wordmark(p, QRectF(round((width - text_w) / 2),
                                       round(top + mascot + gap), text_w, text_h), WHITE)
    p.end()
    return img


def render_wizard_small(width: int, height: int) -> QImage:
    """Esquina de la cabecera de las demás páginas: la mascota sobre blanco,
    el fondo de esa cabecera en WizardStyle=modern (claro)."""
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(QColor(WHITE))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    side = min(width, height)
    brand.paint_mascot(p, QRectF((width - side) / 2, (height - side) / 2, side, side),
                       brand.BRAND_GREEN, margin=0.06)
    p.end()
    return img


def main() -> int:
    """Necesita una QGuiApplication creada (QPainter/QtSvg)."""
    assets = V9 / "assets"
    images = [render(size) for size in ICO_SIZES]
    write_ico(assets / "icon.ico", images)
    images[ICO_SIZES.index(PNG_SIZE)].save(str(assets / "icon.png"), "PNG")
    print(f"assets/icon.ico ({', '.join(map(str, ICO_SIZES))} px) y "
          f"assets/icon.png ({PNG_SIZE} px)")
    # Qt guarda en BMP de 24 bits las imágenes RGB32 (sin canal alfa)
    INSTALLER.mkdir(exist_ok=True)
    for kind, sizes, render_fn in (("wizard", WIZARD_SIZES, render_wizard),
                                   ("wizard_small", WIZARD_SMALL_SIZES, render_wizard_small)):
        for w, h in sizes:
            render_fn(w, h).save(str(INSTALLER / f"{kind}_{w}x{h}.bmp"), "BMP")
    print(f"installer/: {len(WIZARD_SIZES)} wizard_*.bmp y "
          f"{len(WIZARD_SMALL_SIZES)} wizard_small_*.bmp")
    return 0


if __name__ == "__main__":
    _app = QGuiApplication(sys.argv)
    sys.exit(main())
