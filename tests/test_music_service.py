"""MusicService, resolver de enlaces y proxy local contra un servidor local.

Todo sale solo a 127.0.0.1 (un servidor HTTP en un hilo) y el audio va
silenciado: los MP3 son tramas MPEG-1 Layer III a 320 kbps rellenas de
ceros, y el QAudioOutput está en mute. Los datos van a un JSON temporal.
"""
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtMultimedia import QMediaMetaData  # noqa: E402

from core.data_handler import DataHandler  # noqa: E402
from core.music_library import Entry  # noqa: E402
from services import music_resolver, stream_buffer as stream_buffer_mod, workers  # noqa: E402
from services.icy import parse_stream_title  # noqa: E402
from services.music_resolver import ResolveResult, resolve_link  # noqa: E402
from services.music_service import MusicService, _safe_meta  # noqa: E402
from services.stream_buffer import StreamBuffer  # noqa: E402

# 1 trama MPEG-1 Layer III, 320 kbps, 44,1 kHz (1044 bytes, ~26 ms) de silencio
FRAME = bytes([0xFF, 0xFB, 0xE0, 0x04]) + bytes(1040)
SONG = FRAME * 400          # ~10 s
SHORT = FRAME * 45          # ~1,2 s
LONG = FRAME * 1200         # ~31 s
METAINT = 8000
WAIT = 20000                # ms: holgura para FFmpeg en equipos lentos


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _stream(self, seconds=None, metadata=True, quiet_at=None, quiet_s=0.0,
                ctype="audio/mpeg") -> None:
        """Radio en directo a ~40 KB/s (tiempo real a 320 kbps) con ICY.
        quiet_at: a los tantos segundos se calla quiet_s sin cerrar (un hipo)."""
        icy = metadata and self.headers.get("Icy-MetaData") == "1"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("icy-name", "Radio de prueba")
        self.send_header("icy-br", "320")
        if icy:
            self.send_header("icy-metaint", str(METAINT))
        self.end_headers()
        chunk = FRAME * 4
        sent = song = 0
        start = time.monotonic()
        try:
            while not self.server.stopping.is_set():
                if seconds is not None and time.monotonic() - start > seconds:
                    break
                if quiet_at is not None and time.monotonic() - start > quiet_at:
                    quiet_at = None
                    self.server.stopping.wait(quiet_s)
                pos = 0
                while pos < len(chunk):
                    room = METAINT - sent if icy else len(chunk) - pos
                    part = chunk[pos:pos + room]
                    self.wfile.write(part)
                    pos += len(part)
                    sent += len(part)
                    if icy and sent == METAINT:
                        song += 1
                        meta = f"StreamTitle='Artista {song} - Tema {song}';".encode()
                        meta += bytes((-len(meta)) % 16)
                        self.wfile.write(bytes([len(meta) // 16]) + meta)
                        sent = 0
                time.sleep(len(chunk) / 40000)
        except OSError:
            pass

    def _ranged(self, body: bytes) -> None:
        """Archivo con rangos (como nginx o Apache): 206 desde el byte pedido."""
        rng = self.headers.get("Range") or ''
        start = 0
        if rng.startswith("bytes=") and rng[6:].split('-')[0].isdigit():
            start = min(int(rng[6:].split('-')[0]), len(body))
        with self.server.lock:
            self.server.ranges.append(start)
        self.send_response(206 if start else 200)
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        try:
            self.wfile.write(body[start:])
        except OSError:
            pass

    def _chunked(self, body: bytes) -> None:
        """MP3 finito sin Content-Length (HTTP/1.1 por partes), como lo sirve
        un PHP, un Node o un proxy de descargas."""
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        step = len(FRAME) * 8
        try:
            for i in range(0, len(body), step):
                part = body[i:i + step]
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
        self.close_connection = True

    def do_GET(self):  # noqa: N802 - nombre de la API de http.server
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        path = self.path.split("?", 1)[0]
        is_ffmpeg = (self.headers.get("User-Agent") or "").startswith("Lavf")
        if path in ("/song.mp3", "/short1.mp3", "/short2.mp3"):
            self._send(SONG if path == "/song.mp3" else SHORT, "audio/mpeg")
        elif path == "/long.mp3":
            self._ranged(LONG)
        elif path in ("/chunked1.mp3", "/chunked2.mp3"):
            self._chunked(FRAME * 115)          # ~3 s
        elif path == "/slow.mp3":
            time.sleep(1.5)
            self._send(SHORT, "audio/mpeg")
        elif path == "/live":
            self._stream()
        elif path == "/drop":
            # La primera conexión del reproductor se corta a los 3 s; las
            # siguientes emiten sin parar (el monitor ICY no cuenta)
            with self.server.lock:
                if is_ffmpeg:
                    self.server.drop_count += 1
                first = is_ffmpeg and self.server.drop_count == 1
            self._stream(seconds=3.0 if first else None, metadata=False)
        elif path == "/ogg":
            # Radio Ogg que se corta a los 1,5 s en cada conexión
            with self.server.lock:
                self.server.ogg_count = getattr(self.server, 'ogg_count', 0) + 1
            self._stream(seconds=1.5, metadata=False, ctype="audio/ogg")
        elif path == "/silence":
            # Cada conexión: 1,5 s de audio, 3 s callada (sin cerrar) y sigue
            self._stream(metadata=False, quiet_at=1.5, quiet_s=3.0)
        elif path == "/hiccup":
            # Reproductor: la 1.ª conexión se corta a los 2 s (corte → búfer);
            # la 2.ª se calla 3 s a los 2 s (vacía un búfer pequeño); el resto, sin parar
            with self.server.lock:
                if is_ffmpeg:
                    self.server.hiccup_count = getattr(self.server, 'hiccup_count', 0) + 1
                nth = getattr(self.server, 'hiccup_count', 0) if is_ffmpeg else 0
            if nth == 1:
                self._stream(seconds=2.0, metadata=False)
            elif nth == 2:
                self._stream(metadata=False, quiet_at=2.0, quiet_s=3.0)
            else:
                self._stream(metadata=False)
        elif path == "/mute":
            # Las 3 primeras conexiones: audio sin un byte y cierre limpio; las
            # siguientes se quedan calladas sin cerrar (y se cuentan)
            with self.server.lock:
                self.server.mute_count = getattr(self.server, 'mute_count', 0) + 1
                nth = self.server.mute_count
            if nth <= 3:
                self._send(b"", "audio/mpeg")
            else:
                with self.server.lock:
                    self.server.mute_hung = getattr(self.server, 'mute_hung', 0) + 1
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.end_headers()
                self.server.stopping.wait(5)
        elif path == "/list.m3u":
            body = ("#EXTM3U\n#EXTINF:-1,Uno\nshort1.mp3\n"
                    f"#EXTINF:1,Dos\n{base}/short2.mp3\n"
                    "C:\\Users\\yo\\secreto.mp3\nfile:///C:/x.mp3\n\\\\srv\\a.mp3\n")
            self._send(body.encode(), "audio/x-mpegurl")
        elif path == "/radio.pls":
            body = (f"[playlist]\nNumberOfEntries=2\nFile1={base}/live\nTitle1=Radio uno\n"
                    "File2=/song.mp3\nVersion=2\n")
            self._send(body.encode(), "audio/x-scpls")
        elif path == "/hls.m3u8":
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nseg1.ts\n"
            self._send(body.encode(), "application/vnd.apple.mpegurl")
        elif path == "/page.html":
            self._send(b"<!DOCTYPE html><html><body>Hola</body></html>",
                       "text/html; charset=utf-8")
        elif path == "/icy-raw":
            # SHOUTcast v1: línea de estado «ICY 200 OK» en vez de HTTP
            try:
                self.wfile.write(b"ICY 200 OK\r\nicy-name: Vieja\r\n"
                                 b"Content-Type: audio/mpeg\r\n\r\n" + FRAME * 8)
            except OSError:
                pass
        else:
            self._send(b"no", "text/plain", status=404)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.stopping = threading.Event()
    srv.lock = threading.Lock()
    srv.drop_count = 0
    srv.ranges = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.stopping.set()
    srv.shutdown()
    srv.server_close()
    thread.join(5)


@pytest.fixture
def make_service(qtbot, tmp_path):
    """Fábrica de servicios silenciados con datos en tmp; todos se paran al final."""
    created = []

    def make(path=None, **kwargs):
        dh = DataHandler(path or tmp_path / "data.json")
        lofi = tmp_path / "lofi"
        lofi.mkdir(exist_ok=True)
        kwargs.setdefault("reconnect_delays", (200, 400, 600, 800, 1000))
        kwargs.setdefault("buffer_steps", (1, 2))     # precargas cortas: tests rápidos
        svc = MusicService(None, data_handler=dh, lofi_dir=lofi, **kwargs)
        svc.set_muted(True)
        svc.ERROR_SKIP_MS = 300
        created.append(svc)
        return svc

    yield make
    for svc in created:
        svc.stop()
        svc.set_muted(True)
    QThreadPool.globalInstance().waitForDone(15000)
    workers.wait_threads(15)
    qtbot.wait(50)
    for svc in created:
        svc.deleteLater()
    qtbot.wait(50)


def _list_with(svc, *entries, name="Prueba"):
    lid = svc.create_list(name)
    assert svc.add_entries(lid, entries) == len(entries)
    return lid, [i['id'] for i in svc.items(lid)]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ resolver
def test_resolve_mp3_finito(server):
    res = resolve_link(f"{server.base}/song.mp3")
    assert res.error is None and res.list_name is None
    [e] = res.entries
    assert (e.source, e.title, e.live, e.mime) == (f"{server.base}/song.mp3", "song", False,
                                                  "audio/mpeg")


def test_resolve_radio_icy(server):
    [e] = resolve_link(f"{server.base}/live").entries
    assert (e.title, e.live, e.bitrate, e.mime) == ("Radio de prueba", True, 320, "audio/mpeg")


def test_resolve_playlists_remotas(server):
    res = resolve_link(f"{server.base}/list.m3u")
    assert res.error is None and res.list_name == "list"
    assert [(e.source, e.title) for e in res.entries] == [
        (f"{server.base}/short1.mp3", "Uno"), (f"{server.base}/short2.mp3", "Dos")]
    res = resolve_link(f"{server.base}/radio.pls")
    assert [(e.source, e.title) for e in res.entries] == [
        (f"{server.base}/live", "Radio uno"), (f"{server.base}/song.mp3", "")]
    [hls] = resolve_link(f"{server.base}/hls.m3u8").entries
    assert hls.live is True and hls.source == f"{server.base}/hls.m3u8"


def test_resolve_errores(server):
    html = resolve_link(f"{server.base}/page.html")
    assert not html.entries and html.error.startswith("Ese enlace es una página web")
    assert "HTTP 404" in resolve_link(f"{server.base}/nada.mp3").error
    assert "YouTube" in resolve_link("https://www.youtube.com/watch?v=x").error
    secret = f"http://127.0.0.1:{_free_port()}/radio.mp3?token=SECRETO"
    err = resolve_link(secret, timeout=3).error
    assert err.startswith("No se pudo conectar") and "SECRETO" not in err


def test_resolve_shoutcast_v1(server):
    [e] = resolve_link(f"{server.base}/icy-raw", timeout=5).entries
    assert e.live is True


def test_resolve_local(tmp_path):
    folder = tmp_path / "Mi carpeta"
    folder.mkdir()
    for name in ("b.mp3", "A.flac", "nota.txt"):
        (folder / name).write_bytes(FRAME)
    res = resolve_link(str(folder))
    assert res.list_name == "Mi carpeta"
    assert [e.title for e in res.entries] == ["A", "b"]
    (folder / "sub").mkdir()
    (folder / "sub" / "c.ogg").write_bytes(b"x")
    m3u = folder / "lista.m3u"
    m3u.write_text("#EXTM3U\n#EXTINF:3,Tercera\nsub\\c.ogg\nb.mp3\n", encoding="cp1252")
    res = resolve_link(str(m3u))
    assert res.list_name == "lista"
    assert [Path(e.source) for e in res.entries] == [folder / "sub" / "c.ogg", folder / "b.mp3"]
    [single] = resolve_link(str(folder / "b.mp3")).entries
    assert single.live is False and single.mime == "audio/mpeg"
    assert resolve_link(str(folder / "nota.txt")).error
    empty = tmp_path / "vacia"
    empty.mkdir()
    assert resolve_link(str(empty)).error == "Esa carpeta no tiene archivos de audio"


def test_resolve_header_utf8():
    assert music_resolver.fix_header_text("RÃ¡dio") == "Rádio"
    assert music_resolver.parse_bitrate("128,128") == 128
    assert music_resolver.parse_bitrate("abc") == 0


# ------------------------------------------------------------------ ICY
def test_parse_stream_title_robusto():
    assert parse_stream_title(b"StreamTitle='Guns N' Roses - Patience';StreamUrl='';\0\0") \
        == "Guns N' Roses - Patience"
    assert parse_stream_title(b"StreamTitle='';\0") is None
    assert parse_stream_title(b"\xff\xfe basura") is None
    assert parse_stream_title("StreamTitle='Canción';".encode("utf-8")) == "Canción"


# ------------------------------------------------------------------ reproducción
def test_reproduce_mp3_320_por_url(qtbot, server, make_service):
    svc = make_service()
    lid, [iid] = _list_with(svc, Entry(f"{server.base}/song.mp3", live=False, mime="audio/mpeg"))
    states = []
    svc.state_changed.connect(states.append)
    svc.play(lid)
    assert svc.is_playing
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    assert states[0] == 'loading'
    assert _safe_meta(svc._player, QMediaMetaData.AudioBitRate) == 320000
    qtbot.waitUntil(lambda: svc.current()['bitrate_kbps'] == 320, timeout=WAIT)
    cur = svc.current()
    assert (cur['codec'], cur['live'], cur['item_id'], cur['subtitle']) == ('MP3', False, iid,
                                                                           'Pista 1 de 1')
    assert cur['duration_ms'] > 5000 and cur['seekable']
    pos = cur['position_ms']
    qtbot.waitUntil(lambda: svc.current()['position_ms'] > pos + 300, timeout=WAIT)
    assert svc.items(lid)[0]['bitrate'] == 320          # se guarda en el item
    svc.pause()
    assert svc.state == 'paused' and not svc.is_playing
    svc.toggle_pause()
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)


def test_directo_se_detecta_y_muestra_icy(qtbot, server, make_service):
    svc = make_service()
    lid, [iid] = _list_with(svc, Entry(f"{server.base}/live", title="Radio de prueba"))
    assert svc.items(lid)[0]['live'] is None
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    qtbot.waitUntil(lambda: svc.items(lid)[0]['live'] is True, timeout=WAIT)
    pos = svc.current()['position_ms']
    qtbot.waitUntil(lambda: svc.current()['position_ms'] > pos + 500, timeout=WAIT)
    qtbot.waitUntil(lambda: svc.current()['subtitle'].startswith("Artista "), timeout=WAIT)
    cur = svc.current()
    assert cur['live'] and cur['duration_ms'] == 0 and not cur['seekable']
    assert cur['station'] == "Radio de prueba" and cur['bitrate_kbps'] == 320
    qtbot.waitUntil(lambda: svc.current()['codec'] == 'MP3', timeout=WAIT)   # mime del ICY
    # Pausar una radio corta la conexión; al reanudar se reconecta
    svc.pause()
    assert svc._loaded_key is None and svc.state == 'paused'
    svc.resume()
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)


def test_reconecta_tras_un_corte(qtbot, server, make_service):
    with server.lock:
        server.drop_count = 0
    svc = make_service()
    lid, [iid] = _list_with(svc, Entry(f"{server.base}/drop", title="Radio", live=True))
    states = []
    svc.state_changed.connect(states.append)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: 'reconnecting' in states, timeout=WAIT)
    assert svc.is_playing and svc.current()['item_id'] == iid      # sin saltar de item
    qtbot.waitUntil(lambda: 'playing' in states[states.index('reconnecting'):], timeout=WAIT)
    assert server.drop_count >= 2


