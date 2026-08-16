"""Estágio 2: alinhamento monotônico com penalidade de gap afim (banda).

Needleman-Wunsch SEMI-GLOBAL (gaps livres nas pontas — recap no começo e
créditos no fim são a norma) com gap afim: abrir custa caro (OPEN), estender
custa quase nada (EXTEND). É o que faz o DP preferir UM gap de 400 frames
(cena removida) a 400 gaps de 1 frame — sem isso o caminho sai picotado.

O laço roda em numba @njit(cache=True) sobre a banda de Sakoe-Chiba (matriz
cheia de um episódio não cabe em RAM nem em tempo). `align_reference` é a
implementação de referência em Python puro, usada pelos testes para validar o
kernel — as duas têm que produzir o MESMO traceback.

Convenção de operações do traceback (runs de meia-abertura [i0,i1) x [j0,j1)):
- MATCH:  diagonal — A[i] casa com B[j]
- GAP_A:  só A avança — trecho do DUBLADO sem correspondente no original
- GAP_B:  só B avança — trecho do ORIGINAL sem correspondente no dublado
"""
import numpy as np
from numba import njit

MATCH = 0
GAP_A = 1
GAP_B = 2

MATCH_MAX = 12.0   # Hamming < 12 em 64 bits = mesmo frame entre encodes
OPEN = 40.0        # abrir gap custa ~3 frames de match perfeito
EXTEND = 0.5       # estender é barato: cena longa removida não é punida

_NEG = -1e18


@njit(cache=True)
def _dp_band(D, lo, m, match_max, open_, extend):  # pragma: no cover — validado via referência
    """Kernel do DP na banda. Retorna (ops, n_ops, best_score).

    ops: array (passos, 3) com [tipo, i, j] do FIM para o COMEÇO (invertido);
    o chamador reverte, comprime em runs e anexa os gaps livres das pontas.
    """
    n, width = D.shape
    M = np.full((n, width), _NEG, dtype=np.float64)
    X = np.full((n, width), _NEG, dtype=np.float64)
    Y = np.full((n, width), _NEG, dtype=np.float64)
    # ponteiros: M: 0=deM 1=deX 2=deY 3=início livre; X: 0=deM 1=deX; Y: idem
    pM = np.zeros((n, width), dtype=np.int8)
    pX = np.zeros((n, width), dtype=np.int8)
    pY = np.zeros((n, width), dtype=np.int8)

    for i in range(n):
        for k in range(width):
            if D[i, k] == 255:
                continue  # fora da banda
            j = lo[i] + k
            if j >= m:
                continue
            s = match_max - D[i, k]

            # --- M: diagonal (A[i] x B[j]) ---
            best = _NEG
            ptr = np.int8(3)
            if i == 0 or j == 0:
                best = 0.0  # gap livre até aqui (semi-global)
                ptr = np.int8(3)
            if i > 0 and j > 0:
                kd = (j - 1) - lo[i - 1]
                if 0 <= kd < width:
                    if M[i - 1, kd] > best:
                        best = M[i - 1, kd]
                        ptr = np.int8(0)
                    if X[i - 1, kd] > best:
                        best = X[i - 1, kd]
                        ptr = np.int8(1)
                    if Y[i - 1, kd] > best:
                        best = Y[i - 1, kd]
                        ptr = np.int8(2)
            if best > _NEG / 2:
                M[i, k] = s + best
                pM[i, k] = ptr

            # --- X: gap em B (só A avança; vem de (i-1, j)) ---
            if i > 0:
                ku = j - lo[i - 1]
                if 0 <= ku < width:
                    a = M[i - 1, ku] - (open_ + extend)
                    b = X[i - 1, ku] - extend
                    if a >= b:
                        if a > _NEG / 2:
                            X[i, k] = a
                            pX[i, k] = np.int8(0)
                    elif b > _NEG / 2:
                        X[i, k] = b
                        pX[i, k] = np.int8(1)

            # --- Y: gap em A (só B avança; vem de (i, j-1) = k-1) ---
            if k > 0 and lo[i] + k - 1 >= 0:
                a = M[i, k - 1] - (open_ + extend)
                b = Y[i, k - 1] - extend
                if a >= b:
                    if a > _NEG / 2:
                        Y[i, k] = a
                        pY[i, k] = np.int8(0)
                elif b > _NEG / 2:
                    Y[i, k] = b
                    pY[i, k] = np.int8(1)

    # fim semi-global: melhor M numa borda (última linha OU última coluna)
    best_score = _NEG
    bi = -1
    bk = -1
    for k in range(width):
        if M[n - 1, k] > best_score:
            best_score = M[n - 1, k]
            bi = n - 1
            bk = k
    for i in range(n):
        k = (m - 1) - lo[i]
        if 0 <= k < width and M[i, k] > best_score:
            best_score = M[i, k]
            bi = i
            bk = k
    ops = np.empty((n + m + 2, 3), dtype=np.int64)
    n_ops = 0
    if bi < 0:
        return ops, 0, best_score

    # traceback
    state = 0  # 0=M 1=X 2=Y
    i = bi
    k = bk
    while True:
        j = lo[i] + k
        if state == 0:
            ops[n_ops, 0] = MATCH
            ops[n_ops, 1] = i
            ops[n_ops, 2] = j
            n_ops += 1
            p = pM[i, k]
            if p == 3:
                break
            state = p
            ni = i - 1
            nk = (j - 1) - lo[ni]
            i = ni
            k = nk
        elif state == 1:
            ops[n_ops, 0] = GAP_A
            ops[n_ops, 1] = i
            ops[n_ops, 2] = j
            n_ops += 1
            p = pX[i, k]
            state = 0 if p == 0 else 1
            ni = i - 1
            nk = j - lo[ni]
            i = ni
            k = nk
        else:
            ops[n_ops, 0] = GAP_B
            ops[n_ops, 1] = i
            ops[n_ops, 2] = j
            n_ops += 1
            p = pY[i, k]
            state = 0 if p == 0 else 2
            k = k - 1
    return ops, n_ops, best_score


