"""CLI avulso do merge (a logica mora em services/merger.py).

Uso:
    python merge.py <arquivo1> <arquivo2> <saida.mkv> [--audio-lang pt]
    python merge.py --series <pasta1> <pasta2> <pasta_saida> [--audio-lang pt]
    python merge.py --segments <arquivo1> <arquivo2> <saida.mkv> [--audio-lang pt]

- Escolhe o melhor video entre os dois; melhor audio por lingua entre os dois.
- Se o arquivo de melhor video ja tiver audio no idioma alvo, nao faz merge:
  cria um hardlink no destino (fallback: copia — nunca symlink).
- Alinha os audios do outro arquivo via GCC-PHAT (re-encodados: AAC se
  mono/estereo, AC3 se multicanal — preserva a ordem dos canais surround);
  o resto e stream copy.

--series: file1/file2 viram PASTAS (escaneadas recursivamente). Os episodios
sao casados pelo padrao SxxExx no nome do arquivo e cada par vira um merge
normal, salvo na pasta de saida como S01E02.mkv. Episodios presentes em so
uma das pastas sao pulados (com aviso); saida que ja existe e pulada (da para
rodar de novo e continuar de onde parou); erro num episodio nao para os outros.

--segments: alinhamento POR SEGMENTO (para cortes/versoes diferentes, onde um
offset constante nao basta). Detecta cortes de cena no arquivo de referencia
(trechos PRETOS no video + SILENCIO no audio, com validacao cruzada — um corte
so vale onde os dois coincidem) e mede o offset de cada segmento separadamente.
Se os offsets concordam, cai no merge classico (stream copy); se divergem, o
audio dublado e remontado fatia a fatia. Combina com --series.
Ajustes: --black-*/--silence-* afinam a deteccao; --disable-black ou
--disable-silence usam so um dos detectores (desliga a validacao cruzada).
"""
import argparse
import re
import sys
from pathlib import Path

from services.merger import MergeError, merge
from services.merger_segments import SegmentParams, merge_segmented

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg"}

# S01E02 / s1e2 / S01.E02 / S01 E02 / S01_E02 — normalizado para S01E02
_EPISODE_RE = re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})")


def _episode_key(name: str) -> str | None:
    """Extrai a chave SxxExx do nome do arquivo (None se nao tiver)."""
    m = _EPISODE_RE.search(name)
    if not m:
        return None
    return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"


def _scan_episodes(root: Path) -> dict[str, Path]:
    """Mapeia SxxExx -> arquivo de video, varrendo a pasta recursivamente.

    Chave repetida (ex.: o mesmo episodio em 720p e 1080p) fica com o MAIOR
    arquivo — mesmo criterio do pipeline normal para pastas de torrent.
    """
    episodes: dict[str, Path] = {}
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if "sample" in f.name.lower():
            continue
        key = _episode_key(f.name)
        if not key:
            print(f"  (ignorado, sem SxxExx no nome: {f.name})")
            continue
        cur = episodes.get(key)
        if cur is None or f.stat().st_size > cur.stat().st_size:
            if cur is not None:
                print(f"  ({key}: dois arquivos, ficando com o maior: {f.name})")
            episodes[key] = f
    return episodes


