"""A EDL persistida guarda o `extra` inteiro dos segmentos (junção exata da
cena cortada), não só action/refine — o render lê a EDL, não os Segments."""
from services.series.align import edl as edl_mod
from services.series.align.classify import Segment


def test_extra_da_juncao_sobrevive_a_serializacao():
    g = Segment("gap_orig", 28.0, 28.0, 28.0, 38.0)
    g.extra.update({"action": "cut_video", "junction_a": 30.2,
                    "junction_b0": 30.25, "junction_b1": 40.25})
    m = Segment("match", 0.0, 28.0, 0.0, 28.0, offset=0.0)
    m.extra["refine"] = {"peak": 12.0}
    d = {"segments": [edl_mod._seg_dict(m), edl_mod._seg_dict(g)]}
    back = edl_mod.segments(d)
    assert back[1].extra["action"] == "cut_video"
    assert back[1].extra["junction_b0"] == 30.25 and back[1].extra["junction_b1"] == 40.25
    assert back[1].extra["junction_a"] == 30.2
    assert back[0].extra.get("refine") == {"peak": 12.0}
    assert "extra" not in edl_mod._seg_dict(m)   # sem sobra: não grava vazio
