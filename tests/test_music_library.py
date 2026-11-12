"""Biblioteca de música (core.music_library) y su persistencia en DataHandler.

Sin red ni Qt: enlaces, playlists (M3U extendido, PLS, XSPF, HLS), límites,
deduplicado, edición de listas y el ida y vuelta por el JSON de datos (en un
directorio temporal, nunca los datos reales).
"""
import json
import os
import sys
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))

from core import music_library as ml  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from core.music_library import (Entry, MusicLibrary, classify_link, detect_playlist,  # noqa: E402
                                normalize_music, parse_playlist, playlist_name,
                                title_from_source)


@pytest.fixture
def audio_file(tmp_path):
    p = tmp_path / "Mi tema.mp3"
    p.write_bytes(b"\xff\xfb\xe0\x04" + bytes(1040))
    return p


# ------------------------------------------------------------------ enlaces
def test_classify_http_y_https():
    assert classify_link("https://stream.example.com/mp3-320").kind == 'url'
    info = classify_link("  http://radio.example.com:8000/live.mp3  ")
    assert info.kind == 'url' and info.normalized == "http://radio.example.com:8000/live.mp3"


@pytest.mark.parametrize("link, service", [
    ("https://www.youtube.com/watch?v=abc", "YouTube"),
    ("https://youtu.be/abc", "YouTube"),
    ("https://music.youtube.com/watch?v=abc", "YouTube"),
    ("https://open.spotify.com/track/123", "Spotify"),
    ("spotify:track:123", "Spotify"),
    ("open.spotify.com/playlist/9", "Spotify"),
    ("https://soundcloud.com/artista/tema", "SoundCloud"),
    ("https://www.deezer.com/es/track/1", "Deezer"),
    ("https://music.apple.com/es/album/x/1", "Apple Music"),
    ("https://listen.tidal.com/track/1", "Tidal"),
])
def test_classify_servicios_no_soportados(link, service):
    info = classify_link(link)
    assert info.kind == 'unsupported'
    assert service in info.reason and "enlace directo" in info.reason


@pytest.mark.parametrize("link", [
    r"\\servidor\musica\tema.mp3",
    "//servidor/musica/tema.mp3",
    "file://servidor/musica/tema.mp3",
])
def test_classify_rechaza_rutas_de_red(link):
    info = classify_link(link)
    assert info.kind == 'invalid' and "red" in info.reason


def test_classify_rutas_locales(audio_file, tmp_path):
    info = classify_link(str(audio_file))
    assert info.kind == 'file' and Path(info.normalized) == audio_file
    # «Copiar como ruta» de Windows: entre comillas
    assert classify_link(f'"{audio_file}"').kind == 'file'
    assert classify_link(audio_file.as_uri()).kind == 'file'
    assert classify_link(str(tmp_path)).kind == 'file'          # carpeta
    assert classify_link(str(tmp_path / "no-existe.mp3")).kind == 'invalid'
    assert classify_link("tema.mp3").kind == 'invalid'          # relativa


@pytest.mark.parametrize("link", ["", "   ", "ftp://x.com/a.mp3", "javascript:alert(1)",
                                  "radio.example.com/stream", "hola mundo",
                                  "http://a.com/x\nhttp://b.com/y"])
def test_classify_invalidos(link):
    info = classify_link(link)
    assert info.kind == 'invalid' and info.reason


def test_title_from_source():
    assert title_from_source("http://h.com/dir/Mi%20tema.mp3") == "Mi tema"
    assert title_from_source("https://www.radio.com/stream") == "radio.com"
    assert title_from_source("https://stream.radioparadise.com/mp3-320") == "stream.radioparadise.com"
    assert title_from_source(r"C:\Música\Lofi\track 01.flac") == "track 01"


def test_codec_name():
    assert ml.codec_name('audio/mpeg') == 'MP3'
    assert ml.codec_name('audio/aacp; charset=x') == 'AAC'
    assert ml.codec_name('', r'C:\x\y.flac') == 'FLAC'
    assert ml.codec_name('', 'http://h/stream') == ''


# ------------------------------------------------------------------ playlists
M3U = """#EXTM3U
#PLAYLIST:Mi radio
#EXTINF:-1 tvg-name="a,b" group-title="x",Radio Uno, la mejor
http://radio.example.com/uno.mp3
#EXTINF:215,Artista - Tema
canciones/tema%201.mp3
/raiz.mp3
"""


