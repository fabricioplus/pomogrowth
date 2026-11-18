"""Radios de pago tipo DI.FM («one stream at a time»): UNA sola conexión con
la emisora en todo momento, y no insistir cuando la emisora la rechaza.

Contra una emisora Icecast falsa en 127.0.0.1 (tests/fakes/icecast.py: cuenta
las conexiones abiertas a la vez y registra User-Agent e Icy-MetaData de
cada una). Primero el proxy (StreamBuffer) solo, con un «reproductor» que lee
por un socket como FFmpeg; luego MusicService entero (FFmpeg de verdad, en
silencio). Los tiempos del proxy y de MusicService van acortados; las
pruebas de los valores por defecto están al final.

Invariantes (las mismas del docstring de services/stream_buffer.py):
I1 nunca dos conexiones a la vez · I2 los títulos salen de la misma conexión
· I3 todo enlace que puede ser un directo pasa por el proxy desde la 1.ª
conexión (una pista, directa por 302 tras cerrar) · I4 un rechazo, una sola
petición y parar · I5 otros errores, 502 (sin 302) · I6 reconexiones
espaciadas y con tope · I7 sin empalmar formatos distintos · I8 pausa,
parar, cambiar y cerrar dejan 0 conexiones y un solo User-Agent.
"""
import importlib.util
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402

from core.data_handler import DataHandler  # noqa: E402
from core.music_library import Entry  # noqa: E402
from services import music_service, workers  # noqa: E402
from services.audio_format import find_signature  # noqa: E402
from services.music_service import MusicService  # noqa: E402
from services.stream_buffer import StreamBuffer  # noqa: E402
from tests.fakes import icecast  # noqa: E402
from tests.fakes.icecast import STATION  # noqa: E402

WAIT = 20000            # ms: holgura para FFmpeg en equipos cargados
UA = "Lavf/61.7.100"    # el User-Agent con el que pide FFmpeg


@pytest.fixture
def emisora():
    hub = icecast.start()
    yield hub
    hub.close()


def _acortar(buf: StreamBuffer) -> StreamBuffer:
    buf.RETRY_DELAYS_S = (0.4, 0.6, 0.8)
    buf.DEAD_AIR_S = 1.5
    buf.STABLE_AUDIO_S = 3.0
    return buf


@pytest.fixture
def proxy(qtbot):
    buf = _acortar(StreamBuffer())
    yield buf
    buf.shutdown()


class _Reproductor:
    """Lee la URL local en un hilo, como FFmpeg (con su User-Agent y
    pidiendo metadatos ICY, que el proxy no le da). `finished`: llegó la
    parte final del cuerpo (el audio acabó entero, no se cortó)."""

    def __init__(self, url: str):
        parts = urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=30)
        self.sock.sendall(f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
                          f"User-Agent: {UA}\r\nIcy-MetaData: 1\r\n"
                          f"Connection: close\r\n\r\n".encode())
        self.status = None
        self.headers = {}
        self.data = bytearray()
        self.closed = False
        self.finished = False
        self.first_data_at = None
        self._chunked = False
        self._pending = bytearray()
        self._left = self._skip = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    self.closed = True
                    return
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            self.headers = {k.strip().lower(): v.strip() for k, _, v in
                            (line.partition(":") for line in lines[1:])}
            self._chunked = 'chunked' in self.headers.get('transfer-encoding', '')
            self.status = int(lines[0].split()[1])
            self._feed(body)
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._feed(chunk)
        except OSError:
            pass
        self.closed = True

    def _feed(self, raw: bytes) -> None:
        if not self._chunked:
            self._add(raw)
            return
        self._pending += raw
        while self._pending and not self.finished:
            if self._skip:                       # el \r\n tras cada parte
                n = min(self._skip, len(self._pending))
                del self._pending[:n]
                self._skip -= n
            elif self._left:
                n = min(self._left, len(self._pending))
                self._add(bytes(self._pending[:n]))
                del self._pending[:n]
                self._left -= n
                self._skip = 0 if self._left else 2
            else:
                end = self._pending.find(b"\r\n")
                if end < 0:
                    return
                size = int(bytes(self._pending[:end]).split(b";")[0], 16)
                del self._pending[:end + 2]
                self.finished = size == 0
                self._left = size

    def _add(self, data: bytes) -> None:
        if data:
            if self.first_data_at is None:
                self.first_data_at = time.monotonic()
            self.data += data

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _sin_solapes(records):
    """Cada conexión cerrada antes de abrir la siguiente."""
    ordered = sorted(records, key=lambda r: r['t_open'])
    return all(a['t_close'] is not None and a['t_close'] <= b['t_open']
               for a, b in zip(ordered, ordered[1:]))


