"""Enquadramento: um dos arquivos é um RECORTE do outro?

Caso de campo: original 1920x1080 (16:9) e dublado 1088x720 (1,51) — o
dublado era o original sem 288 px do lado esquerdo, o mesmo recorte no
episódio inteiro. O dHash olha a estrutura do quadro inteiro (9x8): com
15% da imagem a mais de um lado, nenhum frame casava (distância 11-27 onde
o limiar de match é 12) e o alinhador acusava "quase nada casa" num par do
mesmo episódio. Com o original recortado igual, a distância caiu para 2-6.

Só roda quando as proporções de imagem (depois do corte das barras pretas)
diferem — no caso normal não custa nada. Aí:

1. três janelas de 40 s do dublado (a 30%, 50% e 70%) e, no original, as
   janelas correspondentes com folga de ±60 s (o offset não é conhecido),
   decodificadas a 4 fps em miniaturas cinza 128x72;
2. candidatos de recorte: o lado mais LARGO recortado na proporção do
   estreito (pan & scan / recorte lateral), ou o mais ESTREITO recortado na
   proporção do largo (open matte), em várias posições e com um pouco de
   zoom; o dHash de cada candidato sai das miniaturas em numpy;
3. nota de cada candidato = distância mediana na melhor defasagem de cada
   janela; vence o menor, se for bom o bastante E bem melhor que o quadro
   inteiro. Senão fica tudo como está.
"""
import subprocess

import numpy as np

from services.series.align import fingerprint

ASPECT_TOL = 1.03       # proporções até 3% diferentes = mesmo enquadramento
WIN_S = 40.0            # janela do dublado
RADIUS_S = 60.0         # folga do original em volta da posição proporcional
SAMPLE_AT = (0.3, 0.5, 0.7)
GRID_W, GRID_H = 128, 72
ZOOMS = (1.0, 0.94, 0.88)
POSITIONS = 9
GOOD = 10.0             # nota até isto: o recorte casa
MIN_GAIN = 4.0          # e tem que ganhar do quadro inteiro por esta margem


