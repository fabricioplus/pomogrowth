"""Plan de hoy: qué tareas sugerir al empezar el día (lógica pura, sin Qt).

suggest() ordena las tareas PENDIENTES por urgencia: vencidas recientes (las
más atrasadas primero), vencen hoy, mañana, en ≤ 7 días, tareas fijas, tareas
con actividad reciente («En curso») y, al final, las vencidas hace más de un
mes (probablemente abandonadas: se sugieren, pero no llegan marcadas). El
resto no se sugiere. default_selection() elige cuáles llegan marcadas al
diálogo de planificación.

Fechas: 'due' viene de Asana como due_on ('2026-10-27') o due_at con hora en
UTC ('2026-10-28T03:00:00.000Z'). Se compara siempre por la fecha LOCAL: a las
22:00 de Lima esa tarea vence HOY, aunque en UTC ya sea mañana.
"""
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, Iterable, List, NamedTuple, Optional

# Motivos de sugerencia, de más a menos urgente
OVERDUE = 'overdue'
DUE_TODAY = 'due_today'
DUE_TOMORROW = 'due_tomorrow'
DUE_SOON = 'due_soon'
RECURRING = 'recurring'
IN_PROGRESS = 'in_progress'
OVERDUE_STALE = 'overdue_stale'  # vencida hace más de STALE_DAYS

# Los que se preseleccionan solos al abrir el diálogo
URGENT_REASONS = (OVERDUE, DUE_TODAY, DUE_TOMORROW)

SOON_DAYS = 7     # «vence en N días» hasta una semana vista
RECENT_DAYS = 3   # actividad de hoy y de los 3 días anteriores = «En curso»
STALE_DAYS = 30   # más atrasada que esto: al final y sin preseleccionar

# Base de puntuación de cada motivo: tramos separados por 1000 para que el
# ajuste dentro de un tramo (días de atraso, minutos) nunca cruce al siguiente
_BASE = {OVERDUE: 6000, DUE_TODAY: 5000, DUE_TOMORROW: 4000, DUE_SOON: 3000,
         RECURRING: 2000, IN_PROGRESS: 1000, OVERDUE_STALE: 0}

_WEEKDAYS = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')
_MONTHS = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
           'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')


class Suggestion(NamedTuple):
    task_id: Any       # int de Python (timestamps en ms, > 2^31)
    score: float       # mayor = más urgente
    reason_code: str   # OVERDUE, DUE_TODAY…
    reason_text: str   # «Vencida hace 3 días», «Vence hoy»…


def format_long_date(d: date) -> str:
    """'martes 27 de octubre' — en español sin depender del locale."""
    return f"{_WEEKDAYS[d.weekday()]} {d.day} de {_MONTHS[d.month - 1]}"


def _parse_local_date(raw: Any, tz: Optional[tzinfo] = None) -> Optional[date]:
    """Fecha local de un ISO con o sin hora. Con zona (Asana manda UTC, 'Z'),
    se pasa a la zona local (o a `tz`, para los tests) antes de quedarse con
    el día. Sin hora o sin zona, la fecha tal cual. None si no se entiende."""
    if not raw:
        return None
    text = str(raw).strip()
    if len(text) <= 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        d = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone(tz) if tz is not None else d.astimezone()
    return d.date()


def due_local_date(task: Dict[str, Any], tz: Optional[tzinfo] = None) -> Optional[date]:
    """Fecha local de vencimiento de una tarea, o None si no tiene."""
    return _parse_local_date(task.get('due'), tz)


def _overdue_text(days: int) -> str:
    if days == 1:
        return "Vencida ayer"
    if days < 30:
        return f"Vencida hace {days} días"
    # Atrasos largos, redondeados: «hace 328 días» no cabe en un chip
    if days < 365:
        months = days // 30
        return f"Vencida hace {months} {'mes' if months == 1 else 'meses'}"
    return "Vencida hace más de un año"


