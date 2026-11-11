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
   áudios do outro arquivo. Audiodescrição, comentários e legendas SDH são
   faixas próprias: não são descartadas como duplicata da comum do mesmo idioma.
5. **Legendas externas** dos torrents (`.srt/.ass/.ssa/.vtt`) entram no MKV com
   o mesmo deslocamento do áudio, idioma pelo nome/pasta/conteúdo, duplicatas
   descartadas. Na entrega por hardlink viram sidecars (`Filme.por.srt`).
6. **Intercalação conferida**: depois do mux, o job lê a ordem gravada dos
   pacotes em vários pontos do arquivo. Faixa de áudio gravada longe do vídeo do
   mesmo instante (o Jellyfin corta o áudio quando isso acontece) é
   reintercalada pelo `mkvmerge`, com um leitor próprio para ela.
7. Entrega no destino, dentro da pasta do título **que já existir** (de outro
   programa ou de versão antiga), em vez de criar uma paralela.

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
  candidatos (score, corte, motivo de rejeição). Cada papel também aceita um
  **magnet/link próprio** ali no gate, no lugar de um candidato da lista.
- **Pular busca de torrents** (só no modo manual, filmes e séries): o indexador
  nem é consultado — o job abre direto na escolha, com a lista vazia, para você
  colar os magnets. Útil quando já se sabe qual release quer.
- **Troca automática de torrent travado**: download sem progresso, sem fontes
  ou preso nos metadados além do limite é trocado pelo próximo candidato do
  mesmo corte. Liga/desliga e limites globais (Configurações → Torrents) e por
  torrent, na própria linha dele no job, com o relógio "sem progresso há X —
  troca em Y". Durante o download dá para trocar à mão: *Próximo*, *Escolher
  outro…* (lista da busca) ou **magnet/link próprio**, que entra direto no
  qBittorrent sem passar pelo indexador.
- **Envio resiliente**: link do indexador lento no envio ao qBittorrent é
  tentado de novo; esgotadas as tentativas, o watchdog reinsere depois — não
  derruba o job. O progresso do job pondera cada torrent pelo tamanho real.
- **Drift**: o offset é medido numa janela a cada ~20 min do filme inteiro.
  Constante → stream copy; numa reta (velocidade levemente diferente) → a
  velocidade do áudio é corrigida sozinha, **sempre com aviso** (a dublagem é
  re-encodada; TrueHD/DTS não sobrevivem); degrau ou janelas sem correlação → o job
  mede o offset a cada 5 min (drift × corte), pausa e você decide: **alinhamento avançado**
  (o alinhador das séries, com EDL e revisão — resolve cena/junção diferente
  no mesmo corte), continuar com o offset do início, trocar de torrent ou
  cancelar. A política do merge avançado (ver *Merge avançado*) pode ser
  escolhida no modal de download e na pausa, herdando a global.
- **Agendar download**: o job nasce **Programado** e só começa a procurar na
  data/hora escolhida (dá para mudar a data ou *Buscar agora* antes disso).
- **Qualidade mínima** (modo automático): piso de resolução, de fonte e de
  áudio (5.1+, 7.1/Atmos, sem perda) para o original e para o dublado. O áudio
  só é lido do nome do release (quem não informa não passa); por isso o piso
  de vídeo do dublado ajuda — o vídeo do release dublado diz muito do áudio
  dele. A lista de escolha continua mostrando tudo.
- **Busca contínua**: a busca que não acha nada que sirva (nada, nada na
  qualidade mínima, nenhum par do mesmo corte, Jackett/TMDB fora do ar) deixa o
  job **Programado** para tentar de novo depois do intervalo das Configurações
  (padrão 6 h), até achar ou o job ser excluído — com a escolha aberta
  enquanto isso (lista + magnet próprio). A nova busca é **incremental**: o que
  a anterior achou fica, a menos que o tracker tenha respondido sem ele (saiu
  do tracker) ou com zero seeders. Liga/desliga global e por job.
- **Limpeza** (`QBIT_CLEANUP`): `keep` | `remove` | `remove_data`. Troca,
  remoção e limpeza nunca apagam um torrent que ainda serve a outro papel
  (pack DUAL usado como original e dublado) ou a outro job: só saem as tags
  que não valem mais.
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
sem busca nem torrent, com navegador de pastas do servidor (ou caminho
digitado). Na aba **Séries** do catálogo o botão vira **Adicionar
série** (ver *Merge manual de série*).

## Séries

O toggle **Filmes/Séries** na aba Buscar alterna para a base de TV do TMDB. O
modal da série lista temporadas e episódios, marcando o que já está na coleção
(inclusive série baixada por outro programa, sem ano nem `[tmdbid-N]` na
pasta) e o que já está em algum job ativo. Modos como nos filmes: **Baixar e
fazer merge**, **Só original** e **Só dublado** (com um lado só, cada episódio
é entregue direto no layout do Jellyfin). Episódio que ainda não estreou vira um job **Programado** — um
por data de estreia — que começa a procurar no dia do lançamento, a partir da
hora escolhida (padrão nas Configurações); se o TMDB mudar a data, o job
acompanha. Agendar, qualidade mínima e busca contínua valem como nos filmes.