# ================================================================== proxy
def test_proxy_una_conexion_y_los_titulos_en_ella(qtbot, emisora, proxy):
    """I2/I3/I8: los títulos, el nombre y los kbps salen de la misma conexión
    del audio (con Icy-MetaData y el User-Agent del reproductor) y los
    bloques de metadatos no llegan al audio."""
    seen = []
    proxy.metadata.connect(lambda sid, info: seen.append(dict(info)))
    url = proxy.open(emisora.url(), 0, known_live=False)
    player = _Reproductor(url)
    qtbot.waitUntil(lambda: len(player.data) > 150000, timeout=WAIT)
    qtbot.waitUntil(lambda: any(i['title'] for i in seen), timeout=WAIT)
    assert player.status == 200 and player.headers['icy-name'] == STATION
    assert 'icy-metaint' not in player.headers
    data = bytes(player.data)
    assert b"StreamTitle" not in data and find_signature(data) == ('mpeg', 3, 3, 44100)
    assert seen[0] == {'title': None, 'name': STATION, 'bitrate': 320, 'mime': 'audio/mpeg'}
    assert [i['title'] for i in seen if i['title']][:2] == ["Artista 1 - Tema 1",
                                                           "Artista 2 - Tema 2"]
    [rec] = emisora.streams()
    assert (rec['ua'], rec['icy']) == (UA, '1') and emisora.max_active == 1
    player.stop()


def test_el_titulo_llega_con_su_audio(qtbot, emisora, proxy):
    """Con precarga, cada título se avisa cuando se entrega su audio, no
    cuando lo lee el proxy (N s antes)."""
    titles = []
    proxy.metadata.connect(lambda sid, info: info['title'] and titles.append(time.monotonic()))
    t0 = time.monotonic()
    player = _Reproductor(proxy.open(emisora.url(), 1.0, known_live=True))
    qtbot.waitUntil(lambda: bool(titles), timeout=WAIT)
    # La ráfaga trae varios bloques al momento: sin retrasarlos saldrían ya
    assert titles[0] - t0 >= 0.9
    assert player.first_data_at is not None and titles[0] >= player.first_data_at - 0.2
    player.stop()


def test_una_pista_va_directa_por_302_tras_cerrar_la_conexion(qtbot, emisora, proxy):
    """I3: de vida desconocida, con Content-Length y Accept-Ranges y sin
    icy-*: el proxy cierra su conexión y SOLO entonces contesta 302; el
    reproductor la pide directa (con Range) sin solaparse."""
    emisora.set_mode('track')
    tracks, bypassed = [], []
    proxy.track_detected.connect(tracks.append)
    proxy.bypassed.connect(bypassed.append)
    url = proxy.open(emisora.url(), 0, known_live=False)
    sid = proxy.session_id
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == 302 and resp.headers['Location'] == emisora.url()
    with requests.get(emisora.url(), headers={'Range': 'bytes=0-'}, stream=True,
                      timeout=10) as direct:
        assert direct.status_code == 200 and direct.headers['Accept-Ranges'] == 'bytes'
    qtbot.waitUntil(lambda: tracks == [sid] and bypassed == [sid], timeout=WAIT)
    first, second = emisora.streams()
    assert (first['icy'], second['range']) == ('1', 'bytes=0-')
    assert first['t_close'] is not None and first['t_close'] <= second['t_open']
    assert emisora.max_active == 1


