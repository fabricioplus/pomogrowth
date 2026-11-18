"""Radios lofi de serie (core/radio_catalog.py): siembra y catálogo, sin red.

La siembra está apagada en toda la suite (tests/conftest.py,
_sin_radios_de_serie): aquí se enciende con el fixture `sembrar`, que pone un
catálogo de URLs de 127.0.0.1 a un puerto que nadie abre. Nada de este
archivo reproduce: sembrar es solo datos. El catálogo real solo se inspecciona
(test_catalogo_real_*), nunca se conecta.
"""
import json
from urllib.parse import urlsplit

import pytest

from core import music_library as ml
from core import radio_catalog as rc
from core.data_handler import DataHandler
from core.music_library import Entry, MusicLibrary, normalize_music

CAT = (rc.DefaultRadio('r1', "Uno", "http://127.0.0.1:9/uno.mp3", 'audio/mpeg', 128),
       rc.DefaultRadio('r2', "Dos", "http://127.0.0.1:9/dos.aac", 'audio/aac', 64))
# Una radio que llega en una versión futura
TRES = rc.DefaultRadio('r3', "Tres", "http://127.0.0.1:9/tres.mp3", 'audio/mpeg', 128)


@pytest.fixture
def sembrar(monkeypatch):
    monkeypatch.setattr(DataHandler, "SEED_DEFAULT_RADIOS", True)
    monkeypatch.setattr(rc, "DEFAULT_RADIOS", CAT)


def _seed_list(music):
    return next((x for x in music['lists'] if x['id'] == rc.SEED_LIST_ID), None)


def _seed_titles(music):
    lst = _seed_list(music)
    return [] if lst is None else [i['title'] for i in lst['items']]


def _write(path, music=None, **extra):
    data = {'tasks': [], **extra}
    if music is not None:
        data['music'] = music
    path.write_text(json.dumps(data), encoding='utf-8')


def _saved_music(path):
    return json.loads(path.read_text(encoding='utf-8'))['music']


# ------------------------------------------------------------------ siembra
def test_la_suite_las_tiene_apagadas(tmp_path):
    """Sin el fixture de conftest, cualquier test de UI que pulsara reproducir
    sacaría a FFmpeg a internet (la guarda de red no lo ve)."""
    assert DataHandler.SEED_DEFAULT_RADIOS is False
    music = DataHandler(tmp_path / "d.json").get_music()
    assert music['lists'] == [] and music['seeds'] == []


def test_usuario_nuevo_recibe_las_radios_en_su_lista(tmp_path, sembrar):
    path = tmp_path / "d.json"
    dh = DataHandler(path)
    music = dh.get_music()
    assert [x['name'] for x in music['lists']] == [rc.SEED_LIST_NAME]
    items = _seed_list(music)['items']
    assert [(i['kind'], i['source'], i['title'], i['live'], i['mime'], i['bitrate'])
            for i in items] == [
        ('url', CAT[0].source, "Uno", True, 'audio/mpeg', 128),
        ('url', CAT[1].source, "Dos", True, 'audio/aac', 64)]
    assert music['seeds'] == ['r1', 'r2']
    assert music['current'] == {'list': None, 'item': None}     # nada elegido
    # Instalación nueva: ni se escribe el archivo para esto ni se salta la
    # presentación del primer arranque
    assert not path.exists()
    assert dh.data['settings']['onboarding_seen'] is False


def test_usuario_existente_las_recibe_al_final_una_sola_vez(tmp_path, sembrar):
    path = tmp_path / "d.json"
    lib = MusicLibrary(None)
    lid = lib.add_list("Mías")
    lib.add_entries(lid, [Entry("https://h/r.mp3", "Mía", live=True)])
    lib.set_current(lid, lib.lists[0]['items'][0]['id'])
    _write(path, lib.to_dict())
    DataHandler(path)                      # migra y guarda al arrancar
    raw = _saved_music(path)
    assert [x['name'] for x in raw['lists']] == ["Mías", rc.SEED_LIST_NAME]
    assert raw['seeds'] == ['r1', 'r2']
    assert raw['current']['list'] == lid   # lo que sonaba no cambia
    again = DataHandler(path).get_music()
    assert _seed_titles(again) == ["Uno", "Dos"]       # sin duplicados al reabrir
    assert again == normalize_music(raw)


