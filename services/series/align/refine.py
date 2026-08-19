"""Estágio 4: refino por ÁUDIO — o offset fino e os pontos de corte vêm daqui.

Lição de um caso real de campo (S01E01, WEB-DL BR vs BluRay):
- o vídeo a 4 fps NÃO enxerga edições pequenas (2 frames cortados numa junção
  de intervalo comercial = 68 ms; 1,7 s numa outra), então o EDL do vídeo
  vira um match gigante com UM offset e o áudio fica fora dali em diante;
- e o vídeo INVENTA cortes onde não há (plano parado/escuro: gap 0,5 s +
  match de 4 s com offset +0,25 + gap 0,5 s), que renderizados viram cortes
  secos no meio do diálogo com o áudio pulando e voltando.

Regra: o vídeo dá a estrutura grossa (cenas removidas/inseridas >= ~1 s); o
offset fino e os PONTOS DE CORTE são do áudio:

1. wobbles do vídeo (match curto entre gaps minúsculos, vizinhos com o mesmo
   offset) são fundidos de volta num único match — o áudio decide;
2. em cada match, GCC-PHAT em janelas deslizantes (12 s a cada 15 s, busca
   ±0,6 s em torno do palpite do vídeo) dá o PERFIL do offset ao longo do
   trecho;
3. onde o perfil muda (> 30 ms, confirmado pela janela seguinte), o ponto de
   edição é localizado por bissecção (~1-2 s) e ENCAIXADO NO SILÊNCIO mais
   próximo do áudio dublado — o corte nunca cai no meio de uma palavra;
4. o match é dividido nesses pontos, cada parte com o offset MEDIDO (precisão
   de amostra), e partes curtas demais para medir herdam a vizinha.
"""
import subprocess
import tempfile
from pathlib import Path

from services import merger
from services.merger_segments import _extract_wav_window
from services.series.align.classify import Segment

WIN_S = 12.0            # janela de correlação
STEP_S = 15.0           # passo entre janelas
SEARCH_RADIUS_S = 0.6   # busca em torno do palpite do vídeo (±0,25 de precisão
                        # do dHash + folga para o wobble)
MIN_SEGMENT_S = 10.0    # abaixo disto não há áudio para pico estável: herda
MIN_PEAK_QUALITY = 6.0  # razão pico/média mínima (dublagem real: 20-600)
CHANGE_TOL_S = 0.030    # mudança de offset que conta como edição (lip sync)
GROUP_TOL_S = 0.020     # janelas dentro disto = mesmo offset
BISECT_WIN_S = 6.0      # janela nas iterações de bissecção
SILENCE_SNAP_S = 5.0    # procura silêncio até isto do ponto estimado
SILENCE_DB = -30.0
SILENCE_MIN_S = 0.12
WOBBLE_MAX_S = 8.0      # anomalia do vídeo até este tamanho funde SEM consulta
WOBBLE_AUDIO_MAX_S = 90.0  # até isto funde SE o áudio confirmar continuidade
WOBBLE_OFF_TOL_S = 0.35  # vizinhos com offset até isto de diferença = contínuo
WOBBLE_ANCHOR_S = 5.0   # match menor que isto não serve de âncora
WOBBLE_ANCHOR_RES = 12.0  # ... nem match com resíduo alto (plano preto/parado
                          # casa qualquer coisa com qualquer coisa)
WOBBLE_ANCHOR_LONG_S = 30.0  # match longo é estrutural, âncora mesmo ruidoso


def _is_anchor(t: Segment) -> bool:
    if t.kind != "match" or t.offset is None:
        return False
    dur = t.a_end - t.a_start
    return dur >= WOBBLE_ANCHOR_LONG_S or (
        dur >= WOBBLE_ANCHOR_S and t.residual <= WOBBLE_ANCHOR_RES)
WOBBLE_AUDIO_TOL_S = 0.10  # áudio no span dentro disto do offset das âncoras


# -------------------- 1. wobbles do vídeo --------------------

def _audio_continuous(dub_path, dub_a, orig_path, orig_a,
                      a0: float, a1: float, offset: float, log) -> bool:
    """O áudio dublado em [a0, a1] correlaciona com o original no `offset`
    das âncoras? Mede 3 janelas espalhadas pelo span (ou o que couber)."""
    span = a1 - a0
    win = min(WIN_S, max(3.0, span / 3))
    centers = [a0 + span * f for f in (0.2, 0.5, 0.8)] if span > 2 * win else [a0 + span / 2]
    good = 0
    for c in centers:
        st = min(max(a0, c - win / 2), a1 - win)
        try:
            tau, q = _measure(dub_path, dub_a, orig_path, orig_a, st, st + offset, win)
        except merger.MergeError:
            return False
        if q < MIN_PEAK_QUALITY or abs(tau) > WOBBLE_AUDIO_TOL_S:
            log(f"  span {a0:.1f}-{a1:.1f}s: áudio NÃO contínuo em {st:.1f}s "
                f"(desvio {tau * 1000:+.0f} ms, pico {q:.0f}) — estrutura mantida")
            return False
        good += 1
    return good > 0


