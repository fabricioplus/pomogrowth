"""Genera los sonidos de ambiente (assets/sounds/ambient/*.ogg y CREDITOS.txt).

Uso: python scripts/build_ambient_sounds.py [--cache CARPETA]   (desde la raíz del repositorio)
Necesita, solo para este script: numpy, scipy y soundfile (pip install numpy
scipy soundfile; soundfile trae libsndfile con OGG Vorbis). La app no los usa:
reproduce los .ogg con QtMultimedia.

- Naturaleza: grabaciones reales con licencia CC0 o de dominio público
  (FUENTES). Se descargan una vez a la caché (por defecto la carpeta temporal
  del sistema), se comprueba su SHA-1 y se usa solo el tramo indicado:
  remuestreado a 44,1 kHz, filtro paso alto contra el retumbe, sonoridad
  igualada (BS.1770, sin puerta), limitador de picos a -1 dBFS, fundidos de
  50 ms y recodificado en OGG Vorbis estéreo. Ninguna exige atribución, pero
  CREDITOS.txt las acredita igual.
- Ruidos blanco, rosa y marrón: sintetizados aquí (semilla fija), en el
  dominio de la frecuencia y con cada canal independiente.

Los archivos no se repiten «a pelo»: la app los encadena con un fundido
cruzado (services/ambient_service.py), así que no hace falta que el final
empalme con el principio.
"""
import argparse
import hashlib
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "sounds" / "ambient"
SR = 44100
PEAK_CEILING_DB = -1.0
VORBIS_LEVEL = 0.62        # compression_level de libsndfile: calidad Vorbis ~0,38 (~110 kbps)
FADE_S = 0.05
UA = "PomoGrowth-build/1.0 (https://github.com/fabricioplus)"

# id: (título, autor, licencia, página, URL de descarga, SHA-1, tramo en s,
#      paso alto en Hz, sonoridad objetivo en LUFS)
FUENTES = {
    'lluvia': (
        "Light Rain Distant Thunder July 5th 2016", "kvgarlic (Freesound)", "CC0 1.0",
        "https://commons.wikimedia.org/wiki/File:Light_Rain_Distant_Thunder_July_5th_2016.wav",
        "https://upload.wikimedia.org/wikipedia/commons/b/b6/Light_Rain_Distant_Thunder_July_5th_2016.wav",
        "f01eb449e15f0a7647ba37ea3b54713ebb82d71c", (30.0, 108.0), 40, -22.0),
    'tormenta': (
        "Thunderstorm after hot summer day, 17 minutes, 01 of 04", "stephan (pdsounds.org)",
        "Dominio público",
        "https://commons.wikimedia.org/wiki/File:Thunderstorm_after_hot_summer_day_17_minutes_01_of_04.ogg",
        "https://upload.wikimedia.org/wikipedia/commons/c/cd/Thunderstorm_after_hot_summer_day_17_minutes_01_of_04.ogg",
        "f5f6ac09efe05beb3285780e1989f053f4a8a5c8", (62.0, 222.0), 30, -22.0),
    'oceano': (
        "Ocean Waves on a Tropical Beach", "Jarrod Stanley (J. D. Savanyu)", "CC0 1.0",
        "https://commons.wikimedia.org/wiki/File:Ocean_Waves_on_a_Tropical_Beach.ogg",
        "https://upload.wikimedia.org/wikipedia/commons/6/64/Ocean_Waves_on_a_Tropical_Beach.ogg",
        "22b9faaa8c718fa1596d8f41d221906623bebd5a", (40.0, 160.0), 30, -22.0),
    'rio': (
        "Sanna river rapids", "Jarrod Stanley (J. D. Savanyu)", "CC0 1.0",
        "https://commons.wikimedia.org/wiki/File:Sanna_river_rapids.ogg",
        "https://upload.wikimedia.org/wikipedia/commons/a/a4/Sanna_river_rapids.ogg",
        "781dbd41ee11d11adbf51ecee45250dc8f7ecaf4", (30.0, 120.0), 30, -23.0),
    'bosque': (
        "Forest ambience (20090610_0)", "nille (pdsounds.org)", "Dominio público",
        "https://commons.wikimedia.org/wiki/File:20090610_0_ambience.ogg",
        "https://upload.wikimedia.org/wikipedia/commons/0/0a/20090610_0_ambience.ogg",
        "981f161f9e1a5749bf5da8e37c7f37b892afe7cc", (4.0, 121.0), 120, -24.0),
    'chimenea': (
        "Crackling fireplace", "nielstii (Freesound)", "CC0 1.0",
        "https://freesound.org/people/nielstii/sounds/346321/",
        "https://cdn.freesound.org/previews/346/346321_6290213-hq.ogg",
        "c399593d4aae607d41b5254edcd3d67d42b12468", (4.0, 72.0), 50, -24.0),
}

