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
parar, cambiar y cerrar dejan 0 conexiones y un solo User-Agent · I9 nada
espera al hilo lector en el hilo de la interfaz. Y lo que el proxy no debe
cambiar respecto a ir directo: un corte llega a FFmpeg como corte (no como
el fin de una pista) y una emisora https que solo el proxy no puede validar
suena igual, directa.
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
from services.stream_buffer import ConnectionBudget, StreamBuffer  # noqa: E402
from tests.fakes import icecast  # noqa: E402
from tests.fakes.icecast import STATION  # noqa: E402

WAIT = 20000            # ms: holgura para FFmpeg en equipos cargados
UA = "Lavf/61.7.100"    # el User-Agent con el que pide FFmpeg


@pytest.fixture
def emisora():
    hub = icecast.start()
    yield hub
    hub.close()


@pytest.fixture
def emisora_tls():
    """La misma emisora por https, con un certificado que certifi no valida."""
    cert = icecast.tls_cert()
    if cert is None:
        pytest.skip("este Python no trae los certificados de prueba de Lib/test")
    hub = icecast.start(tls=cert)
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
    parte final del cuerpo (el audio acabó entero); `truncated`: se cerró a
    mitad de una parte (un corte)."""

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

    @property
    def truncated(self) -> bool:
        """¿Se cerró a mitad de una parte? Así distingue FFmpeg un corte (un
        error: «Stream ends prematurely») del fin: si la conexión se cierra
        justo tras una parte entera, lo lee como el fin del archivo."""
        return self.closed and self._chunked and not self.finished and self._left > 0

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
    ('reject402', 402, 402), ('reject407', 407, 407), ('reject451', 451, 451),
    ('html200', 200, 502), ('html_as_audio', 200, 502)])
def test_un_rechazo_es_una_sola_peticion_sin_302(qtbot, emisora, proxy, mode, status, answered):
    """I4: el proxy no reintenta ni redirige (el 302 hacía a FFmpeg repetir
    la petición): avisa con rejected y contesta el error. También con la
    página servida como audio/mpeg y con su tamaño: de vida desconocida,
    sus cabeceras son las de una pista, pero el cuerpo dice que es una
    página (sin track_detected, que guardaría la radio como pista)."""
    emisora.set_mode(mode)
    seen, tracks = [], []
    proxy.rejected.connect(lambda sid, code: seen.append((sid, code)))
    proxy.track_detected.connect(tracks.append)
    url = proxy.open(emisora.url(), 0, known_live=False)
    sid = proxy.session_id
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == answered and 'Location' not in resp.headers
    assert proxy.last_rejected == (sid, status)
    qtbot.waitUntil(lambda: seen == [(sid, status)], timeout=WAIT)
    qtbot.wait(1500)                    # más que todas las esperas del lector
    assert len(emisora.streams()) == 1 and tracks == []


def test_una_pagina_con_tipo_de_audio_en_un_directo_tambien_es_un_rechazo(qtbot, emisora,
                                                                        proxy):
    """I4: con un directo confirmado, la página con tipo de audio solo se
    reconoce por el principio del cuerpo."""
    emisora.set_mode('html_as_audio')
    seen = []
    proxy.rejected.connect(lambda sid, code: seen.append((sid, code)))
    url = proxy.open(emisora.url(), 0, known_live=True)
    sid = proxy.session_id
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == 502 and proxy.last_rejected == (sid, 200)
    qtbot.waitUntil(lambda: seen == [(sid, 200)], timeout=WAIT)
    qtbot.wait(1500)
    assert len(emisora.streams()) == 1


@pytest.mark.parametrize("known_live", [False, True])
def test_una_lista_hls_con_tipo_de_audio_va_por_302(qtbot, emisora, proxy, known_live):
    """I5: una lista HLS servida como audio/mpeg (y con su tamaño) no es
    audio que se pueda pasar: 302 para que FFmpeg la abra como lista, sin
    tomarla por una pista (track_detected guardaría live False)."""
    emisora.set_mode('hls_as_audio')
    bypassed, tracks = [], []
    proxy.bypassed.connect(bypassed.append)
    proxy.track_detected.connect(tracks.append)
    url = proxy.open(emisora.url(), 0, known_live=known_live)
    sid = proxy.session_id
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == 302 and resp.headers['Location'] == emisora.url()
    qtbot.waitUntil(lambda: bypassed == [sid], timeout=WAIT)
    assert tracks == [] and len(emisora.streams()) == 1 and proxy.last_failure is None


def test_un_certificado_que_el_proxy_no_valida_va_directo_por_302(qtbot, emisora_tls, proxy):
    """Una emisora https con un certificado que requests (certifi) no valida
    y FFmpeg, que no lo comprueba, sí abre (el de un antivirus que inspecciona
    HTTPS, una cadena incompleta…): la negociación falla antes de que la
    emisora vea ninguna petición y el proxy redirige; el reproductor conecta
    directo en ese mismo intento. Con un 502, la entrada de un .pls se
    saltaba y un directo perdía un intento y su espera."""
    bypassed = []
    proxy.bypassed.connect(bypassed.append)
    url = proxy.open(emisora_tls.url(), 0, known_live=False)
    sid = proxy.session_id
    resp = requests.get(url, allow_redirects=False, timeout=10)
    assert resp.status_code == 302 and resp.headers['Location'] == emisora_tls.url()
    qtbot.waitUntil(lambda: bypassed == [sid], timeout=WAIT)
    assert emisora_tls.streams() == [] and proxy.last_failure is None


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
    con la conexión del lector ya cerrada. Se mira cuándo la cerró el lector
    (el falso ve el cierre en ~10 ms): con solo esperar a que no quedara
    ninguna abierta, el propio falso la cerraba a los 2 s y un proxy que la
    cerrara tarde (dos conexiones a la vez), o nunca, pasaba igual."""
    emisora.set_mode('icy_v1')
    url = proxy.open(emisora.url(), 0, known_live=True)
    resp = requests.get(url, allow_redirects=False, timeout=10)
    t_302 = emisora.now()
    assert resp.status_code == 302 and resp.headers['Location'] == emisora.url()
    qtbot.waitUntil(lambda: emisora.active_count() == 0, timeout=2000)
    [rec] = emisora.streams()
    assert rec['end'] == 'client_closed'
    assert rec['t_close'] is not None and rec['t_close'] <= t_302 + 0.3