def collapse_wobbles(segs: list[Segment], dub_path=None, dub_a=None,
                     orig_path=None, orig_a=None, log=print) -> list[Segment]:
    """Funde [match A][gaps/matches curtos][match B] quando A e B têm (quase)
    o mesmo offset: o vídeo escorregou num plano parado/preto; o áudio vai
    medir o offset real dentro do trecho contínuo.

    Span até WOBBLE_MAX_S (8 s) funde direto. Até WOBBLE_AUDIO_MAX_S (90 s —
    cena preta longa faz o DP passear por dezenas de segundos) funde SÓ se o
    áudio confirmar continuidade no offset das âncoras: um wobble de 23 s
    pode esconder um corte real de 3 s, e só o áudio distingue."""
    out: list[Segment] = []
    i = 0
    n = len(segs)
    can_audio = dub_path is not None
    while i < n:
        s = segs[i]
        if _is_anchor(s):
            # procura a próxima âncora dentro do teto
            j = i + 1
            span = 0.0
            ok = False
            limit = WOBBLE_AUDIO_MAX_S if can_audio else WOBBLE_MAX_S
            while j < n:
                t = segs[j]
                if _is_anchor(t):
                    same = abs(s.offset - t.offset) <= WOBBLE_OFF_TOL_S
                    if not same and can_audio and t.a_end - t.a_start < WOBBLE_ANCHOR_LONG_S:
                        # âncora candidata com OUTRO offset: só vale se o áudio
                        # confirmar esse offset — numa cena preta o vídeo casa
                        # qualquer coisa com qualquer coisa (resíduo baixo,
                        # offset falso) e isso não pode barrar a fusão
                        if not _audio_continuous(dub_path, dub_a, orig_path, orig_a,
                                                 t.a_start, t.a_end, t.offset,
                                                 lambda m: None):
                            log(f"  match {t.a_start:.1f}-{t.a_end:.1f}s (offset "
                                f"{t.offset:+.2f}s) sem confirmação de áudio — "
                                f"não é âncora")
                            span += t.a_end - t.a_start
                            if span > limit:
                                break
                            j += 1
                            continue
                    if j > i + 1 and same and span <= WOBBLE_MAX_S:
                        ok = True
                    elif j > i + 1 and same and span <= limit and can_audio:
                        ok = _audio_continuous(dub_path, dub_a, orig_path, orig_a,
                                               s.a_end, t.a_start, s.offset, log)
                        if ok:
                            log(f"  wobble de {span:.0f}s em {s.a_end:.1f}-"
                                f"{t.a_start:.1f}s: áudio contínuo — fundido")
                    break
                if t.kind == "replaced":
                    break  # tem checagem própria (_resolve_replaced_by_audio)
                if (t.kind in ("gap_dub", "gap_orig")
                        and max(t.a_end - t.a_start,
                                (t.b_end or 0) - (t.b_start or 0)) > limit):
                    break  # estrutura de verdade: não é wobble
                span += max(t.a_end - t.a_start,
                            (t.b_end or 0) - (t.b_start or 0))
                if span > limit:
                    break
                j += 1
            if ok:
                t = segs[j]
                merged = Segment(
                    "match", s.a_start, t.a_end, s.b_start, t.b_end,
                    slope=1.0, residual=(s.residual + t.residual) / 2,
                    confidence=min(s.confidence, t.confidence),
                    offset=s.offset,
                    note="wobble do vídeo fundido — offset decidido pelo áudio",
                    extra=dict(s.extra))
                # continua tentando estender a partir do merged
                segs = segs[:i] + [merged] + segs[j + 1:]
                n = len(segs)
                continue
        out.append(s)
        i += 1
    return out


# -------------------- 2-4. perfil por áudio, cortes em silêncio --------------------

def _measure(dub_path, dub_a, orig_path, orig_a, a_start, b_start, dur,
             radius=SEARCH_RADIUS_S) -> tuple[float, float]:
    with tempfile.TemporaryDirectory(prefix="align_refine_") as td:
        wa, wb = str(Path(td) / "a.wav"), str(Path(td) / "b.wav")
        _extract_wav_window(dub_path, dub_a, wa, a_start, dur)
        _extract_wav_window(orig_path, orig_a, wb, b_start, dur)
        return merger.gcc_phat_delay_with_confidence(
            merger._read_wav(wb), merger._read_wav(wa), merger.ALIGN_SR,
            max_tau=radius)


def _silences(dub_path: str, dub_a: int, t0: float, t1: float) -> list[tuple[float, float]]:
    """Trechos de silêncio do áudio dublado em [t0, t1] (tempos absolutos)."""
    t0 = max(0.0, t0)
    if t1 <= t0:
        return []
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{t0:.3f}", "-t",
           f"{t1 - t0:.3f}", "-i", dub_path, "-map", f"0:a:{dub_a}", "-vn",
           "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S}",
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out: list[tuple[float, float]] = []
    start = None
    for line in p.stderr.splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].split()[0])
            except ValueError:
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.split("silence_end:")[1].split()[0])
            except ValueError:
                continue
            out.append((t0 + start, t0 + end))
            start = None
    if start is not None:
        out.append((t0 + start, t1))
    return out


