"""CLI avulso do merge (a logica mora em services/merger.py).

Uso:
    python merge.py <arquivo1> <arquivo2> <saida.mkv> [--audio-lang pt]
    python merge.py --series <pasta1> <pasta2> <pasta_saida> [--audio-lang pt]

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
"""
import argparse
import re
import sys
from pathlib import Path

from services.merger import MergeError, merge

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


def run_series(dir1: Path, dir2: Path, out_dir: Path, target_lang: str | None) -> int:
    """Faz o merge de cada episodio casado por SxxExx. Retorna o nº de falhas."""
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
            result = merge(str(eps1[key]), str(eps2[key]), str(output),
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
    args = ap.parse_args()

    if args.series:
        failures = run_series(Path(args.file1), Path(args.file2), Path(args.output),
                              args.audio_lang)
        sys.exit(1 if failures else 0)

    try:
        result = merge(args.file1, args.file2, args.output, target_lang=args.audio_lang)
    except MergeError as e:
        sys.exit(f"ERRO: {e}")
    for note in result.notes:
        print(f"  - {note}")
    print(f"Saída: {result.output}" + (" (hardlink, sem merge)" if result.linked else ""))


if __name__ == "__main__":
    main()
