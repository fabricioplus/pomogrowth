"""Asistente de la mascota pixel: frases locales gratis + consejos vía IA.

Multi-proveedor: Claude (SDK oficial `anthropic`), ChatGPT (API de OpenAI vía
requests) o un endpoint propio compatible con el formato OpenAI — p. ej. un
servidor local (Ollama, LM Studio, n8n) expuesto con un túnel de ngrok.
Toda la red corre en hilo con run_async — nunca en el hilo de UI. La IA es
opcional: sin key/URL la mascota sigue funcionando con frases locales.
"""
import random
from typing import Any, Callable, Dict, Optional

from services.workers import run_async

PET_NAMES = {
    'pet_cat': 'Gato Pixel',
    'pet_dog': 'Perro Pixel',
    'pet_rat': 'Rata Pixel',
}

PET_PERSONALITIES = {
    'pet_cat': "un gato pixel 8-bit independiente y sabio, con humor seco pero cariñoso",
    'pet_dog': "un perro pixel 8-bit entusiasta y leal que celebra cada logro",
    'pet_rat': "una rata pixel 8-bit astuta y pragmática, experta en trucos de productividad",
}

# --- Proveedores de IA -------------------------------------------------------
PROVIDERS = [('claude', 'Claude (Anthropic)'),
             ('openai', 'ChatGPT (OpenAI)'),
             ('custom', 'Endpoint propio (ngrok / local)')]

DEFAULT_MODEL = "claude-opus-4-8"  # compat con settings previos (proveedor claude)
SUGGESTED_MODELS = {
    'claude': ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
    'openai': ["gpt-5-mini", "gpt-5.2", "gpt-5.1", "gpt-4o-mini"],
    'custom': ["llama3", "mistral", "qwen2.5"],  # sugerencias típicas de Ollama
}
AVAILABLE_MODELS = SUGGESTED_MODELS['claude']  # alias de compat


def provider_config(settings: Dict[str, Any]) -> Dict[str, str]:
    """Config del proveedor activo a partir de settings. Cada proveedor guarda
    su propia key/modelo para poder alternar sin re-pegar credenciales."""
    provider = settings.get('pet_provider') or 'claude'
    if provider == 'openai':
        return {'provider': 'openai',
                'api_key': (settings.get('pet_openai_key') or '').strip(),
                'model': (settings.get('pet_openai_model') or '').strip() or 'gpt-5-mini',
                'base_url': ''}
    if provider == 'custom':
        return {'provider': 'custom',
                'api_key': (settings.get('pet_custom_key') or '').strip(),
                'model': (settings.get('pet_custom_model') or '').strip(),
                'base_url': (settings.get('pet_custom_url') or '').strip()}
    return {'provider': 'claude',
            'api_key': (settings.get('pet_api_key') or '').strip(),
            'model': (settings.get('pet_model') or '').strip() or DEFAULT_MODEL,
            'base_url': ''}


def is_configured(config: Dict[str, str]) -> bool:
    """¿El proveedor activo tiene lo mínimo para llamar? El endpoint propio
    solo exige URL (muchos servidores locales van sin key)."""
    if config['provider'] == 'custom':
        return bool(config['base_url'])
    return bool(config['api_key'])


def normalize_custom_url(base_url: str) -> str:
    """URL del endpoint propio → ruta completa de chat completions.
    Acepta la URL pelada de ngrok/localhost, con /v1, o ya completa."""
    url = (base_url or '').strip().rstrip('/')
    if not url:
        return ''
    if url.endswith('/chat/completions'):
        return url
    if url.endswith('/v1'):
        return url + '/chat/completions'
    return url + '/v1/chat/completions'