def test_un_archivo_sin_musica_tambien_las_recibe(tmp_path, sembrar):
    """Quien viene de una versión sin música (o sin la clave) también."""
    path = tmp_path / "d.json"
    _write(path)
    DataHandler(path)
    assert _seed_titles(_saved_music(path)) == ["Uno", "Dos"]


def test_una_radio_quitada_no_vuelve_ni_la_lista_borrada(tmp_path, sembrar):
    path = tmp_path / "d.json"
    dh = DataHandler(path)
    lib = MusicLibrary(dh.get_music())
    first = _seed_list(lib.data)['items'][0]['id']
    assert lib.remove_item(rc.SEED_LIST_ID, first)
    dh.save_music(lib.to_dict())
    assert _seed_titles(DataHandler(path).get_music()) == ["Dos"]
    dh = DataHandler(path)
    lib = MusicLibrary(dh.get_music())
    assert lib.delete_list(rc.SEED_LIST_ID)
    dh.save_music(lib.to_dict())
    music = DataHandler(path).get_music()
    assert music['lists'] == [] and music['seeds'] == ['r1', 'r2']


def test_una_radio_nueva_va_a_la_lista_aunque_este_renombrada(tmp_path, sembrar,
                                                             monkeypatch):
    path = tmp_path / "d.json"
    dh = DataHandler(path)
    lib = MusicLibrary(dh.get_music())
    lib.rename_list(rc.SEED_LIST_ID, "Mis radios")
    lib.add_list("Otra")
    dh.save_music(lib.to_dict())
    monkeypatch.setattr(rc, "DEFAULT_RADIOS", CAT + (
        rc.DefaultRadio('r3', "Tres", "http://127.0.0.1:9/tres.mp3", 'audio/mpeg', 128),))
    music = DataHandler(path).get_music()
    assert [x['name'] for x in music['lists']] == ["Mis radios", "Otra"]
    assert _seed_titles(music) == ["Uno", "Dos", "Tres"]


def test_una_radio_nueva_llega_aunque_borrara_la_lista(tmp_path, sembrar, monkeypatch):
    path = tmp_path / "d.json"
    dh = DataHandler(path)
    lib = MusicLibrary(dh.get_music())
    lib.delete_list(rc.SEED_LIST_ID)
    dh.save_music(lib.to_dict())
    monkeypatch.setattr(rc, "DEFAULT_RADIOS", CAT + (
        rc.DefaultRadio('r3', "Tres", "http://127.0.0.1:9/tres.mp3", 'audio/mpeg', 128),))
    music = DataHandler(path).get_music()
    assert _seed_titles(music) == ["Tres"]
    assert music['seeds'] == ['r1', 'r2', 'r3']


def test_no_duplica_una_que_el_usuario_ya_tenia(tmp_path, sembrar):
    path = tmp_path / "d.json"
    lib = MusicLibrary(None)
    lid = lib.add_list("Mías")
    lib.add_entries(lid, [Entry(CAT[0].source, "La mía")])
    _write(path, lib.to_dict())
    music = DataHandler(path).get_music()
    assert _seed_titles(music) == ["Dos"]
    assert [i['title'] for i in music['lists'][0]['items']] == ["La mía"]
    assert music['seeds'] == ['r1', 'r2']


@pytest.mark.parametrize("mine", [
    "https://stream.bigfm.de/lofifocus/aac-128",            # sin la barra final
    "http://ec3.yesstreaming.net:3755/stream",              # http en vez de https
    "https://EC3.yesstreaming.net:3755/stream",             # host en mayúsculas
    "HTTPS://radio.nia.nc:443/radio/8020/lofi-hq-stream.aac/",  # puerto por defecto
])
def test_no_duplica_la_misma_emisora_escrita_algo_distinta(mine):
    """Quien ya escuchaba una de las radios de serie con su URL escrita con
    una minucia de diferencia no la ve dos veces al actualizar."""
    music = normalize_music({'lists': [{'id': 'a', 'name': "Mías",
                                        'items': [{'source': mine, 'title': "La mía"}]}]})
    assert rc.apply_default_radios(music) is True
    assert len(_seed_list(music)['items']) == len(rc.DEFAULT_RADIOS) - 1
    assert [i['title'] for i in music['lists'][0]['items']] == ["La mía"]
    assert music['seeds'] == [r.id for r in rc.DEFAULT_RADIOS]


