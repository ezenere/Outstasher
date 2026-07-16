"""Parser do -progress do ffmpeg: escrita (out_time) vs leitura (frames).

O progresso de LEITURA (frames lidos ÷ total) vai à frente da ESCRITA
(out_time ÷ duração) pela latência de buffer do encoder — grande em AV1, quase
nula em H264/HEVC. As duas viram barras sobrepostas na UI.
"""
from services import merger


def test_write_and_read_progress():
    # início do AV1: o encoder já leu 2000 frames mas ainda não emitiu nada
    block = {"frame": "2000", "fps": "5.0", "out_time_us": "0",
             "total_size": "0", "speed": "0.1x", "bitrate": "N/A"}
    info = merger._parse_progress_block(block, duration_s=5220.0, total_frames=125000)
    assert info["pct"] == 0.0                 # escrita: nada saiu
    assert info["read_pct"] == round(2000 / 125000 * 100, 1)  # leitura: 1.6%
    assert info["read_pct"] > info["pct"]     # leitura à frente da escrita
    assert info["frame"] == 2000 and info["fps"] == 5.0


def test_read_never_below_write():
    # se a estimativa de total_frames erra para baixo, a leitura nunca fica
    # ABAIXO da escrita (o clamp garante read_pct >= pct)
    block = {"frame": "100", "out_time_us": str(50 * 1_000_000), "total_size": "1000"}
    info = merger._parse_progress_block(block, duration_s=100.0, total_frames=120)
    # escrita = 50/100 = 50%; leitura crua = 100/120 = 83% -> fica 83 (>50)
    assert info["pct"] == 50.0 and info["read_pct"] >= info["pct"]
    # agora com total_frames subestimado: leitura crua daria <50 -> clampa em 50
    info2 = merger._parse_progress_block(block, duration_s=100.0, total_frames=1000)
    assert info2["read_pct"] == info2["pct"]  # nunca volta atrás


def test_no_total_frames_disables_read_bar():
    block = {"frame": "500", "out_time_us": str(10 * 1_000_000), "total_size": "0"}
    info = merger._parse_progress_block(block, duration_s=100.0, total_frames=0)
    # sem estimativa de total: read_pct cai para o pct (barra de leitura some)
    assert info["read_pct"] == info["pct"] == 10.0


def test_total_frames_estimate():
    # nb_frames exato quando presente
    probe = {"streams": [{"codec_type": "video", "nb_frames": "125000",
                          "avg_frame_rate": "24/1", "disposition": {}}]}
    assert merger._total_frames(probe, 5220.0) == 125000
    # sem nb_frames: duração × fps (24000/1001 ≈ 23.976)
    probe = {"streams": [{"codec_type": "video", "avg_frame_rate": "24000/1001",
                          "disposition": {}}]}
    assert merger._total_frames(probe, 100.0) == int(100.0 * 24000 / 1001)
    # capa embutida não conta como vídeo
    probe = {"streams": [{"codec_type": "video", "avg_frame_rate": "24/1",
                          "disposition": {"attached_pic": 1}}]}
    assert merger._total_frames(probe, 100.0) == 0
    # sem dados suficientes -> 0 (barra de leitura desligada)
    assert merger._total_frames({"streams": []}, 100.0) == 0
