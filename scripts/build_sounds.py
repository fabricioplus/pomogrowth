"""Sintetiza los sonidos del temporizador (assets/sounds/timer/<estilo>/<evento>.wav).

Uso: python scripts/build_sounds.py   (desde la raíz del repositorio; solo
numpy y la biblioteca estándar: sin Qt, sin red, sin display)
Determinista: misma salida byte a byte en cada ejecución (semilla fija para
el ruido del golpe de la marimba). Los WAV generados se versionan junto a
este script; regenerarlos solo si se cambia la síntesis, y volver a pasar
tests/test_timer_sounds.py (formato, duración, pico, bordes en cero).

Formato: WAV PCM de 16 bits, mono, 44,1 kHz (lo que QSoundEffect carga sin
convertir). Tres estilos y cuatro eventos (services/sound_service.py):

- campana: campana/cuenco tibetano, parciales inarmónicos (1, 2,76, 5,4,
  8,93 y un 2,01 que da el brillo) con caída larga; los agudos se apagan antes.
- marimba: barra de madera, modos 1, 3,9 y 9,2 con caída corta, ataque suave
  y un golpe de baqueta (ruido filtrado) casi imperceptible.
- suave: tonos casi sinusoidales tipo cristal, ataque lento y caída amable.

- start (al iniciar o reanudar): dos notas breves ascendentes, discretas.
- stop (al pausar): dos notas breves descendentes.
- complete (fin del pomodoro → a descansar; también fin de la sesión Libre):
  arpegio mayor ascendente de 3 notas que se queda sonando.
- break (fin del descanso → a enfocarse): llamada de 2 notas (quinta justa
  ascendente, más espaciada), distinta del arpegio.

Sin clics: cada nota arranca desde cero con un ataque de coseno de al menos
5 ms y el archivo acaba con un fundido de coseno hasta cero (40 ms en
start/stop; 0,3 s en complete/break, para que la cola se apague sin cortarse). Nivel: pico
≈ −3 dBFS; la sonoridad (RMS máxima en ventanas de 50 ms) se iguala entre
estilos para cada evento, y start/stop quedan ~5 dB por debajo de complete.
"""
import wave
from pathlib import Path

import numpy as np

V9 = Path(__file__).resolve().parent.parent
OUT_DIR = V9 / "assets" / "sounds" / "timer"

SAMPLE_RATE = 44100
SEED = 20261104
PEAK_DBFS = -3.0
# Cuánto más bajos que complete van los avisos breves de iniciar/pausar
SHORT_EVENTS_DB = -5.0

STYLES = ("campana", "marimba", "suave")
EVENTS = ("start", "stop", "complete", "break")
# Duración total de cada evento (s): start/stop ≤ 0,4; complete 1,5-2,5;
# break 1,2-2
DURATIONS = {"start": 0.32, "stop": 0.32, "complete": 2.0, "break": 1.6}
# Fundido final: corto en los avisos breves; largo en los que se quedan
# sonando (la cola se apaga en vez de cortarse)
FADE_OUT_S = {"start": 0.04, "stop": 0.04, "complete": 0.35, "break": 0.30}


def _hz(note: str) -> float:
    """'A4' → 440 Hz (temperamento igual; solo naturales y sostenidos)."""
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4, "F#": -3,
             "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}
    name, octave = note[:-1], int(note[-1])
    return 440.0 * 2 ** ((names[name] + 12 * (octave - 4)) / 12)


# ---------------------------------------------------------------- estilos
# Cada estilo: parciales (relación de frecuencia, amplitud, factor de caída
# respecto a la fundamental), ataque, caída de la fundamental en la nota
# larga y en la breve, y las notas de cada evento.
STYLE_DEFS = {
    "campana": {
        "partials": [(1.0, 1.0, 1.0), (1.003, 0.35, 0.9),   # batido lento
                     (2.01, 0.30, 0.55), (2.76, 0.45, 0.45),
                     (5.40, 0.18, 0.25), (8.93, 0.07, 0.15)],
        "attack": 0.006, "tau_long": 0.95, "tau_short": 0.10,
        "notes": {"start": ["E5", "B5"], "stop": ["B5", "E5"],
                  "complete": ["E5", "G#5", "B5"], "break": ["B5", "F#6"]},
    },
    "marimba": {
        "partials": [(1.0, 1.0, 1.0), (3.9, 0.28, 0.30), (9.2, 0.07, 0.12)],
        "attack": 0.010, "tau_long": 0.42, "tau_short": 0.09,
        "mallet": 0.06,
        "notes": {"start": ["C5", "G5"], "stop": ["G5", "C5"],
                  "complete": ["C5", "E5", "G5"], "break": ["G5", "D6"]},
    },
    "suave": {
        "partials": [(1.0, 1.0, 1.0), (2.0, 0.10, 0.6), (3.0, 0.025, 0.4)],
        "attack": 0.025, "tau_long": 0.85, "tau_short": 0.09,
        "notes": {"start": ["G5", "D6"], "stop": ["D6", "G5"],
                  "complete": ["G5", "B5", "D6"], "break": ["D6", "A6"]},
    },
}