def run_series(dir1: Path, dir2: Path, out_dir: Path, target_lang: str | None,
               merge_fn=None) -> int:
    """Faz o merge de cada episodio casado por SxxExx. Retorna o nº de falhas.

    merge_fn: funcao de merge a usar (default: o merge classico) — com
    --segments o CLI passa a versao segmentada.
    """
    merge_fn = merge_fn or merge
    for d in (dir1, dir2):
        if not d.is_dir():
            sys.exit(f"ERRO: pasta não existe: {d}")

    print(f"Escaneando {dir1} ...")
    eps1 = _scan_episodes(dir1)
    print(f"Escaneando {dir2} ...")
    eps2 = _scan_episodes(dir2)

    common = sorted(set(eps1) & set(eps2))
    for key in sorted(set(eps1) ^ set(eps2)):
        where = dir1 if key in eps1 else dir2
        print(f"⚠️  {key}: só existe em {where} — pulado")
    if not common:
        sys.exit("ERRO: nenhum episódio em comum entre as duas pastas")

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{len(common)} episódio(s) para converter -> {out_dir}\n")

    failures: list[tuple[str, str]] = []
    done = skipped = 0
    for i, key in enumerate(common, 1):
        output = out_dir / f"{key}.mkv"
        # saida ja existente (qualquer extensao — o hardlink preserva a original)
        existing = list(out_dir.glob(f"{key}.*"))
        if existing:
            print(f"[{i}/{len(common)}] {key}: já existe ({existing[0].name}) — pulado")
            skipped += 1
            continue
        print(f"[{i}/{len(common)}] {key}: {eps1[key].name} + {eps2[key].name}")
        try:
            result = merge_fn(str(eps1[key]), str(eps2[key]), str(output),
                              target_lang=target_lang)
        except MergeError as e:
            print(f"  ❌ ERRO em {key}: {e}\n")
            failures.append((key, str(e)))
            continue
        for note in result.notes:
            print(f"  - {note}")
        print(f"  ✅ {result.output}" + (" (hardlink, sem merge)" if result.linked else "") + "\n")
        done += 1

    print(f"Resumo: {done} convertido(s), {skipped} pulado(s), {len(failures)} falha(s)")
    for key, err in failures:
        print(f"  ❌ {key}: {err}")
    return len(failures)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file1", help="arquivo de video 1 (ex.: versao original); com --series, a PASTA 1")
    ap.add_argument("file2", help="arquivo de video 2 (ex.: versao dublada); com --series, a PASTA 2")
    ap.add_argument("output", help="arquivo de saida (.mkv recomendado); com --series, a PASTA de saida")
    ap.add_argument("--audio-lang", default=None,
                    help="idioma alvo (pt/es/it/de/fr ou codigo ISO tipo por/spa)")
    ap.add_argument("--series", action="store_true",
                    help="modo serie: escaneia as duas pastas, casa episodios por "
                         "SxxExx e faz o merge de cada par (saida: SXXEXX.mkv)")

    d = SegmentParams()  # defaults dos thresholds exibidos no --help
    seg = ap.add_argument_group(
        "alinhamento por segmentos (--segments)",
        "detecta cortes de cena (preto no video + silencio no audio, validacao "
        "cruzada) e alinha cada segmento separadamente")
    seg.add_argument("--segments", action="store_true",
                     help="ativa o alinhamento por segmentos")
    seg.add_argument("--disable-black", action="store_true",
                     help="nao usa blackdetect: cortes so pelo silencio (sem validacao cruzada)")
    seg.add_argument("--disable-silence", action="store_true",
                     help="nao usa silencedetect: cortes so pelo preto (sem validacao cruzada)")
    seg.add_argument("--black-min-dur", type=float, default=d.black_min_dur, metavar="S",
                     help=f"duracao minima do trecho preto, em s (default: {d.black_min_dur})")
    seg.add_argument("--black-pix-th", type=float, default=d.black_pix_th, metavar="0-1",
                     help=f"quao escuro um pixel precisa ser (default: {d.black_pix_th})")
    seg.add_argument("--black-pic-th", type=float, default=d.black_pic_th, metavar="0-1",
                     help=f"fracao da imagem que precisa estar preta (default: {d.black_pic_th})")
    seg.add_argument("--silence-noise", type=float, default=d.silence_noise_db, metavar="dB",
                     help=f"teto de ruido do silencio, em dB (default: {d.silence_noise_db})")
    seg.add_argument("--silence-min-dur", type=float, default=d.silence_min_dur, metavar="S",
                     help=f"duracao minima do silencio, em s (default: {d.silence_min_dur})")
    seg.add_argument("--match-tolerance", type=float, default=d.match_tolerance, metavar="S",
                     help=f"distancia maxima preto<->silencio na validacao cruzada, em s "
                          f"(default: {d.match_tolerance})")
    seg.add_argument("--min-segment", type=float, default=d.min_segment, metavar="S",
                     help=f"tamanho minimo de um segmento, em s (default: {d.min_segment})")
    seg.add_argument("--seg-align-window", type=float, default=d.seg_align_dur, metavar="S",
                     help=f"teto da janela de correlacao por segmento, em s "
                          f"(default: {d.seg_align_dur})")
    args = ap.parse_args()

    if args.disable_black and args.disable_silence:
        ap.error("--disable-black e --disable-silence juntos desligariam a deteccao inteira")
    if (args.disable_black or args.disable_silence) and not args.segments:
        ap.error("--disable-black/--disable-silence so fazem sentido com --segments")

    if args.segments:
        params = SegmentParams(
            black_min_dur=args.black_min_dur, black_pix_th=args.black_pix_th,
            black_pic_th=args.black_pic_th, silence_noise_db=args.silence_noise,
            silence_min_dur=args.silence_min_dur, match_tolerance=args.match_tolerance,
            min_segment=args.min_segment, seg_align_dur=args.seg_align_window,
            use_black=not args.disable_black, use_silence=not args.disable_silence)

        def merge_fn(f1, f2, out, target_lang=None):
            return merge_segmented(f1, f2, out, target_lang=target_lang, params=params)
    else:
        merge_fn = None  # run_series/abaixo caem no merge classico

    if args.series:
        failures = run_series(Path(args.file1), Path(args.file2), Path(args.output),
                              args.audio_lang, merge_fn=merge_fn)
        sys.exit(1 if failures else 0)

    try:
        result = (merge_fn or merge)(args.file1, args.file2, args.output,
                                     target_lang=args.audio_lang)
    except MergeError as e:
        sys.exit(f"ERRO: {e}")
    for note in result.notes:
        print(f"  - {note}")
    print(f"Saída: {result.output}" + (" (hardlink, sem merge)" if result.linked else ""))


if __name__ == "__main__":
    main()
