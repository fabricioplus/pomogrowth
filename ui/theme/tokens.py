"""Tokens de tema — ÚNICA fuente de verdad de la paleta de v9.

Paleta base portada de v8 ui/theme.py (identidad PomoGrowth: fondo oscuro +
verde neón), evolucionada para Qt: superficies elevadas, radios y sombras.
Los temas comprables de la tienda (fase 4) son variantes de ThemeTokens cuyo
id coincide con el item_id de core/rewards.py.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from PySide6.QtCore import QObject, Signal


@dataclass(frozen=True)
class ThemeTokens:
    # Fondos: neutros con un leve tinte azulado (ya no el gris plano de
    # VS Code). Cada nivel sube un poco de luminosidad: base < tarjeta < hover
    bg: str = '#14161b'            # base de ventana
    bg_header: str = '#101216'     # titlebar / zonas hundidas
    surface: str = '#1b1e25'       # tarjetas y paneles
    surface_hover: str = '#21252e'  # hover de tarjetas
    surface_active: str = '#282d38'  # seleccionado / presionado

    # Texto
    fg: str = '#e7e9ee'
    fg_dim: str = '#a2a8b6'
    fg_muted: str = '#6c7383'

    # Acentos (identidad PomoGrowth: menta). El neón puro queda para el
    # progreso; en superficies grandes, un tono algo menos saturado
    accent: str = '#34e0a1'
    accent_hover: str = '#5eeab7'
    accent_grad_end: str = '#39c6ff'   # gradiente menta→cian del progreso
    accent_dim: str = '#1d7a58'
    on_accent: str = '#05261a'         # texto sobre botones de acento

    # Semánticos
    warning: str = '#f2a33a'
    error: str = '#f05454'
    error_hover: str = '#f47070'
    info: str = '#5ea3ff'
    danger: str = '#ef5b5b'
    asana: str = '#f06a6a'             # marca de las tareas de Asana
    clickup: str = '#7b68ee'           # marca de ClickUp (botón del alta)

    # Descanso (la UI cambia de humor en break)
    break_accent: str = '#6aa6ff'
    break_grad_end: str = '#a98bff'

    # Tags de tareas: pasteles legibles sobre fondo oscuro
    tag_colors: List[str] = field(default_factory=lambda: [
        '#f28b82', '#81c995', '#8ab4f8', '#fdd663',
        '#c58af9', '#fcad70', '#c9b5a2', '#9fb0c3'])

    # Bordes / formas
    border: str = '#252a33'
    radius: int = 12       # tarjetas y paneles
    radius_small: int = 8  # botones, inputs
    radius_pill: int = 8   # segmentos de filtro (≤ mitad de su alto de 30 px)


# Temas disponibles: 'default' + los item_id de temas de la tienda
# (core/rewards.py). Cada tema comprable es una variante de la paleta.
THEMES = {
    'default': ThemeTokens(),
    'theme_ocean': ThemeTokens(
        accent='#0ea5e9', accent_hover='#38bdf8', accent_grad_end='#22d3ee',
        accent_dim='#0369a1', on_accent='#082333', break_accent='#a78bfa',
        break_grad_end='#c4b5fd', bg='#101c24', bg_header='#0b141a',
        surface='#1a2c38', surface_hover='#213543', surface_active='#2a4152',
        border='#28404f'),
    'theme_forest': ThemeTokens(
        accent='#22c55e', accent_hover='#4ade80', accent_grad_end='#86efac',
        accent_dim='#166534', on_accent='#04150a', break_accent='#38bdf8',
        break_grad_end='#7dd3fc', bg='#131a14', bg_header='#0e130f',
        surface='#1e2a20', surface_hover='#253327', surface_active='#2e4031',
        border='#2b3d2e'),
    'theme_sunset': ThemeTokens(
        accent='#f97316', accent_hover='#fb923c', accent_grad_end='#f43f5e',
        accent_dim='#9a3412', on_accent='#1f0d02', break_accent='#a78bfa',
        break_grad_end='#f0abfc', bg='#1d1512', bg_header='#160f0c',
        surface='#2b201b', surface_hover='#342722', surface_active='#40302a',
        border='#3d2d26'),
    'theme_midnight': ThemeTokens(
        accent='#60a5fa', accent_hover='#93c5fd', accent_grad_end='#818cf8',
        accent_dim='#1e40af', on_accent='#0a1226', break_accent='#34d399',
        break_grad_end='#6ee7b7', bg='#0f172a', bg_header='#0a1120',
        surface='#1e293b', surface_hover='#253248', surface_active='#334155',
        border='#2e3c52'),
    'theme_cherry': ThemeTokens(
        accent='#ec4899', accent_hover='#f472b6', accent_grad_end='#f9a8d4',
        accent_dim='#9d174d', on_accent='#22060f', break_accent='#38bdf8',
        break_grad_end='#7dd3fc', bg='#1c1218', bg_header='#150d12',
        surface='#2a1c24', surface_hover='#33222c', surface_active='#402a37',
        border='#3c2833'),
    'theme_matrix': ThemeTokens(
        accent='#00ff41', accent_hover='#66ff8c', accent_grad_end='#00e5ff',
        accent_dim='#0a7a2a', on_accent='#02140a', break_accent='#00e5ff',
        break_grad_end='#7df9ff', bg='#0a0f0a', bg_header='#060a06',
        surface='#12190f', surface_hover='#182213', surface_active='#1f2c19',
        border='#213321', fg='#c8facc', fg_dim='#7bbf87'),
    'theme_golden': ThemeTokens(
        accent='#fbbf24', accent_hover='#fcd34d', accent_grad_end='#f59e0b',
        accent_dim='#92400e', on_accent='#1c1203', break_accent='#a78bfa',
        break_grad_end='#c4b5fd', bg='#191510', bg_header='#120f0b',
        surface='#272019', surface_hover='#2f2820', surface_active='#3a3128',
        border='#3a2f22'),
}


class ThemeManager(QObject):
    """Singleton con el tema activo. theme_changed avisa a los widgets con
    paintEvent propio (los estilados por QSS se refrescan re-aplicando el
    stylesheet global — solo al equipar un tema, nunca en el tick)."""
    theme_changed = Signal()

    _instance: Optional['ThemeManager'] = None

    def __init__(self):
        super().__init__()
        self.current: ThemeTokens = THEMES['default']

    @classmethod
    def instance(cls) -> 'ThemeManager':
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance

    def set_theme(self, theme_id: str) -> bool:
        tokens = THEMES.get(theme_id)
        if tokens is None or tokens is self.current:
            return False
        self.current = tokens
        self.theme_changed.emit()
        return True


def tokens() -> ThemeTokens:
    """Atajo: tokens del tema activo."""
    return ThemeManager.instance().current