# id: (nombre, exponente del espectro de potencia, sonoridad objetivo, duración)
RUIDOS = {
    'ruido_blanco': ("Ruido blanco", 0.0, -27.0, 45.0),
    'ruido_rosa': ("Ruido rosa", -1.0, -25.0, 45.0),
    'ruido_marron': ("Ruido marrón", -2.0, -23.0, 45.0),
}


# ------------------------------------------------------------------ descarga
def _strip_ogg_skeleton(data: bytes) -> bytes:
    """Deja solo el flujo Vorbis: libsndfile no abre los .ogg con Ogg Skeleton
    (los de pdsounds.org en Commons lo llevan)."""
    if data[:4] != b'OggS' or b'fishead' not in data[:200]:
        return data
    out, keep, i = [], None, 0
    while i < len(data):
        if data[i:i + 4] != b'OggS':
            raise ValueError("página Ogg corrupta")
        nseg = data[i + 26]
        size = 27 + nseg + sum(data[i + 27:i + 27 + nseg])
        serial = int.from_bytes(data[i + 14:i + 18], 'little')
        first = data[i + 27 + nseg:i + 27 + nseg + 7]
        if data[i + 5] & 2 and first[1:7] == b'vorbis':
            keep = serial
        if serial == keep:
            out.append(data[i:i + size])
        i += size
    return b''.join(out)


def fetch(url: str, sha1: str, cache: Path) -> Path:
    path = cache / f"{sha1}{Path(url.split('?')[0]).suffix}"
    if not path.exists():
        print(f"  descargando {url}")
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        data = urllib.request.urlopen(req, timeout=300).read()
        if hashlib.sha1(data).hexdigest() != sha1:
            raise SystemExit(f"SHA-1 distinto en {url}: el archivo de origen ha cambiado")
        path.write_bytes(data)
    fixed = path.with_name(path.stem + "_vorbis" + path.suffix)
    if path.suffix == '.ogg':
        if not fixed.exists():
            fixed.write_bytes(_strip_ogg_skeleton(path.read_bytes()))
        return fixed
    return path


# ---------------------------------------------------------------- procesado
def _biquad(b, a):
    return np.array([[*b, *a]], dtype=np.float64)


def _k_weighting(sr: int) -> np.ndarray:
    """Filtro K de ITU-R BS.1770 (estante agudo + paso alto) para cualquier sr."""
    # Estante: G = +4 dB, fc ≈ 1681,97 Hz, Q ≈ 0,7072 (fórmulas RBJ)
    g, fc, q = 3.999843853973347, 1681.974450955533, 0.7071752369554196
    a_ = 10 ** (g / 40)
    w = 2 * np.pi * fc / sr
    alpha = np.sin(w) / (2 * q)
    cs = np.cos(w)
    b0 = a_ * ((a_ + 1) + (a_ - 1) * cs + 2 * np.sqrt(a_) * alpha)
    b1 = -2 * a_ * ((a_ - 1) + (a_ + 1) * cs)
    b2 = a_ * ((a_ + 1) + (a_ - 1) * cs - 2 * np.sqrt(a_) * alpha)
    a0 = (a_ + 1) - (a_ - 1) * cs + 2 * np.sqrt(a_) * alpha
    a1 = 2 * ((a_ - 1) - (a_ + 1) * cs)
    a2 = (a_ + 1) - (a_ - 1) * cs - 2 * np.sqrt(a_) * alpha
    shelf = _biquad(np.array([b0, b1, b2]) / a0, np.array([1, a1 / a0, a2 / a0]))
    # Paso alto: fc ≈ 38,14 Hz, Q ≈ 0,5003
    fc, q = 38.13547087602444, 0.5003270373238773
    w = 2 * np.pi * fc / sr
    alpha = np.sin(w) / (2 * q)
    cs = np.cos(w)
    b = np.array([(1 + cs) / 2, -(1 + cs), (1 + cs) / 2])
    a = np.array([1 + alpha, -2 * cs, 1 - alpha])
    hp = _biquad(b / a[0], a / a[0])
    return np.vstack([shelf, hp])


