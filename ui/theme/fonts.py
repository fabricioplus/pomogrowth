"""Tipografía de v9 — rampa tipográfica de Windows 11 sobre Segoe UI Variable.

Segoe UI Variable tiene dos cortes ópticos: «Text» para cuerpo y tamaños
pequeños, y «Display» para títulos y números grandes (el timer). Las cifras
del timer usan figuras tabulares (tnum): todas con el mismo ancho, así los
números no bailan al cambiar ni dejan huecos alrededor de los dos puntos.
"""
from typing import Optional

from PySide6.QtGui import QFont, QFontDatabase

_ui_family: str = "Segoe UI"        # cuerpo
_display_family: str = "Segoe UI"   # títulos y números grandes
_mono_family: str = "Consolas"
_glyph_family: Optional[str] = None  # Segoe Fluent Icons: botones de ventana

MIN_POINT_SIZE = 8.0  # por debajo de esto el texto deja de leerse a 110 %


def init_fonts() -> None:
    """Resuelve las mejores familias disponibles. Llamar tras crear QApplication."""
    global _ui_family, _display_family, _mono_family, _glyph_family
    available = set(QFontDatabase.families())

    def first(*candidates, default=None):
        return next((c for c in candidates if c in available), default)

    _ui_family = first("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI",
                       default=_ui_family)
    _display_family = first("Segoe UI Variable Display", "Segoe UI Variable",
                            "Segoe UI", default=_ui_family)
    _mono_family = first("Cascadia Mono", "Cascadia Code", "Consolas",
                         default=_mono_family)
    _glyph_family = first("Segoe Fluent Icons", "Segoe MDL2 Assets")


def ui_family() -> str:
    return _ui_family


def glyph_family() -> Optional[str]:
    return _glyph_family


def font(size: float, bold: bool = False, weight: Optional[QFont.Weight] = None) -> QFont:
    """Texto de interfaz. `bold` = semibold (600): la negrita completa (700)
    en todo era parte de lo que hacía «gritar» a la interfaz."""
    f = QFont(_ui_family)
    f.setPointSizeF(max(MIN_POINT_SIZE, float(size)))
    f.setWeight(weight if weight is not None
                else (QFont.Weight.DemiBold if bold else QFont.Weight.Normal))
    return f


def display(size: float, weight: QFont.Weight = QFont.Weight.DemiBold,
            tabular: bool = True) -> QFont:
    """Títulos y números grandes (timer, contadores)."""
    f = QFont(_display_family)
    f.setPointSizeF(float(size))
    f.setWeight(weight)
    if tabular:
        f.setFeature(QFont.Tag("tnum"), 1)
    return f


def mono(size: int, bold: bool = True) -> QFont:
    f = QFont(_mono_family, size)
    f.setBold(bold)
    return f


def glyph(size: float) -> Optional[QFont]:
    """Fuente de iconos del sistema (botones de ventana estilo Windows 11)."""
    if not _glyph_family:
        return None
    f = QFont(_glyph_family)
    f.setPointSizeF(size)
    return f


# Escala (puntos). A 110 %: XS ≈ 13 px (caption), SMALL ≈ 14,5 px (cuerpo),
# NORMAL ≈ 17 px, MEDIUM ≈ 21 px (subtítulo), LARGE ≈ 28 px (título)
XS, SMALL, NORMAL, MEDIUM, LARGE, XL = 9, 10, 12, 15, 20, 32
TIMER_MAIN, TIMER_COMPACT = 46, 24