# Frases locales por evento (sin red): la mascota siempre reacciona
_LOCAL_PHRASES = {
    'pomodoro': {
        'pet_cat': ["Mrrr… otro pomodoro. Admito que estoy impresionado. 🍅",
                    "Bien. Ahora estírate, como hago yo. 🐱",
                    "25 minutos sin distraerte. Casi tan disciplinado como un gato."],
        'pet_dog': ["¡GUAU! ¡Lo lograste! ¡Sabía que podías! 🎉",
                    "¡Otro pomodoro! ¡Eres el mejor humano del mundo! 🐕",
                    "¡Sigue así y te persigo la cola de la emoción!"],
        'pet_rat': ["Buen trabajo. Dato: los descansos cortos consolidan la memoria. 🧀",
                    "Otro bloque terminado. Eficiencia nivel roedor.",
                    "Pomodoro registrado. Mi consejo: hidrátate ahora."],
    },
    # Sesión Libre: ni cuenta como pomodoro ni dura un tiempo fijo
    'free_session': {
        'pet_cat': ["Sesión terminada. No está mal… para un humano. 🐱",
                    "Has estado concentrado. Yo también: en dormir. 😼",
                    "Bien hecho. Ahora estírate, como hago yo."],
        'pet_dog': ["¡Sesión terminada! ¡Qué bien lo has hecho! 🎉",
                    "¡Eso sí que es concentrarse! ¡Choca esa pata! 🐕",
                    "¡Buen trabajo! ¿Un paseíto para celebrarlo?"],
        'pet_rat': ["Sesión registrada. Cada rato enfocado suma. 🧀",
                    "Buen bloque de trabajo. Mi consejo: hidrátate ahora.",
                    "Tiempo enfocado anotado. Eficiencia nivel roedor."],
    },
    'cycle': {
        'pet_cat': ["Un ciclo completo. Te has ganado mi respeto… y una siesta. 😼",
                    "Ciclo terminado. Yo llevaría 4 siestas en ese tiempo."],
        'pet_dog': ["¡¡CICLO COMPLETO!! ¡Vuelta de la victoria! 🏆🐕",
                    "¡Eres imparable! ¡Descanso largo bien merecido!"],
        'pet_rat': ["Ciclo completo: el interés compuesto de la concentración. 📈",
                    "4 pomodoros seguidos. Estadísticamente, estás en tu mejor racha."],
    },
    'level_up': {
        'pet_cat': ["¿Nivel nuevo? Supongo que mereces un ronroneo. 🎉"],
        'pet_dog': ["¡¡SUBISTE DE NIVEL!! ¡Ladridos de celebración! 🎊"],
        'pet_rat': ["Nivel nuevo. Tus stats mejoran — sigue optimizando. 🎯"],
    },
    'greeting': {
        'pet_cat': ["Ya era hora de que llegaras. Elige una tarea. 🐱"],
        'pet_dog': ["¡Llegaste! ¡Hoy será un gran día de pomodoros! 🐕"],
        'pet_rat': ["Puntual. Las ratas valoramos la rutina. A trabajar. 🐭"],
    },
}


def local_message(event: str, species: str) -> Optional[str]:
    """Frase local (sin red) para un evento; None si no hay frase."""
    phrases = _LOCAL_PHRASES.get(event, {}).get(species)
    return random.choice(phrases) if phrases else None


def build_context(stats: Dict[str, Any], pending_tasks: int,
                  today_minutes: int) -> str:
    """Resumen del progreso del usuario para el prompt del consejo."""
    return (
        f"Nivel: {stats.get('level', 1)} · XP: {stats.get('xp', 0)} · "
        f"Monedas: {stats.get('coins', 0)}\n"
        f"Pomodoros totales: {stats.get('total_pomodoros', 0)} · "
        f"Ciclos completos: {stats.get('total_cycles', 0)}\n"
        f"Racha actual: {stats.get('streak_days', 0)} días · "
        f"Tiempo total enfocado: {stats.get('total_time_minutes', 0)} min\n"
        f"Hoy: {today_minutes} min enfocado · Tareas pendientes: {pending_tasks}"
    )


