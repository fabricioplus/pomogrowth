"""Saludo con el nombre de la persona y textos de la racha de uso.

El nombre vive en settings['user_name'] (se pide al primer arranque y se
cambia en Configuración → Perfil). Los textos con nombre son texto
enriquecido: el nombre va escapado (html.escape), en negrita y con el color
de acento. Sin nombre, el saludo es el de siempre («Buenos días»).
"""
import html
from datetime import datetime
from typing import Optional, Tuple

NAME_MAX_LEN = 30  # tope del campo: el nombre va en títulos de ancho fijo


def clean_name(name) -> str:
    """Nombre tal como se guarda: sin espacios sobrantes y con su tope."""
    return " ".join(str(name or "").split())[:NAME_MAX_LEN].strip()


def first_name(name) -> str:
    """Primera palabra del nombre (para los textos más estrechos)."""
    parts = clean_name(name).split()
    return parts[0] if parts else ""


def greeting_word(now: datetime) -> str:
    """Saludo según la hora: días 5-12, tardes 12-20, noches 20-5."""
    if 5 <= now.hour < 12:
        return "Buenos días"
    if 12 <= now.hour < 20:
        return "Buenas tardes"
    return "Buenas noches"


def name_html(name, accent: Optional[str] = None) -> str:
    """El nombre escapado, en negrita y (con `accent`) en ese color."""
    safe = html.escape(clean_name(name))
    if accent:
        return f"<b style='color:{accent}'>{safe}</b>"
    return f"<b>{safe}</b>"


def with_name(text: str, name, accent: Optional[str] = None) -> str:
    """«<texto>, <nombre>» en texto enriquecido; sin nombre, el texto solo
    (también escapado, para usarlo en etiquetas RichText)."""
    if not clean_name(name):
        return html.escape(text)
    return f"{html.escape(text)}, {name_html(name, accent)}"


def greeting_text(now: datetime, name="", accent: Optional[str] = None) -> str:
    """«Buenos días, Fabricio» (texto enriquecido con el nombre destacado);
    sin nombre, el saludo de siempre sin marcas («Buenos días»)."""
    word = greeting_word(now)
    return with_name(word, name, accent) if clean_name(name) else word


def _days(n: int) -> str:
    return f"{n} {'día' if n == 1 else 'días'}"


def streak_text(current: int) -> str:
    """«12 días seguidos» / «1 día seguido»."""
    return f"{_days(current)} {'seguido' if current == 1 else 'seguidos'}"


def streak_tooltip(best: int) -> str:
    return f"Mejor racha: {_days(best)}"


def valid_streak(streak) -> Optional[Tuple[int, int]]:
    """(actual, mejor) si hay racha que mostrar; si no, None."""
    try:
        current, best = (int(x) for x in streak)
    except (TypeError, ValueError):
        return None
    return (current, max(best, current)) if current > 0 else None