def test_sin_precarga_un_corte_acaba_la_sesion_sin_reconectar(qtbot, emisora, proxy):
    """Con precarga 0 la sesión es «como directa»: si el origen corta, el
    reproductor recibe un corte (y MusicService reconecta con su escalera).
    Un corte y no el fin: la conexión se le cierra a mitad de una parte, lo
    que FFmpeg toma por un error; tras una parte entera, sería el fin."""
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=True))
    qtbot.waitUntil(lambda: len(player.data) > 70000, timeout=WAIT)
    emisora.cut()
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    qtbot.wait(1000)                    # más que la primera espera del lector
    assert len(emisora.streams()) == 1
    assert player.truncated and not player.finished


def test_el_fin_de_una_pista_por_partes_llega_como_fin(qtbot, emisora, proxy):
    """Una pista sin tamaño ni icy-* se sirve por el proxy (nada dice que sea
    un archivo); cuando el origen termina entero, el reproductor recibe el fin
    (no un corte): MusicService pasa a la siguiente y la recuerda como pista."""
    emisora.set_mode('chunked', track_s=1.0)
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=False))
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    assert player.status == 200 and player.finished and not player.truncated
    assert len(player.data) == len(icecast.MP3_FRAME) * int(1.0 / 0.026)
    qtbot.wait(800)
    assert len(emisora.streams()) == 1