def _geometry(path: str, crop: str | None) -> tuple[int, int, int, int, float] | None:
    """(w, h, x, y, proporção de exibição) da área útil (já sem barras)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,sample_aspect_ratio", "-of", "default=nw=1", path],
        capture_output=True, text=True)
    vals = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    try:
        fw, fh = int(vals["width"]), int(vals["height"])
    except (KeyError, ValueError):
        return None
    sar = 1.0
    try:
        n, d = vals.get("sample_aspect_ratio", "1:1").split(":")
        if int(n) > 0 and int(d) > 0:
            sar = int(n) / int(d)
    except ValueError:
        pass
    if crop:
        w, h, x, y = (int(v) for v in crop.split(":"))
    else:
        w, h, x, y = fw, fh, 0, 0
    return w, h, x, y, w * sar / h


def _thumbs(path: str, crop: str | None, start: float, dur: float) -> np.ndarray:
    """Miniaturas cinza GRID_W x GRID_H a 4 fps de [start, start+dur)."""
    vf = [f"fps={fingerprint.FPS}"] + ([f"crop={crop}"] if crop else []) + [
        f"scale={GRID_W}:{GRID_H}:flags=area", "format=gray"]
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error",
           "-ss", f"{max(0.0, start):.3f}", "-i", path, "-t", f"{dur:.3f}",
           "-an", "-sn", "-vf", ",".join(vf), "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (GRID_W * GRID_H)
    return np.frombuffer(raw[:n * GRID_W * GRID_H], np.uint8).reshape(n, GRID_H, GRID_W)


def _integral(frames: np.ndarray) -> np.ndarray:
    s = np.zeros((frames.shape[0], GRID_H + 1, GRID_W + 1), np.float32)
    s[:, 1:, 1:] = frames.astype(np.float32).cumsum(1).cumsum(2)
    return s


def _hashes(integ: np.ndarray, fx: float, fy: float, fw: float, fh: float) -> np.ndarray:
    """dHash 9x8 da região fracionária (fx, fy, fw, fh) de cada miniatura,
    por média de área — o mesmo que o scale=9:8:flags=area do fingerprint."""
    hw, hh = fingerprint.HASH_W, fingerprint.HASH_H
    xs = np.round(np.linspace(fx, fx + fw, hw + 1) * GRID_W).astype(int)
    ys = np.round(np.linspace(fy, fy + fh, hh + 1) * GRID_H).astype(int)
    xs = np.clip(xs, 0, GRID_W)
    ys = np.clip(ys, 0, GRID_H)
    c = integ[:, ys[:, None], xs[None, :]]                       # (n, 9, 10)
    sums = c[:, 1:, 1:] - c[:, :-1, 1:] - c[:, 1:, :-1] + c[:, :-1, :-1]
    area = np.outer(np.diff(ys), np.diff(xs)).clip(min=1)
    cells = sums / area                                          # (n, 8, 9)
    bits = cells[:, :, 1:] > cells[:, :, :-1]
    weights = (1 << np.arange(64, dtype=np.uint64)).reshape(hh, hw - 1)
    return (bits.astype(np.uint64) * weights).sum(axis=(1, 2))


def _best_shift(short: np.ndarray, long_: np.ndarray) -> float:
    """Menor distância mediana entre `short` e algum trecho de `long_`."""
    if len(short) < 8 or len(long_) < len(short):
        return 64.0
    x = short[:, None] ^ long_[None, :]
    d = fingerprint._POPCOUNT8[x.view(np.uint8)].reshape(*x.shape, 8).sum(-1)
    shifts = len(long_) - len(short) + 1
    idx = np.arange(len(short))[None, :] + np.arange(shifts)[:, None]
    diag = d[np.arange(len(short))[None, :], idx]                # (shifts, n)
    return float(np.median(diag, axis=1).min())


def _candidates(target_aspect: float, own_aspect: float) -> list[tuple]:
    """Recortes fracionários (fx, fy, fw, fh) de um quadro de proporção
    `own_aspect` que chegam à proporção `target_aspect`, em POSITIONS
    posições por eixo livre e com os zooms de ZOOMS."""
    out = []
    for z in ZOOMS:
        if target_aspect < own_aspect:     # mais estreito: corta as laterais
            fw, fh = z * target_aspect / own_aspect, z
        else:                              # mais alto: corta em cima/embaixo
            fw, fh = z, z * own_aspect / target_aspect
        xs = np.linspace(0, 1 - fw, POSITIONS) if fw < 0.995 else [0.0]
        ys = np.linspace(0, 1 - fh, POSITIONS) if fh < 0.995 else [0.0]
        out += [(float(x), float(y), fw, fh) for x in xs for y in ys]
    return out


def _refine(c: tuple) -> list[tuple]:
    """Posições entre as da grade, em volta do vencedor."""
    fx, fy, fw, fh = c
    step_x = (1 - fw) / (POSITIONS - 1) if fw < 0.995 else 0.0
    step_y = (1 - fh) / (POSITIONS - 1) if fh < 0.995 else 0.0
    frac = (-0.5, -0.25, 0.0, 0.25, 0.5)
    out = []
    for dx in (frac if step_x else (0.0,)):
        for dy in (frac if step_y else (0.0,)):
            out.append((min(max(0.0, fx + dx * step_x), 1 - fw),
                        min(max(0.0, fy + dy * step_y), 1 - fh), fw, fh))
    return out


def _to_crop(c: tuple, geo) -> str:
    """Recorte fracionário da área útil -> "w:h:x:y" em pixels do arquivo
    (compondo com o corte das barras), em números pares."""
    fx, fy, fw, fh = c
    w, h, x, y, _ = geo
    pw = int(round(fw * w)) // 2 * 2
    ph = int(round(fh * h)) // 2 * 2
    px = (x + int(round(fx * w))) // 2 * 2
    py = (y + int(round(fy * h))) // 2 * 2
    return f"{pw}:{ph}:{px}:{py}"


def match_framing(dub_path: str, crop_a: str | None, dur_a: float,
                  orig_path: str, crop_b: str | None, dur_b: float,
                  log=lambda _m: None) -> tuple[str | None, str | None, str | None]:
    """(crop do dublado, crop do original, nota). Sem recorte detectado
    devolve os crops recebidos e nota None."""
    geo_a = _geometry(dub_path, crop_a)
    geo_b = _geometry(orig_path, crop_b)
    if geo_a is None or geo_b is None:
        return crop_a, crop_b, None
    ar_a, ar_b = geo_a[4], geo_b[4]
    if max(ar_a, ar_b) / min(ar_a, ar_b) < ASPECT_TOL:
        return crop_a, crop_b, None

    wins = []
    for frac in SAMPLE_AT:
        ta = dur_a * frac
        tb = ta * dur_b / dur_a
        ia = _integral(_thumbs(dub_path, crop_a, ta, WIN_S))
        ib = _integral(_thumbs(orig_path, crop_b, tb - RADIUS_S, WIN_S + 2 * RADIUS_S))
        if len(ia) >= 8 and len(ib) >= len(ia):
            wins.append((ia, ib))
    if not wins:
        return crop_a, crop_b, None
    full = (0.0, 0.0, 1.0, 1.0)
    full_a = [_hashes(ia, *full) for ia, _ in wins]
    full_b = [_hashes(ib, *full) for _, ib in wins]

    def score(side: str, c: tuple) -> float:
        """Nota com o recorte `c` no lado `side` e o outro inteiro."""
        vals = []
        for k, (ia, ib) in enumerate(wins):
            ha = _hashes(ia, *c) if side == "a" else full_a[k]
            hb = _hashes(ib, *c) if side == "b" else full_b[k]
            vals.append(_best_shift(ha, hb))
        return float(np.median(vals))

    base = float(np.median([_best_shift(a, b) for a, b in zip(full_a, full_b)]))
    # anamórfico espremido (mesmo conteúdo, proporção diferente): o dHash
    # 9x8 não liga para a proporção e já casa
    if base <= GOOD - MIN_GAIN:
        return crop_a, crop_b, None
    wide = "b" if ar_b > ar_a else "a"
    narrow = "a" if wide == "b" else "b"
    lo, hi = min(ar_a, ar_b), max(ar_a, ar_b)
    # o largo recortado na proporção do estreito (pan & scan, recorte
    # lateral) e o estreito recortado na proporção do largo (open matte)
    trials = [(score(wide, c), wide, c) for c in _candidates(lo, hi)]
    trials += [(score(narrow, c), narrow, c) for c in _candidates(hi, lo)]
    best, side, c = min(trials)
    for c2 in _refine(c):
        s2 = score(side, c2)
        if s2 < best:
            best, c = s2, c2
    log(f"enquadramento: proporção {ar_a:.2f} (dublado) x {ar_b:.2f} (original); "
        f"quadro inteiro {base:.0f}, melhor recorte {best:.0f}")
    if best > GOOD or base - best < MIN_GAIN:
        return crop_a, crop_b, None
    if side == "a":
        new_a, new_b = _to_crop(c, geo_a), crop_b
        quem, de = "original", "dublado"
    else:
        new_a, new_b = crop_a, _to_crop(c, geo_b)
        quem, de = "dublado", "original"
    nota = (f"o {quem} mostra só parte da imagem do {de} (proporção "
            f"{lo:.2f} x {hi:.2f}): o {de} é "
            f"comparado recortado em {new_a if side == 'a' else new_b} "
            f"(distância {base:.0f} → {best:.0f})")
    return new_a, new_b, nota