def test_detect_playlist():
    assert detect_playlist(M3U) == 'm3u'
    assert detect_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchunk.m3u8") == 'hls'
    assert detect_playlist("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nseg1.ts") == 'hls'
    assert detect_playlist("\ufeff[playlist]\nFile1=http://a/b") == 'pls'
    assert detect_playlist('<?xml version="1.0"?><playlist xmlns="http://xspf.org/ns/0/">'
                           '<trackList/></playlist>') == 'xspf'
    assert detect_playlist("http://a/b.mp3\n", url="http://h/lista.m3u") == 'm3u'
    assert detect_playlist("algo", content_type="audio/x-scpls") == 'pls'
    assert detect_playlist("<!DOCTYPE html><html></html>", url="http://h/x.m3u") is None
    assert detect_playlist("nada que ver") is None


def test_parse_m3u_extendido_con_base_url():
    entries = parse_playlist(M3U, 'm3u', base="http://h.com/listas/radio.m3u", remote=True)
    assert [e.source for e in entries] == [
        "http://radio.example.com/uno.mp3",
        "http://h.com/listas/canciones/tema%201.mp3",
        "http://h.com/raiz.mp3"]
    assert entries[0].title == "Radio Uno, la mejor"
    assert entries[1].title == "Artista - Tema"
    assert entries[2].title == ""
    assert playlist_name(M3U, 'm3u') == "Mi radio"


def test_parse_m3u_local_resuelve_contra_la_carpeta(tmp_path):
    text = "#EXTM3U\nsub\\uno.mp3\nC:\\otra\\dos.mp3\nhttp://h/tres.mp3\n" \
           + (tmp_path / "cuatro.mp3").as_uri() + "\n\\\\srv\\x\\cinco.mp3\n"
    entries = parse_playlist(text, 'm3u', base=str(tmp_path), remote=False)
    sources = [e.source for e in entries]
    assert sources[0] == os.path.normpath(str(tmp_path / "sub" / "uno.mp3"))
    assert sources[1] == os.path.normpath("C:\\otra\\dos.mp3")
    assert sources[2] == "http://h/tres.mp3"
    assert sources[3] == os.path.normpath(str(tmp_path / "cuatro.mp3"))
    assert len(sources) == 4  # la UNC nunca entra


def test_parse_remota_filtra_rutas_locales_unc_y_file():
    text = ("#EXTM3U\nC:\\Users\\yo\\secreto.mp3\nfile:///C:/x.mp3\n\\\\srv\\share\\a.mp3\n"
            "spotify:track:1\nhttps://ok.example.com/a.mp3\nrelativa.mp3\n")
    entries = parse_playlist(text, 'm3u', base="https://h.example.com/l/x.m3u", remote=True)
    assert [e.source for e in entries] == ["https://ok.example.com/a.mp3",
                                           "https://h.example.com/l/relativa.mp3"]
    # Sin base http, una playlist remota no puede aportar relativas
    assert parse_playlist("relativa.mp3\n", 'm3u', base="", remote=True) == []


def test_parse_pls():
    text = ("[playlist]\nNumberOfEntries=3\nFile2=http://h/dos.mp3\nfile10=http://h/diez.mp3\n"
            "File1=http://h/uno.mp3\nTitle1=Radio Uno\nLength1=-1\nVersion=2\n")
    entries = parse_playlist(text, 'pls', base="http://h/radio.pls", remote=True)
    assert [e.source for e in entries] == ["http://h/uno.mp3", "http://h/dos.mp3",
                                           "http://h/diez.mp3"]
    assert entries[0].title == "Radio Uno" and entries[1].title == ""


def test_parse_xspf_y_rechazo_de_entidades():
    text = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<playlist version="1" xmlns="http://xspf.org/ns/0/"><title>Mi XSPF</title>'
            '<trackList>'
            '<track><location>http://h/a.mp3</location><title>Tema A</title>'
            '<creator>Grupo</creator></track>'
            '<track><location>b.ogg</location></track>'
            '<track><title>sin location</title></track>'
            '</trackList></playlist>')
    entries = parse_playlist(text, 'xspf', base="http://h/l/x.xspf", remote=True)
    assert [e.source for e in entries] == ["http://h/a.mp3", "http://h/l/b.ogg"]
    assert entries[0].title == "Grupo - Tema A"
    assert playlist_name(text, 'xspf') == "Mi XSPF"
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            '<playlist xmlns="http://xspf.org/ns/0/"><trackList><track>'
            '<location>http://h/&lol;.mp3</location></track></trackList></playlist>')
    assert parse_playlist(bomb, 'xspf', base="http://h/", remote=True) == []


