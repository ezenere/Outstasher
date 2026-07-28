"""Presets de conversão: ConvertOptions nomeadas, salvas no banco.

Exercitam o store de verdade (fixture temp_db, SQLite real) — sem stubs.
"""
from services import transcode


def _opts(**over) -> dict:
    base = {
        "video_codec": "av1", "hw_accel": "none", "preset": "default",
        "resolution": "keep", "quality_mode": "crf", "video_bitrate": None,
        "crf": 20, "bit_depth": "keep", "audio_tracks": "all",
        "audio_codec": "keep", "audio_bitrate": None, "channels": "keep",
        "subtitles": "default",
    }
    return {**base, **over}


def test_create_list_delete(temp_db):
    assert temp_db.list_convert_presets() == []

    p = temp_db.create_convert_preset("AV1 CRF 20", _opts())
    assert p["name"] == "AV1 CRF 20"
    assert p["options"]["crf"] == 20

    listed = temp_db.list_convert_presets()
    assert [x["name"] for x in listed] == ["AV1 CRF 20"]
    assert listed[0]["options"] == _opts()

    assert temp_db.delete_convert_preset(p["id"]) is True
    assert temp_db.list_convert_presets() == []
    assert temp_db.delete_convert_preset(p["id"]) is False  # já foi


def test_same_name_overwrites(temp_db):
    """Salvar de novo com o mesmo nome atualiza — nunca duplica na lista (o
    nome é a identidade do preset para quem escolhe no dropdown)."""
    first = temp_db.create_convert_preset("QSV AV1", _opts(crf=20))
    second = temp_db.create_convert_preset("QSV AV1", _opts(crf=28))

    assert first["id"] == second["id"]
    presets = temp_db.list_convert_presets()
    assert len(presets) == 1
    assert presets[0]["options"]["crf"] == 28


def test_listed_alphabetically(temp_db):
    for name in ("zeta", "Alpha", "meio"):
        temp_db.create_convert_preset(name, _opts())
    assert [p["name"] for p in temp_db.list_convert_presets()] == ["Alpha", "meio", "zeta"]


def test_options_survive_roundtrip(temp_db):
    """As opções voltam idênticas ao que entrou (JSON opaco, sem coerção)."""
    opts = _opts(video_codec="hevc", hw_accel="qsv", preset="veryslow",
                 quality_mode="bitrate", video_bitrate=8000, crf=None,
                 audio_codec="opus", audio_bitrate=256, channels="surround51",
                 subtitles="none", audio_tracks="target", bit_depth="10")
    temp_db.create_convert_preset("completo", opts)
    assert temp_db.list_convert_presets()[0]["options"] == opts


def test_saved_options_are_valid_for_transcode(temp_db):
    """O que sai do preset alimenta transcode.validate() sem adaptação — é
    exatamente o payload que os modais enviam ao converter."""
    temp_db.create_convert_preset("software av1", _opts())
    stored = temp_db.list_convert_presets()[0]["options"]
    o = transcode.validate(stored)
    assert o.video_codec == "av1" and o.crf == 20