@pytest.mark.parametrize("value", ['abc', '0', '999999'])
def test_un_icy_metaint_que_no_vale_reconecta_sin_metadatos(qtbot, emisora, proxy, value):
    """I2: con un icy-metaint que no vale no se sabe dónde van los bloques
    de metadatos: ese audio no se pasa (los mezclaría) y se vuelve a conectar
    sin pedirlos, tras cerrar la primera conexión y esperar como en un corte."""
    emisora.set_mode('normal', metaint_header=value)
    player = _Reproductor(proxy.open(emisora.url(), 0, known_live=True))
    qtbot.waitUntil(lambda: len(player.data) > 100000, timeout=WAIT)
    first, second = emisora.streams()
    assert (first['icy'], second['icy']) == ('1', '')
    assert _sin_solapes([first, second]) and emisora.max_active == 1
    assert second['t_open'] - first['t_close'] >= proxy.RETRY_DELAYS_S[0] - 0.05
    assert b"StreamTitle" not in bytes(player.data)
    player.stop()


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
        # Un demuxer nuevo por conexión: cada una empieza con icy-metaint
        # bytes de audio; con el de la anterior se colarían sus bloques
        assert b"StreamTitle" not in bytes(player.data)
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


def test_si_ya_no_llegara_mas_audio_no_espera_a_la_precarga(qtbot, emisora, proxy):
    """Una reconexión con precarga larga (los escalones altos) cuyo origen
    corta enseguida y vuelve en otro formato: el proxy ya sabe que no habrá
    más audio y le da al reproductor lo que tiene, y el corte, al momento,
    sin esperar a cumplir la precarga (MusicService recarga antes)."""
    emisora.formats = ['mp3', 'aac']
    emisora.set_mode('cut', cut_after_s=0.3, burst=8 * 1024)
    t0 = time.monotonic()
    player = _Reproductor(proxy.open(emisora.url(), 8.0, known_live=True))
    qtbot.waitUntil(lambda: player.closed, timeout=WAIT)
    assert time.monotonic() - t0 < 4.0
    assert player.status == 200 and player.truncated
    data = bytes(player.data)
    assert find_signature(data) == ('mpeg', 3, 3, 44100)
    assert icecast.FORMATS['aac'][1][:4] not in data
    assert len(emisora.streams()) == 2 and emisora.max_active == 1


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


def test_varias_sesiones_seguidas_esperan_en_cadena(qtbot, emisora, proxy):
    """I1: «siguiente» varias veces seguidas. Si la sesión intermedia aún
    espera a la suya anterior (que sigue a medio conectar), la nueva no se
    adelanta: espera a que acabe esa espera."""
    proxy.PREVIOUS_WAIT_S = 1.0
    proxy.open(emisora.url('prem1'), 0, known_live=True)
    first = proxy._session
    hold = threading.Event()
    first.reader = threading.Thread(target=hold.wait, daemon=True)  # su lector, vivo
    first.reader.start()
    first.connecting = True              # y a medio conectar: sin liberar
    started = emisora.now()
    second = _Reproductor(proxy.open(emisora.url('prem2'), 0, known_live=True))
    qtbot.waitUntil(lambda: proxy._session.reader is not None, timeout=WAIT)
    third = _Reproductor(proxy.open(emisora.url('prem4'), 0, known_live=True))
    qtbot.waitUntil(lambda: len(third.data) > 0, timeout=WAIT)
    [rec] = emisora.streams()            # la intermedia, cerrada, no llegó a conectar
    assert rec['host'] == 'prem4' and rec['t_open'] - started >= 0.8
    hold.set()
    second.stop()
    third.stop()


def test_una_sesion_cerrada_mientras_espera_no_gasta_saldo(qtbot, emisora, proxy):
    """I6: el tope global cuenta conexiones de verdad. Una sesión que se
    cierra mientras su lector espera a que cierre la anterior (la anterior,
    a medio abrir) no llega a conectar y no gasta saldo."""
    proxy.PREVIOUS_WAIT_S = 1.0
    budget = ConnectionBudget(1, 600)
    proxy.open(emisora.url('prem1'), 0, known_live=True, budget=budget)
    first = proxy._session
    hold = threading.Event()
    first.reader = threading.Thread(target=hold.wait, daemon=True)  # su lector, vivo
    first.reader.start()
    first.connecting = True              # y a medio conectar: sin liberar
    player = _Reproductor(proxy.open(emisora.url('prem2'), 0, known_live=True, budget=budget))
    qtbot.waitUntil(lambda: proxy._session.reader is not None, timeout=WAIT)
    second = proxy._session
    proxy.close()                        # pausa mientras espera a la anterior
    qtbot.waitUntil(lambda: not second.reader.is_alive(), timeout=WAIT)
    assert emisora.streams() == [] and not budget.full()
    hold.set()
    player.stop()


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