def test_reconexion_se_rinde_tras_los_intentos(qtbot, server, make_service):
    svc = make_service(reconnect_delays=(50, 50))
    # Un 404 falla al instante (un puerto cerrado tarda ~30 s en Windows)
    url = f"{server.base}/radio-caida"
    lid, [iid] = _list_with(svc, Entry(url, title="Caída", live=True))
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT)
    assert not svc.is_playing and errors and "emisora" in errors[-1]


def test_fin_de_pista_pasa_a_la_siguiente(qtbot, server, make_service):
    svc = make_service()
    lid, (a, b) = _list_with(svc, Entry(f"{server.base}/short1.mp3", live=False),
                             Entry(f"{server.base}/short2.mp3", live=False))
    svc.set_repeat('off')
    svc.play(lid)
    assert svc.current()['item_id'] == a
    qtbot.waitUntil(lambda: svc.current()['item_id'] == b and svc.state == 'playing',
                    timeout=WAIT)
    # repeat 'off': al acabar la última se para
    qtbot.waitUntil(lambda: svc.state == 'idle', timeout=WAIT)
    assert not svc.is_playing and svc.current()['item_id'] == b


def test_error_en_pista_salta_y_sin_bucle(qtbot, server, make_service, tmp_path):
    svc = make_service()
    missing = str(tmp_path / "no-existe.mp3")
    lid, (bad, good) = _list_with(svc, Entry(missing), Entry(f"{server.base}/short1.mp3"))
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid, bad)
    qtbot.waitUntil(lambda: svc.current()['item_id'] == good and svc.state == 'playing',
                    timeout=WAIT)
    assert errors and "no-existe" in errors[0]
    # Todas fallan: se rinde en vez de saltar sin fin
    lid2, _ = _list_with(svc, Entry(str(tmp_path / "x1.mp3")), Entry(str(tmp_path / "x2.mp3")),
                         name="Rota")
    errors.clear()
    svc.play(lid2)
    qtbot.waitUntil(lambda: svc.state == 'error' and not svc.is_playing, timeout=WAIT)
    qtbot.wait(800)
    assert svc.state == 'error' and len(errors) == 2