@pytest.mark.parametrize("other", [
    "https://stream.bigfm.de/lofifocus/mp3-128/",           # otro montaje
    "https://ec3.yesstreaming.net:3756/stream",             # otro puerto
    "https://radio.nia.nc/radio/8020/lofi-hq-stream.aac?x=1",   # otra consulta
    "https://radio.nia.nc/radio/8020/LOFI-hq-stream.aac",   # la ruta sí distingue
])
def test_una_url_parecida_de_otra_emisora_no_la_tapa(other):
    music = normalize_music({'lists': [{'id': 'a', 'name': "Mías",
                                        'items': [{'source': other, 'title': "Otra"}]}]})
    rc.apply_default_radios(music)
    assert len(_seed_list(music)['items']) == len(rc.DEFAULT_RADIOS)


def test_station_key_solo_iguala_minucias():
    key = ml.station_key
    assert key("https://h.example/s/") == key("http://H.example:80/s") == "//h.example/s"
    assert key("https://h.example:443/s") == key("https://h.example/s")
    assert key("http://h.example:443/s") != key("https://h.example/s")   # no es su puerto
    assert key("https://h.example:8000/s") == "//h.example:8000/s"
    assert key("https://h.example/s?a=1#frag") == "//h.example/s?a=1"
    assert key("https://[::1]:8000/s") == "//[::1]:8000/s"
    assert key("https://h.example:99999/s") == "https://h.example:99999/s"   # puerto roto
    # Lo que no es http(s) se compara como hasta ahora (source_key)
    assert key(r"C:\Música\a.mp3") == ml.source_key(r"C:\Música\a.mp3")
    # Y source_key, el del resto de la app, no cambia: añadir a mano sigue
    # distinguiendo lo que el usuario escribió distinto
    assert ml.source_key("https://h.example/s/") != ml.source_key("https://h.example/s")


def test_borrar_todo_no_resucita_nada(tmp_path, sembrar):
    path = tmp_path / "d.json"
    dh = DataHandler(path)
    lib = MusicLibrary(dh.get_music())
    lib.delete_list(rc.SEED_LIST_ID)
    dh.save_music(lib.to_dict())
    dh.clear_data()
    assert dh.get_music()['lists'] == []
    assert DataHandler(path).get_music()['lists'] == []


def test_importar_un_backup(tmp_path, sembrar):
    path = tmp_path / "d.json"
    dh = DataHandler(path)
    # Backup anterior a las radios de serie (música sin marca) en quien las
    # conserva: se ofrecen, y queda guardado (no solo en memoria)
    dh.import_data({'tasks': [], 'settings': {}, 'music': {'lists': [], 'volume': 0.9}})
    assert _seed_titles(dh.get_music()) == ["Uno", "Dos"]
    assert _seed_titles(_saved_music(path)) == ["Uno", "Dos"]
    assert _saved_music(path)['volume'] == 0.9
    lib = MusicLibrary(dh.get_music())
    lib.delete_list(rc.SEED_LIST_ID)
    dh.save_music(lib.to_dict())
    # El mismo backup cuando ya las quitó: no vuelven
    dh.import_data({'tasks': [], 'settings': {}, 'music': {'lists': [], 'volume': 0.9}})
    assert dh.get_music()['lists'] == [] and _saved_music(path)['lists'] == []
    assert _saved_music(path)['seeds'] == ['r1', 'r2']
    # Backup con la marca: se respeta tal cual
    dh.import_data({'tasks': [], 'settings': {},
                    'music': {'lists': [], 'seeds': ['r1', 'r2']}})
    assert dh.get_music()['lists'] == [] and _saved_music(path)['lists'] == []
    # Backup sin música válida: se conserva la actual, con su marca
    dh.import_data({'tasks': [], 'settings': {}})
    assert dh.get_music()['lists'] == [] and dh.get_music()['seeds'] == ['r1', 'r2']
    assert DataHandler(path).get_music()['lists'] == []


