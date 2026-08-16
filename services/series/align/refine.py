"""Estágio 4: refino do offset por ÁUDIO dentro de cada segmento match.

O alinhamento por vídeo a 4 fps tem precisão de ±125 ms — audível como
dessincronia labial. Dentro de cada match, GCC-PHAT refina usando o offset do
vídeo como palpite e busca RESTRITA a ±500 ms em torno dele (sem a restrição,
GCC-PHAT em montagens diferentes acha pico espúrio com facilidade).

- pico fraco (razão pico/média < 3): mantém o offset do vídeo e reduz a
  confiança do segmento;
- segmento < 10 s: áudio de menos para um pico estável — interpola o offset
  dos vizinhos (aqui: herda o refinado mais próximo).
"""
import tempfile
from pathlib import Path

from services import merger
from services.merger_segments import _extract_wav_window
from services.series.align.classify import Segment

SEARCH_WINDOW_S = 30.0    # janela de correlação no meio do segmento
SEARCH_RADIUS_S = 0.5     # busca ±500 ms em torno do palpite do vídeo
MIN_SEGMENT_S = 10.0      # abaixo disto não há áudio para um pico estável
MIN_PEAK_QUALITY = 3.0    # razão pico/média mínima para confiar no refino
WEAK_PEAK_PENALTY = 0.7   # multiplicador de confiança com pico fraco


def refine_offsets(segs: list[Segment], dub_path: str, dub_a: int,
                   orig_path: str, orig_a: int, log=print) -> list[Segment]:
    """Refina o offset dos segmentos match IN PLACE (retorna a mesma lista).

    O offset refinado vai em seg.offset (segundos, b = a + offset) e o bruto
    do vídeo fica em seg.extra["video_offset"] para inspeção.
    """
    refined_any = False
    for seg in segs:
        if seg.kind not in ("match", "drift", "pal") or seg.offset is None:
            continue
        seg.extra["video_offset"] = seg.offset
        dur = seg.a_end - seg.a_start
        if dur < MIN_SEGMENT_S:
            seg.extra["refine"] = "curto demais — herda vizinho"
            continue
        win = min(SEARCH_WINDOW_S, dur * 0.8)
        # janela centrada no meio do segmento, nas DUAS timelines pelo palpite
        a_start = seg.a_start + (dur - win) / 2
        b_start = a_start + seg.offset
        try:
            tau, quality = _measure(dub_path, dub_a, orig_path, orig_a,
                                    a_start, b_start, win)
        except merger.MergeError as e:
            log(f"refino falhou em {seg.a_start:.1f}s ({e}) — mantendo offset do vídeo")
            seg.confidence *= WEAK_PEAK_PENALTY
            continue
        if quality < MIN_PEAK_QUALITY:
            seg.extra["refine"] = f"pico fraco ({quality:.1f}) — offset do vídeo mantido"
            seg.confidence *= WEAK_PEAK_PENALTY
            continue
        # tau: quanto o áudio do ORIGINAL (extraído já deslocado pelo palpite)
        # ainda está atrasado vs o dublado — soma ao palpite
        seg.offset = seg.offset + tau
        seg.extra["refine"] = f"ajuste {tau * 1000:+.1f} ms (pico {quality:.1f})"
        refined_any = True

    # segmentos curtos herdam o offset refinado do vizinho com o palpite de
    # vídeo mais parecido (antes ou depois)
    if refined_any:
        _inherit_short(segs)
    return segs


def _measure(dub_path: str, dub_a: int, orig_path: str, orig_a: int,
             a_start: float, b_start: float, dur: float) -> tuple[float, float]:
    with tempfile.TemporaryDirectory(prefix="align_refine_") as td:
        wa, wb = str(Path(td) / "a.wav"), str(Path(td) / "b.wav")
        _extract_wav_window(dub_path, dub_a, wa, a_start, dur)
        _extract_wav_window(orig_path, orig_a, wb, b_start, dur)
        return merger.gcc_phat_delay_with_confidence(
            merger._read_wav(wb), merger._read_wav(wa), merger.ALIGN_SR,
            max_tau=SEARCH_RADIUS_S)


def _inherit_short(segs: list[Segment]):
    """Match curto (sem refino próprio) herda o AJUSTE do vizinho refinado
    mais próximo — o offset base dele continua o do próprio vídeo."""
    refined = [(s, s.offset - s.extra["video_offset"]) for s in segs
               if s.kind in ("match", "drift", "pal")
               and s.extra.get("refine", "").startswith("ajuste")]
    if not refined:
        return
    for seg in segs:
        if seg.kind not in ("match", "drift", "pal"):
            continue
        if seg.extra.get("refine") == "curto demais — herda vizinho":
            nearest = min(refined,
                          key=lambda rs: abs(rs[0].a_start - seg.a_start))
            seg.offset = seg.extra["video_offset"] + nearest[1]