# Separación entre notas (s) y peso de cada nota en cada evento
ONSETS = {"start": (0.0, 0.07), "stop": (0.0, 0.07),
          "complete": (0.0, 0.17, 0.34), "break": (0.0, 0.32)}
NOTE_GAINS = {"start": (0.8, 1.0), "stop": (1.0, 0.8),
              "complete": (0.85, 0.9, 1.0), "break": (1.0, 1.0)}


def _attack_env(n: int, attack_s: float) -> np.ndarray:
    """Rampa de coseno de 0 a 1 en `attack_s` (≥ 5 ms) y 1 después."""
    a = max(int(attack_s * SAMPLE_RATE), int(0.005 * SAMPLE_RATE))
    env = np.ones(n)
    a = min(a, n)
    env[:a] = 0.5 - 0.5 * np.cos(np.pi * np.arange(a) / a)
    return env


def _note(style: dict, freq: float, length: int, tau: float, rng) -> np.ndarray:
    t = np.arange(length) / SAMPLE_RATE
    out = np.zeros(length)
    nyquist = SAMPLE_RATE / 2
    for ratio, amp, decay in style["partials"]:
        f = freq * ratio
        if f >= nyquist * 0.9:
            continue
        out += amp * np.sin(2 * np.pi * f * t) * np.exp(-t / (tau * decay))
    mallet = style.get("mallet")
    if mallet:
        # Golpe de baqueta: 15 ms de ruido suavizado (media móvil) con su caída
        n = int(0.015 * SAMPLE_RATE)
        burst = rng.standard_normal(n)
        burst = np.convolve(burst, np.ones(12) / 12, mode="same")
        burst *= np.exp(-np.arange(n) / (0.004 * SAMPLE_RATE))
        out[:n] += mallet * burst / max(1e-9, np.abs(burst).max())
    return out * _attack_env(length, style["attack"])


def _render(style_name: str, event: str, rng) -> np.ndarray:
    style = STYLE_DEFS[style_name]
    total = int(DURATIONS[event] * SAMPLE_RATE)
    out = np.zeros(total)
    short = event in ("start", "stop")
    for i, (note, onset, gain) in enumerate(zip(style["notes"][event], ONSETS[event],
                                                NOTE_GAINS[event])):
        start = int(onset * SAMPLE_RATE)
        tau = style["tau_short"] if short else style["tau_long"]
        if not short and i == len(ONSETS[event]) - 1:
            tau *= 1.25  # la última nota se queda sonando un poco más
        out[start:] += gain * _note(style, _hz(note), total - start, tau, rng)
    # Fundido final de coseno hasta cero exacto en la última muestra
    fade = int(FADE_OUT_S[event] * SAMPLE_RATE)
    out[-fade:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(1, fade + 1) / fade)
    return out


def _loudness(x: np.ndarray) -> float:
    """RMS máxima en ventanas de 50 ms (salto de 10 ms): lo que se oye de un
    golpe de campana es su arranque, no la media con la cola."""
    win, hop = int(0.05 * SAMPLE_RATE), int(0.01 * SAMPLE_RATE)
    best = 0.0
    for i in range(0, max(1, len(x) - win), hop):
        best = max(best, float(np.sqrt(np.mean(x[i:i + win] ** 2))))
    return best


def build() -> dict:
    """Renderiza los 12 sonidos y los deja nivelados: {(estilo, evento): array}."""
    rng = np.random.default_rng(SEED)
    peak = 10 ** (PEAK_DBFS / 20)
    sounds = {}
    for style in STYLES:
        for event in EVENTS:
            x = _render(style, event, rng)
            sounds[(style, event)] = x * (peak / np.abs(x).max())
    # Igualar la sonoridad entre estilos: manda el más bajo (el pico de los
    # demás queda por debajo de −3 dBFS, nunca por encima)
    target = {}
    for event in EVENTS:
        target[event] = min(_loudness(sounds[(s, event)]) for s in STYLES)
    short_cap = target["complete"] * 10 ** (SHORT_EVENTS_DB / 20)
    for event in ("start", "stop"):
        target[event] = min(target[event], short_cap)
    for (style, event), x in sounds.items():
        sounds[(style, event)] = x * (target[event] / _loudness(x))
    return sounds


def write_wav(path: Path, x: np.ndarray) -> None:
    pcm = np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def main() -> None:
    total = 0
    for (style, event), x in build().items():
        path = OUT_DIR / style / f"{event}.wav"
        write_wav(path, x)
        size = path.stat().st_size
        total += size
        peak_db = 20 * np.log10(np.abs(x).max())
        print(f"{path.relative_to(V9).as_posix():42s} {len(x) / SAMPLE_RATE:5.2f} s  "
              f"pico {peak_db:5.1f} dBFS  {size / 1024:6.1f} KB")
    print(f"Total: {total / 1024:.0f} KB")


if __name__ == "__main__":
    main()
