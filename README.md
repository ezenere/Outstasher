<div align="center">
  <img src="frontend/src/assets/logo.png" alt="Outstasher" width="128" height="128">
</div>

<h1 align="center">Outstasher</h1>

Baixa duas versões de um filme ou série (original + dublada), escolhendo o melhor
torrent para cada uma, e junta tudo num único MKV: **melhor imagem + todos os
áudios**, alinhados automaticamente.

## Fluxo

1. Você escolhe um filme ou série (busca/populares do TMDB) e o idioma de áudio.
2. Busca no **Jackett**: título traduzido + marcadores de idioma ("Dublado",
   "Dual Áudio") → melhor **áudio**; título original → melhor **vídeo**, do mesmo
   corte. Títulos não-ingleses são buscados também pelo nome em inglês.
3. Envia os torrents ao **qBittorrent** (magnet ou `.torrent`).
4. Merge: se o melhor vídeo já tem o áudio alvo, entrega por **hardlink**; senão
   melhor áudio por língua, legendas, capítulos e offset por **GCC-PHAT**. Offset
   constante → sync no container, **stream copy total**; drift → re-encode dos
   áudios do outro arquivo.
5. **Legendas externas** dos torrents (`.srt/.ass/.ssa/.vtt`) entram no MKV com
   o mesmo deslocamento do áudio, idioma pelo nome/pasta/conteúdo, duplicatas
   descartadas. Na entrega por hardlink viram sidecars (`Filme.por.srt`).

## Requisitos