def loudness(x: np.ndarray, sr: int) -> float:
    """Sonoridad en LUFS (BS.1770 sin puerta: los ambientes son continuos)."""
    y = signal.sosfilt(_k_weighting(sr), x, axis=0)
    return -0.691 + 10 * np.log10(np.sum(np.mean(y ** 2, axis=0)) + 1e-12)


def limit(x: np.ndarray, ceiling_db: float, sr: int) -> np.ndarray:
    """Limitador de picos sencillo: ganancia por bloques de 5 ms con
    anticipación de un bloque y recuperación suave (~150 ms)."""
    ceiling = 10 ** (ceiling_db / 20)
    block = int(sr * 0.005)
    nb = int(np.ceil(len(x) / block))
    pad = np.zeros((nb * block - len(x), x.shape[1]), x.dtype)
    peaks = np.abs(np.vstack([x, pad])).reshape(nb, block, -1).max(axis=(1, 2))
    need = np.minimum(1.0, ceiling / np.maximum(peaks, 1e-9))
    need = np.minimum(need, np.concatenate([need[1:], [1.0]]))     # anticipación
    need = np.minimum(need, np.concatenate([[1.0], need[:-1]]))
    gain = np.empty_like(need)
    rel = np.exp(-1 / (0.150 / 0.005))
    g = 1.0
    for i, n in enumerate(need):
        g = n if n < g else n + (g - n) * rel
        gain[i] = g
    per_sample = np.interp(np.arange(len(x)), np.arange(nb) * block + block / 2, gain)
    y = x * per_sample[:, None]
    return np.clip(y, -ceiling, ceiling)


def fades(x: np.ndarray, sr: int) -> np.ndarray:
    n = int(sr * FADE_S)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n))
    x[:n] *= ramp[:, None]
    x[-n:] *= ramp[::-1, None]
    return x


def to_target(x: np.ndarray, sr: int, lufs: float) -> np.ndarray:
    x = x * 10 ** ((lufs - loudness(x, sr)) / 20)
    return limit(x, PEAK_CEILING_DB, sr)


def write(name: str, x: np.ndarray) -> Path:
    path = OUT / f"{name}.ogg"
    x = x.astype(np.float32)
    # Por bloques: libsndfile revienta (sin traza) si recibe de golpe
    # muchos segundos para codificar en Vorbis
    with sf.SoundFile(path, 'w', SR, x.shape[1], format='OGG', subtype='VORBIS',
                      compression_level=VORBIS_LEVEL) as f:
        for i in range(0, len(x), 8192):
            f.write(x[i:i + 8192])
    return path