### Busca e seleção

Busca em duas fases: por temporada/pack e, só para lacunas, por episódio.
Seleção por episódio com a escada de qualidade dos filmes; em empate vence o
pack que cobre mais episódios pedidos, e um pack baixa só os arquivos
necessários (vídeo + legendas do episódio, casados pelo nome real dos arquivos).

Episódio no nome do arquivo/release: `S01E02`, `S012E05`, `T01E02`, `1x02`,
por extenso ("9ª Temporada 08º Episódio") e, só em pack de **uma temporada**
que numera os arquivos pela posição (`1 - Nome.mp4`), o número do início do
nome — desde que nenhum arquivo traga `SxxEyy` e os números não se repitam.
Quando os metadados chegam, o pack é conferido pelos arquivos reais: episódio
que o título prometia e não está lá sai da cobertura e vira lacuna. No fim do
download, o lugar de cada torrent é confirmado no qBittorrent pelo hash (vale
torrent movido de pasta depois de concluído).

Situações suspeitas pausam o job num **gate**:

- **Lacunas**: episódio sem torrent em um dos idiomas → continuar sem ele, ou
  editar a seleção (abre com o plano atual marcado, inclusive o que já baixa;
  confirmar **substitui** o plano). Aberto no meio do download, os torrents
  seguem no qBittorrent e dá para trocar/remover cada um dali mesmo.
- **Torrents incompatíveis**: numeração absoluta, episódios por data, ordem
  alternativa (DVD/TMDB episode groups) → aceitar, trocar ou remapear.
- **Seleção manual**: marque torrents e atribua episódios (ou "auto" pelo
  título); aceita magnets/links avulsos. Depois do download os arquivos são
  casados de verdade e o que faltou volta ao gate.

Durante o download, por torrent: **tentar próximo(s)**, **escolher outro**,
**remover** (episódio que só ele cobria vira lacuna) e a troca automática de
travado. No job: **editar torrents** (a escolha reabre com o plano atual para
adicionar qualquer resultado ou magnet) e **forçar a próxima etapa**. A lista
mostra o tamanho real de cada torrent (dos metadados, não o estimado pelo
indexador) e, em pack parcial, quanto foi selecionado. Busca que não acha nada
não encerra o job: a escolha fica aberta. A lista de Jobs filtra por
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
  stream copy); só a faixa dublada é remontada (junto com os outros áudios e
  legendas embutidas do dublado). Cena sem dublagem segue a política do
  *Merge avançado*; PAL corrigido com `rubberband`. O mux final usa o
  `mkvmerge` quando disponível (intercala por blocos: legenda esparsa nunca
  segura memória nem sai do lugar); sem ele, ffmpeg com a intercalação
  dimensionada por um orçamento de memória. Intercalação frouxa é medida e
  consertada como nos filmes.
- **Arquivo fundido** (dois episódios num só, razão de duração ~2): o episódio
  é localizado pelo offset dominante e o vídeo é cortado na janela dele; recap
  e prévia são descartados.
- **Cena substituída nunca passa sozinha**: revisão com timeline de duas faixas
  e frames lado a lado; decisões podem virar **regras** para todos os episódios
  do job.
- Legendas externas do lado dublado seguem a mesma EDL do áudio.
- **Episódios desalinhados** (2+ pares sem quase nada em comum = provável
  ordem de episódios trocada): o job pausa e oferece **casar automaticamente**
  (todos × todos por fingerprint, que fica em cache em disco), **casar
  manualmente** (dublado de quem ↔ original de quem — inclusive o dublado de
  um episódio já concluído, para episódio duplo) ou **finalizar** sem
  eles — com "ignorar" por episódio. O gate reabre enquanto sobrarem 2+.

Só **um alinhamento pesado por vez** no servidor: o fingerprint decodifica os
dois arquivos inteiros, e dois em paralelo brigam pelo mesmo disco — o segundo
job espera na fila (aparece como *na fila de alinhamento*). O fingerprint tem
**barra de progresso** por arquivo (posição, fps, velocidade e ETA).

Triagem sem merge: `python merge.py --align-report <orig> <dub> <saida>`.

### Merge avançado

Política do trecho sem dublagem e dos cortes, global (Configurações → Merge
avançado) e por job (modais de download, pausa de drift do filme). Vale para
séries e para o alinhamento avançado dos filmes, como regras padrão — a
decisão tomada na revisão sempre vence.

- **Trecho sem dublagem**: sai do vídeo (a partir de N s; abaixo fica mudo),
  fica mudo, ou recebe o áudio **original** da obra (nunca outra língua). Na
  revisão a ação equivalente é **cortar do vídeo** (`cut_video`): o trecho sai
  por stream copy (`mkvmerge --split`), com legendas e capítulos reajustados.
- **Tela preta** que só o original tem (começo, entre atos, fim) é medida com
  `blackdetect` e fica no vídeo, muda: cortar ~1 s de preto custava re-encodar
  o episódio inteiro.