def test_un_backup_anterior_no_resucita_las_que_quito(tmp_path, sembrar, monkeypatch):
    """Quien actualiza, quita una radio de serie y restaura un backup de la
    1.0.1 (con música, sin 'seeds') recupera lo suyo y las radios que
    conservaba, no la que quitó."""
    path = tmp_path / "d.json"
    dh = DataHandler(path)
    lib = MusicLibrary(dh.get_music())
    lib.remove_item(rc.SEED_LIST_ID, _seed_list(lib.data)['items'][0]['id'])   # «Uno»
    lib.rename_list(rc.SEED_LIST_ID, "Mis radios")     # renombrada: sigue contando
    dh.save_music(lib.to_dict())
    monkeypatch.setattr(rc, "DEFAULT_RADIOS", CAT + (TRES,))
    old = {'tasks': [{'id': 1, 'name': "Del backup"}], 'settings': {},
           'music': {'lists': [{'id': 'abc', 'name': "Mías", 'items': []}]}}
    dh.import_data(old)
    assert 'seeds' not in old['music']                  # el backup no se toca
    for music in (dh.get_music(), _saved_music(path)):
        assert [x['name'] for x in music['lists']] == ["Mías", rc.SEED_LIST_NAME]
        assert _seed_titles(music) == ["Dos", "Tres"]
        assert music['seeds'] == ['r1', 'r2', 'r3']
    assert [t['name'] for t in dh.data['tasks']] == ["Del backup"]


def test_declined_radios(monkeypatch):
    music = normalize_music({'seeds': ['r1', 'r2', 'vieja'], 'lists': [
        {'id': 'a', 'name': "A", 'items': [
            {'source': "https://127.0.0.1:9/dos.aac/", 'title': "Mi Dos"}]}]})
    # r1 no está, r2 sí (escrita algo distinta) y 'vieja' ya no es del catálogo
    assert rc.declined_radios(music, CAT) == ['r1', 'vieja']
    assert rc.declined_radios(normalize_music(None), CAT) == []
    monkeypatch.setattr(rc, "DEFAULT_RADIOS", CAT[:1])
    assert rc.declined_radios(music) == ['r1', 'r2', 'vieja']


def test_si_leer_lo_quitado_falla_se_importa_igual(tmp_path, sembrar, monkeypatch,
                                                   capsys):
    from core import data_handler

    def roto(music, catalog=None):
        raise RuntimeError("roto")
    monkeypatch.setattr(data_handler, "declined_radios", roto)
    dh = DataHandler(tmp_path / "d.json")
    dh.import_data({'tasks': [{'id': 1, 'name': "T"}], 'settings': {},
                    'music': {'lists': []}})
    assert [t['name'] for t in dh.data['tasks']] == ["T"]
    assert _seed_titles(dh.get_music()) == ["Uno", "Dos"]   # lo peor: vuelven
    assert "radios de serie se quitaron" in capsys.readouterr().out


def test_normalize_conserva_la_marca_saneada():
    out = normalize_music({'lists': [], 'seeds': ['a', 'a', 7, '', 'x' * 65, 'b', None]})
    assert out['seeds'] == ['a', 'b']
    assert normalize_music({'seeds': 'r1'})['seeds'] == []
    assert normalize_music({'lists': []})['seeds'] == []
    assert normalize_music(None) == ml.default_music()
    many = [f"s{n}" for n in range(ml.MAX_SEEDS + 10)]
    assert normalize_music({'seeds': many})['seeds'] == many[:ml.MAX_SEEDS]
    # Y sobrevive a la biblioteca (es lo que guarda MusicService)
    assert MusicLibrary({'lists': [], 'seeds': ['r1']}).to_dict()['seeds'] == ['r1']