def build_recording(key: str, src: Path, seg, hp_hz: int, lufs: float) -> np.ndarray:
    x, sr = sf.read(src, dtype='float64', always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    x = x[:, :2]
    a, b = int(seg[0] * sr), int(seg[1] * sr)
    margin = int(sr * 0.5)              # margen para que el filtro no deje transitorio
    x = x[max(0, a - margin):min(len(x), b + margin)]
    x = signal.sosfiltfilt(signal.butter(2, hp_hz, 'highpass', fs=sr, output='sos'), x, axis=0)
    if sr != SR:
        g = np.gcd(SR, sr)
        x = signal.resample_poly(x, SR // g, sr // g, axis=0)
    m = int(SR * 0.5) if a >= margin else 0
    x = x[m:m + int((seg[1] - seg[0]) * SR)]
    return fades(to_target(x, SR, lufs), SR)


def build_noise(exponent: float, lufs: float, seconds: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    shape = np.ones_like(freqs)
    nz = freqs > 0
    shape[nz] = freqs[nz] ** (exponent / 2)          # amplitud ∝ f^(exponente/2)
    shape[freqs < 20] = 0                            # sin infrasonidos ni continua
    shape[freqs > 18000] *= np.exp(-(freqs[freqs > 18000] - 18000) / 1500)
    chans = []
    for _ in range(2):
        spec = (rng.standard_normal(len(freqs)) + 1j * rng.standard_normal(len(freqs))) * shape
        chans.append(np.fft.irfft(spec, n))
    x = np.stack(chans, axis=1)
    x /= np.max(np.abs(x))
    return fades(to_target(x, SR, lufs), SR)


CREDITOS_CABECERA = """SONIDOS DE AMBIENTE DE POMOGROWTH
=================================

Generados con scripts/build_ambient_sounds.py, que descarga los originales,
comprueba su SHA-1 y aplica el mismo proceso a todos: tramo indicado,
remuestreo a 44,1 kHz, filtro paso alto contra el retumbe, sonoridad igualada,
limitador de picos, fundidos de 50 ms y recodificación en OGG Vorbis estéreo.

Las grabaciones son de dominio público o están bajo CC0: no exigen
atribución, pero se agradece aquí a sus autores. Los ruidos están
sintetizados por el propio script y se distribuyen con la licencia de la app
(GPL-3.0-or-later).
"""


def credits(durations) -> str:
    lines = [CREDITOS_CABECERA]
    for key, (title, author, lic, page, _url, _sha, seg, hp, lufs) in FUENTES.items():
        mm = lambda s: f"{int(s // 60)}:{int(s % 60):02d}"
        lines += [f"{key}.ogg ({durations[key]:.0f} s)",
                  f"  Original: «{title}»",
                  f"  Autor:    {author}",
                  f"  Licencia: {lic}",
                  f"  Fuente:   {page}",
                  f"  Tramo:    {mm(seg[0])}–{mm(seg[1])}; paso alto {hp} Hz; sonoridad objetivo {lufs:.0f} LUFS",
                  ""]
    for key, (name, exp, lufs, secs) in RUIDOS.items():
        lines += [f"{key}.ogg ({secs:.0f} s)",
                  f"  {name} sintetizado (potencia ∝ f^{exp:g}); sonoridad objetivo {lufs:.0f} LUFS. GPL-3.0-or-later.",
                  ""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument('--cache', type=Path,
                    default=Path(tempfile.gettempdir()) / "pomogrowth_ambient_src",
                    help="carpeta para los originales descargados")
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    durations = {}
    for key, (title, _a, _l, _p, url, sha, seg, hp, lufs) in FUENTES.items():
        print(f"{key}: «{title}»")
        x = build_recording(key, fetch(url, sha, args.cache), seg, hp, lufs)
        path = write(key, x)
        durations[key] = len(x) / SR
        print(f"  -> {path.relative_to(ROOT)}  {durations[key]:.0f} s  "
              f"{loudness(x, SR):.1f} LUFS  {os.path.getsize(path) / 1e6:.2f} MB")
    for i, (key, (name, exp, lufs, secs)) in enumerate(RUIDOS.items()):
        x = build_noise(exp, lufs, secs, seed=1000 + i)
        path = write(key, x)
        print(f"{key}: {name} -> {path.relative_to(ROOT)}  {loudness(x, SR):.1f} LUFS  "
              f"{os.path.getsize(path) / 1e6:.2f} MB")
    (OUT / "CREDITOS.txt").write_text(credits(durations), encoding='utf-8', newline='\n')
    total = sum(p.stat().st_size for p in OUT.glob('*.ogg'))
    print(f"Total: {total / 1e6:.1f} MB en {OUT.relative_to(ROOT)}")


if __name__ == '__main__':
    sys.exit(main())