def test_solo_durante_el_enfoque(qtbot, server, make_service):
    svc = make_service()
    lid, [iid] = _list_with(svc, Entry(f"{server.base}/song.mp3", live=False))
    svc.set_focus_only(True)
    svc.play(lid, iid)                      # armada, pero el timer está parado
    assert svc.is_playing and svc.state == 'paused' and svc.current()['waiting_focus']
    assert svc._loaded_key is None
    svc.on_focus_state(True)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    svc.on_focus_state(False)               # descanso: pausa, sigue armada
    assert svc.state == 'paused' and svc.is_playing
    svc.on_focus_state(True)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    svc.pause()                             # pausa a mano: se desarma
    svc.on_focus_state(False)
    svc.on_focus_state(True)
    qtbot.wait(300)
    assert svc.state == 'paused' and not svc.is_playing
    svc.play()                              # vuelve a armarla
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    svc.set_focus_only(False)
    svc.on_focus_state(False)               # sin focus_only el timer no manda
    assert svc.state == 'playing'


def test_add_link_crea_lista_desde_playlist(qtbot, server, make_service):
    svc = make_service()
    with qtbot.waitSignal(svc.link_resolved, timeout=WAIT) as blocker:
        svc.add_link(f"{server.base}/list.m3u")
    lid, added, error = blocker.args
    assert (added, error) == (2, '')
    assert {'id': lid, 'name': 'list', 'count': 2, 'readonly': False} in svc.lists()
    # Un audio suelto sin lista actual va a «Mis enlaces»
    with qtbot.waitSignal(svc.link_resolved, timeout=WAIT) as blocker:
        svc.add_link(f"{server.base}/live")
    mine, added, error = blocker.args
    assert added == 1 and not error
    assert next(x for x in svc.lists() if x['id'] == mine)['name'] == "Mis enlaces"
    assert svc.items(mine)[0]['title'] == "Radio de prueba"
    # Repetido, YouTube y página web: error y nada añadido
    for link, text in ((f"{server.base}/live", "ya estaba"),
                       ("https://youtu.be/x", "YouTube"),
                       (f"{server.base}/page.html", "página web")):
        with qtbot.waitSignal(svc.link_resolved, timeout=WAIT) as blocker:
            svc.add_link(link, mine)
        assert blocker.args[1] == 0 and text in blocker.args[2]
    with qtbot.waitSignal(svc.link_resolved, timeout=WAIT) as blocker:
        svc.add_link(f"{server.base}/song.mp3", "lofi")
    assert blocker.args[1] == 0 and "solo lectura" in blocker.args[2]