def _snap_to_silence(dub_path, dub_a, t: float, log,
                     radius: float = SILENCE_SNAP_S) -> float:
    """Ponto de corte -> meio do silêncio mais próximo (até `radius`).

    O raio depende de quem chama: cortes onde o conteúdo dos dois lados é o
    mesmo (junção de intervalo, divisão por perfil) aceitam silêncio longe
    (qualquer um serve); fronteiras de CONTEÚDO (cena substituída, recap)
    usam raio curto — o silêncio errado deslocaria material de verdade."""
    sil = _silences(dub_path, dub_a, t - radius, t + radius)
    if not sil:
        log(f"  corte em {t:.2f}s sem silêncio por perto — mantido")
        return t
    best = min(sil, key=lambda iv: abs((iv[0] + iv[1]) / 2 - t))
    c = (best[0] + best[1]) / 2
    log(f"  corte em {t:.2f}s encaixado no silêncio {best[0]:.2f}-{best[1]:.2f}s "
        f"({c - t:+.2f}s)")
    return c


def _profile(seg: Segment, dub_path, dub_a, orig_path, orig_a, log):
    """[(centro_a, offset_medido, q)] ao longo do match — None onde o pico é
    fraco (música/silêncio)."""
    dur = seg.a_end - seg.a_start
    win = min(WIN_S, dur * 0.8)
    pts = []
    t = seg.a_start + 1.0
    while t + win <= seg.a_end - 1.0 + 1e-6:
        try:
            tau, q = _measure(dub_path, dub_a, orig_path, orig_a,
                              t, t + seg.offset, win)
        except merger.MergeError as e:
            log(f"  janela {t:.0f}s falhou ({e})")
            tau, q = 0.0, 0.0
        pts.append((t + win / 2, seg.offset + tau if q >= MIN_PEAK_QUALITY else None, q))
        t += STEP_S
    if not pts or all(o is None for _, o, _ in pts):
        # segmento curto: uma medição centrada
        c = seg.a_start + (dur - win) / 2
        try:
            tau, q = _measure(dub_path, dub_a, orig_path, orig_a,
                              c, c + seg.offset, win)
            pts = [(c + win / 2, seg.offset + tau if q >= MIN_PEAK_QUALITY else None, q)]
        except merger.MergeError:
            pts = [(c + win / 2, None, 0.0)]
    return pts