def _compress(ops: np.ndarray, n_ops: int, n: int, m: int) -> list[tuple]:
    """Passos (do fim para o começo) -> runs [tipo, i0, i1, j0, j1] em ordem,
    com os gaps livres das pontas anexados explicitamente."""
    if n_ops == 0:
        return []
    steps = ops[:n_ops][::-1]  # ordem começo -> fim
    runs: list[list] = []
    for t, i, j in steps:
        t = int(t)
        i = int(i)
        j = int(j)
        if runs and runs[-1][0] == t:
            runs[-1][2] = i + 1
            runs[-1][4] = j + 1
            continue
        if t == MATCH:
            runs.append([t, i, i + 1, j, j + 1])
        elif t == GAP_A:
            runs.append([t, i, i + 1, j + 1, j + 1])  # B parado (após j)
        else:
            runs.append([t, i + 1, i + 1, j, j + 1])  # A parado (após i)

    # gaps livres das pontas (semi-global não os pontua, mas eles EXISTEM)
    first_i, first_j = int(steps[0][1]), int(steps[0][2])
    last_i, last_j = int(steps[-1][1]), int(steps[-1][2])
    head: list[list] = []
    if first_i > 0:
        head.append([GAP_A, 0, first_i, first_j, first_j])
    if first_j > 0:
        head.append([GAP_B, 0, 0, 0, first_j])
    tail: list[list] = []
    if last_i + 1 < n:
        tail.append([GAP_A, last_i + 1, n, last_j + 1, last_j + 1])
    if last_j + 1 < m:
        tail.append([GAP_B, last_i + 1, last_i + 1, last_j + 1, m])
    return [tuple(r) for r in head + runs + tail]


def align_band(D: np.ndarray, lo: np.ndarray, m: int,
               match_max: float = MATCH_MAX, open_: float = OPEN,
               extend: float = EXTEND) -> list[tuple]:
    """Alinha via kernel numba na banda. Retorna runs (tipo, i0, i1, j0, j1).

    Lista vazia = nenhum match viável na banda (sintoma clássico de ordem de
    episódios trocada — o chamador trata como conflito, não como bug)."""
    ops, n_ops, _score = _dp_band(
        D, lo.astype(np.int64), int(m),
        float(match_max), float(open_), float(extend))
    return _compress(ops, n_ops, len(D), int(m))


def align_reference(a: np.ndarray, b: np.ndarray,
                    match_max: float = MATCH_MAX, open_: float = OPEN,
                    extend: float = EXTEND) -> list[tuple]:
    """Referência em Python puro (matriz CHEIA) para validar o kernel.

    Só para sequências pequenas (testes): O(n*m) em Python. Constrói a
    "banda" cobrindo a matriz inteira e reusa o mesmo kernel — assim os testes
    validam exatamente o código de produção, não uma cópia paralela."""
    n, m = len(a), len(b)
    width = m
    D = np.full((n, width), 255, dtype=np.uint8)
    lo = np.zeros(n, dtype=np.int64)
    popcount = np.array([bin(v).count("1") for v in range(256)], dtype=np.uint8)
    for i in range(n):
        x = a[i] ^ b
        view = x.view(np.uint8).reshape(-1, 8)
        D[i, :m] = popcount[view].sum(axis=1)
    return align_band(D, lo, m, match_max, open_, extend)
