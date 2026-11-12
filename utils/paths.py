"""Rutas de la app en desarrollo y congelada (PyInstaller onedir).

- app_base():      donde viven los DATOS del usuario (JSON, logs, música).
                   Junto al exe cuando está congelada; raíz del proyecto en dev.
- resource_base(): donde viven los RECURSOS empaquetados (iconos SVG).
                   _internal cuando está congelada; raíz del proyecto en dev.
- app_icon_path(): el icono de la ventana y de la bandeja, entre los recursos.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def app_base() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return _PROJECT_ROOT


def resource_base() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return _PROJECT_ROOT


def app_icon_path() -> Path:
    """assets/icon.ico (la mascota en el esmeralda oficial). En el exe va en
    _internal con los demás recursos, no junto al exe."""
    return resource_base() / "assets" / "icon.ico"
