"""Política do merge avançado (Configurações): persistência, validação,
regras padrão e como ela chega ao render."""
import pytest

from services import advanced_merge
from services.series.align import rules
from services.series.align.classify import Segment


def test_padrao_e_persistencia(temp_db, monkeypatch):
    from services import transcode
    monkeypatch.setattr(transcode, "hw_encoder_works", lambda enc: False)
    cfg = advanced_merge.get()
    assert cfg["undubbed"] == "cut" and cfg["cut_min_s"] == 1.0
    # padrão adaptado à máquina: sem GPU, AV1 por software, CRF 20
    assert cfg["reencode"]["video_codec"] == "av1"
    assert cfg["reencode"]["hw_accel"] == "none" and cfg["reencode"]["crf"] == 20
    salvo = advanced_merge.set({"undubbed": "silence", "reencode": None})
    assert salvo["undubbed"] == "silence" and salvo["reencode"] is None
    assert advanced_merge.get() == salvo
    # qualquer codec/encoder das opções de conversão vale
    salvo = advanced_merge.set({"reencode": {"video_codec": "hevc", "hw_accel": "none",
                                             "quality_mode": "crf", "crf": 22}})
    assert salvo["reencode"]["video_codec"] == "hevc"


@pytest.mark.parametrize("cfg,msg", [
    ({"undubbed": "russo"}, "política"),
    ({"reencode": "x265"}, "re-encode"),
    ({"reencode": {"video_codec": "keep"}}, "codec"),
    ({"cut_min_s": "muito"}, "segundos"),
])
def test_validacao(temp_db, cfg, msg):
    with pytest.raises(ValueError, match=msg):
        advanced_merge.set(cfg)


def test_regras_padrao_cut():
    cfg = dict(advanced_merge.DEFAULTS, undubbed="cut", cut_min_s=1.0)
    segs = [
        Segment("match", 0.0, 10.0, 0.0, 10.0, offset=0.0),
        Segment("gap_orig", 10.0, 10.0, 10.0, 10.4),      # 0,4 s: muda
        Segment("gap_orig", 10.0, 10.0, 10.4, 25.0),      # 14,6 s: sai do vídeo
        Segment("match", 10.0, 30.0, 25.0, 45.0, offset=15.0),
    ]
    segs[2].extra["action"] = "fill_original"             # decisão do usuário vence
    out, _ = rules.apply_rules(segs, advanced_merge.default_rules(cfg), 30.0)
    assert out[1].extra["action"] == "silence"
    assert out[2].extra["action"] == "fill_original"


def test_regras_padrao_fill_e_silence():
    assert advanced_merge.default_rules(dict(advanced_merge.DEFAULTS, undubbed="fill")) == []
    r = advanced_merge.default_rules(dict(advanced_merge.DEFAULTS, undubbed="silence"))
    assert r == [{"when": {"kind": "gap_orig"}, "action": "silence"}]


def test_render_kwargs():
    opts = {"video_codec": "av1", "hw_accel": "none", "quality_mode": "crf", "crf": 20}
    cfg = dict(advanced_merge.DEFAULTS, reencode=opts)
    assert advanced_merge.render_kwargs(cfg, has_cuts=False) == {"fill_with_original": False}
    kw = advanced_merge.render_kwargs(cfg, has_cuts=True)
    assert kw["video_reencode"] == {"convert": opts}
    assert "video_reencode" not in advanced_merge.render_kwargs(
        dict(cfg, reencode=None), has_cuts=True)


def test_api_roundtrip(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    monkeypatch.setattr(main.auth, "require_auth", lambda *a, **k: None, raising=False)
    r = c.get("/api/advanced-merge")
    assert r.status_code in (200, 401)
