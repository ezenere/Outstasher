"""Extração do título em inglês (tmdb._english_title) — lógica pura, sem rede.

Filmes cujo original não é inglês (anime, cinema europeu) são indexados nos
trackers pelo nome em inglês; a busca da versão original usa esse título também.
"""
from services import tmdb


def _data(original, orig_lang, translations):
    return {"original_title": original, "original_language": orig_lang,
            "translations": {"translations": translations}}


def _tr(lang, title):
    return {"iso_639_1": lang, "data": {"title": title}}


def test_english_title_for_foreign_film():
    d = _data("Kaze no Monogatari", "ja",
              [_tr("en", "Tale of the Wind"), _tr("pt", "A História do Vento")])
    assert tmdb._english_title(d) == "Tale of the Wind"


def test_none_when_original_is_english():
    # filme já em inglês: não há nada a ganhar buscando "o inglês"
    d = _data("Example", "en", [_tr("en", "Example")])
    assert tmdb._english_title(d) is None


def test_none_when_english_equals_original():
    # original não-inglês mas o "título inglês" coincide (ex.: nome próprio):
    # buscar de novo seria redundante
    d = _data("Exemple", "fr", [_tr("en", "Exemple")])
    assert tmdb._english_title(d) is None


def test_none_when_no_english_translation():
    d = _data("Das Boot", "de", [_tr("pt", "O Barco")])
    assert tmdb._english_title(d) is None


def test_handles_missing_translations():
    # respostas do TMDB sem o bloco translations não podem quebrar
    assert tmdb._english_title({"original_title": "X", "original_language": "ja"}) is None
    assert tmdb._english_title({"original_title": "X", "original_language": "ja",
                                "translations": {}}) is None
    assert tmdb._english_title({"original_title": "X", "original_language": "ja",
                                "translations": {"translations": [
                                    {"iso_639_1": "en", "data": {}}]}}) is None