def test_add_paths_y_lofi(qtbot, make_service, tmp_path):
    svc = make_service()
    lofi = svc.lofi_dir
    for name in ("b.mp3", "a.mp3"):
        (lofi / name).write_bytes(FRAME * 45)
    assert svc.rescan() == 2
    assert [p.name for p in svc.tracks] == ["a.mp3", "b.mp3"]
    assert svc.lists()[0] == {'id': 'lofi', 'name': 'Carpeta lofi', 'count': 2, 'readonly': True}
    assert not svc.rename_list('lofi', "x") and not svc.delete_list('lofi')
    assert svc.add_entries('lofi', [Entry("http://h/x.mp3")]) == 0
    svc.play(1)                                     # compat: índice lofi
    assert svc.current_track_name() == "b" and svc.index == 1
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    svc.stop()
    folder = tmp_path / "Álbum"
    folder.mkdir()
    (folder / "uno.mp3").write_bytes(FRAME)
    (tmp_path / "suelto.mp3").write_bytes(FRAME)
    assert svc.add_paths([folder, tmp_path / "suelto.mp3", r"\\srv\x.mp3"]) == 2
    names = {x['name']: x for x in svc.lists()}
    assert names["Álbum"]['count'] == 1 and names["Mis enlaces"]['count'] == 1
    errors = []
    svc.error_occurred.connect(errors.append)
    assert svc.add_paths([r"\\srv\x.mp3"]) == 0 and "red" in errors[-1]


def test_persistencia_de_preferencias(qtbot, make_service, tmp_path):
    path = tmp_path / "prefs.json"
    svc = make_service(path)
    svc.set_volume(0.3)
    svc.set_shuffle(True)
    svc.set_repeat('one')
    svc.set_focus_only(True)
    lid, (a, _b) = _list_with(svc, Entry("http://127.0.0.1:9/a.mp3"), Entry("http://127.0.0.1:9/b.mp3"))
    svc._select(lid, a)
    # La pista actual se guarda con retardo, como el volumen
    qtbot.waitUntil(lambda: DataHandler(path).get_music()['current'] == {'list': lid, 'item': a},
                    timeout=3000)
    music = DataHandler(path).get_music()
    assert music['volume'] == 0.3
    assert (music['shuffle'], music['repeat'], music['focus_only']) == (True, 'one', True)
    assert music['current'] == {'list': lid, 'item': a}
    again = make_service(path)
    assert (again.volume(), again.shuffle(), again.repeat(), again.focus_only()) == (
        0.3, True, 'one', True)
    assert again.current()['item_id'] == a and again.state == 'idle'   # sin autoplay


def test_biblioteca_sigue_a_un_import(make_service, tmp_path):
    path = tmp_path / "imp.json"
    svc = make_service(path)
    svc.create_list("Antes")
    svc._dh.import_data({'tasks': [], 'settings': {}, 'music': {
        'lists': [{'id': 'nueva', 'name': 'Importada', 'items': []}], 'volume': 0.8}})
    assert [x['name'] for x in svc.lists()[1:]] == ["Importada"] and svc.volume() == 0.8


def test_quitar_o_borrar_lo_que_suena(qtbot, server, make_service):
    svc = make_service()
    lid, (a, b) = _list_with(svc, Entry(f"{server.base}/song.mp3", live=False),
                             Entry(f"{server.base}/short1.mp3", live=False))
    svc.play(lid, a)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    assert svc.remove_item(lid, a)              # sonaba: pasa a la siguiente
    assert svc.current()['item_id'] == b and svc.is_playing
    assert svc.delete_list(lid)                 # borra la lista que suena: se para
    assert svc.current() is None and not svc.is_playing and svc.state == 'idle'
    assert [x['id'] for x in svc.lists()] == ['lofi']


def test_controller_avisa_del_enfoque(qtbot, tmp_path, monkeypatch):
    """El timer real manda: arrancar/pausar/reiniciar llegan a on_focus_state."""
    from tests.test_ui_smoke import ID1, _make_data
    from ui.controllers.app_controller import AppController
    from ui.theme import fonts
    from ui.windows.main_window import MainWindow
    monkeypatch.chdir(tmp_path)
    fonts.init_fonts()
    dh = DataHandler(_make_data(tmp_path))
    win = MainWindow(dh.data.get('settings', {}))
    qtbot.addWidget(win)
    ctrl = AppController(win, dh)
    ctrl.music.set_muted(True)
    assert ctrl.music._dh is dh
    calls = []
    monkeypatch.setattr(ctrl.music, 'on_focus_state', calls.append)
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    ctrl.toggle_timer()
    ctrl.toggle_timer()
    ctrl.reset_timer()
    assert calls == [True, False, True, False]
    ctrl.music.stop()


def test_shuffle_y_repeat_en_siguiente(make_service):
    svc = make_service()
    entries = [Entry(f"http://127.0.0.1:9/{n}.mp3") for n in range(6)]
    lid, ids = _list_with(svc, *entries)
    svc._select(lid, ids[0])
    assert svc._neighbor(+1, auto=False) == ids[1]
    svc._select(lid, ids[-1])
    assert svc._neighbor(+1, auto=False) == ids[0]           # manual: da la vuelta
    svc.set_repeat('off')
    assert svc._neighbor(+1, auto=True) is None              # fin natural: para
    svc.set_repeat('one')
    assert svc._neighbor(+1, auto=True) == ids[-1]
    assert svc._neighbor(+1, auto=True, from_error=True) == ids[0]
    svc.set_repeat('all')
    svc.set_shuffle(True)
    seen = {svc._cur_item}
    for _ in range(5):
        nxt = svc._neighbor(+1, auto=True)
        svc._select(lid, nxt)
        seen.add(nxt)
    assert seen == set(ids)                                  # una vuelta sin repetir


# ------------------------------------------------------------------ revisión: red y plazos
class _Drip:
    """Servidor TCP crudo en 127.0.0.1 que gotea un byte cada `every` s (menos
    que el timeout por lectura de requests) durante `lasting` s:
    /hdr  cabeceras que nunca terminan · /body.m3u  una lista que no acaba ·
    /icy  una radio con icy-metaint enorme (el bloque de metadatos no llega)."""

    def __init__(self, every: float = 0.3, lasting: float = 30):
        self.every, self.lasting = every, lasting
        self.stopping = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(20)
        self.base = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            path = conn.recv(4096).split(b" ", 2)[1]
            if path == b"/body.m3u":
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: audio/x-mpegurl\r\n\r\n#EXTM3U\n")
            elif path == b"/icy":
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n"
                             b"icy-name: Goteo\r\nicy-metaint: 65536\r\n\r\n")
            else:
                conn.sendall(b"HTTP/1.0 200 OK\r\nX-Goteo: ")
            end = time.monotonic() + self.lasting
            while time.monotonic() < end and not self.stopping.is_set():
                conn.sendall(b"a")
                time.sleep(self.every)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stopping.set()
        self.sock.close()


@pytest.fixture
def drip():
    srv = _Drip()
    yield srv
    srv.close()