@pytest.mark.parametrize("mode, status, answered", [
    ('reject403', 403, 403), ('reject401', 401, 401), ('reject429', 429, 429),
    ('html200', 200, 502)])
def test_un_rechazo_es_una_sola_peticion_sin_302(qtbot, emisora, proxy, mode, status, answered):
    """I4: el proxy no reintenta ni redirige (el 302 hacía a FFmpeg repetir
    la petición): avisa con rejected y contesta el error."""
    emisora.set_mode(mode)
    seen = []
    proxy.rejected.connect(lambda sid, code: seen.append((sid, code)))
    url = proxy.open(emisora.url(), 0, known_live=False)
    sid = proxy.session_id
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == answered and 'Location' not in resp.headers
    assert proxy.last_rejected == (sid, status)
    qtbot.waitUntil(lambda: seen == [(sid, status)], timeout=WAIT)
    qtbot.wait(1500)                    # más que todas las esperas del lector
    assert len(emisora.streams()) == 1


@pytest.mark.parametrize("mode, kind", [('down404', 'http'), ('accept_close', 'network')])
def test_otros_errores_contestan_502_sin_302(qtbot, emisora, proxy, mode, kind):
    """I5: un 404 o la red caída en la 1.ª conexión: 502 y nada más."""
    emisora.set_mode(mode)
    url = proxy.open(emisora.url(), 0, known_live=True)
    sid = proxy.session_id
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == 502 and 'Location' not in resp.headers
    assert proxy.last_failure == (sid, kind) and proxy.last_rejected is None
    qtbot.wait(1500)
    assert len(emisora.streams()) == 1


def test_icy_200_ok_va_directo_por_302_tras_cerrar(qtbot, emisora, proxy):
    """I5: SHOUTcast v1 («ICY 200 OK») no se puede leer con requests: 302,
    con la conexión del lector ya cerrada."""
    emisora.set_mode('icy_v1')
    url = proxy.open(emisora.url(), 0, known_live=True)
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == 302 and resp.headers['Location'] == emisora.url()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    assert len(emisora.streams()) == 1


def test_sin_precarga_un_corte_acaba_la_sesion_sin_reconectar(qtbot, emisora, proxy):
    """Con precarga 0 la sesión es «como directa»: si el origen corta, el
    reproductor recibe el fin (y MusicService reconecta con su escalera)."""
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=True))
    qtbot.waitUntil(lambda: len(player.data) > 70000, timeout=WAIT)
    emisora.cut()
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    qtbot.wait(1000)                    # más que la primera espera del lector
    assert len(emisora.streams()) == 1
    assert not player.finished          # un corte llega como corte (FFmpeg: error)


def test_el_fin_de_una_pista_por_partes_llega_como_fin(qtbot, emisora, proxy):
    """Una pista sin tamaño ni icy-* se sirve por el proxy (nada dice que sea
    un archivo); cuando el origen termina entero, el reproductor recibe el fin
    (no un corte): MusicService pasa a la siguiente y la recuerda como pista."""
    emisora.set_mode('chunked', track_s=1.0)
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=False))
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    assert player.status == 200 and player.finished
    assert len(player.data) == len(icecast.MP3_FRAME) * int(1.0 / 0.026)
    qtbot.wait(800)
    assert len(emisora.streams()) == 1


def test_un_hipo_corto_no_corta_ni_abre_otra_conexion(qtbot, emisora, proxy):
    """I6: un silencio más corto que DEAD_AIR_S sigue en la misma conexión, y
    con precarga 0 el proxy no cierra al reproductor a los 0,5 s."""
    proxy.DEAD_AIR_S = 3.0
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=True))
    qtbot.waitUntil(lambda: len(player.data) > 70000, timeout=WAIT)
    emisora.set_mode('silent')
    qtbot.wait(1800)
    before = len(player.data)
    emisora.set_mode('normal')
    qtbot.waitUntil(lambda: len(player.data) > before + 20000, timeout=WAIT)
    assert not player.closed and len(emisora.streams()) == 1
    player.stop()