def test_cerrar_y_abrir_no_esperan_al_lector(qtbot, emisora, proxy):
    """I9: con una emisora «sorda» (tras la ráfaga ni manda ni ve el cierre),
    el hilo lector sigue bloqueado aunque se le corte la conexión (en
    Windows, hasta DEAD_AIR_S). close() y open(), que llama la interfaz,
    vuelven al momento: a ese lector lo espera, si hace falta, el nuevo."""
    proxy.DEAD_AIR_S = 5.0
    emisora.set_mode('deaf')
    first = _Reproductor(proxy.open(emisora.url('prem2'), 0, known_live=True))
    qtbot.waitUntil(lambda: len(first.data) > 40000, timeout=WAIT)
    session = proxy._session
    t0 = time.monotonic()
    proxy.close()
    closing = time.monotonic() - t0
    t0 = time.monotonic()
    url = proxy.open(emisora.url('prem4'), 0, known_live=True)
    opening = time.monotonic() - t0
    assert closing < 0.5 and opening < 0.5
    assert session.reader.is_alive()     # sigue bloqueado: nadie lo esperó
    second = _Reproductor(url)
    qtbot.waitUntil(lambda: len(second.data) > 40000, timeout=WAIT)
    assert len(emisora.streams()) == 2 and emisora.max_active == 1
    first.stop()
    second.stop()


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


def test_un_rechazo_para_aunque_su_aviso_llegue_tarde(qtbot, emisora, make_service):
    """I4: el fallo de FFmpeg y la señal rejected llegan cada uno de su hilo.
    Si el fallo se atiende antes (aquí, la señal no llega), last_rejected
    basta para parar: ni reintento ni salto al siguiente espejo del .pls,
    que lleva la misma clave."""
    svc = make_service()
    lid, (first, _b, _c) = _desde_el_pls(qtbot, svc, emisora)
    svc._stream_buffer.rejected.disconnect(svc._on_buffer_rejected)
    emisora.set_mode('reject403')
    svc.play(lid, first)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT)
    qtbot.wait(2000)                             # más que ERROR_SKIP_MS y una reconexión
    assert len(emisora.streams()) == 1
    assert not svc.is_playing and svc.current()['item_id'] == first
    assert "HTTP 403" in svc.current()['error']


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


@pytest.fixture
def tres_emisoras():
    """Tres emisoras gratuitas distintas (un Hub cada una), como las radios
    de serie de «Radios lofi»."""
    hubs = [icecast.start() for _ in range(3)]
    yield hubs
    for hub in hubs:
        hub.close()


def _sin_cuenta(hub):
    """La URL de una emisora gratuita: sin la ?clave (las de serie no llevan)."""
    return hub.url().split('?', 1)[0]


@pytest.mark.parametrize("mode, text", [
    ('reject403', "«Lofi 0» rechazó la conexión (HTTP 403): paso a la siguiente"),
    ('reject429', "«Lofi 0» pide esperar antes de volver a conectar (HTTP 429): "
                  "paso a la siguiente"),
    ('reject451', "«Lofi 0» rechazó la conexión (HTTP 451): paso a la siguiente"),
    ('html200', "«Lofi 0» envió una página web en vez del audio: paso a la siguiente")])
def test_un_rechazo_de_una_emisora_sin_cuenta_pasa_a_la_siguiente(qtbot, tres_emisoras,
                                                                 make_service, mode, text):
    """I4 con emisoras distintas y sin cuenta en la URL (las radios de serie):
    la 1.ª, llena (Icecast da 403 con el cupo de oyentes lleno), vetada en
    este país (451), que pide esperar (429) o tras un portal (una página),
    no apaga la música: una sola petición a ella y suena la 2.ª, sin tocar
    la 3.ª. Sin el consejo de las radios de pago, que aquí no pinta nada."""
    a, _b, _c = tres_emisoras
    a.set_mode(mode)
    svc = make_service()
    lid, (first, second, _third) = _radio(svc, *(_sin_cuenta(h) for h in tres_emisoras))
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid, first)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    assert svc.current()['item_id'] == second and svc.is_playing
    assert errors == [text]
    assert [len(h.streams()) for h in tres_emisoras] == [1, 1, 0]