def test_el_resolver_cumple_su_plazo_total_aunque_el_servidor_gotee(drip):
    t0 = time.monotonic()
    res = resolve_link(f"{drip.base}/hdr", timeout=1.5)      # plazo total: 3 s
    assert res.error and time.monotonic() - t0 < 6
    t0 = time.monotonic()
    res = resolve_link(f"{drip.base}/body.m3u", timeout=1.5)
    assert res.error and time.monotonic() - t0 < 6


def test_un_plazo_cortado_desde_otro_hilo(drip):
    deadline = music_resolver.Deadline(30)
    threading.Timer(0.5, deadline.abort).start()
    t0 = time.monotonic()
    res = resolve_link(f"{drip.base}/hdr", timeout=5, deadline=deadline)
    assert res.error and time.monotonic() - t0 < 4
    assert resolve_link(f"{drip.base}/hdr", deadline=deadline).error   # ya cortado: ni lo intenta


def test_los_enlaces_no_ocupan_el_pool_global(qtbot, server, make_service):
    """Asana y el resto usan el QThreadPool global: la música va en hilos propios."""
    svc = make_service()
    with qtbot.waitSignal(svc.link_resolved, timeout=WAIT) as blocker:
        svc.add_link(f"{server.base}/slow.mp3")
        assert QThreadPool.globalInstance().activeThreadCount() == 0
        assert len(svc._link_deadlines) == 1
    assert blocker.args[1] == 1 and not svc._link_deadlines


