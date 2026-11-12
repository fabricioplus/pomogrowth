"""Marca: la mascota de assets/icono.svg en la interfaz (acento del tema) y
los iconos del sistema (esmeralda oficial) que genera
scripts/build_brand_assets.py.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_brand.py -q
"""
import os
import struct
import sys
from pathlib import Path

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402

from ui.theme import brand  # noqa: E402


def _colors(image: QImage, *colors: str, tolerance: int = 6) -> dict:
    """Píxeles opacos de cada color (± tolerancia por canal)."""
    image = image.convertToFormat(QImage.Format_ARGB32)
    wanted = {c: QColor(c) for c in colors}
    found = dict.fromkeys(colors, 0)
    for y in range(image.height()):
        for x in range(image.width()):
            px = image.pixelColor(x, y)
            if px.alpha() < 250:
                continue
            for name, c in wanted.items():
                if max(abs(px.red() - c.red()), abs(px.green() - c.green()),
                       abs(px.blue() - c.blue())) <= tolerance:
                    found[name] += 1
    return found


def test_mascota_del_color_pedido_con_cara_y_ojos(qtbot):
    ocean = "#0ea5e9"
    found = _colors(brand.mascot_pixmap(ocean, 64).toImage(), ocean, brand.BRAND_GREEN,
                    "#ffffff", brand.BRAND_INK)
    assert found[ocean] > 600              # cuerpo y hojas
    assert found[brand.BRAND_GREEN] == 0   # nada del verde original
    assert found["#ffffff"] > 600          # cara
    assert found[brand.BRAND_INK] > 10     # ojos


def test_logo_del_splash_lleva_el_texto_en_dos_colores(qtbot):
    accent, text = "#34e0a1", "#e7e9ee"
    pm = brand.logo_pixmap(44, accent, text)
    assert pm.height() / pm.devicePixelRatio() == 44
    assert pm.width() > pm.height() * 4    # horizontal: mascota + «PomoGrowth»
    found = _colors(pm.toImage(), accent, text, brand.BRAND_INK)
    assert found[accent] > 300 and found[text] > 300 and found[brand.BRAND_INK] > 5


def test_cabecera_y_fichas_siguen_al_tema(qtbot):
    """La mascota de la cabecera y la de las fichas de marca toman el acento
    del tema. Se re-tematiza a mano: emitir theme_changed despertaría a
    ventanas ya destruidas de otros tests."""
    from ui.dialogs.branded_dialogs import icon_chip, style_icon_chip
    from ui.theme.tokens import THEMES, ThemeManager
    from ui.widgets.title_bar import TitleBar
    manager = ThemeManager.instance()
    original = manager.current
    tb = TitleBar()
    qtbot.addWidget(tb)
    chip = icon_chip(brand.MASCOT_ICON, manager.current.accent, size=40, icon_size=24)
    qtbot.addWidget(chip)
    try:
        for theme in ('theme_ocean', 'theme_sunset', 'theme_golden', 'default'):
            manager.current = t = THEMES[theme]
            tb._retheme()
            style_icon_chip(chip, brand.MASCOT_ICON, t.accent)
            for label in (tb.logo, chip):
                found = _colors(label.pixmap().toImage(), t.accent, "#ffffff")
                assert found[t.accent] > 50 and found["#ffffff"] > 50, (theme, label)
    finally:
        manager.current = original


def test_iconos_del_sistema_con_todos_los_tamanos():
    """assets/icon.ico: un PNG por tamaño, cada uno de su tamaño real y con
    el esmeralda oficial; icon.png de 256. Con los tamaños exactos que pide
    Windows a 110 % (106 ppp): 18 para la bandeja, 35 para el icono grande y
    26 para la barra de tareas (si no, Qt y Windows reescalan el de al lado y
    los ojos se emborronan). Hasta 35 px, los ojos van ajustados a píxeles:
    dos del color exacto, sin halo (a 32 px eran 1 px oscuro y un halo gris)."""
    data = (V9 / "assets" / "icon.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = []
    for i in range(count):
        w, h, _, _, _, bpp, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        size = w or 256
        png = data[offset:offset + length]
        assert png[:4] == b"\x89PNG" and bpp == 32
        image = QImage.fromData(png, "PNG")
        assert (image.width(), image.height()) == (size, size)
        assert _colors(image, brand.BRAND_GREEN, tolerance=2)[brand.BRAND_GREEN] > 0
        if size <= 35:  # dos ojos de al menos 1×3 px
            assert _colors(image, brand.BRAND_INK, tolerance=2)[brand.BRAND_INK] >= 6, size
        sizes.append(size)
    assert sizes == [16, 18, 20, 24, 26, 32, 35, 40, 48, 64, 96, 128, 256]
    png = QImage(str(V9 / "assets" / "icon.png"))
    assert (png.width(), png.height()) == (256, 256)