def test_con_todas_las_listas_ocupadas_solo_marca(tmp_path, sembrar):
    path = tmp_path / "d.json"
    _write(path, {'lists': [{'id': f"l{n}", 'name': f"L{n}", 'items': []}
                            for n in range(ml.MAX_LISTS)]})
    dh = DataHandler(path)
    out = dh.get_music()
    assert len(out['lists']) == ml.MAX_LISTS and _seed_list(out) is None
    assert out['seeds'] == ['r1', 'r2']          # no se reintenta en cada arranque
    # Y no solo a la vista: get_music vuelve a recortar a MAX_LISTS, así que
    # se mira lo que queda en memoria y en el archivo
    for music in (dh.data['music'], _saved_music(path)):
        assert len(music['lists']) == ml.MAX_LISTS and _seed_list(music) is None
        assert music['seeds'] == ['r1', 'r2']



def test_con_la_lista_de_serie_llena_la_nueva_solo_se_marca(monkeypatch):
    music = normalize_music(None)
    rc.apply_default_radios(music, CAT)
    monkeypatch.setattr(ml, "MAX_ITEMS_PER_LIST", len(CAT))
    assert rc.apply_default_radios(music, CAT + (TRES,)) is True
    assert _seed_titles(music) == ["Uno", "Dos"] and len(music['lists']) == 1
    assert music['seeds'] == ['r1', 'r2', 'r3']


def test_sin_texto_libre_en_la_lista_de_serie_la_nueva_solo_se_marca(monkeypatch):
    music = normalize_music(None)
    rc.apply_default_radios(music, CAT)
    used = sum(ml.item_text_size(i) for i in _seed_list(music)['items'])
    monkeypatch.setattr(ml, "MAX_LIST_TEXT", used + 1)
    assert rc.apply_default_radios(music, CAT + (TRES,)) is True
    assert _seed_titles(music) == ["Uno", "Dos"] and len(music['lists']) == 1
    assert music['seeds'] == ['r1', 'r2', 'r3']


def test_con_la_biblioteca_llena_de_texto_no_crea_una_lista_vacia(monkeypatch):
    music = normalize_music({'lists': [{'id': 'a', 'name': "A", 'items': [
        {'source': "https://h/x.mp3", 'title': "X"}]}]})
    used = ml.item_text_size(music['lists'][0]['items'][0])
    monkeypatch.setattr(ml, "MAX_LIBRARY_TEXT", used + 10)
    assert rc.apply_default_radios(music, CAT) is True
    assert [x['id'] for x in music['lists']] == ['a'] and music['seeds'] == ['r1', 'r2']


def test_una_semilla_no_valida_se_marca_sin_lista_vacia():
    music = normalize_music(None)
    bad = (rc.DefaultRadio('mala', "Mala", "ftp://h/x.mp3", 'audio/mpeg', 128),)
    assert rc.apply_default_radios(music, bad) is True
    assert music['lists'] == [] and music['seeds'] == ['mala']
    assert rc.apply_default_radios(music, bad) is False      # ya ofrecida


def test_con_la_marca_llena_no_siembra_lo_que_no_recordaria(monkeypatch):
    monkeypatch.setattr(ml, "MAX_SEEDS", 3)
    music = normalize_music({'lists': [], 'seeds': ['x', 'y']})
    assert rc.apply_default_radios(music, CAT) is True
    assert music['seeds'] == ['x', 'y', 'r1'] and _seed_titles(music) == ["Uno"]
    assert normalize_music(music) == music        # todo lo marcado se conserva


@pytest.mark.parametrize("raw", [
    "{roto",                                                   # JSON roto
    "[]",                                                      # no es un dict
    '{"tasks": [{"id": 1, "name": "MI TAREA"}], "settings": "x"}',   # estructura inesperada
], ids=["json-roto", "no-dict", "estructura"])
def test_el_archivo_que_no_se_pudo_leer_no_se_siembra_encima(tmp_path, sembrar, raw):
    """Con load_error, _load devuelve los datos por defecto: si llevaran las
    radios (o marcaran la migración), el arranque guardaría eso encima del
    archivo del usuario. Por eso la siembra va al final de _load."""
    path = tmp_path / "d.json"
    path.write_bytes(raw.encode('utf-8'))
    dh = DataHandler(path)
    assert dh.load_error
    assert _seed_list(dh.data['music']) is None and dh.data['music']['seeds'] == []
    assert dh.get_music()['lists'] == [] and dh.get_music()['seeds'] == []
    assert path.read_bytes() == raw.encode('utf-8')         # su archivo, intacto