def test_un_silencio_largo_cierra_espera_y_se_rinde(qtbot, emisora, proxy):
    """I6: tras DEAD_AIR_S de silencio la conexión se da por muerta (el
    servidor ve el cierre), la siguiente espera RETRY_DELAYS_S y, sin audio
    estable, el lector se rinde tras len(RETRY_DELAYS_S) reconexiones."""
    player = _Reproductor(proxy.open(emisora.url(), 1.0, known_live=True))
    qtbot.waitUntil(lambda: player.status == 200, timeout=WAIT)
    emisora.set_mode('silent')
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    recs = emisora.streams()
    assert len(recs) == 1 + len(proxy.RETRY_DELAYS_S)
    assert _sin_solapes(recs) and emisora.max_active == 1
    for (a, b), delay in zip(zip(recs, recs[1:]), proxy.RETRY_DELAYS_S):
        assert b['t_open'] - a['t_close'] >= delay - 0.05
        assert a['t_close'] - a['t_open'] >= proxy.DEAD_AIR_S - 0.1


def test_un_origen_caido_tiene_tope_y_esperas(qtbot, emisora, proxy):
    """I6: con el origen caído (404) tras un corte, como mucho
    len(RETRY_DELAYS_S) reintentos, espaciados, y luego el fin."""
    player = _Reproductor(proxy.open(emisora.url(), 1.0, known_live=True))
    qtbot.waitUntil(lambda: player.status == 200, timeout=WAIT)
    since = emisora.now()
    emisora.set_mode('down404', kill=True)
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    qtbot.wait(1000)
    after = emisora.streams(since)
    assert len(after) == len(proxy.RETRY_DELAYS_S)
    gaps = [b['t_open'] - a['t_open'] for a, b in zip(after, after[1:])]
    assert all(g >= d - 0.05 for g, d in zip(gaps, proxy.RETRY_DELAYS_S[1:])), gaps


def test_cerrar_limpio_al_poco_una_y_otra_vez_cuenta_para_el_tope(qtbot, emisora, proxy):
    """I6: un origen que da un poco de audio y cierra limpio (un archivo, o
    una emisora que echa) no se empalma sin fin."""
    emisora.set_mode('cut', cut_after_s=0.3)
    player = _Reproductor(proxy.open(emisora.url(), 0.5, known_live=True))
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    qtbot.wait(1000)
    assert len(emisora.streams()) == 1 + len(proxy.RETRY_DELAYS_S)


@pytest.mark.parametrize("formats, spliced", [
    (['mp3', 'aac'], False), (['aac', 'aac22'], False), (['mp3', 'mp3'], True)])
def test_solo_se_empalma_el_mismo_formato(qtbot, emisora, proxy, formats, spliced):
    """I7: tras un corte, una reconexión en otro formato (MP3 ↔ AAC, o AAC a
    otra frecuencia) no se empalma: se sirve lo que quedaba y se cierra."""
    emisora.formats = formats
    player = _Reproductor(proxy.open(emisora.url(), 0.5, known_live=True))
    qtbot.waitUntil(lambda: len(player.data) > 30000, timeout=WAIT)
    emisora.cut()
    qtbot.waitUntil(lambda: len(emisora.streams()) == 2, timeout=WAIT)
    if spliced:
        mark = len(player.data)
        qtbot.waitUntil(lambda: len(player.data) > mark + 100000, timeout=WAIT)
        assert not player.closed
        player.stop()
    else:
        qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
        data = bytes(player.data)
        first = find_signature(data)
        # Solo el audio de la primera conexión: ni una trama del otro formato
        assert first is not None and find_signature(data[-4096:]) in (first, None)
        other = icecast.FORMATS[formats[1]][1][:4]
        assert other not in data
    assert emisora.max_active == 1


