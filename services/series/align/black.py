"""Tela preta nos trechos sem dublagem.

O original costuma ter ~1 s de tela preta que a fonte dublada não tem: no
começo do arquivo, entre os atos de série (o intervalo comercial do Blu-ray)
e no fim. O alinhador vê isso como `gap_orig` — material do original sem
dublagem — e a política "cortar cena sem dublagem" tirava o trecho do vídeo.
Só que cortar no frame exige re-encodar o vídeo INTEIRO (keyframe forçado
nos pontos de corte): perde Dolby Vision/HDR10+, perde geração, e levou 1h29
num 4K para tirar 1,0 s de preto do segundo 0 (caso real). No histórico, 25
de 27 re-encodes foram para isso — todos os medidos eram preto puro com
silêncio digital no áudio original.

Preto não é cena: fica no vídeo, mudo, e o vídeo segue em stream copy. Este
módulo mede quanto de IMAGEM (o que não é preto) sobra em cada trecho; a
política decide com isso (advanced_merge.default_rules).
"""
import re
import subprocess

# trechos menores que isto são ruído do alinhador (classify.MIN_GAP_S); os
# maiores que MAX não são intervalo, são cena — nem gastam decode (um trecho
# de 30 s em 4K já custa dezenas de segundos de CPU)
MEASURE_MIN_S = 0.5
MEASURE_MAX_S = 30.0
# blackdetect: frame preto = 98% dos pixels abaixo de 10% da faixa de luma
# (o filtro já considera faixa limitada e profundidade de bits — 16 em 8 bits,
# 64 em 10 bits). Céu estrelado e cena noturna muito escura podem passar como
# preto; o erro aí é benigno: o trecho fica no vídeo, mudo, em vez de sair
PIC_TH = 0.98
PIX_TH = 0.10
KINDS = ("gap_orig", "replaced")

_BLACK_RE = re.compile(r"black_start:\s*([-0-9.]+)\s+black_end:\s*([-0-9.]+)")


def black_seconds(path: str, t0: float, t1: float) -> float | None:
    """Segundos de tela preta no vídeo principal de `path` entre t0 e t1
    (decodifica só esse pedaço). None se o ffmpeg falhar — quem chama trata
    como "não medido" e a política segue como antes."""
    dur = t1 - t0
    if dur <= 0:
        return 0.0
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
           "-ss", f"{max(0.0, t0):.3f}", "-t", f"{dur:.3f}", "-i", path,
           "-map", "0:v:0", "-an", "-sn", "-dn",
           "-vf", f"blackdetect=d=0:pic_th={PIC_TH}:pix_th={PIX_TH}",
           "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120 + 10 * dur)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    total = 0.0
    for a, b in _BLACK_RE.findall(p.stderr):
        # tempos relativos ao -ss; o último intervalo, aberto no fim do
        # trecho, é reportado quando o stream acaba
        lo, hi = max(0.0, float(a)), min(dur, float(b))
        if hi > lo:
            total += hi - lo
    return round(total, 3)


def mark(segs, orig_path: str, dur_orig: float | None = None, log=None) -> int:
    """Grava em seg.extra["picture_s"] os segundos de IMAGEM (não preta) de
    cada trecho sem dublagem medível. Só mede o que ainda não tem a medida
    (EDL nova já vem medida do alinhamento; EDL antiga é medida no render).
    Devolve quantos trechos mediu. Bloqueante (ffmpeg)."""
    medidos = pretos = 0
    for seg in segs:
        if seg.kind not in KINDS or "picture_s" in seg.extra:
            continue
        if seg.b_start is None or seg.b_end is None:
            continue
        t0 = seg.b_start
        t1 = min(seg.b_end, dur_orig) if dur_orig else seg.b_end
        dur = t1 - t0
        if not MEASURE_MIN_S <= dur <= MEASURE_MAX_S:
            continue
        preto = black_seconds(orig_path, t0, t1)
        if preto is None:
            continue
        medidos += 1
        # o que o arquivo não tem além do fim conta como imagem: na dúvida, a
        # política continua podendo cortar
        seg.extra["picture_s"] = round(max(0.0, (seg.b_end - seg.b_start) - preto), 3)
        if seg.extra["picture_s"] < MEASURE_MIN_S:
            pretos += 1
    if log and medidos:
        log(f"Tela preta: {pretos} de {medidos} trecho(s) sem dublagem são "
            f"preto no original")
    return medidos
