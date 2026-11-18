"""Genera el stylesheet global desde los tokens — cero colores hardcodeados aquí.

Variantes por propiedad dinámica:
  QPushButton[variant="accent"|"ghost"|"danger"]
  QPushButton[pill="true"]        → segmentos de filtro/orden (checkable)
  QFrame[segmented="true"]        → carril del control segmentado
  QLabel[role="dim"|"muted"|"accent"]
  QFrame[card="true"]             → paneles con superficie elevada
"""
import tempfile
from pathlib import Path

from .tokens import ThemeTokens


def _qss_icon(name: str, color: str, stroke: str = None) -> str:
    """Ruta (con «/») a una copia del SVG teñida del color dado, para usarla
    en «image: url(...)» del QSS (que no sabe teñir iconos)."""
    from utils.paths import resource_base
    src = resource_base() / "assets" / "icons" / f"{name}.svg"
    cache = Path(tempfile.gettempdir()) / "pomogrowth-qss"
    cache.mkdir(exist_ok=True)
    dst = cache / f"{name}-{color.lstrip('#')}{'-' + stroke if stroke else ''}.svg"
    if not dst.exists() and src.exists():
        svg = src.read_text(encoding="utf-8").replace("currentColor", color)
        svg = svg.replace('stroke="#fff"', f'stroke="{color}"').replace('fill="#fff"', f'fill="{color}"')
        if stroke:
            svg = svg.replace('stroke-width="1.75"', f'stroke-width="{stroke}"')
        dst.write_text(svg, encoding="utf-8")
    return dst.as_posix()