def test_la_sesion_nueva_espera_a_que_cierre_la_anterior(qtbot, emisora, proxy):
    """I1: abrir otra sesión (pausa y reanudar, siguiente…) no conecta hasta
    que la conexión anterior esté cerrada, aunque el origen estuviera callado
    (el hilo viejo bloqueado en la lectura), y no bloquea a quien llama."""
    first = _Reproductor(proxy.open(emisora.url('prem2'), 0, known_live=True))
    qtbot.waitUntil(lambda: len(first.data) > 60000, timeout=WAIT)
    emisora.set_mode('silent')
    qtbot.wait(300)
    t0 = time.monotonic()
    url = proxy.open(emisora.url('prem4'), 0, known_live=True)
    assert time.monotonic() - t0 < 0.2
    emisora.set_mode('normal')
    second = _Reproductor(url)
    qtbot.waitUntil(lambda: len(second.data) > 60000, timeout=WAIT)
    recs = emisora.streams()
    assert len(recs) == 2 and _sin_solapes(recs) and emisora.max_active == 1
    second.stop()
    first.stop()


def test_cerrar_y_apagar_dejan_cero_conexiones(qtbot, emisora, proxy):
    """I8: close() y shutdown() cortan la conexión con la emisora al momento."""
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=True))
    qtbot.waitUntil(lambda: emisora.active_count() == 1 and len(player.data) > 0, timeout=WAIT)
    proxy.close()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=1000)
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=True))
    qtbot.waitUntil(lambda: emisora.active_count() == 1 and len(player.data) > 0, timeout=WAIT)
    proxy.shutdown()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=1000)
    assert emisora.max_active == 1


# ================================================================== MusicService
@pytest.fixture
def make_service(qtbot, tmp_path):
    created = []

    def make(**kwargs):
        dh = DataHandler(tmp_path / "data.json")
        lofi = tmp_path / "lofi"
        lofi.mkdir(exist_ok=True)
        kwargs.setdefault("reconnect_delays", (200, 400, 600, 800, 1000))
        kwargs.setdefault("buffer_steps", (1, 2))
        svc = MusicService(None, data_handler=dh, lofi_dir=lofi, **kwargs)
        svc.set_muted(True)
        svc.ERROR_SKIP_MS = 300
        _acortar(svc._stream_buffer)
        created.append(svc)
        return svc

    yield make
    for svc in created:
        svc.shutdown()
    QThreadPool.globalInstance().waitForDone(15000)
    workers.wait_threads(15)
    qtbot.wait(50)
    for svc in created:
        svc.deleteLater()
    qtbot.wait(50)


def _radio(svc, *urls, live=True):
    lid = svc.create_list("DI.FM")
    svc.add_entries(lid, [Entry(u, title=f"Lofi {n}", live=live) for n, u in enumerate(urls)])
    return lid, [i['id'] for i in svc.items(lid)]


def _desde_el_pls(qtbot, svc, hub):
    """Como la añade el usuario: el .pls de DI.FM con sus tres espejos."""
    with qtbot.waitSignal(svc.link_resolved, timeout=WAIT) as blocker:
        svc.add_link(hub.pls_url())
    lid, added, error = blocker.args
    assert (added, error) == (3, '')
    items = svc.items(lid)
    assert [i['live'] for i in items] == [None, None, None]
    return lid, [i['id'] for i in items]


def test_radio_de_un_pls_suena_con_una_sola_conexion(qtbot, emisora, make_service):
    """I1/I2/I3/I8: ni sondeo ICY aparte ni otra sesión: el reproductor
    conecta al proxy (127.0.0.1) desde la 1.ª vez, sin precarga, y los
    títulos llegan por esa misma conexión."""
    svc = make_service()
    lid, (first, _b, _c) = _desde_el_pls(qtbot, svc, emisora)
    states = []
    svc.state_changed.connect(states.append)
    svc.play(lid, first)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    source = svc._player.source()
    assert source.host() == "127.0.0.1" and source.port() == svc._stream_buffer.port
    assert svc.current()['buffer_s'] == 0 and 'buffering' not in states
    qtbot.waitUntil(lambda: svc.current()['subtitle'].startswith("Artista "), timeout=WAIT)
    qtbot.wait(2000)
    cur = svc.current()
    assert cur['live'] and cur['station'] == STATION and cur['codec'] == 'MP3'
    assert svc.items(lid)[0]['live'] is True       # confirmado por las cabeceras icy-*
    recs = emisora.streams()
    assert len(recs) == 1 and emisora.max_active == 1
    assert recs[0]['ua'].startswith("Lavf") and recs[0]['icy'] == '1'


