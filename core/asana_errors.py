"""Errores al LEER de Asana (listados de tareas, proyectos…), sin nada de Qt.

Un fetch que no sale bien lanza AsanaFetchError en vez de devolver una lista
vacía o a medias: un listado incompleto pasaría por un sync correcto («Sin
cambios desde Asana») y lo que faltara se verificaría una a una como si se
hubiera borrado. Una página que falla a mitad de la paginación invalida el
listado entero. Quien llama decide qué contar al usuario: el texto por
defecto (str(error)) ya sirve para mostrarlo tal cual.

La verificación de una tarea suelta (GET /tasks/{gid}) no pasa por aquí: su
404 sigue significando «borrada en Asana».
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# auth 401 · forbidden 403 (y 402, función de pago) · not_found 404 de un
# workspace o proyecto · rate_limit 429 · server 5xx (y 408) · network sin
# red, timeout o red que bloquea Asana (451) · bad_response JSON ilegible o
# cualquier otra respuesta inesperada
FETCH_ERROR_KINDS = ('auth', 'forbidden', 'not_found', 'rate_limit', 'server',
                     'network', 'bad_response')

_DEFAULT_HINT = "Revisa la conexión en ⚙ Configuración → Asana."

# Lo más que se espera por un Retry-After (s): Asana pide pocos segundos; un
# valor absurdo (o una fecha muy lejana) no puede dejar el reintento del sync
# (ni el de la cola, que comparte temporizador) parado durante días
MAX_RETRY_AFTER_S = 3600.0


def kind_for_status(code: int) -> str:
    """Tipo de error de una respuesta HTTP que no es el 200 esperado."""
    if code == 401:
        return 'auth'
    if code in (402, 403):
        return 'forbidden'
    if code == 404:
        return 'not_found'
    if code == 429:
        return 'rate_limit'
    if code == 451:
        return 'network'
    if code == 408 or code >= 500:
        return 'server'
    return 'bad_response'


def parse_retry_after(value: Any) -> Optional[float]:
    """Segundos de la cabecera Retry-After (un número o una fecha HTTP),
    acotados a [0, MAX_RETRY_AFTER_S], o None si no viene o no se entiende.
    Nunca lanza: un número enorme (o infinito) cuenta como el tope y una
    fecha pasada, como 0."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
            if when is None:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if seconds != seconds:  # NaN
        return None
    return min(max(0.0, seconds), MAX_RETRY_AFTER_S)


def default_message(kind: str, status: Optional[int] = None, target: str = '',
                    hint: str = '') -> str:
    """Texto para el usuario según el tipo de error (sin «se reintentará»:
    eso lo añade quien programa el reintento)."""
    hint = hint or _DEFAULT_HINT
    if kind == 'auth':
        return "Asana rechazó el token de acceso (HTTP 401). Revísalo en ⚙ Configuración → Asana."
    if kind == 'forbidden':
        what = f"ver {target}" if target else "hacer esa consulta"
        return f"Asana no te deja {what} (HTTP {status or 403}). {hint}"
    if kind == 'not_found':
        return f"Asana no encuentra {target or 'lo que se pidió'} (HTTP 404). {hint}"
    if kind == 'rate_limit':
        return "Asana pide esperar: demasiadas peticiones."
    if kind == 'server':
        return f"Asana no responde bien (error {status})." if status else "Asana no responde bien."
    if kind == 'network':
        if status:
            return f"Sin conexión con Asana (HTTP {status}: la red o una VPN la bloquea)."
        return "Sin conexión con Asana."
    if status and status >= 400:
        return f"Asana rechazó la petición (error {status}). {hint}"
    return "Asana devolvió una respuesta que no se entiende."


class AsanaFetchError(Exception):
    """Un fetch de Asana que no se pudo completar.

    kind: uno de FETCH_ERROR_KINDS · status: código HTTP (None sin respuesta)
    · retry_after: segundos que pidió Asana con un 429 (None si no lo dijo)
    · message: texto para el usuario (por defecto, según el tipo) · target:
    qué se estaba leyendo («el workspace 123», «el proyecto «Web»») · detail:
    lo técnico (mensaje de Asana o de la excepción), para el registro."""

    def __init__(self, kind: str, status: Optional[int] = None,
                 retry_after: Optional[float] = None, message: str = '', *,
                 target: str = '', hint: str = '', detail: str = ''):
        if kind not in FETCH_ERROR_KINDS:
            raise ValueError(f"AsanaFetchError: tipo desconocido {kind!r}")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.target = target
        self.detail = detail
        self.message = message or default_message(kind, status, target, hint)
        super().__init__(self.message)


def _asana_error_text(resp: Any) -> str:
    """El primer mensaje de error de Asana ({'errors': [{'message': …}]}) o
    el principio del cuerpo."""
    try:
        body = resp.json()
        errors = body.get('errors') if isinstance(body, dict) else None
        if errors and isinstance(errors[0], dict) and errors[0].get('message'):
            return str(errors[0]['message'])
    except Exception:
        pass
    return str(getattr(resp, 'text', '') or '')[:300]


def error_from_response(resp: Any, target: str = '', hint: str = '') -> AsanaFetchError:
    """AsanaFetchError de una respuesta HTTP distinta del 200 esperado."""
    status = int(resp.status_code)
    kind = kind_for_status(status)   # un 2xx o 3xx inesperado: bad_response
    retry_after = None
    if kind == 'rate_limit':
        headers = getattr(resp, 'headers', None) or {}
        retry_after = parse_retry_after(headers.get('Retry-After'))
    return AsanaFetchError(kind, status, retry_after, target=target, hint=hint,
                           detail=_asana_error_text(resp))