def test_si_todas_las_emisoras_sin_cuenta_rechazan_una_peticion_a_cada_una(
        qtbot, tres_emisoras, make_service):
    """Saltar tiene el tope de siempre (tantos fallos como items): una
    petición a cada una y se para, sin dar vueltas."""
    for hub in tres_emisoras:
        hub.set_mode('reject403')
    svc = make_service()
    lid, (first, _s, _t) = _radio(svc, *(_sin_cuenta(h) for h in tres_emisoras))
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid, first)
    qtbot.waitUntil(lambda: svc.state == 'error' and not svc.is_playing, timeout=WAIT)
    qtbot.wait(1500)                             # más que ERROR_SKIP_MS y una reconexión
    assert [len(h.streams()) for h in tres_emisoras] == [1, 1, 1]
    assert errors[-1] == "No se pudo reproducir ninguna pista de la lista"


def test_una_emisora_sin_cuenta_que_ya_sonaba_y_rechaza_para(qtbot, tres_emisoras,
                                                             make_service):
    """Si ya sonaba, un rechazo al reconectar tras un corte para, como con
    cualquier radio: el salto es solo para la que nunca llegó a sonar."""
    a, b, c = tres_emisoras
    svc = make_service()
    lid, (first, _s, _t) = _radio(svc, *(_sin_cuenta(h) for h in tres_emisoras))
    svc.play(lid, first)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    qtbot.wait(800)
    a.set_mode('reject403', kill=True)           # corte y, al reconectar, 403
    qtbot.waitUntil(lambda: svc.state == 'error' and not svc.is_playing, timeout=WAIT)
    qtbot.wait(1500)
    assert svc.current()['item_id'] == first and "HTTP 403" in svc.current()['error']
    assert a.streams()[-1]['status'] == 403
    assert b.streams() == [] and c.streams() == []


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


def test_un_corte_de_una_radio_sin_icy_no_la_convierte_en_pista(qtbot, emisora, make_service):
    """Una radio sin cabeceras icy-* (nginx, un CDN) de vida desconocida: el
    proxy la sirve y, si el origen se corta, se lo pasa al reproductor como
    un corte (error), no como el fin del audio. Si no, MusicService la daba
    por una pista que acabó, la guardaba como live False y dejaba de
    reconectar (el siguiente corte ya era «No se pudo reproducir»)."""
    emisora.set_mode('noicy')
    svc = make_service()
    lid, [iid] = _radio(svc, emisora.url(), live=None)
    states = []
    svc.state_changed.connect(states.append)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc._live_runtime is True, timeout=WAIT)   # dura 0: un directo
    emisora.cut()
    qtbot.waitUntil(lambda: 'reconnecting' in states, timeout=WAIT)
    qtbot.waitUntil(lambda: svc.state == 'playing' and len(emisora.streams()) == 2,
                    timeout=WAIT)
    assert svc.items(lid)[0]['live'] is None and svc.current()['item_id'] == iid
    assert svc._player.source().port() == svc._stream_buffer.port and emisora.max_active == 1


@pytest.mark.parametrize("live", [None, True])
def test_radio_https_que_el_proxy_no_valida_suena_a_la_primera(qtbot, emisora_tls,
                                                               make_service, live):
    """Un certificado que solo el proxy rechaza: el reproductor conecta
    directo en el mismo intento. Antes, la entrada de un .pls (live None)
    daba «No se pudo reproducir» y un directo confirmado perdía un intento
    (y la espera de la reconexión) en cada arranque."""
    svc = make_service()
    lid, [iid] = _radio(svc, emisora_tls.url(), live=live)
    states, errors = [], []
    svc.state_changed.connect(states.append)
    svc.error_occurred.connect(errors.append)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    assert 'reconnecting' not in states and 'error' not in states and not errors
    [rec] = emisora_tls.streams()                # solo la de FFmpeg, tras el 302
    assert rec['ua'].startswith("Lavf") and emisora_tls.max_active == 1


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