def test_hls_no_se_expande():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:10\nseg1.ts\n"
    assert parse_playlist(text, 'hls', base="http://h/live.m3u8", remote=True) == []


def test_limite_de_entradas_por_playlist():
    text = "\n".join(f"http://h/{n}.mp3" for n in range(2500))
    assert len(parse_playlist(text, 'm3u', remote=True)) == ml.MAX_PLAYLIST_ENTRIES == 2000


# ------------------------------------------------------------------ biblioteca
def test_listas_items_dedupe_y_edicion(tmp_path):
    lib = MusicLibrary(None)
    lid = lib.add_list("  Para   estudiar ")
    assert lib.get_list(lid)['name'] == "Para estudiar"
    local = str(tmp_path / "A.mp3")
    added = lib.add_entries(lid, [
        Entry("http://h/uno.mp3", "Uno", live=False, mime="audio/mpeg", bitrate=320),
        Entry("http://h/uno.mp3", "Repetido"),
        Entry(local),
        Entry(local.upper() if os.name == 'nt' else local),   # misma ruta en Windows
        Entry(r"\\srv\share\x.mp3"), Entry("ftp://h/x.mp3"), Entry(""),
        {'source': "https://h/dos.mp3", 'title': "Dos", 'id': 'forzado'},
    ])
    assert added == 3
    items = lib.get_list(lid)['items']
    assert [i['title'] for i in items] == ["Uno", "A", "Dos"]
    assert items[0] == {**items[0], 'kind': 'url', 'live': False, 'mime': 'audio/mpeg',
                        'bitrate': 320}
    assert items[1]['kind'] == 'file' and items[1]['live'] is None
    assert len({i['id'] for i in items}) == 3

    a, b, c = (i['id'] for i in items)
    assert lib.move_item(lid, c, 0)
    assert [i['id'] for i in lib.get_list(lid)['items']] == [c, a, b]
    assert lib.move_item(lid, c, 99)
    assert [i['id'] for i in lib.get_list(lid)['items']] == [a, b, c]
    assert lib.rename_item(lid, a, "Nuevo nombre") and lib.find_item(lid, a)['title'] == "Nuevo nombre"
    assert not lib.rename_item(lid, a, "   ")
    assert lib.update_item(lid, a, live=True, bitrate=128, mime="AUDIO/AAC", source="x")
    item = lib.find_item(lid, a)
    assert (item['live'], item['bitrate'], item['mime']) == (True, 128, 'audio/aac')
    assert item['source'] == "http://h/uno.mp3"           # source no se toca
    assert not lib.update_item(lid, a, bitrate=-5, live="si")

    lib.set_current(lid, b)
    assert lib.remove_item(lid, b) and lib.current == {'list': lid, 'item': None}
    lib.set_current(lid, a)
    assert lib.rename_list(lid, "Otra") and not lib.rename_list(lid, "  ")
    assert lib.delete_list(lid) and lib.current == {'list': None, 'item': None}
    assert not lib.delete_list(lid)


def test_limites_de_listas_e_items(monkeypatch):
    lib = MusicLibrary(None)
    lid = lib.add_list("Grande")
    entries = [Entry(f"http://h/{n}.mp3") for n in range(ml.MAX_ITEMS_PER_LIST + 50)]
    assert lib.add_entries(lid, entries) == ml.MAX_ITEMS_PER_LIST
    assert lib.add_entries(lid, [Entry("http://h/otra.mp3")]) == 0
    assert lib.add_entries(lid, [Entry("http://h/" + "x" * 3000 + ".mp3")]) == 0
    monkeypatch.setattr(ml, 'MAX_LISTS', 3)
    lib2 = MusicLibrary(None)
    for n in range(3):
        lib2.add_list(f"L{n}")
    with pytest.raises(ValueError):
        lib2.add_list("Una más")