def _system_prompt(species: str) -> str:
    personality = PET_PERSONALITIES.get(species, PET_PERSONALITIES['pet_cat'])
    return (f"Eres {personality}, la mascota virtual de PomoGrowth, una app "
            "Pomodoro de productividad. Hablas en español, en 2-4 frases "
            "cortas, con la voz de tu personaje. Analiza las estadísticas "
            "del usuario y dale UN consejo concreto y accionable para "
            "mejorar su productividad, más un comentario motivador. "
            "No uses listas ni encabezados: solo texto de burbuja de chat.")


def _user_prompt(context: str) -> str:
    return ("Estas son mis estadísticas de hoy en PomoGrowth:\n\n"
            f"{context}\n\n¿Qué me aconsejas?")


def _call_claude(api_key: str, model: str, system: str, user: str) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "El paquete 'anthropic' no está instalado. "
            "Instálalo con: pip install anthropic")
    client = anthropic.Anthropic(api_key=api_key)
    model_id = model or DEFAULT_MODEL
    extra = {}
    if model_id != "claude-haiku-4-5":
        # sonnet-5 corre adaptive thinking por defecto y puede consumir el
        # presupuesto de salida pensando; para una burbuja corta lo apagamos.
        # (haiku no acepta el parámetro: se omite y ya corre sin thinking)
        extra["thinking"] = {"type": "disabled"}
    response = client.messages.create(
        model=model_id, max_tokens=600, **extra, system=system,
        messages=[{"role": "user", "content": user}])
    if response.stop_reason == "refusal":
        return "Hoy no tengo consejos… pero ¡sigue con tus pomodoros! 🍅"
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or "…(la mascota se quedó pensando; inténtalo de nuevo)"


def _call_openai_style(url: str, api_key: str, model: str, system: str,
                       user: str, max_tokens_key: str, timeout: int,
                       extra_payload: Optional[Dict] = None,
                       extra_headers: Optional[Dict] = None) -> str:
    """Chat completions estilo OpenAI: sirve para api.openai.com y para
    servidores compatibles (Ollama, LM Studio, vLLM…) detrás de ngrok."""
    import requests
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               max_tokens_key: 1000}
    if extra_payload:
        payload.update(extra_payload)
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code == 401:
        raise RuntimeError("API key inválida o sin permisos (401)")
    if r.status_code == 404:
        raise RuntimeError(f"Modelo o ruta no encontrados (404) — revisa el "
                           f"modelo «{model}» y la URL")
    if r.status_code != 200:
        raise RuntimeError(f"Error {r.status_code}: {r.text[:200]}")
    data = r.json() or {}
    choices = data.get("choices") or [{}]
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    return text or "…(la mascota se quedó pensando; inténtalo de nuevo)"


def request_advice(config: Dict[str, str], species: str, context: str,
                   on_done: Callable, on_error: Callable) -> None:
    """Pide un consejo al proveedor configurado en un hilo; el resultado
    vuelve por señal. config: dict de provider_config()."""
    system = _system_prompt(species)
    user = _user_prompt(context)
    provider = config.get('provider', 'claude')
    api_key = config.get('api_key', '')
    model = config.get('model', '')

    def call() -> str:
        if provider == 'openai':
            extra = {}
            if model.startswith(('gpt-5', 'o1', 'o3', 'o4')):
                # Los razonadores gastan salida pensando: mínimo razonable
                extra['reasoning_effort'] = 'low'
            return _call_openai_style(
                "https://api.openai.com/v1/chat/completions", api_key,
                model or 'gpt-5-mini', system, user,
                max_tokens_key='max_completion_tokens', timeout=45,
                extra_payload=extra)
        if provider == 'custom':
            url = normalize_custom_url(config.get('base_url', ''))
            if not url:
                raise RuntimeError("Configura la URL del endpoint (p. ej. la "
                                   "de tu túnel de ngrok) en ⚙ → Mascota IA")
            # ngrok free intercala un aviso para navegadores; el header lo evita
            return _call_openai_style(
                url, api_key, model or 'llama3', system, user,
                max_tokens_key='max_tokens', timeout=90,
                extra_headers={"ngrok-skip-browser-warning": "1"})
        return _call_claude(api_key, model, system, user)

    run_async(call, on_done, on_error)