- **Re-encode nos cortes**: automático (AV1 da GPU Intel/QSV quando existe,
  SVT-AV1 de fallback), um encoder fixo, as opções de conversão do app, ou
  nenhum (corte em keyframe, com raspas de até um GOP). Keyframe forçado em
  cada ponto de corte, e a borda encaixa na fronteira de cena do original.
  A capa embutida (imagem como faixa de vídeo) fica fora do re-encode.

### Merge manual (arquivos locais)

**Catálogo → Séries → Adicionar série**: você aponta as pastas do original e as
do dublado — **quantas quiser de cada lado**, para release espalhado em várias
pastas — com um navegador de pastas do servidor que começa nos destinos
cadastrados. Ele lê as árvores e soma os arquivos por temporada.

A lista de temporadas vem do **TMDB**, não das pastas: temporada sem arquivo
reconhecido também aparece (para receber uma pasta), e cada uma liga/desliga
inteira num checkbox.

**Ordem dos episódios**: além da exibição (padrão), dá para escolher qualquer
*episode group* do TMDB — DVD, Blu-ray, absoluta, produção, streaming. A
numeração das linhas e a do arquivo final passam a ser as dessa ordem, e cada
linha mostra a que episódio de exibição ela corresponde. Por temporada aparece a **ordem
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
(valida a contagem de pacotes de vídeo antes de substituir). Pasta já existente
do título — de outro programa (`Série/Season 1/...`) ou sem o `[tmdbid-N]` — é
reconhecida no catálogo e recebe a entrega. A página inicial e o catálogo
carregam com scroll infinito (catálogo paginado no backend).

## Configurações

- **Destinos** do arquivo final (por mídia), com uso de disco.
- **Torrents**: destinos dos torrents — `save_path` do qBittorrent + caminho
  local montado nesta máquina. Na hora de localizar o download, valem **todos** os destinos
  cadastrados (não só o escolhido no job): se a pasta de salvamento foi trocada
  no próprio qBittorrent, basta que o novo caminho seja o de algum destino —
  ganha o primeiro que existe em disco; sem nenhum, o job falha listando os
  caminhos tentados. Também a **troca automática de torrent travado**:
  liga/desliga e os limites sem progresso, sem fontes (`stalledDL`) e buscando
  metadados (`metaDL`); sem nada salvo, vale o `STALL_TIMEOUT_MINUTES` do `.env`.
- **Presets** das opções de conversão.
- **Idiomas**: código, idioma do TMDB e marcadores fortes/fracos do release
  ("Dublado", "Dual Áudio"...).
- **Buscas extras** por idioma (sem o ano, numeral romano, os dois): cada regra
  lista só os indexers escolhidos, com "+ adicionar". Indexer que saiu do
  Jackett aparece em âmbar e é pulado na busca, com aviso no job.
- **Busca programada**: busca contínua ligada por padrão, intervalo entre as
  tentativas (dias/horas/minutos) e a hora do dia da estreia em que episódio
  futuro começa a ser procurado. A lista de Jobs tem o filtro **Programado**.
- **Merge avançado**: política global do trecho sem dublagem e do re-encode
  nos cortes (a tela informa o que a máquina tem: GPU, SVT-AV1).
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
| `services/selector.py` | escolha do melhor torrent (filmes) e qualidade mínima |
| `services/candidates.py` | candidatos de busca do job (filmes e séries) |
| `services/search_retry.py`, `stall_switch.py`, `advanced_merge.py` | políticas globais: busca contínua, troca de torrent travado, merge avançado |
| `services/jobs/` | jobs de filme em camadas: estado (`runtime.py`), leituras (`views.py`), busca (`search.py`), qBittorrent (`downloads.py`), entrega (`delivery.py`), alinhamento avançado (`advanced.py`), recompressão (`recompress.py`), pipeline (`movies.py`), jobs programados (`schedule.py`) e ações da UI (`actions.py`) |
| `services/merger.py`, `transcode.py` | merge, GCC-PHAT, intercalação, opções de conversão |
| `services/browse.py` | navegador de pastas do servidor |
| `services/store/` | SQLite: conexão e schema (`db.py`), configuração (`configuration.py`), eventos (`events.py`), persistência do job (`jobs.py`) e uma seção por módulo — candidatos (`candidates.py`), série (`series.py`), EDLs (`edls.py`), escalares e metadados do TMDB (`scalars.py`, `scalar_rows.py`) |
| `services/catalog.py` | catálogo |
| `services/series/` | séries: `pipeline.py` (gates), `selector_tv.py`, `parse.py`, `merge_runner.py`, `rematch.py` (desalinhados), `manual.py` (merge manual), `activity.py`, `subs.py` (legendas externas), `recompress.py`, `naming.py` |
| `services/series/align/` | alinhador: `fingerprint.py`, `dp.py`, `classify.py`, `refine.py`, `engine.py`, `black.py` (tela preta), `edl.py`, `render.py`, `rules.py` |
| `merge.py` | CLI avulso |
| `frontend/` | React + TS + Tailwind |

## Licença

[MIT](LICENSE)