def test_titulo_largo_se_recorta():
    lib = MusicLibrary(None)
    lid = lib.add_list("x" * 500)
    assert len(lib.get_list(lid)['name']) == ml.MAX_LIST_NAME
    lib.add_entries(lid, [Entry("http://h/a.mp3", "t\x00\n" + "y" * 500)])
    assert len(lib.get_list(lid)['items'][0]['title']) == ml.MAX_TITLE


def test_normalize_descarta_basura_y_renueva_ids():
    assert normalize_music("basura") == ml.default_music()
    raw = {
        'lists': [
            {'id': 'a', 'name': 'Uno', 'items': [
                {'id': 'i1', 'source': 'http://h/1.mp3', 'title': 'T1', 'live': 'x',
                 'bitrate': True, 'kind': 'file'},
                {'id': 'i1', 'source': 'http://h/2.mp3'},                 # id repetido
                {'id': 'i3', 'source': 'http://h/1.mp3'},                 # fuente repetida
                {'id': 'i4', 'source': r'\\srv\x.mp3'},                   # UNC
                'no soy un item', {'source': 42}]},
            {'id': 'a', 'name': '', 'items': 'nada'},                     # id de lista repetido
            {'id': 'lofi', 'name': 'Falsa lofi', 'items': []},            # id reservado
            None,
        ],
        'current': {'list': 'a', 'item': 'i3'},
        'volume': 7, 'shuffle': 'si', 'repeat': 'todo', 'focus_only': True,
    }
    out = normalize_music(raw)
    assert [x['name'] for x in out['lists']] == ['Uno', 'Lista sin nombre', 'Falsa lofi']
    ids = [x['id'] for x in out['lists']]
    assert ids[0] == 'a' and len(set(ids)) == 3 and 'lofi' not in ids
    items = out['lists'][0]['items']
    assert [i['source'] for i in items] == ['http://h/1.mp3', 'http://h/2.mp3']
    assert items[0]['kind'] == 'url' and items[0]['live'] is None and items[0]['bitrate'] == 0
    assert items[1]['id'] != 'i1' and items[1]['title'] == '2'
    assert out['current'] == {'list': 'a', 'item': None}     # i3 se descartó
    assert (out['volume'], out['shuffle'], out['repeat'], out['focus_only']) == (1.0, False,
                                                                                'all', True)
    lofi = normalize_music({'current': {'list': 'lofi', 'item': 'tema.mp3'}})
    assert lofi['current'] == {'list': 'lofi', 'item': 'tema.mp3'}


# ------------------------------------------------------------------ persistencia
def _music_sample():
    lib = MusicLibrary(None)
    lid = lib.add_list("Radios")
    lib.add_entries(lid, [Entry("https://h/r.mp3", "Radio", live=True, bitrate=320,
                                mime="audio/mpeg")])
    lib.set_current(lid, lib.get_list(lid)['items'][0]['id'])
    data = lib.to_dict()
    data.update(volume=0.25, shuffle=True, repeat='one', focus_only=True)
    return data


def test_ida_y_vuelta_con_data_handler(tmp_path):
    path = tmp_path / "data.json"
    dh = DataHandler(path)
    assert dh.get_music() == ml.default_music()
    music = _music_sample()
    dh.save_music(music)
    again = DataHandler(path).get_music()
    assert again == music
    # get_music devuelve una copia: modificarla no toca los datos
    again['lists'].clear()
    assert DataHandler(path).get_music()['lists']


def test_load_valida_la_musica_del_archivo(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({'tasks': [], 'music': {
        'lists': [{'id': 'x', 'name': 'L', 'items': [{'source': 'ftp://nope'}]}],
        'volume': 'alto'}}), encoding='utf-8')
    music = DataHandler(path).get_music()
    assert music['lists'][0]['items'] == [] and music['volume'] == ml.DEFAULT_VOLUME


def test_import_data_conserva_la_musica_si_el_backup_no_trae(tmp_path):
    dh = DataHandler(tmp_path / "data.json")
    sample = _music_sample()
    dh.save_music(sample)
    dh.import_data({'tasks': [], 'settings': {}})
    assert dh.get_music() == sample
    dh.import_data({'tasks': [], 'settings': {}, 'music': 'roto'})
    assert dh.get_music() == sample
    # Un backup con biblioteca válida sí la reemplaza
    dh.import_data({'tasks': [], 'settings': {}, 'music': {'lists': [], 'volume': 0.9}})
    assert dh.get_music()['lists'] == [] and dh.get_music()['volume'] == 0.9


