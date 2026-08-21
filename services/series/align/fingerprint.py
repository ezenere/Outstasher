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
import hashlib
import os
import re
import struct
import subprocess
import tempfile
import time
import zlib
from collections import Counter
from pathlib import Path

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


# aceleradores de decode tentados, em ordem, antes do software (a chave é a
# família do transcode: "qsv" decodifica por VAAPI, "nvenc" por CUDA)
HW_DECODE_ORDER = ("qsv", "nvenc")


def _hw_decode_candidates(path: str) -> list[tuple[list[str], str]]:
    """[(args de entrada, formato de hwframe)] dos aceleradores que ABREM este
    arquivo na GPU (probe de 1 frame, como o transcode faz)."""
    from services import transcode
    out = []
    for accel in HW_DECODE_ORDER:
        try:
            if transcode._hw_decode_works(path, accel):
                out.append((transcode._hw_decode_args(accel),
                            transcode._decode_output_format(accel)))
        except Exception:  # noqa: BLE001 — probe é best-effort
            continue
    return out


def _run_frames(cmd: list[str], frame_bytes: int, fps: float,
                duration: float | None, on_progress) -> tuple[bytes, int, bytes]:
    """Roda o ffmpeg lendo os frames crus do stdout aos poucos.

    O stdout É o dado (rawvideo), então não dá para usar `-progress pipe:1`
    como o resto do projeto — mas o progresso sai de graça do próprio volume
    lido: cada frame tem `frame_bytes` e sai a `fps`, então bytes/frame_bytes/fps
    é a posição no vídeo. stderr vai para um arquivo temporário (não trava o
    pipe) e só é lido se o ffmpeg falhar.

    Retorna (dados, returncode, stderr).
    """
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        chunks: list[bytes] = []
        got = 0
        t0 = time.monotonic()
        last = 0.0
        try:
            while True:
                buf = proc.stdout.read1(1 << 16)
                if not buf:
                    break
                chunks.append(buf)
                got += len(buf)
                now = time.monotonic()
                if on_progress and duration and now - last >= 0.5:
                    last = now
                    on_progress(_progress(got // frame_bytes, fps, duration,
                                          now - t0))
        finally:
            proc.stdout.close()
            proc.wait()
        if on_progress and duration and proc.returncode == 0:
            on_progress(_progress(got // frame_bytes, fps, duration,
                                  time.monotonic() - t0, done=True))
        err.seek(0)
        return b"".join(chunks), proc.returncode, err.read()


def _progress(frames: int, fps: float, duration: float, elapsed: float,
              done: bool = False) -> dict:
    """Mesmo formato do progresso de ffmpeg do merge — a UI reusa a barra."""
    out_s = frames / fps
    pct = 100.0 if done else min(99.9, out_s / duration * 100)
    speed = out_s / elapsed if elapsed > 0 else 0.0
    return {"pct": round(pct, 1), "out_s": out_s, "duration_s": duration,
            "fps": frames / elapsed if elapsed > 0 else 0.0,
            "speed": round(speed, 2), "size": 0, "bitrate": 0,
            "eta": 0 if done else (int((duration - out_s) / speed) if speed > 0 else None)}