def _bisect_change(dub_path, dub_a, orig_path, orig_a, seg: Segment,
                   t_lo: float, off_lo: float, t_hi: float, off_hi: float) -> float:
    """Ponto (tempo a) onde o offset passa de off_lo para off_hi, por
    bissecção com janelas curtas. Precisão ~BISECT_WIN_S/4."""
    lo, hi = t_lo, t_hi
    for _ in range(4):
        if hi - lo <= BISECT_WIN_S / 2:
            break
        mid = (lo + hi) / 2
        a0 = max(seg.a_start, mid - BISECT_WIN_S / 2)
        try:
            tau, q = _measure(dub_path, dub_a, orig_path, orig_a,
                              a0, a0 + seg.offset, BISECT_WIN_S)
        except merger.MergeError:
            break
        if q < MIN_PEAK_QUALITY:
            break
        off = seg.offset + tau
        if abs(off - off_lo) <= abs(off - off_hi):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _split_by_profile(seg: Segment, pts, dub_path, dub_a, orig_path, orig_a,
                      log) -> list[Segment]:
    """Divide o match onde o offset medido muda; cada parte leva o offset
    MEDIDO (mediana das janelas), corte encaixado em silêncio."""
    valid = [(t, o, q) for t, o, q in pts if o is not None]
    if not valid:
        seg.extra["refine"] = "pico fraco em todo o trecho — offset do vídeo mantido"
        seg.confidence *= 0.7
        return [seg]
    # agrupa janelas consecutivas por offset (mudança confirmada pela seguinte)
    groups: list[list[tuple[float, float]]] = [[(valid[0][0], valid[0][1])]]
    for k in range(1, len(valid)):
        t, o, _q = valid[k]
        cur = groups[-1]
        med = sorted(x[1] for x in cur)[len(cur) // 2]
        if abs(o - med) > CHANGE_TOL_S:
            nxt_ok = (k + 1 >= len(valid)
                      or abs(valid[k + 1][1] - o) <= GROUP_TOL_S)
            if nxt_ok:
                groups.append([(t, o)])
                continue
            # outlier isolado: ignora
            continue
        cur.append((t, o))
    parts: list[Segment] = []
    a_cursor = seg.a_start
    for gi, g in enumerate(groups):
        off = sorted(x[1] for x in g)[len(g) // 2]
        if gi + 1 < len(groups):
            nxt = groups[gi + 1]
            t_lo, off_lo = g[-1]
            t_hi, off_hi = nxt[0]
            cut = _bisect_change(dub_path, dub_a, orig_path, orig_a, seg,
                                 t_lo, off_lo, t_hi, off_hi)
            log(f"  offset muda {off_lo * 1000:+.0f} -> {off_hi * 1000:+.0f} ms "
                f"entre {t_lo:.0f}s e {t_hi:.0f}s; ponto ~{cut:.1f}s")
            cut = _snap_to_silence(dub_path, dub_a, cut, log)
            cut = min(max(cut, a_cursor + 0.5), seg.a_end - 0.5)
            a_end = cut
        else:
            a_end = seg.a_end
        part = Segment("match", a_cursor, a_end,
                       a_cursor + off, a_end + off,
                       slope=1.0, residual=seg.residual,
                       confidence=seg.confidence, offset=off,
                       note=seg.note, extra=dict(seg.extra))
        part.extra["video_offset"] = seg.offset
        part.extra["refine"] = (f"offset medido {off * 1000:+.1f} ms "
                                f"({len(g)} janela(s))")
        parts.append(part)
        a_cursor = a_end
    if len(parts) > 1:
        log(f"  match {seg.a_start:.0f}-{seg.a_end:.0f}s dividido em "
            f"{len(parts)} parte(s) por edição no áudio")
    return parts


REPLACED_AUDIO_TOL_S = 0.10   # áudio no mesmo offset dos vizinhos = mesma cena
STRAY_MATCH_S = 3.0           # match isolado curto com offset longe dos vizinhos
STRAY_AUDIO_MAX_S = 10.0      # até aqui, offset destoante precisa do aval do áudio
STRAY_OFF_S = 1.0
MERGE_OFF_TOL_S = 0.005       # matches vizinhos com offset igual (5 ms) fundem


def _neighbor_offset(segs: list[Segment], i: int) -> float | None:
    """Offset do match mais próximo (antes, senão depois)."""
    for j in range(i - 1, -1, -1):
        if segs[j].kind in ("match", "pal", "drift") and segs[j].offset is not None:
            return segs[j].offset
    for j in range(i + 1, len(segs)):
        if segs[j].kind in ("match", "pal", "drift") and segs[j].offset is not None:
            return segs[j].offset
    return None


def _resolve_replaced_by_audio(segs, dub_path, dub_a, orig_path, orig_a, log):
    """'Cena substituída' pelo VÍDEO cujo ÁUDIO correlaciona com o original no
    offset dos vizinhos NÃO é substituição para o nosso fim: o vídeo final é
    sempre o original, e o áudio dublado dali pertence dali. Vira match; só
    fica para revisão quando o áudio também diverge."""
    for i, seg in enumerate(segs):
        if seg.kind != "replaced" or seg.b_start is None:
            continue
        near = _neighbor_offset(segs, i)
        if near is None:
            continue
        dur = seg.a_end - seg.a_start
        if dur < 1.0:
            continue
        # o offset do vizinho vem do VÍDEO (grade de 0,25 s: erro até 125 ms —
        # maior que a tolerância). A referência tem que ser o offset REAL do
        # vizinho, medido pelo áudio colado na fronteira (caso real de campo, S01E03
        # 14:20: vídeo -5,00, áudio -5,14 → recusado por 41 ms de folga)
        ref = _neighbor_audio_offset(segs, i, dub_path, dub_a, orig_path, orig_a)
        if ref is None:
            ref = near
        try:
            tau, q = _measure(dub_path, dub_a, orig_path, orig_a,
                              seg.a_start, seg.a_start + ref, dur)
        except merger.MergeError:
            continue
        off = ref + tau
        if q < MIN_PEAK_QUALITY or abs(off - ref) > REPLACED_AUDIO_TOL_S:
            log(f"  'cena substituída' {seg.a_start:.1f}-{seg.a_end:.1f}s: áudio "
                f"NÃO confirma continuidade (offset {off * 1000:+.0f} ms vs "
                f"vizinho {ref * 1000:+.0f} ms, pico {q:.0f}) — fica para revisão")
        else:
            log(f"  'cena substituída' {seg.a_start:.1f}-{seg.a_end:.1f}s: áudio "
                f"contínuo (offset {off * 1000:+.0f} ms, pico {q:.0f}) — vira match")
            seg.kind = "match"
            seg.offset = off
            seg.b_start = seg.a_start + off
            seg.b_end = seg.a_end + off
            seg.slope = 1.0
            seg.confidence = max(seg.confidence, 0.6)
            seg.note = "vídeo divergente, áudio contínuo — tratado como match"
            seg.extra["refine"] = f"offset medido {off * 1000:+.1f} ms (áudio no lugar)"
            seg.extra["video_offset"] = near
    return segs


def _neighbor_audio_offset(segs, i, dub_path, dub_a, orig_path, orig_a,
                           win: float = 6.0) -> float | None:
    """Offset REAL (áudio) do match vizinho, medido numa janela colada na
    fronteira com segs[i] — antes, senão depois. None se nada confiável."""
    cands = []
    for j in range(i - 1, -1, -1):
        if segs[j].kind in ("match", "pal", "drift") and segs[j].offset is not None:
            a1 = min(segs[j].a_end, segs[i].a_start)
            a0 = max(segs[j].a_start, a1 - win)
            cands.append((a0, a1 - a0, segs[j].offset))
            break
    for j in range(i + 1, len(segs)):
        if segs[j].kind in ("match", "pal", "drift") and segs[j].offset is not None:
            a0 = max(segs[j].a_start, segs[i].a_end)
            a1 = min(segs[j].a_end, a0 + win)
            cands.append((a0, a1 - a0, segs[j].offset))
            break
    for a0, dur, guess in cands:
        if dur < 1.0:
            continue
        try:
            tau, q = _measure(dub_path, dub_a, orig_path, orig_a, a0, a0 + guess, dur)
        except merger.MergeError:
            continue
        if q >= MIN_PEAK_QUALITY:
            return guess + tau
    return None


def _drop_stray_matches(segs: list[Segment], log, dub_path=None, dub_a=None,
                        orig_path=None, orig_a=None) -> list[Segment]:
    """Match isolado, curto, com offset longe dos dois vizinhos = colisão de
    hash (créditos, tela preta) ou MONTAGEM (recap / "nos próximos": a cena
    existe mesmo, 30 s ou 20 min antes). Vira gap_orig (preenchido com o
    original) — melhor do que segundos de dublagem de outro ponto.

    Até STRAY_MATCH_S descarta direto. Entre isso e STRAY_AUDIO_MAX_S pergunta
    ao ÁUDIO no offset do próprio match: montagem tem narração/música por
    cima e não correlaciona (descarta); take reaproveitado com o mesmo áudio
    correlaciona e fica (inofensivo — é o mesmo som)."""
    out = list(segs)
    for i, seg in enumerate(out):
        dur = seg.a_end - seg.a_start
        if seg.kind != "match" or dur >= STRAY_AUDIO_MAX_S:
            continue
        neigh = [out[j].offset for j in (i - 1, i + 1)
                 if 0 <= j < len(out) and out[j].kind in ("match", "pal", "drift")
                 and out[j].offset is not None]
        if not neigh:
            neigh = [o for o in (_neighbor_offset(out, i),) if o is not None]
        if not (neigh and all(abs(seg.offset - o) > STRAY_OFF_S for o in neigh)):
            continue
        if dur >= STRAY_MATCH_S:
            if dub_path is None:
                continue
            try:
                tau, q = _measure(dub_path, dub_a, orig_path, orig_a,
                                  seg.a_start, seg.a_start + seg.offset, dur)
            except merger.MergeError:
                continue
            if q >= MIN_PEAK_QUALITY and abs(tau) <= REPLACED_AUDIO_TOL_S:
                log(f"  match curto {seg.a_start:.1f}-{seg.a_end:.1f}s com offset "
                    f"{seg.offset:+.2f}s (≠ vizinhos): áudio confirma (pico {q:.0f}) "
                    f"— mantido")
                continue
            why = f"áudio não confirma (pico {q:.0f}, desvio {tau * 1000:+.0f} ms)"
        else:
            why = "curto demais"
        log(f"  match espúrio {seg.a_start:.1f}-{seg.a_end:.1f}s (offset "
            f"{seg.offset:+.2f}s vs vizinhos; {why}) — descartado")
        out[i] = Segment("gap_orig", seg.a_start, seg.a_start,
                         seg.b_start, seg.b_end)
    return out


def _merge_adjacent(segs: list[Segment]) -> list[Segment]:
    """Matches vizinhos, contíguos e com o mesmo offset (5 ms) fundem: menos
    fatias no render — e nenhuma fronteira que não seja uma edição real."""
    out: list[Segment] = []
    for s in segs:
        if (out and s.kind == "match" and out[-1].kind == "match"
                and s.offset is not None and out[-1].offset is not None
                and abs(s.offset - out[-1].offset) <= MERGE_OFF_TOL_S
                and s.a_start - out[-1].a_end <= 0.5):
            prev = out[-1]
            prev.a_end = s.a_end
            prev.b_end = s.b_end
            continue
        out.append(s)
    return out


EXTRA_DUB_MAX_S = 180.0    # gap "só no dublado" entre matches: junção ou recap
EXTRA_DUB_TOL_S = 0.5      # ... quando a mudança de offset explica o gap
EXTRA_DUB_TOL_FRAC = 0.02  # ... com folga proporcional em gaps longos


def _tighten_extra_dub(segs: list[Segment], dub_path, dub_a, log) -> list[Segment]:
    """[match A][gap_dub][match B] com A.offset - B.offset ~ tamanho do gap
    = o DUBLADO tem material a mais (respiro/repetição numa junção); no
    original a cena segue direto, sem buraco. Então: o corte no dublado vai
    para o SILÊNCIO mais próximo, a fatia seguinte retoma exatamente onde a
    anterior parou no original (b contínuo) e o excesso do dublado é pulado
    dentro do silêncio. Antes, as fronteiras quantizadas do vídeo (0,25 s)
    deixavam um buraco de ~100 ms no original que era preenchido com áudio
    ORIGINAL no meio da fala — sem nenhum motivo.

    É AQUI que toda fronteira de gap_dub ENTRE dois matches se resolve: mexer
    nas duas bordas separadamente furaria a continuidade do original, já que
    os dois lados têm offsets diferentes."""
    out: list[Segment] = []
    i = 0
    while i < len(segs):
        s = segs[i]
        if (s.kind == "match" and i + 2 < len(segs)
                and segs[i + 1].kind == "gap_dub"
                and segs[i + 2].kind == "match"
                and s.offset is not None and segs[i + 2].offset is not None):
            g, nxt = segs[i + 1], segs[i + 2]
            gap = g.a_end - g.a_start
            extra = s.offset - nxt.offset  # segundos a mais no dublado
            tol = max(EXTRA_DUB_TOL_S, EXTRA_DUB_TOL_FRAC * gap)
            if 0 < gap <= EXTRA_DUB_MAX_S and extra > 0 \
                    and abs(extra - gap) <= tol:
                cut = _snap_to_silence(dub_path, dub_a, s.a_end, log)
                cut = min(max(cut, s.a_start + 0.5), nxt.a_end - extra - 0.5)
                s.a_end = cut
                s.b_end = cut + s.offset
                nxt.a_start = cut + extra
                nxt.b_start = s.b_end
                log(f"  junção com {extra * 1000:.0f} ms a mais no dublado em "
                    f"{cut:.2f}s: corte no silêncio, original contínuo")
                # o gap fica no EDL (a revisão mostra quanto do dublado foi
                # descartado), agora com os limites certos e b colapsado —
                # o render o ignora, porque b_start == b_end
                g.a_start, g.a_end = cut, cut + extra
                g.b_start = g.b_end = s.b_end
                out.append(s)
                out.append(g)
                # só até aqui: `nxt` volta ao laço como início de uma POSSÍVEL
                # próxima junção — num episódio elas vêm em série, e avançar 3
                # fazia as junções dispararem alternadamente
                i += 2
                continue
        out.append(s)
        i += 1
    return out


# -------------------- 5. fronteiras entre segmentos --------------------
# Cortes DENTRO de um match já saem do áudio (perfil + bissecção + silêncio);
# as fronteiras ENTRE segmentos vinham cruas do vídeo — grade de 0,25 s.

CUT_A_CONT_S = 0.75    # dub contínuo na junção: buraco em `a` até isto
CUT_BRACKET_S = 4.0    # colchete da bissecção em volta da junção do vídeo
CUT_WIN_S = 3.0        # janela curta das medições de presença
CUT_SNAP_S = 1.5       # silêncio até isto do ponto bissectado
EDGE_SNAP_S = 1.0      # borda de conteúdo (recap/substituída): raio curto
EDGE_MIN_MATCH_S = 1.5 # match menor que isto não tem borda para mexer


def _bisect_junction(dub_path, dub_a, orig_path, orig_a,
                     t_lo: float, t_hi: float,
                     off_a: float, off_b: float) -> float | None:
    """Ponto (tempo `a`) onde o dub deixa de correlacionar em off_a e passa a
    correlacionar em off_b — a junção de uma cena CORTADA do dublado, onde o
    áudio dublado é contínuo e só o alvo no original salta.

    Numa janela curta centrada no palpite, mede o pico nos DOIS offsets: antes
    do corte ganha off_a, depois ganha off_b, perto do corte empatam (a janela
    contém os dois lados). None = música/silêncio não deixou medir nada.
    """
    lo, hi = t_lo, t_hi
    decisive = False
    for _ in range(5):
        if hi - lo <= CUT_WIN_S / 4:
            break
        mid = (lo + hi) / 2
        a0 = mid - CUT_WIN_S / 2
        try:
            tau_a, q_a = _measure(dub_path, dub_a, orig_path, orig_a,
                                  a0, a0 + off_a, CUT_WIN_S)
            tau_b, q_b = _measure(dub_path, dub_a, orig_path, orig_a,
                                  a0, a0 + off_b, CUT_WIN_S)
        except merger.MergeError:
            break
        ga = q_a >= MIN_PEAK_QUALITY and abs(tau_a) <= 0.15
        gb = q_b >= MIN_PEAK_QUALITY and abs(tau_b) <= 0.15
        if ga and (not gb or q_a > 2 * q_b):
            lo = mid
            decisive = True
        elif gb and (not ga or q_b > 2 * q_a):
            hi = mid
            decisive = True
        elif ga and gb:
            # os dois lados fortes: a janela contém o corte — está aqui perto
            lo = hi = mid
            decisive = True
            break
        else:
            break   # nem um nem outro (música/silêncio): sem informação
    return (lo + hi) / 2 if decisive else None


def _refine_cut_junctions(segs: list[Segment], dub_path, dub_a,
                          orig_path, orig_a, log) -> list[Segment]:
    """[match A][gap_orig][match B] com o DUB contínuo (cena cortada da versão
    dublada): a fronteira exata em `a` vem do áudio (bissecção de presença) e
    cai num silêncio — não na grade de 0,25 s do vídeo. Os lados b são
    recomputados dos offsets medidos, então o preenchimento se ajusta."""
    for i in range(1, len(segs) - 1):
        g = segs[i]
        if g.kind != "gap_orig":
            continue
        a, b = segs[i - 1], segs[i + 1]
        if not (a.kind == "match" and b.kind == "match"
                and a.offset is not None and b.offset is not None):
            continue
        if b.a_start - a.a_end > CUT_A_CONT_S:
            continue    # o dub NÃO é contínuo aqui — não é este padrão
        if b.offset - a.offset <= 0.2:
            continue    # sem material extra no original: suspeito, não mexe
        video_cut = (a.a_end + b.a_start) / 2
        t_lo = max(a.a_start + 0.5, video_cut - CUT_BRACKET_S)
        t_hi = min(b.a_end - 0.5, video_cut + CUT_BRACKET_S)
        if t_hi - t_lo < CUT_WIN_S / 2:
            continue
        cut = _bisect_junction(dub_path, dub_a, orig_path, orig_a,
                               t_lo, t_hi, a.offset, b.offset)
        if cut is None:
            cut = video_cut   # indecisivo: fica o palpite do vídeo
        else:
            log(f"  junção de cena cortada ~{video_cut:.1f}s: áudio localizou "
                f"o corte em {cut:.2f}s")
        cut = _snap_to_silence(dub_path, dub_a, cut, log, radius=CUT_SNAP_S)
        cut = min(max(cut, a.a_start + 0.5), b.a_end - 0.5)
        a.a_end = cut
        a.b_end = cut + a.offset
        b.a_start = cut
        b.b_start = cut + b.offset
        g.a_start = g.a_end = cut
        g.b_start, g.b_end = a.b_end, b.b_start
    return segs


def _movable_edge(segs: list[Segment], i: int, side: str) -> bool:
    """Esta borda de match pode ser deslocada sozinha?

    NÃO quando o vizinho é um gap_dub ENTRE dois matches: ali o original é
    contínuo (b_end de um == b_start do outro) e os lados têm offsets
    diferentes, então mover uma borda só abriria buraco/sobreposição no
    original — esse caso é do `_tighten_extra_dub`, que move as duas pontas
    juntas. gap_dub com match de um lado só (abertura/rabo do arquivo) é
    seguro, e `replaced` também: ali o original tem extensão própria."""
    j = i + 1 if side == "right" else i - 1
    if not 0 <= j < len(segs):
        return False
    g = segs[j]
    if g.kind == "replaced":
        return True
    if g.kind != "gap_dub":
        return False
    k = j + 1 if side == "right" else j - 1
    other = segs[k] if 0 <= k < len(segs) else None
    return other is None or other.kind not in ("match", "drift", "pal")


def _snap_gap_edges(segs: list[Segment], dub_path, dub_a,
                    log) -> list[Segment]:
    """Bordas de match encostadas em conteúdo divergente (cena substituída ou
    gap_dub na ponta do arquivo): encaixa a borda no silêncio do DUB num raio
    curto. O corte real está a
    <= 0,25 s da grade do vídeo e quase sempre há uma respiração ali; raio
    curto porque silêncio longe deslocaria material de verdade."""
    for i, seg in enumerate(segs):
        if (seg.kind != "match" or seg.offset is None
                or seg.a_end - seg.a_start < EDGE_MIN_MATCH_S):
            continue
        prv = segs[i - 1] if i > 0 else None
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        # borda direita: match -> gap_dub/replaced
        if (nxt is not None and nxt.kind in ("gap_dub", "replaced")
                and nxt.a_end - nxt.a_start > 0
                and _movable_edge(segs, i, "right")):
            c = _snap_to_silence(dub_path, dub_a, seg.a_end, log,
                                 radius=EDGE_SNAP_S)
            c = min(max(c, seg.a_start + 0.5), nxt.a_end - 0.1)
            if abs(c - seg.a_end) > 1e-6:
                seg.a_end = c
                seg.b_end = c + seg.offset
                nxt.a_start = c
                if nxt.kind == "gap_dub":
                    nxt.b_start = nxt.b_end = seg.b_end
                else:
                    nxt.b_start = seg.b_end
        # borda esquerda: gap_dub/replaced -> match
        if (prv is not None and prv.kind in ("gap_dub", "replaced")
                and prv.a_end - prv.a_start > 0
                and _movable_edge(segs, i, "left")):
            c = _snap_to_silence(dub_path, dub_a, seg.a_start, log,
                                 radius=EDGE_SNAP_S)
            c = min(max(c, prv.a_start + 0.1), seg.a_end - 0.5)
            if abs(c - seg.a_start) > 1e-6:
                seg.a_start = c
                seg.b_start = c + seg.offset
                prv.a_end = c
                if prv.kind == "gap_dub":
                    prv.b_start = prv.b_end = seg.b_start
                else:
                    prv.b_end = seg.b_start
    return segs


def refine_offsets(segs: list[Segment], dub_path: str, dub_a: int,
                   orig_path: str, orig_a: int, log=print) -> list[Segment]:
    """Refino por áudio: funde wobbles do vídeo, resolve 'substituídas' cujo
    áudio é contínuo, mede o perfil de offset em cada match e divide onde há
    edição (corte em silêncio), descarta matches espúrios, refina as
    FRONTEIRAS entre segmentos (junção de cena cortada por bissecção; bordas
    de recap/substituída no silêncio) e funde vizinhos iguais. Retorna a
    lista NOVA de segmentos (a estrutura pode mudar)."""
    segs = collapse_wobbles(segs, dub_path, dub_a, orig_path, orig_a, log)
    segs = _resolve_replaced_by_audio(segs, dub_path, dub_a, orig_path, orig_a, log)
    # substituídas resolvidas podem abrir novos wobbles
    segs = collapse_wobbles(segs, dub_path, dub_a, orig_path, orig_a, log)
    out: list[Segment] = []
    for seg in segs:
        if seg.kind not in ("match", "drift", "pal") or seg.offset is None:
            out.append(seg)
            continue
        seg.extra["video_offset"] = seg.offset
        if seg.a_end - seg.a_start < MIN_SEGMENT_S:
            seg.extra["refine"] = "curto demais — herda vizinho"
            out.append(seg)
            continue
        pts = _profile(seg, dub_path, dub_a, orig_path, orig_a, log)
        out.extend(_split_by_profile(seg, pts, dub_path, dub_a, orig_path,
                                     orig_a, log))
    _inherit_short(out)
    out = _drop_stray_matches(out, log, dub_path, dub_a, orig_path, orig_a)
    out = _tighten_extra_dub(out, dub_path, dub_a, log)
    # fronteiras ENTRE segmentos: também saem da grade do vídeo para o áudio
    out = _refine_cut_junctions(out, dub_path, dub_a, orig_path, orig_a, log)
    out = _snap_gap_edges(out, dub_path, dub_a, log)
    return _merge_adjacent(out)


def _inherit_short(segs: list[Segment]):
    """Match curto (sem refino próprio) herda do vizinho refinado mais próximo:
    o offset MEDIDO dele quando o vídeo não indica mudança real entre os dois
    (|diferença de offsets do vídeo| <= WOBBLE_OFF_TOL_S — 6 s de plano
    parado com o vídeo escorregando 70 ms), senão só o AJUSTE (medido -
    vídeo), preservando a mudança que o vídeo viu."""
    refined = [s for s in segs
               if s.kind in ("match", "drift", "pal")
               and str(s.extra.get("refine", "")).startswith("offset medido")]
    if not refined:
        return
    for seg in segs:
        if seg.kind not in ("match", "drift", "pal"):
            continue
        if seg.extra.get("refine") == "curto demais — herda vizinho":
            near = min(refined, key=lambda r: abs(r.a_start - seg.a_start))
            v_seg, v_near = seg.extra["video_offset"], near.extra["video_offset"]
            if abs(v_seg - v_near) <= WOBBLE_OFF_TOL_S:
                seg.offset = near.offset
            else:
                seg.offset = v_seg + (near.offset - v_near)
            if seg.b_start is not None:
                seg.b_start = seg.a_start + seg.offset
                seg.b_end = seg.a_end + seg.offset


# -------------------- validação do caminho rápido --------------------

def scan_constant_offset(dub_path: str, dub_a: int, orig_path: str,
                         orig_a: int, duration: float,
                         step: float = 300.0, win: float = WIN_S,
                         tol: float = 0.050) -> tuple[bool, list[tuple[float, float, float]]]:
    """O offset é constante ao longo do episódio inteiro? Mede a cada `step`
    segundos (busca ±60 s, como o merge de filmes). Duas janelas (0:30 e ~60%)
    NÃO bastam em série: junções de intervalo comercial (2 frames) e cenas
    cortadas aparecem em qualquer ponto. `tol` = 50 ms, o limiar de lip sync.
    Retorna (constante?, [(t, offset, q)]). Janelas com pico fraco são
    ignoradas."""
    pts = []
    t = 30.0
    while t + win < duration - 30.0:
        try:
            tau, q = _measure(dub_path, dub_a, orig_path, orig_a, t, t, win,
                              radius=merger.MAX_OFFSET_SECONDS)
        except merger.MergeError:
            t += step
            continue
        if q >= MIN_PEAK_QUALITY:
            pts.append((t, tau, q))
        t += step
    if len(pts) < 2:
        return True, pts
    offs = [o for _, o, _ in pts]
    return (max(offs) - min(offs)) <= tol, pts
