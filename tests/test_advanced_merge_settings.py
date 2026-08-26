"""Política do merge avançado (Configurações): persistência, validação,
regras padrão e como ela chega ao render."""
import pytest

from services import advanced_merge
from services.series.align import rules
from services.series.align.classify import Segment


def test_padrao_e_persistencia(temp_db):
    assert advanced_merge.get() == advanced_merge.DEFAULTS
    salvo = advanced_merge.set({"undubbed": "silence", "reencode": "none", "quality": 24})
    assert salvo["undubbed"] == "silence" and salvo["reencode"] == "none"
    assert advanced_merge.get() == salvo


@pytest.mark.parametrize("cfg,msg", [
    ({"undubbed": "russo"}, "política"),
    ({"reencode": "x265"}, "re-encode"),
    ({"quality": 99}, "faixa"),
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
    cfg = dict(advanced_merge.DEFAULTS)
    assert advanced_merge.render_kwargs(cfg, has_cuts=False) == {"fill_with_original": False}
    kw = advanced_merge.render_kwargs(cfg, has_cuts=True)
    assert kw["video_reencode"] == {"codec": "av1", "encoder": "auto", "crf": 20}
    assert "video_reencode" not in advanced_merge.render_kwargs(
        dict(cfg, reencode="none"), has_cuts=True)


def test_api_roundtrip(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(advanced_merge, "encoders_available",
                        lambda: {"av1_qsv": False, "libsvtav1": True})
    c = TestClient(main.app)
    monkeypatch.setattr(main.auth, "require_auth", lambda *a, **k: None, raising=False)
    r = c.get("/api/advanced-merge")
    assert r.status_code in (200, 401)