def _recent_activity(time_chunks: Iterable[Dict[str, Any]], today: date,
                     tz: Optional[tzinfo]) -> Dict[Any, Dict[str, float]]:
    """Por tarea: minutos enfocados y días desde el último pomodoro, contando
    hoy y los RECENT_DAYS anteriores. Los descansos no son trabajo."""
    first_day = today - timedelta(days=RECENT_DAYS)
    out: Dict[Any, Dict[str, float]] = {}
    for c in time_chunks or ():
        if not isinstance(c, dict) or c.get('type', 'pomodoro') != 'pomodoro':
            continue
        day = _parse_local_date(c.get('timestamp'), tz)
        if day is None or not first_day <= day <= today:
            continue
        info = out.setdefault(c.get('task_id'), {'minutes': 0.0, 'days_ago': RECENT_DAYS})
        try:
            info['minutes'] += float(c.get('duration') or 0) / 60
        except (TypeError, ValueError):
            pass
        info['days_ago'] = min(info['days_ago'], (today - day).days)
    return out


def suggest(tasks: Iterable[Dict[str, Any]], time_chunks: Iterable[Dict[str, Any]],
            today: Optional[date] = None, tz: Optional[tzinfo] = None) -> List[Suggestion]:
    """Sugerencias para hoy, de más a menos urgente. Solo tareas pendientes;
    las marcadas como eliminadas en Asana no se sugieren (siguen saliendo en
    «Otras pendientes» del diálogo). Cada tarea aparece una vez, con su motivo
    más urgente; la actividad reciente desempata dentro del mismo motivo."""
    today = today or date.today()
    activity = _recent_activity(time_chunks, today, tz)
    out: List[Suggestion] = []
    for task in tasks or ():
        if not isinstance(task, dict) or task.get('completed') or task.get('asana_deleted'):
            continue
        tid = task.get('id')
        if tid is None:
            continue
        recent = activity.get(tid)
        # Desempate dentro de un tramo: minutos recientes, < 1 punto
        bonus = min(recent['minutes'], 99) / 100 if recent else 0.0
        due = due_local_date(task, tz)
        delta = (due - today).days if due else None
        if delta is not None and -delta > STALE_DAYS:
            # Atraso de más de un mes: casi seguro abandonada; al final
            # (las más recientes primero) y sin preseleccionar
            days = -delta
            code, text = OVERDUE_STALE, _overdue_text(days)
            score = _BASE[OVERDUE_STALE] + max(0, 999 - days)
        elif delta is not None and delta < 0:
            days = -delta
            code, text = OVERDUE, _overdue_text(days)
            score = _BASE[OVERDUE] + min(days, 999)
        elif delta == 0:
            code, text, score = DUE_TODAY, "Vence hoy", _BASE[DUE_TODAY]
        elif delta == 1:
            code, text, score = DUE_TOMORROW, "Vence mañana", _BASE[DUE_TOMORROW]
        elif delta is not None and delta <= SOON_DAYS:
            # Menos días restantes, más arriba
            code, text = DUE_SOON, f"Vence en {delta} días"
            score = _BASE[DUE_SOON] + (SOON_DAYS + 1 - delta)
        elif task.get('recurring'):
            code, text, score = RECURRING, "Tarea fija", _BASE[RECURRING]
        elif recent:
            # Lo trabajado hoy antes que lo de anteayer
            code, text = IN_PROGRESS, "En curso"
            score = _BASE[IN_PROGRESS] + (RECENT_DAYS + 1 - recent['days_ago']) * 100
        else:
            continue
        out.append(Suggestion(tid, score + bonus, code, text))
    # sorted() es estable: a igual puntuación, el orden de la lista de tareas
    return sorted(out, key=lambda s: -s.score)


def default_selection(suggestions: Iterable[Suggestion], limit: int = 5) -> List[Any]:
    """Ids que llegan marcados al diálogo: las urgentes (vencidas en el
    último mes, hoy, mañana) hasta `limit`; si no hay ninguna, las 3 primeras
    sugerencias que no sean vencidas antiguas."""
    suggestions = list(suggestions or ())
    urgent = [s.task_id for s in suggestions if s.reason_code in URGENT_REASONS]
    if urgent:
        return urgent[:max(0, limit)]
    fresh = [s.task_id for s in suggestions if s.reason_code != OVERDUE_STALE]
    return fresh[:min(3, max(0, limit))]


def summary_counts(suggestions: Iterable[Suggestion]) -> Dict[str, int]:
    """Cuántas sugerencias hay de cada motivo (para el resumen del diálogo)."""
    counts: Dict[str, int] = {}
    for s in suggestions or ():
        counts[s.reason_code] = counts.get(s.reason_code, 0) + 1
    return counts