def test_pausar_reanudar_siguiente_y_enfoque_sin_solapar(qtbot, emisora, make_service):
    """I1/I8: cada acción cierra la conexión antes de abrir la siguiente (los
    espejos prem2/prem4 llevan la misma clave) y la pausa deja 0."""
    svc = make_service()
    lid, (a, b) = _radio(svc, emisora.url('prem2'), emisora.url('prem4'))

    def sounding(item=None):
        qtbot.waitUntil(lambda: svc.state == 'playing'
                        and (item is None or svc.current()['item_id'] == item), timeout=WAIT)
        qtbot.wait(400)

    svc.play(lid, a)
    sounding(a)
    svc.pause()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    svc.resume()
    sounding(a)
    svc.next()                                   # otro servidor, misma clave
    sounding(b)
    svc.play(lid, a)
    sounding(a)
    svc.set_focus_only(True)                     # timer parado: en pausa, armada
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    svc.on_focus_state(True)
    sounding(a)
    svc.on_focus_state(False)
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    svc.on_focus_state(True)
    sounding(a)
    svc.stop()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    recs = emisora.streams()
    assert len(recs) == 6                        # una por acción que suena
    assert _sin_solapes(recs) and emisora.max_active == 1
    assert {r['ua'] for r in recs} == {recs[0]['ua']}    # un solo User-Agent


@pytest.mark.parametrize("mode, text", [
    ('reject403', "La emisora rechazó la conexión (HTTP 403). Si es una radio de pago, "
                  "comprueba que no esté sonando en otro dispositivo y vuelve a darle a "
                  "reproducir en unos minutos."),
    ('reject401', "(HTTP 401)"), ('reject429', "pide esperar"), ('html200', "página web")])
def test_un_rechazo_se_rinde_a_la_primera_sin_saltar_de_espejo(qtbot, emisora, make_service,
                                                               mode, text):
    """I4: cuenta en pausa o en otro sitio. Una petición y se para, sin
    reintentar ni probar los otros espejos del .pls."""
    svc = make_service()
    lid, (first, _b, _c) = _desde_el_pls(qtbot, svc, emisora)
    emisora.set_mode(mode)
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid, first)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT)
    qtbot.wait(3500)                             # más que todas las esperas de reconexión
    assert len(emisora.streams()) == 1
    assert not svc.is_playing and svc.current()['item_id'] == first
    assert errors and text in errors[-1]


def test_un_rechazo_con_el_bufer_sonando_no_martillea(qtbot, emisora, make_service):
    """I4: DI.FM pausa la cuenta en mitad de una sesión con búfer: el
    reintento que descubre el 403 y nada más."""
    svc = make_service()
    lid, [iid] = _radio(svc, emisora.url())
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    qtbot.wait(800)
    emisora.cut()                                # un corte: reconecta con búfer
    qtbot.waitUntil(lambda: svc.state == 'playing' and svc.current()['buffer_s'] == 1,
                    timeout=WAIT)
    qtbot.wait(800)
    since = emisora.now()
    emisora.set_mode('reject403', kill=True)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT)
    qtbot.wait(3500)
    after = emisora.streams(since)
    assert len(after) == 1 and after[0]['status'] == 403
    assert "HTTP 403" in svc.current()['error'] and emisora.max_active == 1


def test_un_origen_que_cierra_al_aceptar_un_intento_por_reconexion(qtbot, emisora,
                                                                    make_service):
    """I5/I6: sin 302, cada reintento de MusicService es UNA petición."""
    emisora.set_mode('accept_close')
    svc = make_service()
    lid, [iid] = _radio(svc, emisora.url())
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT)
    qtbot.wait(1500)
    tries = emisora.streams()
    assert 2 <= len(tries) <= 1 + len(svc.RECONNECT_DELAYS_MS)
    gaps = [b['t_open'] - a['t_open'] for a, b in zip(tries, tries[1:])]
    assert all(g >= 0.15 for g in gaps), gaps


