"""Estágios 0-1: normalização geométrica + fingerprint dHash por frame.

- crop_params: cropdetect amostrado longe da abertura (créditos sobre fundo
  preto enganam o detector) — remove pillarbox/letterbox antes de qualquer hash.
- dhash_stream: ffmpeg decodifica a 4 fps direto em 9x8 gray (scale flags=area
  suprime ruído de compressão) e o dHash de 64 bits sai vetorizado em numpy.
- distance_band: matriz de distância de Hamming restrita a uma banda de
  Sakoe-Chiba em torno da diagonal — desalinhamento acumulado além de ±BAND
  frames não é esperado num episódio, e a banda corta memória e tempo.
- dump_matrix_png: a matriz como PNG (zlib puro, sem dependência de imagem) —
  o debug visual que mostra a estrutura do problema a olho nu.
"""
import re
import struct
import subprocess
import zlib
from collections import Counter

import numpy as np

FPS = 4.0            # resolução temporal de 250 ms localiza fronteiras de cena
HASH_W, HASH_H = 9, 8  # 9x8 gray -> 8 comparações por linha x 8 linhas = 64 bits
BAND = 1440          # ±6 min a 4 fps de desalinhamento acumulado tolerado
CROP_SAMPLE_START = 300.0  # amostra o cropdetect a partir de ~5 min
_CROP_RE = re.compile(r"crop=(\d+:\d+:\d+:\d+)")

# popcount por byte (LUT): Hamming de uint64 = soma dos popcounts dos 8 bytes
_POPCOUNT8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


class FingerprintError(RuntimeError):
    pass


def crop_params(path: str, duration: float | None = None) -> str | None:
    """Crop dominante ("w:h:x:y") via cropdetect, ou None (sem crop preciso).

    Amostra 500 frames a partir de ~5 min (ou de 10% da duração, se o arquivo
    for curto) — o começo costuma ter logos/créditos sobre preto, que fariam o
    cropdetect "detectar" um crop gigante.
    """
    start = CROP_SAMPLE_START
    if duration is not None and duration < CROP_SAMPLE_START * 2:
        start = max(0.0, duration * 0.1)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start),
           "-i", path, "-vf", "cropdetect=24:2:0", "-frames:v", "500",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    crops = Counter(_CROP_RE.findall(proc.stderr))
    if not crops:
        return None
    crop, _n = crops.most_common(1)[0]
    w, h, _x, _y = (int(v) for v in crop.split(":"))
    if w <= 0 or h <= 0:
        return None
    return crop


def dhash_stream(path: str, crop: str | None = None,
                 fps: float = FPS) -> np.ndarray:
    """Sequência de dHashes (uint64, um por frame amostrado) do arquivo.

    O ffmpeg entrega rawvideo gray 9x8; o gradiente horizontal entre vizinhos
    vira um bit por comparação. dHash olha ESTRUTURA (onde a imagem clareia da
    esquerda para a direita) — sobrevive a recoloração/brilho, que histograma
    não sobrevive.
    """
    vf = []
    if crop:
        vf.append(f"crop={crop}")
    vf += [f"fps={fps}", f"scale={HASH_W}:{HASH_H}:flags=area", "format=gray"]
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error",
           "-i", path, "-vf", ",".join(vf), "-f", "rawvideo",
           "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FingerprintError(
            f"ffmpeg falhou extraindo fingerprint de {path}: "
            f"{proc.stderr[-400:].decode('utf-8', 'ignore')}")
    raw = proc.stdout
    frame_bytes = HASH_W * HASH_H
    n = len(raw) // frame_bytes
    if n == 0:
        raise FingerprintError(f"nenhum frame extraído de {path}")
    frames = np.frombuffer(raw[:n * frame_bytes], dtype=np.uint8)
    frames = frames.reshape(n, HASH_H, HASH_W)
    bits = frames[:, :, 1:] > frames[:, :, :-1]          # (n, 8, 8) bool
    weights = (1 << np.arange(64, dtype=np.uint64)).reshape(HASH_H, HASH_W - 1)
    return (bits.astype(np.uint64) * weights).sum(axis=(1, 2))


def hamming_band(a: np.ndarray, b: np.ndarray,
                 band: int = BAND) -> tuple[np.ndarray, np.ndarray]:
    """Distância de Hamming numa banda de Sakoe-Chiba.

    Retorna (D, lo): D tem shape (len(a), 2*band+1) com a distância 0..64 na
    banda e 255 fora dela; lo[i] é a coluna de `b` correspondente à posição 0
    da banda na linha i. O centro da banda segue a diagonal esticada
    (i * (M-1)/(N-1)) para sequências de tamanhos diferentes.
    """
    n, m = len(a), len(b)
    width = 2 * band + 1
    D = np.full((n, width), 255, dtype=np.uint8)
    lo = np.zeros(n, dtype=np.int64)
    centers = (np.arange(n) * (m - 1) / max(1, n - 1)).round().astype(np.int64) \
        if n > 1 else np.zeros(n, dtype=np.int64)
    for i in range(n):
        j0 = max(0, int(centers[i]) - band)
        j1 = min(m, int(centers[i]) + band + 1)
        lo[i] = j0
        if j1 <= j0:
            continue
        x = a[i] ^ b[j0:j1]                                # (j1-j0,) uint64
        view = x.view(np.uint8).reshape(-1, 8)
        D[i, :j1 - j0] = _POPCOUNT8[view].sum(axis=1)
    return D, lo


def dump_matrix_png(D: np.ndarray, path: str):
    """Grava a matriz de distância como PNG grayscale (zlib puro, sem PIL).

    Distância baixa = escuro: a diagonal de match aparece como um traço preto;
    saltos e manchas de substituição saltam aos olhos. É a ferramenta de debug
    mais valiosa do alinhador — olhe o PNG antes de mexer em parâmetro.
    """
    img = np.clip(D.astype(np.uint16) * 4, 0, 255).astype(np.uint8)
    h, w = img.shape

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(
            ">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