def test_cerrar_con_un_enlace_en_curso_no_retiene_el_proceso(tmp_path, drip):
    """Antes el QThreadPool global esperaba al hilo al salir: el proceso (y el
    puerto de instancia única) seguía vivo mientras el servidor goteara."""
    child = tmp_path / "hijo.py"
    child.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(V9)!r})
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        from services.music_service import MusicService
        svc = MusicService(None, lofi_dir={str(tmp_path / 'lofi')!r})
        svc.set_muted(True)
        svc.add_link({drip.base + '/hdr'!r})
        # Una radio que gotea leyéndose por el proxy local (su lector, en un hilo)
        import requests, threading
        url = svc._stream_buffer.open({drip.base + '/icy'!r}, 0)
        threading.Thread(target=lambda: requests.get(url, timeout=30), daemon=True).start()
        QTimer.singleShot(400, lambda: (svc.shutdown(), app.quit()))
        app.exec()
        print("FIN", flush=True)
    """), encoding="utf-8")
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, str(child)], capture_output=True, text=True,
                          timeout=60, cwd=str(tmp_path))
    assert "FIN" in proc.stdout
    assert time.monotonic() - t0 < 15        # el servidor gotea durante 30 s


# ------------------------------------------------------------------ revisión: HLS local
HLS_HEAD = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"


def test_hls_local_que_apunta_fuera_de_su_carpeta_se_rechaza(tmp_path):
    folder = tmp_path / "hls"
    folder.mkdir()
    cases = {
        "unc.m3u": "#EXTINF:7,\n\\\\127.0.0.1\\nada\\seg.mp3\n",
        "dispositivo.m3u8": "#EXTINF:7,\n\\\\?\\C:\\secreto.mp3\n",
        "absoluta.m3u8": "#EXTINF:7,\nC:\\Windows\\media\\tada.wav\n",
        "arriba.m3u8": "#EXTINF:7,\n..\\..\\otra\\seg.mp3\n",
        "clave.m3u8": '#EXT-X-KEY:METHOD=AES-128,URI="\\\\srv\\k.key"\n#EXTINF:7,\nseg.ts\n',
        "file.m3u8": "#EXT-X-MAP:URI=file://srv/x.mp4\n#EXTINF:7,\nseg.ts\n",
    }
    for name, body in cases.items():
        (folder / name).write_text(HLS_HEAD + body, encoding="utf-8")
        assert resolve_link(str(folder / name)).error == music_resolver.MSG_HLS_UNSAFE, name
    ok = folder / "buena.m3u8"
    ok.write_text(HLS_HEAD + "#EXTINF:7,\nseg1.ts\n#EXTINF:7,\nsub/seg2.ts\n"
                  "#EXTINF:7,\nhttps://cdn.ejemplo.com/seg3.ts\n", encoding="utf-8")
    [entry] = resolve_link(str(ok)).entries
    assert entry.live is True and entry.source == str(ok)


def test_hls_local_importado_no_llega_a_ffmpeg(qtbot, make_service, tmp_path):
    """Un item local .m3u8 (de datos importados, o editado después) se revisa
    justo antes de sonar."""
    bad = tmp_path / "importada.m3u8"
    bad.write_text(HLS_HEAD + "#EXTINF:7,\n\\\\127.0.0.1\\nada\\seg.mp3\n", encoding="utf-8")
    svc = make_service()
    lid, [iid] = _list_with(svc, Entry(str(bad), live=True))
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT)
    assert errors and "HLS" in errors[-1] and svc._player.source().isEmpty()


# ------------------------------------------------------------------ revisión: reproducción
def test_mp3_sin_content_length_es_una_pista_y_pasa_a_la_siguiente(qtbot, server, make_service):
    [entry] = resolve_link(f"{server.base}/chunked1.mp3").entries
    assert entry.live is None                   # ni icy-* ni tamaño: aún no se sabe
    svc = make_service()
    lid, (a, b) = _list_with(svc, Entry(f"{server.base}/chunked1.mp3"),
                             Entry(f"{server.base}/chunked2.mp3"))
    svc.set_repeat('off')
    states = []
    svc.state_changed.connect(states.append)
    svc.play(lid)
    assert svc.current()['item_id'] == a
    qtbot.waitUntil(lambda: svc.current()['item_id'] == b, timeout=WAIT)
    assert 'reconnecting' not in states
    assert svc.items(lid)[0]['live'] is False   # se aprende: era una pista
    qtbot.waitUntil(lambda: svc.state == 'idle', timeout=WAIT)   # repeat 'off': para
    assert 'reconnecting' not in states and not svc.is_playing


def test_pista_sin_content_length_que_se_corta_no_va_por_el_bufer(qtbot, server, make_service):
    """Durar 0 no confirma un directo: la pista servida por partes que se
    corta una vez reconecta sin precarga (con búfer, el proxy la empalmaría)
    y, al acabar, pasa a la siguiente. Ahora toda conexión de un enlace que
    puede ser un directo pasa por el proxy: se comprueba que ninguna lleve
    precarga ni se dé por directo confirmado."""
    svc = make_service()
    lid, (a, b) = _list_with(svc, Entry(f"{server.base}/chunked1.mp3"),
                             Entry(f"{server.base}/chunked2.mp3"))
    svc.set_repeat('off')
    opened = []
    real_open = svc._stream_buffer.open
    svc._stream_buffer.open = lambda *args, **kw: opened.append((args, kw)) or real_open(*args,
                                                                                         **kw)
    states = []
    svc.state_changed.connect(states.append)
    svc.play(lid)
    # A los 1,5 s sin duración se trata como directo; ahí se corta una vez
    qtbot.waitUntil(lambda: svc._live_runtime is True, timeout=WAIT)
    svc._handle_failure("La reproducción se quedó atascada")
    qtbot.waitUntil(lambda: 'reconnecting' in states, timeout=WAIT)
    qtbot.waitUntil(lambda: svc.current()['item_id'] == b, timeout=WAIT)
    assert len(opened) >= 2 and svc._buffer_level == 0
    assert all(args[1] == 0 and kw['known_live'] is False for args, kw in opened)
    assert svc.items(lid)[0]['live'] is False   # se aprende: era una pista


def test_bufer_no_repite_sin_fin_un_archivo_que_se_acaba(qtbot, server, stream_buffer):
    """Un origen que da audio y cierra limpio enseguida una y otra vez es un
    archivo: tras unos pocos empalmes se vacía y el reproductor recibe el fin.
    (Antes lo cortaba MAX_QUICK_ENDS; ahora, el tope general del lector: cada
    conexión sin STABLE_AUDIO_S de audio cuenta como un reintento.)"""
    track = len(FRAME) * 115
    url = stream_buffer.open(f"{server.base}/chunked1.mp3", 0.5)
    sock, status, _headers, body, _waited = _proxy_get(url)
    with sock:
        assert status == 200
        data, closed = _read_for(sock, 12)
        audio, _final = _dechunk(body + data)
        tries = 1 + len(stream_buffer.RETRY_DELAYS_S)
        assert closed and track <= len(audio) <= tries * track


def test_pausa_de_un_enlace_desconecta_y_sigue_donde_iba(qtbot, server, make_service):
    """En pausa (o en un descanso con «solo durante el enfoque») la pista por
    enlace no deja una conexión parada que el servidor cortaría: al reanudar
    se recarga y salta con un Range a donde iba."""
    svc = make_service()
    lid, [iid] = _list_with(svc, Entry(f"{server.base}/long.mp3", live=False,
                                       mime="audio/mpeg"))
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid)
    qtbot.waitUntil(lambda: svc.state == 'playing' and svc.current()['position_ms'] > 2500,
                    timeout=WAIT)
    svc.pause()
    cur = svc.current()
    pos = cur['position_ms']
    assert svc._loaded_key is None and svc.state == 'paused'      # sin conexión
    assert pos > 2500 and cur['duration_ms'] > 25000 and cur['seekable']
    svc.seek(9000)                               # en pausa: se apunta para luego
    assert svc.current()['position_ms'] == 9000
    with server.lock:
        before = len(server.ranges)
    positions = []
    svc._player.positionChanged.connect(positions.append)
    svc.resume()
    qtbot.waitUntil(lambda: svc.state == 'playing' and svc._player.position() > 9000,
                    timeout=WAIT)
    assert not any(0 < p < 8500 for p in positions), positions[:5]   # no empezó de cero
    with server.lock:
        assert any(start > 0 for start in server.ranges[before:])
    assert not errors and svc.current()['item_id'] == iid


def test_siguiente_y_anterior_con_un_solo_elemento(qtbot, server, make_service):
    svc = make_service()
    lid, [_radio] = _list_with(svc, Entry(f"{server.base}/live", title="Radio", live=True))
    svc.play(lid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    states = []
    svc.state_changed.connect(states.append)
    key = svc._loaded_key
    svc.next()
    svc.prev()
    qtbot.wait(300)
    assert states == [] and svc._loaded_key == key          # la radio no se corta
    svc.stop()
    lid2, [_song] = _list_with(svc, Entry(f"{server.base}/song.mp3", live=False), name="Una")
    svc.play(lid2)
    qtbot.waitUntil(lambda: svc.state == 'playing' and svc._player.position() > 1500,
                    timeout=WAIT)
    states.clear()
    svc.next()                                  # una pista: vuelve al principio
    qtbot.waitUntil(lambda: svc._player.position() < 1000, timeout=WAIT)
    assert 'loading' not in states


def test_el_motivo_del_error_lo_guarda_el_servicio(qtbot, make_service, tmp_path):
    svc = make_service()
    lid, _ = _list_with(svc, Entry(str(tmp_path / "falta1.mp3")),
                        Entry(str(tmp_path / "falta2.mp3")))
    seen = []
    svc.error_occurred.connect(lambda _m: seen.append(svc.current()['error']))
    svc.play(lid)
    qtbot.waitUntil(lambda: len(seen) >= 2, timeout=WAIT)
    # Sin el título (ya se ve encima) y con lo que pasa después
    assert seen[0] == "No se encuentra el archivo: paso a la siguiente"
    assert seen[-1] == svc.current()['error'] == "No se pudo reproducir ninguna pista de la lista"
    lid1, _ = _list_with(svc, Entry(str(tmp_path / "falta3.mp3")), name="Una")
    svc.play(lid1)
    qtbot.waitUntil(lambda: svc.current()['error'] == "No se encuentra el archivo",
                    timeout=WAIT)


def test_cambiar_de_pista_no_reescribe_el_json_cada_vez(qtbot, make_service, tmp_path):
    path = tmp_path / "saves.json"
    svc = make_service(path)
    lid, ids = _list_with(svc, *(Entry(f"http://127.0.0.1:9/{n}.mp3") for n in range(5)))
    saves = []
    real = svc._dh.save_music
    svc._dh.save_music = lambda music: (saves.append(1), real(music))[1]
    for iid in ids:
        svc._select(lid, iid)
    assert saves == []                            # con retardo, como el volumen
    qtbot.waitUntil(lambda: len(saves) == 1, timeout=3000)
    assert DataHandler(path).get_music()['current'] == {'list': lid, 'item': ids[-1]}


def test_textos_icy_en_windows_1252():
    assert (parse_stream_title(b"StreamTitle='Don\x92t Stop Me Now \x96 Queen \x85 \x93Live\x94';")
            == "Don\u2019t Stop Me Now \u2013 Queen \u2026 \u201cLive\u201d")
    assert music_resolver.fix_header_text("Radio Caf\xe9 \x9280s") == "Radio Caf\xe9 \u201980s"


def test_lista_llena_por_texto_avisa(make_service):
    svc = make_service()
    lid = svc.create_list("Grande")
    long_url = "https://ejemplo.com/" + "a" * 1900 + "/{}.mp3"
    svc.add_entries(lid, [Entry(long_url.format(n)) for n in range(600)])
    added, _target, error = svc._add_resolved(
        ResolveResult(entries=[Entry(long_url.format(999))]), lid)
    assert added == 0 and error == "La lista está llena: crea otra para añadir más"


# ------------------------------------------------------------------ proxy local y búfer adaptativo
def _proxy_get(url: str, ua: str = "Lavf/61.7.100", timeout: float = 15):
    """GET al proxy como lo hace FFmpeg (pide metadatos ICY) → (socket,
    estado, cabeceras en minúsculas, lo que ya llegó del cuerpo, segundos
    hasta la primera respuesta). El cuerpo llega por partes: _dechunk."""
    parts = urlsplit(url)
    sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
    t0 = time.monotonic()
    sock.sendall(f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUser-Agent: {ua}\r\n"
                 f"Icy-MetaData: 1\r\nRange: bytes=0-\r\n\r\n".encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    waited = time.monotonic() - t0
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.strip().lower(): v.strip() for k, _, v in
               (line.partition(":") for line in lines[1:])}
    return sock, status, headers, body, waited


def _read_for(sock, seconds: float):
    """Lee durante `seconds` → (bytes, True si el proxy cerró la conexión)."""
    end = time.monotonic() + seconds
    data = b""
    sock.settimeout(0.5)
    while time.monotonic() < end:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            return data, True
        if not chunk:
            return data, True
        data += chunk
    return data, False


def _dechunk(raw: bytes):
    """Cuerpo por partes → (los datos de las partes completas, ¿llegó la
    parte final vacía?: el fin limpio del audio, no un corte)."""
    out, pos = bytearray(), 0
    while True:
        end = raw.find(b"\r\n", pos)
        if end < 0:
            return bytes(out), False
        size = int(raw[pos:end].split(b";")[0], 16)
        if size == 0:
            return bytes(out), True
        if end + 2 + size > len(raw):
            return bytes(out), False
        out += raw[end + 2:end + 2 + size]
        pos = end + 2 + size + 2


@pytest.fixture
def stream_buffer(qtbot):
    buf = StreamBuffer()
    buf.RETRY_DELAYS_S = (0.3, 0.3, 0.3)     # esperas cortas: tests rápidos
    yield buf
    buf.shutdown()


def test_bufer_precarga_por_tiempo_y_sin_metadatos(qtbot, server, stream_buffer):
    url = stream_buffer.open(f"{server.base}/live", 1.0)
    assert url.startswith(f"http://127.0.0.1:{stream_buffer.port}/")
    sock, status, headers, body, waited = _proxy_get(url)
    with sock:
        # Nada (ni las cabeceras) hasta tener 1 s de audio, y luego de golpe
        assert status == 200 and waited >= 1.0
        assert headers['content-type'] == "audio/mpeg" and headers['icy-name'] == "Radio de prueba"
        # El proxy pide los metadatos (los títulos salen de su conexión) y
        # los quita del audio: al reproductor no le dice que haya
        assert 'icy-metaint' not in headers
        more, closed = _read_for(sock, 0.4)
        data, _final = _dechunk(body + more)
        assert not closed and len(data) >= 25000
        assert data.startswith(FRAME[:4]) and b"StreamTitle" not in data
    # Solo la ruta con el token de la sesión en curso
    other = url.rsplit("/", 1)[0] + "/otro-token"
    assert requests.get(other, timeout=5).status_code == 404
    stream_buffer.close()
    assert requests.get(url, timeout=5).status_code == 404
    assert all(t.daemon for t in threading.enumerate() if t.name.startswith("pomogrowth-bufer"))


def test_bufer_reconecta_el_origen_sin_cortar_al_reproductor(qtbot, server, stream_buffer):
    with server.lock:
        server.drop_count = 0
    url = stream_buffer.open(f"{server.base}/drop", 1.0)
    sock, status, _headers, _body, _waited = _proxy_get(url)   # User-Agent de FFmpeg
    with sock:
        assert status == 200
        data, closed = _read_for(sock, 4.5)       # el origen se corta a los 3 s
        assert not closed and len(data) > 100000
    with server.lock:
        assert server.drop_count >= 2             # reconectó con el User-Agent del reproductor


def test_bufer_aguanta_un_silencio_corto_y_cierra_si_el_origen_no_vuelve(qtbot, server,
                                                                          stream_buffer):
    """Un silencio más corto que DEAD_AIR_S no corta (antes, con 0,5 s de
    búfer, el proxy cerraba al reproductor a los 0,5 s y reconectaba). Si el
    origen no vuelve y el lector agota sus reintentos, se cierra."""
    stream_buffer.DEAD_AIR_S = 5.0
    url = stream_buffer.open(f"{server.base}/silence", 0.5)
    sock, status, _headers, body, _waited = _proxy_get(url)
    with sock:
        assert status == 200
        # 1,5 s de audio, 3 s callado (sin cerrar) y sigue: misma conexión
        data, closed = _read_for(sock, 6.5)
        assert not closed and len(_dechunk(body + data)[0]) > 40000 * 3
    stream_buffer.DEAD_AIR_S = 1.0
    stream_buffer.RETRY_DELAYS_S = ()
    url = stream_buffer.open(f"{server.base}/silence", 0.5)
    sock, status, _headers, _body, _waited = _proxy_get(url)
    with sock:
        assert status == 200
        _data, closed = _read_for(sock, 6)
        assert closed


def test_bufer_no_empalma_ogg(qtbot, server, stream_buffer):
    """Ogg trae cabeceras propias al empezar: si el origen se corta, se sirve
    lo que quedaba y se cierra (reconecta MusicService), sin empalmar."""
    with server.lock:
        server.ogg_count = 0
    url = stream_buffer.open(f"{server.base}/ogg", 0.5)
    sock, status, headers, body, _waited = _proxy_get(url)
    with sock:
        assert status == 200 and headers['content-type'] == "audio/ogg"
        data, closed = _read_for(sock, 6)
        assert closed and len(_dechunk(body + data)[0]) > 40000   # ~1,5 s servidos enteros
    with server.lock:
        assert server.ogg_count == 1


def test_bufer_redirige_lo_que_no_es_audio(qtbot, server, stream_buffer):
    seen = []
    stream_buffer.bypassed.connect(seen.append)
    url = stream_buffer.open(f"{server.base}/hls.m3u8", 1.0)
    resp = requests.get(url, allow_redirects=False, timeout=WAIT / 1000)
    assert resp.status_code == 302 and resp.headers['Location'] == f"{server.base}/hls.m3u8"
    qtbot.waitUntil(lambda: seen == [stream_buffer.session_id], timeout=WAIT)


def test_una_primera_conexion_sin_audio_contesta_502_sin_abrir_otra(qtbot, server, stream_buffer,
                                                                     monkeypatch):
    """La 1.ª conexión no da audio (vacía, o nada en el plazo de la precarga):
    502 y ni una conexión más. Antes reintentaba y acababa en un 302, con el
    que FFmpeg repetía la petición: dos conexiones por intento."""
    monkeypatch.setattr(stream_buffer_mod, 'PREFILL_GRACE_S', 1.0)
    with server.lock:
        server.mute_count = server.mute_hung = 0
    bypassed = []
    stream_buffer.bypassed.connect(bypassed.append)
    url = stream_buffer.open(f"{server.base}/mute", 0.5)     # 200 sin un byte de audio
    sid = stream_buffer.session_id
    resp = requests.get(url, allow_redirects=False, timeout=WAIT / 1000)
    assert resp.status_code == 502 and 'Location' not in resp.headers
    assert stream_buffer.last_failure == (sid, 'network')
    with server.lock:
        server.mute_count = 3                                # la siguiente se queda callada
    url = stream_buffer.open(f"{server.base}/mute", 0.5)
    t0 = time.monotonic()
    resp = requests.get(url, allow_redirects=False, timeout=WAIT / 1000)
    assert resp.status_code == 502 and time.monotonic() - t0 < 4
    qtbot.wait(800)
    with server.lock:
        assert server.mute_count == 4 and server.mute_hung == 1
    assert bypassed == []


def test_tras_el_bypass_la_conexion_directa_tiene_su_plazo_entero(qtbot, make_service):
    svc = make_service()
    lid, [radio] = _list_with(svc, Entry("http://127.0.0.1:9/radio", live=True))
    svc._select(lid, radio)
    svc._buffer_level = 1
    assert svc._buffered_url(svc._current_item(), reconnect=True) is not None
    svc._set_state('buffering')
    svc._load_started = svc._last_progress = time.monotonic() - 25
    before = time.monotonic()
    svc._on_buffer_bypassed(svc._stream_buffer.session_id)
    assert svc._buffer_s == 0 and svc.state == 'reconnecting' and not svc._proxied
    assert svc._load_started >= before and svc._last_progress >= before
    svc._stream_buffer.close()


def test_la_primera_va_por_el_proxy_sin_precarga_y_tras_un_corte_con_bufer(qtbot, server,
                                                                          make_service):
    """Antes la 1.ª conexión iba directa; ahora va por el proxy (una sola
    conexión con la emisora y los títulos en ella) pero SIN precarga: la misma
    carga rápida. Un corte sube el escalón y la reconexión lleva precarga."""
    with server.lock:
        server.drop_count = 0
    svc = make_service(buffer_steps=(0.5, 1))
    source = f"{server.base}/drop"
    lid, [iid] = _list_with(svc, Entry(source, title="Radio", live=True))
    states = []
    svc.state_changed.connect(states.append)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    loaded = svc._player.source()
    assert loaded.host() == "127.0.0.1" and loaded.port() == svc._stream_buffer.port
    assert svc.current()['buffer_s'] == 0 and states[0] == 'loading'
    qtbot.waitUntil(lambda: 'reconnecting' in states, timeout=WAIT)
    qtbot.waitUntil(lambda: 'playing' in states[states.index('reconnecting'):], timeout=WAIT)
    after = states[states.index('reconnecting'):]
    assert after.index('buffering') < after.index('playing')     # precarga y luego suena
    loaded = svc._player.source()
    assert loaded.host() == "127.0.0.1" and loaded.port() == svc._stream_buffer.port
    assert loaded.port() != server.server_address[1]
    assert svc.current()['buffer_s'] == 0.5 and svc._buffer_level == 1
    with server.lock:
        assert server.drop_count >= 2
    # Pausar y reanudar a mano: sin precarga (rápido), pero se guarda el escalón
    svc.pause()
    assert svc.current()['buffer_s'] == 0
    svc.resume()
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    assert svc._player.source().port() == svc._stream_buffer.port
    assert svc.current()['buffer_s'] == 0 and svc._buffer_level == 1
    # Otra emisora: vuelve a 0
    lid2, [other] = _list_with(svc, Entry(f"{server.base}/live", live=True), name="Otra")
    svc.play(lid2, other)
    assert svc._buffer_level == 0 and svc.current()['buffer_s'] == 0
    assert svc._player.source().port() == svc._stream_buffer.port


def test_si_el_lector_se_rinde_sube_un_escalon(qtbot, server, make_service):
    """Cada sesión que acaba en un corte sube un escalón. Un silencio ya no
    vacía el búfer y corta él solo: es el lector que da la conexión por muerta
    (DEAD_AIR_S) y se rinde (aquí sin reintentos) el que acaba la sesión."""
    with server.lock:
        server.hiccup_count = 0
    svc = make_service(buffer_steps=(0.5, 1))
    svc._stream_buffer.DEAD_AIR_S = 1.0
    svc._stream_buffer.RETRY_DELAYS_S = ()
    lid, [iid] = _list_with(svc, Entry(f"{server.base}/hiccup", title="Radio", live=True))
    levels = []
    svc.state_changed.connect(lambda _s: levels.append(svc._buffer_level))
    svc.play(lid, iid)
    # Corte → 0,5 s; el origen calla 3 s → conexión muerta → 1 s (tope)
    qtbot.waitUntil(lambda: svc._buffer_level == 2 and svc.state == 'playing',
                    timeout=WAIT * 2)
    assert 1 in levels and svc.current()['buffer_s'] == 1
    assert svc.is_playing and svc.current()['item_id'] == iid


def test_el_bufer_solo_para_directos_por_enlace(qtbot, make_service, tmp_path):
    svc = make_service()
    local = tmp_path / "radio.mp3"
    local.write_bytes(FRAME)
    lid, (radio, hls, track, loc) = _list_with(
        svc, Entry("http://127.0.0.1:9/radio", live=True),
        Entry("http://127.0.0.1:9/lista.m3u8", live=True),
        Entry("http://127.0.0.1:9/tema.mp3", live=False), Entry(str(local)))
    for iid, expected in ((radio, True), (hls, False), (track, False), (loc, False)):
        svc._select(lid, iid)
        assert svc._buffer_eligible(svc._current_item()) is expected
        # HLS, una pista conocida o un archivo local: directos, sin proxy
        assert svc._proxy_eligible(svc._current_item()) is expected
    svc._select(lid, radio)
    item = svc._current_item()
    default = svc._player.playbackOptions().networkTimeout()
    svc._buffer_level = 1
    url = svc._buffered_url(item, reconnect=False)               # reanudar: sin precarga
    assert url.port() == svc._stream_buffer.port and svc.current()['buffer_s'] == 0
    url = svc._buffered_url(item, reconnect=True)
    assert url.port() == svc._stream_buffer.port and svc.current()['buffer_s'] == 1
    # FFmpeg espera a la precarga sin dar la conexión por muerta
    assert svc._player.playbackOptions().networkTimeout() > default + 1000
    # Falla por el proxy sin llegar a sonar por un error de la emisora: el
    # siguiente intento sigue por el proxy (ir directo diría lo mismo)
    svc._stream_buffer.last_failure = (svc._proxy_sid, 'http')
    svc._bump_buffer()
    assert svc._buffered_url(item, reconnect=True) is not None
    # Por un fallo de red: el siguiente va directo, por si el proxy es el problema
    svc._stream_buffer.last_failure = (svc._proxy_sid, 'network')
    svc._bump_buffer()
    assert svc._buffer_level == 1 and svc._buffered_url(item, reconnect=True) is None
    assert svc._buffered_url(item, reconnect=True) is not None
    svc._unload()
    assert svc.current()['buffer_s'] == 0 and svc._stream_buffer.session_id == 0
    # Si el proxy no puede abrir, se conecta directo
    svc._stream_buffer.open = lambda *_a, **_kw: None
    assert svc._buffered_url(item, reconnect=True) is None and svc._buffer_s == 0
    assert svc._player.playbackOptions().networkTimeout() == default