- Python 3.11+ e Node 18+ (`python main.py` builda o frontend sozinho)
- [ffmpeg](https://ffmpeg.org/) (ffmpeg + ffprobe no PATH); opcional
  [mkvtoolnix](https://mkvtoolnix.download/) para reinjetar HDR10 após re-encode
- [Jackett](https://github.com/Jackett/Jackett) com indexadores configurados
- [qBittorrent](https://www.qbittorrent.org/) com a Web UI habilitada
- Chave de API do [TMDB](https://www.themoviedb.org/settings/api)

## Instalação e uso

```sh
pip install -r requirements.txt
cp .env.example .env      # chaves e URLs
python main.py            # produção em :8008
python main.py dev        # API com reload + Vite em :5173
```

No primeiro acesso o serviço pede uma senha. O setup gera uma `API_KEY` para
scripts (`Authorization: Bearer <api_key>`).

### Docker

```sh
docker compose up -d --build   # http://localhost:8008
```

- Chaves e URLs em `environment:` do compose. qBittorrent/Jackett no host:
  `host.docker.internal` (+ `extra_hosts` no Linux).
- Mídia: `/mnt` do host montado em `/mnt/outer` — cadastre destinos com os
  caminhos de dentro do container.
- `jobs.db` e senha em `./outstasher-config` (`/config`).
- GPU Intel: `devices: /dev/dri` + `group_add` com os GIDs de `render`/`video`.

## Filmes

Busque, clique, escolha idioma e destino. Modos: **Baixar e fazer merge**,
**Só original** / **Só dublado** (hardlink, sem merge) e **Apenas baixar**.

- **Modo manual**: o job busca e para em *Aguardando escolha* com as tabelas de
  candidatos (score, corte, motivo de rejeição).
- **Watchdog**: download parado por `STALL_TIMEOUT_MINUTES` é trocado pelo
  próximo candidato do mesmo corte. Durante o download dá para trocar à mão:
  *Próximo*, *Escolher outro…* (lista da busca) ou **magnet/link próprio**,
  que entra direto no qBittorrent sem passar pelo indexador.
- **Drift**: se as duas janelas de offset divergirem, o job mede o offset a
  cada 5 min (drift × corte), pausa e você decide: **alinhamento avançado**
  (o alinhador das séries, com EDL e revisão — resolve cena/junção diferente
  no mesmo corte), continuar com o offset do início, trocar de torrent ou
  cancelar.
- **Limpeza** (`QBIT_CLEANUP`): `keep` | `remove` | `remove_data`.
- **Cancelar / repetir** em cada card; detalhes ao vivo (downloads, candidatos,
  eventos, progresso do ffmpeg) na lupa.

### Opções avançadas de conversão

Converte o resultado em vez de copiar (desligado por padrão):

- **Vídeo**: codec (VVC/AV1/HEVC/H.264 conforme o ffmpeg), encoder (software /
  NVENC / Quick Sync, testados com encode real), preset, resolução (nunca
  aumenta), bitrate ou CRF, 8/10-bit. Keyframe a cada 2 s em qualquer encoder.
  HDR10 preservado; Dolby Vision não sobrevive a re-encode.
- **Áudio**: manter todos ou só original + dublagem; codec, canais, bitrate.
- **Legendas**: padrão / todas / nenhuma.

A validação roda no servidor com o arquivo real e nunca converte "para cima";
se tudo vira cópia, a saída volta a ser hardlink. Em AV1 por software o
lookahead é limitado pela RAM (`IGNORE_AV1_LOOKAHEAD_LIMITS=true` desliga).

### Conversão manual

**Catálogo → Adicionar filme**: merge a partir de dois arquivos já no disco,
sem busca nem torrent. Na aba **Séries** do catálogo o botão vira **Adicionar
série** (ver *Merge manual de série*).

## Séries

O toggle **Filmes/Séries** na aba Buscar alterna para a base de TV do TMDB. O
modal da série lista temporadas e episódios (futuros ficam desabilitados e são
pulados). O job baixa as duas versões de cada episódio.

### Busca e seleção

Busca em duas fases: por temporada/pack e, só para lacunas, por episódio.
Seleção por episódio com a escada de qualidade dos filmes; em empate vence o
pack que cobre mais episódios pedidos, e um pack baixa só os arquivos
necessários (vídeo + legendas do episódio, casados pelo nome real dos arquivos).

Situações suspeitas pausam o job num **gate**:

- **Lacunas**: episódio sem torrent em um dos idiomas → continuar sem ele, ou
  editar a seleção manual.
- **Torrents incompatíveis**: numeração absoluta, episódios por data, ordem
  alternativa (DVD/TMDB episode groups) → aceitar, trocar ou remapear.
- **Seleção manual**: marque torrents e atribua episódios (ou "auto" pelo
  título); aceita magnets/links avulsos. Depois do download os arquivos são
  casados de verdade e o que faltou volta ao gate.

Durante o download: **tentar próximo(s)** por torrent, trocar por outro
candidato, ou **forçar a próxima etapa**. A lista de Jobs filtra por
**Filmes / Séries / Todos**.

### Alinhamento por conteúdo

O merge tenta primeiro offset constante (verificado a cada 5 min). Se o offset
varia — TV/streaming e Blu-ray raramente têm a mesma montagem — entra o
alinhador por conteúdo: crop → **dHash 4 fps** → **Needleman-Wunsch com gap
afim** (numba) → classificação (`match`, `gap_dub`, `gap_orig`, `replaced`,
`pal`, `drift`) → **refino por áudio** (offset real dentro de cada trecho,
falsos positivos do vídeo resolvidos pelo áudio) → **EDL** gravada no job.

Nenhum ponto de corte fica na grade de 0,25 s do vídeo: dentro de um trecho o
ponto vem do perfil de offset; nas junções, de bissecção por áudio — e todos
caem num **silêncio** do dublado. Quando o dublado tem material a mais
(respiro numa junção, recap), o original segue **contínuo**: o excesso é
pulado dentro do silêncio, sem buraco preenchido com inglês no meio da fala.

- A saída usa a timeline do original (vídeo, áudios, legendas e capítulos em
  stream copy); só a faixa dublada é remontada. Cena sem dublagem recebe o áudio
  original (ou silêncio); PAL corrigido com `rubberband`. O mux final usa o
  `mkvmerge` quando disponível (intercala por blocos: legenda esparsa nunca
  segura memória nem sai do lugar); sem ele, ffmpeg com a intercalação
  dimensionada por um orçamento de memória — e o job avisa se o muxer chegou a
  forçar saída (arquivo com intercalação frouxa engasga players).
- **Arquivo fundido** (dois episódios num só, razão de duração ~2): o episódio
  é localizado pelo offset dominante e o vídeo é cortado na janela dele; recap
  e prévia são descartados.
- **Cena substituída nunca passa sozinha**: revisão com timeline de duas faixas
  e frames lado a lado; decisões podem virar **regras** para todos os episódios
  do job.
- Legendas externas do lado dublado seguem a mesma EDL do áudio.

Só **um alinhamento pesado por vez** no servidor: o fingerprint decodifica os
dois arquivos inteiros, e dois em paralelo brigam pelo mesmo disco — o segundo
job espera na fila (aparece como *na fila de alinhamento*). O fingerprint tem
**barra de progresso** por arquivo (posição, fps, velocidade e ETA).

Triagem sem merge: `python merge.py --align-report <orig> <dub> <saida>`.

### Merge manual (arquivos locais)

**Catálogo → Séries → Adicionar série**: você aponta as pastas do original e as
do dublado — **quantas quiser de cada lado**, para release espalhado em várias
pastas — com um navegador de pastas do servidor que começa nos destinos
cadastrados. Ele lê as árvores e soma os arquivos por temporada. Por temporada aparece a **ordem
detectada** de cada lado — `SxxEyy (E01–E23, 1 fundido)`, `absoluta (137–160)`,
`alfabética` — com pasta, contagem de arquivos e de episódios.

Cada temporada também aceita uma **pasta própria**: a pastinha ao lado do
caminho de cada lado (vídeo/áudio) troca só aquela temporada, e a escolha manda
mesmo que os arquivos não digam a temporada — é assim que entram releases com
nomes fora do padrão.

O pareamento é proposto por `SxxEyy` quando os dois lados numeram assim, senão
por posição; arquivo com dois episódios entra nas duas linhas (o alinhador
localiza a metade certa). Em **editar match manualmente** dá para trocar
qualquer arquivo dos dois lados, desmarcar episódios, buscar um arquivo
avulso no disco, ou usar as ações em massa (parear por posição, deslocar o
dublado ±1). Raiz que mistura séries/versões é avisada, e só a pasta que mais
cobre a temporada entra no pareamento.

O job resultante é um job de série normal — alinhamento, revisão, relatório e
entrega no layout Jellyfin — só que sem torrent nenhum.

### Falhas parciais

Episódio que falha não para os outros; o relatório final lista as falhas e cria
um job só com elas. Menos de 75% de sucesso aborta o job.

### Catálogo e destinos

Destinos e catálogo são separados por mídia (**filme** / **tv**). Séries usam o
layout Jellyfin `Série (Ano) [tmdbid-N]/Season 01/Série (Ano) S01E02 [pt+orig].mkv`,
com **Adicionar episódios** e **recompressão** por episódio, temporada ou série
(valida a contagem de pacotes de vídeo antes de substituir).

## Configurações

- **Destinos do arquivo final** (por mídia), com uso de disco.
- **Destinos dos torrents**: `save_path` do qBittorrent + caminho local montado
  nesta máquina.
- **Catálogo**: ffprobe parseado por arquivo, renomear/remover, **Recomprimir**,
  **Marcar ID do TMDB** (renomeia a pasta para `[tmdbid-N]`).

## merge.py avulso

```sh
python merge.py "filme.mkv" "filme.dublado.mkv" "saida.mkv" --audio-lang pt
```

`--series` (diretórios casados por `SxxExx`), `--segments` (alinhamento por
segmentos com cortes de preto/silêncio), `--align-report` (triagem de séries).

## Estrutura

| Arquivo | Função |
| --- | --- |
| `main.py` | API FastAPI + frontend |
| `services/tmdb.py`, `jackett.py`, `qbittorrent.py` | clientes externos |
| `services/selector.py` | escolha do melhor torrent (filmes) |
| `services/jobs/` | jobs de filme em camadas: estado (`runtime.py`), leituras (`views.py`), busca (`search.py`), qBittorrent (`downloads.py`), entrega (`delivery.py`), alinhamento avançado (`advanced.py`), recompressão (`recompress.py`), pipeline (`movies.py`) e ações da UI (`actions.py`) |
| `services/merger.py`, `transcode.py` | merge, GCC-PHAT, opções de conversão |
| `services/store.py`, `catalog.py` | SQLite, catálogo |
| `services/series/` | séries: `pipeline.py` (gates), `selector_tv.py`, `parse.py`, `merge_runner.py`, `subs.py` (legendas externas), `recompress.py`, `naming.py` |
| `services/series/align/` | alinhador: `fingerprint.py`, `dp.py`, `classify.py`, `refine.py`, `edl.py`, `render.py`, `rules.py` |
| `merge.py` | CLI avulso |
| `frontend/` | React + TS + Tailwind |

## Licença

[MIT](LICENSE)