def build_qss(t: ThemeTokens, ui_family: str) -> str:
    chevron_up = _qss_icon("chevron-up", t.fg_dim)
    chevron_down = _qss_icon("chevron-down", t.fg_dim)
    check = _qss_icon("check", t.on_accent, stroke="3")
    return f"""
* {{
    outline: none;
}}
QWidget {{
    background: transparent;
    color: {t.fg};
}}
QMainWindow, QDialog {{
    background: {t.bg};
}}

/* ---------- Labels ---------- */
QLabel {{ background: transparent; }}
QLabel[role="dim"] {{ color: {t.fg_dim}; }}
QLabel[role="dim"]:disabled {{ color: {t.fg_muted}; }}
QLabel[role="muted"] {{ color: {t.fg_muted}; }}
QLabel[role="accent"] {{ color: {t.accent}; }}

/* ---------- Paneles/tarjetas ---------- */
QFrame[card="true"] {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {t.radius}px;
}}

/* ---------- Botones ---------- */
QPushButton {{
    background: {t.surface};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: {t.radius_small}px;
    padding: 7px 14px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {t.surface_hover}; border-color: {t.fg_muted}; }}
QPushButton:pressed {{ background: {t.surface_active}; }}
QPushButton:disabled {{ color: {t.fg_muted}; background: {t.bg_header}; border-color: {t.border}; }}

QPushButton[variant="accent"] {{
    background: {t.accent};
    color: {t.on_accent};
    border: none;
    font-weight: 600;
}}
QPushButton[variant="accent"]:hover {{ background: {t.accent_hover}; }}
QPushButton[variant="accent"]:disabled {{ background: {t.surface_active}; color: {t.fg_muted}; }}

QPushButton[variant="ghost"] {{
    background: transparent;
    border: none;
    color: {t.fg_dim};
}}
QPushButton[variant="ghost"]:hover {{ background: {t.surface_hover}; color: {t.fg}; }}

QPushButton[variant="danger"] {{
    background: transparent;
    border: 1px solid {t.danger};
    color: {t.danger};
}}
QPushButton[variant="danger"]:hover {{ background: {t.danger}; color: {t.fg}; }}

/* Control segmentado (filtros y orden): carril hundido + segmento activo
   en relieve. El radio debe ser ≤ la mitad del alto o Qt lo descarta y
   salen esquinas vivas (era el caso con 14 px sobre 26 px de alto) */
QFrame[segmented="true"] {{
    background: {t.bg_header};
    border: 1px solid {t.border};
    border-radius: {t.radius_small + 2}px;
}}
QPushButton[pill="true"] {{
    background: transparent;
    border: none;
    border-radius: {t.radius_pill - 1}px;
    padding: 5px 12px;
    color: {t.fg_dim};
    font-size: 9.5pt;
    font-weight: 500;
}}
QPushButton[pill="true"]:hover {{ color: {t.fg}; background: {t.surface_hover}; }}
QPushButton[pill="true"]:checked {{
    background: {t.surface_active};
    color: {t.fg};
    font-weight: 600;
}}
/* Bloqueado (p. ej. el selector Pomodoro | Libre con una sesión a medias) */
QPushButton[pill="true"]:disabled {{ color: {t.fg_muted}; }}

/* Iconos de titlebar/header */
QToolButton {{
    background: transparent;
    border: none;
    border-radius: {t.radius_small}px;
    padding: 5px;
    color: {t.fg_dim};
}}
QToolButton:hover {{ background: {t.surface_hover}; color: {t.fg}; }}
QToolButton:pressed {{ background: {t.surface_active}; }}
QToolButton[variant="close"]:hover {{ background: {t.danger}; color: {t.fg}; }}
/* Botones redondos secundarios del timer (reiniciar, saltar, parar) */
QToolButton[variant="round"] {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: 22px;
}}
QToolButton[variant="round"]:hover {{ background: {t.surface_hover}; border-color: {t.surface_active}; }}

/* ---------- Inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {t.bg_header};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: {t.radius_small}px;
    padding: 7px 10px;
    selection-background-color: {t.accent};
    selection-color: {t.on_accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {t.accent}; }}
QLineEdit::placeholder {{ color: {t.fg_muted}; }}

/* ---------- Scrollbars ---------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t.surface_active};
    border-radius: 3px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.fg_muted}; }}
QScrollBar::handle:vertical:pressed {{ background: {t.accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {t.surface_active};
    border-radius: 3px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {t.fg_muted}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ---------- Menú / tooltip / popups ---------- */
QMenu {{
    background: {t.surface};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: {t.radius_small}px;
    padding: 6px;
}}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t.surface_active}; color: {t.accent}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 5px 8px; }}

QToolTip {{
    background: {t.bg_header};
    color: {t.fg_dim};
    border: 1px solid {t.border};
    padding: 5px 8px;
    font-size: 9pt;
}}

QListView {{
    background: {t.surface};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: {t.radius_small}px;
}}
QListView::item {{ padding: 5px 8px; }}
QListView::item:selected {{ background: {t.surface_active}; color: {t.accent}; }}

/* ---------- Splitter: invisible en reposo, línea al pasar el ratón ---------- */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {t.border}; }}

/* ---------- Tabs ---------- */
QTabWidget::pane {{
    border: 1px solid {t.border};
    border-radius: {t.radius_small}px;
    background: {t.surface};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {t.fg_dim};
    border: none;
    padding: 8px 18px;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {t.fg}; }}
QTabBar::tab:selected {{
    color: {t.fg};
    border-bottom: 2px solid {t.accent};
}}

/* ---------- Spin / combo / check / slider ---------- */
QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t.bg_header};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: {t.radius_small}px;
    padding: 5px 8px;
}}
QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {t.surface_active}; }}
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {t.accent}; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    width: 22px;
    border: none;
    background: transparent;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: {t.radius_small}px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: {t.radius_small}px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: {t.surface_hover}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({chevron_up}); width: 11px; height: 11px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({chevron_down}); width: 11px; height: 11px; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border: none;
}}
QComboBox::down-arrow {{ image: url({chevron_down}); width: 12px; height: 12px; }}
QComboBox::down-arrow:on {{ image: url({chevron_up}); }}
QComboBox QAbstractItemView {{
    background: {t.surface};
    color: {t.fg};
    border: 1px solid {t.border};
    selection-background-color: {t.surface_active};
    selection-color: {t.accent};
}}

QCheckBox {{ color: {t.fg}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {t.fg_muted};
    border-radius: 4px;
    background: {t.bg_header};
}}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{
    background: {t.accent};
    border-color: {t.accent};
    image: url({check});
}}
QRadioButton {{ color: {t.fg}; spacing: 8px; }}
QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {t.fg_muted};
    border-radius: 8px;
    background: {t.bg_header};
}}
QRadioButton::indicator:hover {{ border-color: {t.accent}; }}
QRadioButton::indicator:checked {{
    border-color: {t.accent};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 {t.accent}, stop:0.45 {t.accent}, stop:0.55 {t.bg_header}, stop:1 {t.bg_header});
}}

QSlider::groove:horizontal {{
    height: 5px;
    background: {t.bg_header};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {t.accent};
    width: 14px; height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 2px; }}
/* Desactivado (p. ej. el volumen de los avisos con los sonidos apagados o
   con los de Windows): sin el acento, para que se note que no aplica */
QSlider::handle:horizontal:disabled {{ background: {t.fg_muted}; }}
QSlider::sub-page:horizontal:disabled {{ background: {t.surface_active}; }}
"""