def test_una_pista_del_pls_va_directa_por_302_y_se_recuerda(qtbot, emisora, make_service):
    """I3: una entrada de vida desconocida que resulta ser un archivo suena
    directa (302 con la conexión del proxy ya cerrada) y se guarda live False:
    la siguiente vez va directa sin pasar por el proxy. (Con una pista, FFmpeg
    puede pedir más rangos por su cuenta, p. ej. las etiquetas del final: eso
    ya no es una radio y no se mira.)"""
    emisora.set_mode('track')
    svc = make_service()
    lid, [iid] = _radio(svc, emisora.url(), live=None)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    qtbot.waitUntil(lambda: svc.items(lid)[0]['live'] is False, timeout=WAIT)
    probe, direct = emisora.streams()[:2]
    # El proxy nunca pide Range; FFmpeg, sí
    assert (probe['icy'], probe['range']) == ('1', '') and direct['range']
    assert direct['t_open'] >= probe['t_close']
    assert svc.current()['duration_ms'] > 5000 and not svc.current()['live']
    svc.stop()
    since = emisora.now()
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    assert svc._player.source().toString() == emisora.url()
    assert all(r['range'] for r in emisora.streams(since))


def test_tope_global_de_conexiones(qtbot, emisora, make_service):
    """I6: una emisora que da un poco de audio y corta una y otra vez: más
    de RATE_MAX_CONNECTIONS conexiones en la ventana → se para, sin bucle."""
    emisora.set_mode('cut', cut_after_s=0.6)
    svc = make_service()
    svc.RATE_MAX_CONNECTIONS = 4
    lid, [iid] = _radio(svc, emisora.url())
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT * 2)
    assert errors and "una y otra vez" in errors[-1]
    count = len(emisora.streams())
    qtbot.wait(2500)
    assert count <= 4 and len(emisora.streams()) == count


def test_parar_y_cerrar_la_app_dejan_cero_conexiones(qtbot, emisora, make_service):
    svc = make_service()
    lid, [iid] = _radio(svc, emisora.url())
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    svc.stop()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    svc.shutdown()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    assert emisora.max_active == 1


# ================================================================== valores por defecto
def test_los_tiempos_por_defecto_no_martillean():
    """Lo que protege a la cuenta con los valores de verdad: la 1.ª
    reconexión del proxy, a ≥ 2 s (no a 0,25 s), esperas crecientes y con
    tope; ~10 s de silencio para dar una conexión por muerta; los intentos de
    MusicService solo vuelven a cero tras 60 s sonando; tope global."""
    delays = StreamBuffer.RETRY_DELAYS_S
    assert delays[0] >= 2.0 and list(delays) == sorted(delays) and len(delays) <= 4
    assert StreamBuffer.DEAD_AIR_S >= 10
    assert StreamBuffer.STABLE_AUDIO_S >= 30
    assert MusicService.STABLE_MS >= 60000
    assert MusicService.RATE_MAX_CONNECTIONS <= 10 and MusicService.RATE_WINDOW_S >= 600
    # Ni rastro del sondeo ICY aparte (era la 2.ª conexión con la emisora)
    assert importlib.util.find_spec("services.icy_monitor") is None
    assert not any('icy' in name.lower() and 'monitor' in name.lower()
                   for name in vars(music_service))


def test_la_reconexion_del_proxy_espera_de_verdad(qtbot, emisora):
    """Con los tiempos por defecto: tras un corte, la siguiente conexión no
    llega antes de RETRY_DELAYS_S[0] (el servidor registra el cierre)."""
    buf = StreamBuffer()
    try:
        player = _Reproductor(buf.open(emisora.url(), 1.0, known_live=True))
        qtbot.waitUntil(lambda: player.status == 200, timeout=WAIT)
        emisora.cut()
        qtbot.waitUntil(lambda: len(emisora.streams()) == 2, timeout=WAIT)
        first, second = emisora.streams()
        assert second['t_open'] - first['t_close'] >= StreamBuffer.RETRY_DELAYS_S[0] - 0.05
        player.stop()
    finally:
        buf.shutdown()
