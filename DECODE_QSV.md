# Perda silenciosa de frames no decode QSV — o que foi feito e o que falta

Documento de referência do incidente de 24/07/2026, em que filmes recomprimidos
saíram com até 74% dos frames faltando **sem nenhum erro** em lugar nenhum.

O item 2 (trocar o decode para VAAPI) **já está implementado**. Os itens 1 e 3
continuam **pendentes** e estão especificados aqui.

---

## O incidente

Dois filmes recomprimidos ficaram corrompidos. O sintoma que apareceu primeiro
foi "keyframes demais espaçados" (um a cada 10–20 min), mas isso era só
consequência: **os frames em si não estavam lá**.

| filme | frames esperados | frames reais | integridade |
|---|---|---|---|
| Filme A | 130.232 | 34.315 | **26%** ❌ |
| Filme B | 134.722 | 92.402 | **69%** ❌ |
| Filme C | 205.538 | 205.529 | 100% ✅ |
| Filme D | 206.273 | 202.608 | 100% ✅ |
| Filme E | 170.765 | 170.765 | 100% ✅ |

O Filme A tinha um bloco de 10 minutos (4800–5400s) com **zero frames**.
Todos os 5 jobs terminaram com `status=done`.

## A causa

Isolada por eliminação, num recorte de 300s (7201 frames de entrada). Só uma
variável mudava o resultado — todo o resto (`+genpts`, `max_interleave_delta`,
preset, áudio/legendas mapeados) era indiferente:

| decoder | packets lidos | frames decodificados | erros | exit |
|---|---|---|---|---|
| software | 7201 | **7201** ✅ | 0 | 0 |
| **QSV** | 7201 | **3084** ❌ | **0** | **0** |
| VAAPI (mesma GPU) | 7201 | **7201** ✅ | 0 | 0 |

O próprio ffmpeg imprime a contradição e segue em frente:

```
Input stream #0:0 (video): 7201 packets read (3330187762 bytes); 3084 frames decoded; 0 decode errors;
```

**O decoder QSV/oneVPL da Arc engole packets e reporta sucesso.** O encoder QSV
é íntegro — com decode por software ou VAAPI a saída sai completa. O problema é
exclusivamente o caminho de *decode* do QSV.

Bug conhecido do driver Intel, sem fix oficial:

- [intel/media-driver#1740](https://github.com/intel/media-driver/issues/1740) —
  decode QSV trava/para quando parâmetros mudam no meio do stream; CPU e VAAPI
  passam sem problema. Explica por que só 2 de 5 quebraram: depende do conteúdo,
  não do tamanho.
- [intel/media-driver#1576](https://github.com/intel/media-driver/issues/1576) —
  Arc A380 DG2 (a placa deste servidor), saída com keyframes errados via QSV.

---

## ✅ Item 2 — decode por VAAPI (FEITO)

`_hw_decode_args()` em `services/transcode.py` passou a emitir
`-hwaccel vaapi -hwaccel_device /dev/dri/renderD128` quando o encoder é `*_qsv`.
O **encode continua QSV** (é onde está o ganho e ele não tem defeito).

Um detalhe que o teste revelou: o hand-off VAAPI→QSV **em VRAM não funciona**
(`Impossible to convert between the formats`, inclusive com
`hwmap=derive_device=qsv`). Só funciona baixando os frames para a RAM — por isso
`vram` agora é sempre `False` no caminho QSV.

Custo medido (recorte de 300s, 4K HDR):

| caminho | tempo | frames |
|---|---|---|
| QSV decode (antigo) | 30s | 3084 ❌ |
| **VAAPI → QSV (atual)** | **179s** | 7201 ✅ |
| SW → QSV | 212s | 7201 ✅ |

Os 30s do QSV eram artificiais: ele processava 43% do trabalho. Entre as opções
corretas, VAAPI é ~16% mais rápido que software e tira carga da CPU.

---

## ⬜ Item 1 — validação de integridade pós-encode (PENDENTE, prioridade alta)

### Por que

O item 2 remove **esta** causa. Não remove a **classe** de falha: um encoder ou
decoder de hardware pode entregar menos frames e sair com código 0, e hoje o
pipeline aceita o resultado e marca o job como `done`. Foi assim que 96 mil
frames sumiram sem ninguém notar — e só foi descoberto por acaso, dias depois,
porque o usuário reparou nos keyframes.

Esta é a única camada que protege contra bugs de driver **que ainda não
conhecemos** (troca de GPU, atualização de driver, NVENC, um ffmpeg novo). É por
isso que ela importa mais que o item 2: o item 2 conserta um caso, o item 1
transforma "corrompeu em silêncio" em "o job falhou e eu sei por quê".

### O que fazer

Depois do ffmpeg terminar e **antes** de substituir o arquivo original
(`services/jobs.py::_recompress`) ou de dar o merge por concluído, comparar a
contagem de frames da saída com a da fonte e **falhar o job** se divergir.

Duas formas de obter os números, em ordem de preferência:

1. **Parsear o stderr do próprio ffmpeg** (barato, já temos o processo). Com
   `-loglevel verbose` ele imprime, por stream de entrada:
   `N packets read (... bytes); M frames decoded; K decode errors;`
   Se `M < N` de forma relevante, ou `K > 0`, o decode perdeu frames.
   ⚠️ Isso exige subir o loglevel — hoje o pipeline roda com `-loglevel error`
   (`services/transcode.py::convert_single` e `services/merger.py`), que
   **esconde justamente essa linha**. Subir para `warning` não basta: a linha só
   sai em `verbose`. Avaliar `-loglevel verbose` só na linha de sumário, ou usar
   a opção 2.
2. **Contar com ffprobe** na saída e na fonte:
   `ffprobe -v error -count_packets -select_streams v:0 -show_entries stream=nb_read_packets -of csv=p=0 ARQUIVO`
   Custo real medido: alguns minutos num filme 4K de 4 GB (lê o arquivo todo).
   Aceitável no fim de um encode que levou ~1h, mas **não** rode isso em loop.

**Margem de tolerância:** exigir igualdade exata é frágil — corte de `-ss/-t`,
frame duplicado no fim e arredondamento de fps geram diferença de 1–2 frames.
Sugestão: falhar se a saída tiver **menos de 99,5%** dos frames da fonte. Os
casos reais deste incidente foram 26% e 69% — muito longe do limiar, então uma
margem generosa já pega tudo sem falso positivo. (Verificado: Fast X tem 98,2%
pela conta duração×fps mas 100% pela contagem real de packets — **compare
packets com packets**, nunca com uma estimativa de duração×fps, que gera falso
positivo.)

**Onde falhar:** em `_recompress` isso é crítico — com `replace=True` o original
é substituído. A validação tem que rodar **antes** da troca, e em caso de
divergência manter o original e marcar o job como erro com a contagem no texto
(ex.: "saída tem 34.315 de 130.232 frames — arquivo descartado").

### Teste

`tests/test_transcode.py` já tem infra com ffmpeg real (`@pytest.mark.ffmpeg`).
Dá para gerar um vídeo sintético com `-f lavfi -i testsrc`, truncar a saída de
propósito e verificar que a validação reprova.

---

## ⬜ Item 3 — `_hw_decode_works` não detecta esta classe de falha (PENDENTE, prioridade média)

### Por que

`services/transcode.py::_hw_decode_works` decide se o decode em HW é usado. Ele
decodifica **1 frame** (`-frames:v 1`) e olha só o `returncode`:

```python
cmd = [..., "-i", path, "-map", f"0:v:{v_index}", "-frames:v", "1", "-f", "null", "-"]
return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
```

No incidente, o decoder QSV passava nesse teste com folga: o primeiro frame
decodifica bem, a perda começa depois de milhares de frames (nos dois Shrek a
densidade era perfeita nos primeiros ~1200s e degradava a partir daí). O teste
dá uma **falsa sensação de validação** — prova que a GPU abre o arquivo, não que
ela decodifica o arquivo inteiro.

Com o item 1 implementado isso deixa de ser crítico (a corrupção passa a ser
detectada no fim, custando um encode perdido). Sem o item 1, é a única barreira
— e ela não funciona.

### O que fazer

Decodificar um trecho de verdade e **conferir a contagem**, em vez de confiar no
returncode. Esboço:

```python
def _hw_decode_works(path, accel, v_index=0, frames=300):
    cmd = ["ffmpeg", "-hide_banner", "-v", "verbose",
           *_hw_decode_args(accel),
           "-hwaccel_output_format", _decode_output_format(accel),
           "-i", path, "-map", f"0:v:{v_index}",
           "-frames:v", str(frames), "-f", "null", "-"]
    # parsear "N packets read ...; M frames decoded; K decode errors"
    # e exigir M >= frames * 0.995 e K == 0
```

Pontos de atenção:

- **Custo**: hoje o teste roda por job (é chamado em `plan_video`). 300 frames de
  4K são rápidos, mas medir antes; se pesar, amostrar de um ponto no meio do
  arquivo em vez do começo (a falha do QSV não aparecia no início).
- **Não substitui o item 1.** Este teste amostra; o item 1 valida o resultado
  real. Fazer os dois.
- Se o teste reprovar, cair para decode em software (é o que o código já faz
  quando `_hw_decode_works` retorna `False`) e registrar a nota no job.

---

## Como investigar algo parecido no futuro

Comandos que funcionaram, para não redescobrir:

```bash
# contagem real de frames (decodifica; lento mas autoritativo)
ffprobe -v error -count_packets -select_streams v:0 \
        -show_entries stream=nb_read_packets -of csv=p=0 ARQUIVO

# a linha que denuncia perda de frames no decode
ffmpeg -loglevel verbose -i ARQUIVO -map 0:v:0 -f null - 2>&1 | grep "frames decoded"

# distribuição de frames ao longo do filme (acha buracos)
ffprobe -v error -select_streams v:0 -show_entries packet=pts_time -of csv=p=0 ARQUIVO \
  | awk -F, '{b=int($1/600); c[b]++} END {for (i in c) printf "%6ds: %6d\n", i*600, c[i]}'
```

Armadilhas em que eu caí ao diagnosticar, para ninguém repetir:

- **Amostrar janelas curtas engana.** Janelas de 60s caíram em bolsões densos e
  pareciam normais num arquivo que estava com 26%. Varra por blocos grandes ou
  conte o arquivo inteiro.
- **`-ss` depois do `-i` no ffprobe não funciona** como filtro de janela; use
  `-read_intervals INICIO%FIM`.
- **`-t` depois do `-i` num teste de encode** corta antes do esperado e produz
  contagens que parecem perda mas são artefato do comando. O pipeline real não
  usa `-t` — reproduza sem ele.
- **Não confie em duração×fps** para estimar frames esperados; compare contagem
  de packets da fonte com a da saída.