def test_las_conexiones_directas_tambien_cuentan_para_el_tope(qtbot, emisora, make_service):
    """I6: si el proxy no puede abrir, cada intento va directo, y también
    gasta saldo del tope global (si no, el tope real se duplicaría al
    alternar el proxy y la conexión directa)."""
    emisora.set_mode('cut', cut_after_s=0.6)
    svc = make_service()
    svc.RATE_MAX_CONNECTIONS = 4
    svc._stream_buffer.open = lambda *_a, **_kw: None
    lid, [iid] = _radio(svc, emisora.url())
    errors = []
    svc.error_occurred.connect(errors.append)
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT * 2)
    assert errors and "una y otra vez" in errors[-1]
    recs = emisora.streams()
    assert len(recs) <= 4 and all(r['ua'].startswith("Lavf") for r in recs)
    qtbot.wait(1500)
    assert len(emisora.streams()) == len(recs)


def test_los_intentos_solo_vuelven_a_cero_tras_sonar_un_rato(qtbot, emisora, make_service):
    """I6: una emisora que suena un momento y se corta, una y otra vez. Los
    intentos solo vuelven a cero tras STABLE_MS sonando seguidos: si
    volvieran a cero cada vez que suena, MusicService no se rendiría nunca
    (solo lo pararía el tope global)."""
    emisora.set_mode('cut', cut_after_s=1.5, burst=16000)
    svc = make_service(reconnect_delays=(100, 100, 100), buffer_steps=())
    svc.STABLE_MS = 6000
    svc.RATE_MAX_CONNECTIONS = 100
    lid, [iid] = _radio(svc, emisora.url())
    errors, sounded = [], []
    svc.error_occurred.connect(errors.append)
    svc.state_changed.connect(lambda st: st == 'playing' and sounded.append(st))
    svc.play(lid, iid)
    qtbot.waitUntil(lambda: svc.state == 'error', timeout=WAIT * 2)
    assert errors and "Se perdió la conexión" in errors[-1]
    assert len(sounded) >= 2                     # sonó entre corte y corte
    assert len(emisora.streams()) == 1 + len(svc.RECONNECT_DELAYS_MS)


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


def test_siguiente_y_pausa_no_esperan_al_lector(qtbot, emisora, make_service):
    """I9: con la emisora «sorda», el hilo lector de la conexión que se deja
    sigue bloqueado un rato; pasar a la siguiente o pausar no lo esperan en
    el hilo de la interfaz."""
    svc = make_service()
    svc._stream_buffer.DEAD_AIR_S = 5.0
    emisora.set_mode('deaf')
    lid, (a, b) = _radio(svc, emisora.url('prem2'), emisora.url('prem4'))
    svc.play(lid, a)
    qtbot.waitUntil(lambda: svc.state == 'playing', timeout=WAIT)
    t0 = time.monotonic()
    svc.next()
    took_next = time.monotonic() - t0
    qtbot.waitUntil(lambda: svc.state == 'playing' and svc.current()['item_id'] == b,
                    timeout=WAIT)
    t0 = time.monotonic()
    svc.pause()
    took_pause = time.monotonic() - t0
    assert took_next < 1.0 and took_pause < 1.0
    assert len(emisora.streams()) == 2 and emisora.max_active == 1


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
    llega antes de ~2 s (el servidor registra el cierre; antes, 0,25 s)."""
    buf = StreamBuffer()
    try:
        player = _Reproductor(buf.open(emisora.url(), 1.0, known_live=True))
        qtbot.waitUntil(lambda: player.status == 200, timeout=WAIT)
        emisora.cut()
        qtbot.waitUntil(lambda: len(emisora.streams()) == 2, timeout=WAIT)
        first, second = emisora.streams()
        assert second['t_open'] - first['t_close'] >= 1.9
        player.stop()
    finally:
        buf.shutdown()