def test_borrar_todo_conserva_la_musica(tmp_path):
    dh = DataHandler(tmp_path / "data.json")
    sample = _music_sample()
    dh.save_music(sample)
    dh.clear_data()
    assert dh.get_music() == sample


# ------------------------------------------------------------------ revisión
def test_clean_text_quita_controles_c1_y_marcas_de_direccion():
    assert ml.clean_text("a\x92b\u202ec\u2066d\u200f", 50) == "a bcd"
    # U+202E pintaría «Lofi oidar» como «Lofi radio»
    assert ml.clean_text("Lofi \u202eoidar", 50) == "Lofi oidar"
    assert ml.clean_text("Caf\xe9 \u2013 \u201cdirecto\u201d", 50) == "Caf\xe9 \u2013 \u201cdirecto\u201d"


HLS = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"


@pytest.mark.parametrize("body", [
    "#EXTINF:7,\n\\\\servidor\\recurso\\seg.mp3\n",
    "#EXTINF:7,\n//servidor/recurso/seg.mp3\n",
    "#EXTINF:7,\n\\\\?\\C:\\secreto.mp3\n",
    "#EXTINF:7,\nC:\\Windows\\media\\tada.wav\n",
    "#EXTINF:7,\n/C:/x.mp3\n",
    "#EXTINF:7,\n..\\fuera\\seg.ts\n",
    "#EXTINF:7,\nsub/../../fuera.ts\n",
    "#EXTINF:7,\nfile://servidor/x.ts\n",
    "#EXTINF:7,\ndata:audio/mpeg;base64,AAAA\n",
    '#EXT-X-KEY:METHOD=AES-128,URI="\\\\servidor\\k.key"\n#EXTINF:7,\nseg.ts\n',
    "#EXT-X-MAP:URI=\\\\servidor\\init.mp4\n#EXTINF:7,\nseg.ts\n",
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="//srv/a.m3u8"\nvariante.m3u8\n',
])
def test_hls_local_que_apunta_fuera_no_vale(body):
    assert not ml.local_hls_ok(HLS + body)


def test_hls_local_relativo_o_http_vale():
    assert ml.local_hls_ok(HLS + "#EXTINF:7,\nseg1.ts\n#EXTINF:7,\nsub/seg2.ts\n"
                           '#EXT-X-KEY:METHOD=AES-128,URI="clave.key"\n'
                           "#EXTINF:7,\nhttps://cdn.ejemplo.com/seg3.ts?t=1\n")
    # Sin etiquetas #EXT-X- no es HLS: FFmpeg no la abre como tal
    assert ml.local_hls_ok("#EXTM3U\n\\\\servidor\\x.mp3\n")
    assert ml.hls_refs(HLS + '#EXT-X-KEY:URI="k.key"\n#EXTINF:7,\na.ts\n') == ["k.key", "a.ts"]


def test_limite_de_texto_por_lista_y_biblioteca(monkeypatch):
    """Una playlist con URLs largas no puede inflar el JSON a megas."""
    lib = MusicLibrary(None)
    lid = lib.add_list("Grande")
    long_url = "https://ejemplo.com/" + "a" * 1900 + "/{}.mp3"
    entries = [Entry(long_url.format(n), title="t") for n in range(600)]
    added = lib.add_entries(lid, entries)
    assert 0 < added < 600 and lib.limit_hit == 'list'
    text = sum(len(i['source']) + len(i['title']) for i in lib.get_list(lid)['items'])
    assert text <= ml.MAX_LIST_TEXT
    # Y la biblioteca entera tiene su tope
    monkeypatch.setattr(ml, "MAX_LIBRARY_TEXT", text + 5000)
    other = lib.add_list("Otra")
    assert lib.add_entries(other, entries[:20]) < 20 and lib.limit_hit == 'library'
    # Al cargar datos de fuera se aplica lo mismo
    raw = {'lists': [{'id': 'x', 'name': 'X', 'items': [
        {'source': long_url.format(n), 'title': 't'} for n in range(600)]}]}
    monkeypatch.setattr(ml, "MAX_LIBRARY_TEXT", 4 * 1024 * 1024)
    loaded = normalize_music(raw)['lists'][0]['items']
    assert 0 < len(loaded) < 600