def dhash_stream(path: str, crop: str | None = None,
                 fps: float = FPS, use_hw: bool = True,
                 duration: float | None = None, on_progress=None) -> np.ndarray:
    """Sequência de dHashes (uint64, um por frame amostrado) do arquivo.

    O ffmpeg entrega rawvideo gray 9x8; o gradiente horizontal entre vizinhos
    vira um bit por comparação. dHash olha ESTRUTURA (onde a imagem clareia da
    esquerda para a direita) — sobrevive a recoloração/brilho, que histograma
    não sobrevive.

    Decode na GPU quando ela abre o arquivo (mesmos filtros em software
    depois: os hashes saem IDÊNTICOS aos do decode por CPU — medido — com
    metade do tempo de CPU; a variante que aplica `fps` ainda na VRAM escolhe
    frames diferentes e foi descartada). O tempo de parede de um REMUX 4K
    continua limitado pela leitura do disco. Falha da GPU cai para software.

    duration/on_progress: com a duração conhecida, o volume de frames já lido
    vira progresso para a UI (o stdout é o próprio dado, então não há
    `-progress` para consultar).
    """
    vf = [f"fps={fps}"] + ([f"crop={crop}"] if crop else []) + [
        f"scale={HASH_W}:{HASH_H}:flags=area", "format=gray"]
    attempts: list[list[str]] = []
    if use_hw:
        for in_args, _hwfmt in _hw_decode_candidates(path):
            attempts.append(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error",
                             *in_args, "-i", path, "-vf", ",".join(vf),
                             "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    attempts.append(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error",
                     "-i", path, "-vf", ",".join(vf),
                     "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    frame_bytes = HASH_W * HASH_H
    raw = b""
    err = b""
    for cmd in attempts:
        out, code, err_out = _run_frames(cmd, frame_bytes, fps, duration,
                                         on_progress)
        if code == 0 and len(out) >= frame_bytes:
            raw = out
            break
        err = err_out
    if not raw:
        raise FingerprintError(
            f"ffmpeg falhou extraindo fingerprint de {path}: "
            f"{err[-400:].decode('utf-8', 'ignore')}")
    n = len(raw) // frame_bytes
    if n == 0:
        raise FingerprintError(f"nenhum frame extraído de {path}")
    frames = np.frombuffer(raw[:n * frame_bytes], dtype=np.uint8)
    frames = frames.reshape(n, HASH_H, HASH_W)
    bits = frames[:, :, 1:] > frames[:, :, :-1]          # (n, 8, 8) bool
    weights = (1 << np.arange(64, dtype=np.uint64)).reshape(HASH_H, HASH_W - 1)
    return (bits.astype(np.uint64) * weights).sum(axis=(1, 2))


CACHE_MAX_AGE_S = 60 * 24 * 3600   # fingerprints de jobs de 2 meses atrás: fora


def _cache_dir() -> Path:
    import config
    return config.DB_DIR / "fpcache"


def _cache_key(path: str, crop: str | None) -> str:
    st = os.stat(path)
    raw = f"{path}|{st.st_size}|{int(st.st_mtime)}|{crop}|{FPS}"
    return hashlib.sha1(raw.encode()).hexdigest()


def dhash_cached(path: str, crop: str | None = None,
                 duration: float | None = None, on_progress=None) -> np.ndarray:
    """dhash_stream com cache em DISCO (chave: caminho+tamanho+mtime+crop).

    O fingerprint é a parte cara do alinhamento (minutos por arquivo) e o
    resultado é minúsculo (~90 KB por episódio). Persistir significa que um
    alinhamento que FALHOU deixa o trabalho pago — o rematch todos×todos dos
    desalinhados e qualquer nova tentativa partem daqui, de graça."""
    cdir = _cache_dir()
    try:
        f = cdir / f"{_cache_key(path, crop)}.npy"
        if f.exists():
            return np.load(f)
    except OSError:
        f = None
    h = dhash_stream(path, crop, duration=duration, on_progress=on_progress)
    if f is not None:
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp.npy")
            np.save(tmp, h)
            tmp.replace(f)
            _prune_cache(cdir)
        except OSError:
            pass   # cache é otimização: sem espaço/permissão, segue sem
    return h


def _prune_cache(cdir: Path):
    """Remove fingerprints velhos (o cache não pode crescer para sempre)."""
    cutoff = time.time() - CACHE_MAX_AGE_S
    try:
        for f in cdir.glob("*.npy"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def hamming_band(a: np.ndarray, b: np.ndarray,
                 band: int = BAND,
                 offset: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Distância de Hamming numa banda de Sakoe-Chiba.

    Retorna (D, lo): D tem shape (len(a), 2*band+1) com a distância 0..64 na
    banda e 255 fora dela; lo[i] é a coluna de `b` correspondente à posição 0
    da banda na linha i. O centro da banda segue a diagonal esticada
    (i * (M-1)/(N-1)) para sequências de tamanhos diferentes — OU, com
    `offset`, a diagonal de inclinação 1 deslocada (j = i + offset): é o caso
    do episódio dentro de um arquivo FUNDIDO, onde esticar a diagonal joga a
    posição real do episódio para fora da banda no fim (ou no começo).
    """
    n, m = len(a), len(b)
    width = 2 * band + 1
    D = np.full((n, width), 255, dtype=np.uint8)
    lo = np.zeros(n, dtype=np.int64)
    if offset is not None:
        centers = np.arange(n, dtype=np.int64) + int(offset)
    else:
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


COARSE_STEP = 4          # 1 amostra/s no localizador grosseiro
COARSE_GOOD = 14         # distância até isto conta como "mesmo frame"


def coarse_offset(a: np.ndarray, b: np.ndarray, step: int = COARSE_STEP,
                  good: int = COARSE_GOOD) -> tuple[int, float]:
    """Localizador grosseiro: em que offset (frames de b − frames de a) o lado
    curto `a` está dentro do longo `b`?

    Amostra 1 frame a cada `step` dos dois lados, mede a distância de TODOS os
    pares (matriz cheia — pequena nesta resolução) e conta, por diagonal
    j − i, quantos pares são "mesmo frame". A diagonal mais votada é o offset
    dominante do episódio; recap/"nos próximos" (cenas de OUTRO ponto) votam
    em diagonais espalhadas e não competem com 40 min de conteúdo contínuo.
    Retorna (offset em frames de FPS, fração dos frames de `a` que votaram na
    diagonal vencedora ±1 amostra)."""
    sa, sb = a[::step], b[::step]
    n, m = len(sa), len(sb)
    if n == 0 or m == 0:
        return 0, 0.0
    votes = np.zeros(n + m, dtype=np.int64)  # índice = (j - i) + n
    for i in range(n):
        x = sa[i] ^ sb
        d = _POPCOUNT8[x.view(np.uint8).reshape(-1, 8)].sum(axis=1)
        js = np.nonzero(d <= good)[0]
        if len(js):
            np.add.at(votes, js - i + n, 1)
    # suaviza ±1 amostra (o passo grosseiro pode cair um de cada lado)
    sm = votes.copy()
    sm[1:] += votes[:-1]
    sm[:-1] += votes[1:]
    k = int(np.argmax(sm))
    return (k - n) * step, min(1.0, float(sm[k]) / n)


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
