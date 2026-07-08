"""CLI avulso do merge (a logica mora em services/merger.py).

Uso:
    python merge.py <arquivo1> <arquivo2> <saida.mkv> [--audio-lang pt]

- Escolhe o melhor video entre os dois; melhor audio por lingua entre os dois.
- Se o arquivo de melhor video ja tiver audio no idioma alvo, nao faz merge:
  cria um hardlink no destino (fallback: copia — nunca symlink).
- Alinha os audios do outro arquivo via GCC-PHAT (re-encodados: AAC se
  mono/estereo, AC3 se multicanal — preserva a ordem dos canais surround);
  o resto e stream copy.
"""
import argparse
import sys

from services.merger import MergeError, merge


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file1", help="arquivo de video 1 (ex.: versao original)")
    ap.add_argument("file2", help="arquivo de video 2 (ex.: versao dublada)")
    ap.add_argument("output", help="arquivo de saida (.mkv recomendado)")
    ap.add_argument("--audio-lang", default=None,
                    help="idioma alvo (pt/es/it/de/fr ou codigo ISO tipo por/spa)")
    args = ap.parse_args()

    try:
        result = merge(args.file1, args.file2, args.output, target_lang=args.audio_lang)
    except MergeError as e:
        sys.exit(f"ERRO: {e}")
    for note in result.notes:
        print(f"  - {note}")
    print(f"Saída: {result.output}" + (" (hardlink, sem merge)" if result.linked else ""))


if __name__ == "__main__":
    main()
