"""Unitarios sin red: el demultiplexor ICY (services/icy.py) y la firma del
formato de audio que decide si se empalman dos conexiones
(services/audio_format.py)."""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.audio_format import (FormatProbe, find_signature, normalize_ctype,  # noqa: E402
                                   same_format)
from services.icy import (MAX_META_BLOCK, IcyDemuxer, has_stream_title,  # noqa: E402
                          parse_metaint, parse_stream_title)
from tests.fakes.icecast import MP3_FRAME, adts_frame  # noqa: E402

TITLES = ("Artista - Tema", "Don’t Stop", "Otra canción")


def _wire(audio: bytes, metaint: int, every_empty: int = 3, big: bool = False) -> bytes:
    """El audio como lo manda el servidor: cada metaint bytes, un bloque (uno
    de cada `every_empty` vacío, L = 0; con big, el máximo de 4080 bytes)."""
    out = bytearray()
    k = 0
    for pos in range(0, len(audio), metaint):
        out += audio[pos:pos + metaint]
        if pos + metaint <= len(audio):
            if k % every_empty == every_empty - 1:
                out += b'\x00'
            else:
                meta = f"StreamTitle='{TITLES[k % len(TITLES)]}';".encode()
                meta += bytes((MAX_META_BLOCK if big else -len(meta) % 16 + len(meta))
                              - len(meta))
                out += bytes([len(meta) // 16]) + meta
            k += 1
    return bytes(out)


def _demux_all(wire: bytes, metaint: int, steps):
    demux = IcyDemuxer(metaint)
    audio, titles, positions = bytearray(), [], []
    pos = 0
    rnd = steps if callable(steps) else (lambda: steps)
    while pos < len(wire):
        step = rnd()
        got, blocks = demux.feed(wire[pos:pos + step])
        for at, block in blocks:
            positions.append(len(audio) + at)
            titles.append(parse_stream_title(block))
        audio += got
        pos += step
    return bytes(audio), titles, positions


def test_demux_devuelve_el_audio_exacto_con_cortes_al_azar():
    """Trozos que parten el audio, el byte de longitud o el bloque por
    cualquier sitio: el audio sale idéntico y los títulos, en su sitio."""
    rnd = random.Random(7)
    for _trial in range(150):
        metaint = rnd.choice([1, 7, 16, 8192, 16000])
        audio = rnd.randbytes(rnd.randint(0, 1500 if metaint < 16 else 40000))
        wire = _wire(audio, metaint)
        got, titles, positions = _demux_all(
            wire, metaint, lambda: rnd.choice([1, 2, 3, 100, 4096, 16384, 70000]))
        assert got == audio, metaint
        assert all(t in TITLES for t in titles)
        # Cada bloque va justo tras metaint bytes de audio
        assert all(p % metaint == 0 and p > 0 for p in positions)


def test_demux_byte_a_byte_y_bloque_maximo():
    audio = bytes(range(256)) * 8
    for metaint, big in ((1, False), (13, False), (500, True)):
        wire = _wire(audio, metaint, big=big)
        got, titles, _ = _demux_all(wire, metaint, 1)
        assert got == audio and titles and all(t in TITLES for t in titles)


def test_demux_bloques_vacios_no_dan_titulos():
    audio = bytes(5000)
    wire = _wire(audio, 1000, every_empty=1)         # todos con L = 0
    got, titles, _ = _demux_all(wire, 1000, 333)
    assert got == audio and titles == []


def test_parse_metaint():
    assert parse_metaint(None) is None
    assert parse_metaint(" 16000 ") == 16000
    for bad in ("", "0", "-5", "abc", "16000, 16000", "1e3", "²", str(256 * 1024 + 1)):
        with pytest.raises(ValueError):
            parse_metaint(bad)
    with pytest.raises(ValueError):
        IcyDemuxer(0)


def test_titulos_icy():
    assert parse_stream_title(b"StreamTitle='Guns N' Roses - Patience';StreamUrl='';\0\0") \
        == "Guns N' Roses - Patience"
    assert parse_stream_title(b"StreamTitle='';\0") is None
    assert parse_stream_title(b"\xff\xfe basura") is None
    assert parse_stream_title("StreamTitle='Canción';".encode("utf-8")) == "Canción"
    assert (parse_stream_title(b"StreamTitle='Don\x92t Stop Me Now \x96 Queen \x85 \x93Live\x94';")
            == "Don’t Stop Me Now – Queen … “Live”")
    assert has_stream_title(b"StreamTitle='';") and not has_stream_title(b"StreamUrl='x';")


# ------------------------------------------------------------------ formato
def test_firma_mpeg_y_adts():
    assert find_signature(MP3_FRAME * 3) == ('mpeg', 3, 3, 44100)
    # MPEG-2 capa III a 22,05 kHz, 64 kbps: 72 × 64000 / 22050 = 208 bytes
    mp3_22 = bytes([0xFF, 0xF3, 0x80, 0x04]) + bytes(204)
    assert find_signature(mp3_22 * 3) == ('mpeg', 2, 3, 22050)
    assert find_signature(adts_frame(4) * 3) == ('adts', 1, 4, 2)
    assert find_signature(adts_frame(7, length=120) * 3) == ('adts', 1, 7, 2)
    # Basura antes de la primera trama: se salta
    assert find_signature(b"\xff\x00junk\xff" + MP3_FRAME * 2) == ('mpeg', 3, 3, 44100)
    # Una cabecera suelta sin otra detrás no confirma nada
    assert find_signature(MP3_FRAME[:600]) is None
    assert find_signature(bytes(range(256)) * 8) is None


def test_la_sonda_espera_a_tener_dos_tramas_y_tiene_tope():
    probe = FormatProbe()
    data = adts_frame(4) * 4
    assert probe.feed(data[:100]) is False and probe.feed(data[100:150]) is False
    assert probe.feed(data[150:]) is True and probe.signature == ('adts', 1, 4, 2)
    blind = FormatProbe(limit=4096)
    assert blind.feed(bytes(3000)) is False
    assert blind.feed(bytes(3000)) is True and blind.signature is None


def test_mismo_formato():
    assert normalize_ctype("Audio/MP3; charset=x") == "audio/mpeg"
    assert normalize_ctype("audio/x-aac") == "audio/aac"
    mp3 = ('audio/mpeg', ('mpeg', 3, 3, 44100))
    assert same_format(mp3, mp3)
    assert not same_format(mp3, ('audio/aac', ('adts', 1, 4, 2)))
    assert not same_format(('audio/aac', ('adts', 1, 4, 2)), ('audio/aac', ('adts', 1, 7, 2)))
    # Sin firma en alguna de las dos, manda el Content-Type
    assert same_format(('audio/aac', None), ('audio/aac', ('adts', 1, 7, 2)))
    assert not same_format(('audio/aac', None), ('audio/mpeg', None))
