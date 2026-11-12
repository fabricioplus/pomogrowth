"""Métrica de esfuerzo: funciones puras sobre 'effort_ratings' (lista de
{task_id, rating 1..5, timestamp, task_name, project, tags, recurring,
focus_s}). Sin Qt ni DataHandler: las usan Estadísticas y los tests.
"""
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

# Qué significa cada punto de la escala (popup, tooltips y estadísticas)
EFFORT_LABELS = {1: "Muy fácil", 2: "Fácil", 3: "Normal", 4: "Duro", 5: "Muy duro"}


def valid_ratings(ratings: Iterable) -> List[Dict]:
    """Solo entradas bien formadas (dict con rating entero 1..5)."""
    out = []
    for r in ratings or []:
        if not isinstance(r, dict):
            continue
        value = r.get('rating')
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5:
            out.append(r)
    return out


def ratings_since(ratings: Iterable, since: Optional[datetime]) -> List[Dict]:
    """Valoraciones desde `since` (None = todas). Las de fecha ilegible solo
    entran en el histórico."""
    valid = valid_ratings(ratings)
    if since is None:
        return valid
    out = []
    for r in valid:
        try:
            if datetime.fromisoformat(str(r.get('timestamp', ''))) >= since:
                out.append(r)
        except ValueError:
            continue
    return out


def average(ratings: Iterable) -> Optional[float]:
    """Media de esfuerzo, o None sin valoraciones."""
    valid = valid_ratings(ratings)
    if not valid:
        return None
    return sum(r['rating'] for r in valid) / len(valid)


def distribution(ratings: Iterable) -> List[int]:
    """Cuántas valoraciones hay de cada punto: [n1, n2, n3, n4, n5]."""
    counts = [0] * 5
    for r in valid_ratings(ratings):
        counts[r['rating'] - 1] += 1
    return counts


def average_by_project(ratings: Iterable) -> List[Tuple[str, float, int]]:
    """[(proyecto, media, número)] de más a menos valoraciones (empate: más
    esfuerzo primero). Sin proyecto cuenta como «Sin proyecto»."""
    groups: Dict[str, List[int]] = defaultdict(list)
    for r in valid_ratings(ratings):
        groups[str(r.get('project') or '').strip() or 'Sin proyecto'].append(r['rating'])
    rows = [(name, sum(v) / len(v), len(v)) for name, v in groups.items()]
    rows.sort(key=lambda row: (-row[2], -row[1], row[0].lower()))
    return rows


def focus_minutes_per_point(ratings: Iterable) -> Optional[float]:
    """Minutos de foco por punto de esfuerzo: cuánto tiempo «cuesta» cada
    punto. Solo cuentan las valoraciones con tiempo de foco; None sin datos."""
    minutes = 0.0
    points = 0
    for r in valid_ratings(ratings):
        try:
            secs = float(r.get('focus_s') or 0)
        except (TypeError, ValueError):
            continue
        if secs > 0:
            minutes += secs / 60
            points += r['rating']
    if not points:
        return None
    return minutes / points


def summary(ratings: Iterable) -> Dict:
    """Todo lo que muestra la sección «Esfuerzo» de una vez."""
    valid = valid_ratings(ratings)
    return {
        'count': len(valid),
        'average': average(valid),
        'distribution': distribution(valid),
        'by_project': average_by_project(valid),
        'minutes_per_point': focus_minutes_per_point(valid),
    }
