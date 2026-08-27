"""Escolha da melhor faixa de áudio por idioma."""
from services import merger


def _a(codec, ch=2, sr=48000, br=0, profile=None):
    s = {"codec_name": codec, "channels": ch, "sample_rate": str(sr),
         "bit_rate": str(br) if br else None}
    if profile:
        s["profile"] = profile
    return s


def test_dts_ganha_de_ac3():
    """O ffprobe chama DTS de 'dts' (a tabela só tinha o nome antigo 'dca'):
    um DTS-HD MA 7.1 pontuava zero e perdia para um AC3 5.1 — caso real, o
    inglês de um REMUX perdendo para o do arquivo dublado."""
    dts = _a("dts", ch=8, profile="DTS-HD MA")
    ac3 = _a("ac3", ch=6, br=640000)
    assert merger.audio_score(dts) > merger.audio_score(ac3)
    assert max([ac3, dts], key=merger.audio_score) is dts


def test_lossless_ganha_do_core_do_mesmo_codec():
    """Mesmo codec, mesmos canais: o profile lossless desempata."""
    ma = _a("dts", ch=6, profile="DTS-HD MA")
    core = _a("dts", ch=6, br=1500000)
    assert merger.audio_score(ma) > merger.audio_score(core)
    # e continua abaixo do TrueHD
    assert merger.audio_score(_a("truehd", ch=8)) > merger.audio_score(ma)


def test_ordem_geral_dos_codecs():
    ordem = ["truehd", "flac", "dts", "eac3", "ac3", "aac", "mp3"]
    scores = [merger.audio_score(_a(c, ch=6)) for c in ordem]
    assert scores == sorted(scores, reverse=True), list(zip(ordem, scores))
    # codec desconhecido não vence ninguém conhecido
    assert merger.audio_score(_a("xyz", ch=8)) < merger.audio_score(_a("mp3"))


def test_melhor_por_idioma_entre_dois_arquivos():
    """O caso do job: arquivo1 traz o inglês DTS-HD MA, arquivo2 traz o
    português e um inglês AC3 — o inglês tem que vir do arquivo1."""
    p1 = {"streams": [
        {"codec_type": "audio", "codec_name": "dts", "channels": 8,
         "sample_rate": "48000", "profile": "DTS-HD MA",
         "tags": {"language": "eng"}, "index": 1}]}
    p2 = {"streams": [
        {"codec_type": "audio", "codec_name": "ac3", "channels": 6,
         "sample_rate": "48000", "bit_rate": "640000",
         "tags": {"language": "por"}, "index": 1},
        {"codec_type": "audio", "codec_name": "ac3", "channels": 6,
         "sample_rate": "48000", "bit_rate": "640000",
         "tags": {"language": "eng"}, "index": 2}]}
    for p in (p1, p2):
        merger.annotate_type_indexes(p)
    best = merger.choose_best_audio_per_language([p1, p2], {})
    assert best["eng"][0] == 0, "o inglês tem que vir do arquivo do vídeo"
    assert best["por"][0] == 1