def test_si_sembrar_falla_los_datos_cargan_igual(tmp_path, sembrar, monkeypatch, capsys):
    from core import data_handler

    def roto(music, catalog=None):
        raise RuntimeError("catálogo roto")
    monkeypatch.setattr(data_handler, "apply_default_radios", roto)
    path = tmp_path / "d.json"
    _write(path, {'lists': []}, settings={'user_name': "Ana"})
    dh = DataHandler(path)
    assert dh.load_error is None and dh.data['settings']['user_name'] == "Ana"
    assert dh.get_music()['seeds'] == []
    assert "radios de serie" in capsys.readouterr().out


# --------------------------------------------------------- el catálogo real
def test_catalogo_real_ids_estables():
    """Los ids no cambian nunca: uno nuevo para una radio ya sembrada la
    resucitaría a quien la borró."""
    assert [r.id for r in rc.DEFAULT_RADIOS] == [
        'lofi247-londres', 'bigfm-lofi-focus', 'nia-lofi-numea']
    assert rc.SEED_LIST_ID == 'radios-lofi' and rc.SEED_LIST_NAME == "Radios lofi"


def test_catalogo_real_valores_medidos():
    """Las URLs, formatos y bitrates se eligieron y midieron desde el equipo
    del usuario y quedan guardados en sus datos: un cambio sin querer (p. ej.
    la ruta mp3-128 de bigFM, que sirve MP3 o AAC al azar) necesitaría una
    migración para arreglarse."""
    assert [(r.id, r.title, r.source, r.mime, r.bitrate) for r in rc.DEFAULT_RADIOS] == [
        ('lofi247-londres', "24/7 Lofi Radio · Londres",
         "https://ec3.yesstreaming.net:3755/stream", 'audio/mpeg', 64),
        ('bigfm-lofi-focus', "bigFM LoFi Focus · Alemania",
         "https://stream.bigfm.de/lofifocus/aac-128/", 'audio/aac', 128),
        ('nia-lofi-numea', "NIA Radio LoFi · Numea",
         "https://radio.nia.nc/radio/8020/lofi-hq-stream.aac", 'audio/aac', 128),
    ]


def test_catalogo_real_es_reproducible_tal_cual():
    """Sin red: lo que se puede comprobar de cada fuente antes de dársela a
    FFmpeg tal cual (no expande playlists) y al búfer (MP3/AAC)."""
    radios = rc.DEFAULT_RADIOS
    ids = [r.id for r in radios]
    assert len(set(ids)) == len(ids) <= ml.MAX_SEEDS
    assert len({ml.source_key(r.source) for r in radios}) == len(radios)
    for r in radios:
        assert 0 < len(r.id) <= 64 and r.id.strip() == r.id, r
        assert r.id != ml.LOFI_LIST_ID
        assert 0 < len(r.title) <= ml.MAX_TITLE and ml.clean_text(r.title, ml.MAX_TITLE) == r.title
        parts = urlsplit(r.source)
        assert parts.scheme == 'https', r
        host = (parts.hostname or '').lower()
        assert host and not (host == 'radio.garden' or host.endswith('.radio.garden')), r
        assert not parts.query and not parts.fragment, r      # sin tokens que caduquen
        assert ml.classify_link(r.source).kind == 'url', r      # ni YouTube y compañía
        ext = ml.source_ext(r.source)
        assert ext not in ml.HLS_EXTS | ml.PLAYLIST_EXTS, r
        assert ml.codec_name(r.mime) in ('MP3', 'AAC'), r
        assert ext in ('', '.mp3', '.aac'), r
        assert 0 < r.bitrate <= 320, r


def test_catalogo_real_se_siembra_entero():
    music = normalize_music(None)
    assert rc.apply_default_radios(music) is True
    lst = _seed_list(music)
    assert [x['id'] for x in music['lists']] == [rc.SEED_LIST_ID]
    assert [(i['source'], i['title'], i['live'], i['mime'], i['bitrate'])
            for i in lst['items']] == [
        (r.source, r.title, True, r.mime, r.bitrate) for r in rc.DEFAULT_RADIOS]
    assert normalize_music(music) == music       # nada que el esquema descarte
