"""Legendas que o Matroska não guarda ficam de fora do mux.

Caso real: um release iTunes traz closed captions EIA-608 como STREAM própria
ao lado da legenda de texto. Nenhum encoder do ffmpeg converte 608 para o
MKV, e a tentativa de copiar derruba o mux INTEIRO ("Subtitle codec 94218 is
not supported") — o episódio não sai. A defesa é descartar na origem."""
from services import merger


def _s(codec, lang="eng", index=2):
    return {"codec_type": "subtitle", "codec_name": codec, "index": index,
            "tags": {"language": lang}}


def _probe(*streams):
    p = {"streams": [{"codec_type": "video", "codec_name": "h264", "index": 0},
                     {"codec_type": "audio", "codec_name": "aac", "index": 1,
                      "channels": 2, "sample_rate": "48000",
                      "tags": {"language": "eng"}}, *streams]}
    merger.annotate_type_indexes(p)
    return p


def test_closed_captions_saem_da_lista():
    p = _probe(_s("eia_608", index=2), _s("mov_text", index=3))
    assert [s["codec_name"] for s in merger.usable_subtitles(p)] == ["mov_text"]
    assert len(merger.get_streams(p, "subtitle")) == 2, "o probe segue intacto"


def test_indice_de_tipo_da_legenda_boa_nao_desanda():
    """O descarte não pode renumerar o que sobra: o mapeamento usa o índice
    POR TIPO do arquivo de origem, com a CC ainda ocupando o dela."""
    p = _probe(_s("eia_608", index=2), _s("subrip", index=3))
    (boa,) = merger.usable_subtitles(p)
    assert boa["_type_index"] == 1


def test_escolha_por_idioma_ignora_a_cc():
    p = _probe(_s("eia_608", index=2), _s("subrip", index=3))
    escolhidas = merger.pick_subs_for_lang([p, _probe()], "eng", video_src=0)
    assert [s["codec_name"] for _i, s in escolhidas] == ["subrip"]


def test_cc_sozinha_nao_vira_faixa():
    p = _probe(_s("eia_608", index=2))
    assert merger.pick_subs_for_lang([p, _probe()], "eng", video_src=0) == []
