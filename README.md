<div align="center">
  <img src="frontend/src/assets/logo.png" alt="Outstasher" width="128" height="128">
</div>

<h1 align="center">Outstasher</h1>

Serviço que baixa duas versões de um filme (original + dublada), escolhendo o melhor
torrent para cada uma, e junta tudo em um único arquivo: **melhor imagem + todos os
áudios**, alinhados automaticamente.

## Fluxo

1. Você escolhe um filme (busca ou populares do TMDB) e um idioma de áudio.
2. Busca no **Jackett**:
   - título traduzido / com marcadores de idioma → melhor **áudio**. Título no
     idioma dublado tem preferência; título original + marcador é fallback.
     Marcadores fortes ("Dublado", "Dual Áudio") valem mais que "dual" genérico.
     Torrents só **legendado** (sem marca de dublagem) são descartados. Torrents
     com o **ano do filme** no nome têm preferência absoluta sobre os sem ano
     (título sem ano é ambíguo — pode ser outro filme da franquia ou um remake);
     o score só ordena dentro de cada grupo.
   - título original → melhor **vídeo**, **restrito ao mesmo corte** do áudio
     (cortes diferentes não alinham). Sem vídeo do corte do melhor áudio, tenta o
     próximo candidato de áudio.
3. Envia os dois torrents ao **qBittorrent** via Web API. Se o Jackett devolve um
   link HTTP em vez de `magnet:`, o serviço resolve antes (segue o redirect ou
   baixa o `.torrent` e envia como arquivo — o qBittorrent nem sempre segue).
4. Quando terminam, faz o merge:
   - se o melhor vídeo **já tem o áudio alvo**, pula o merge e cria um **hardlink**
     no destino (fallback: cópia; nunca symlink);
   - senão: melhor áudio por língua, legendas só das línguas com áudio, capítulos,
     e offset medido por **GCC-PHAT** em duas janelas. Offset **constante**: sync
     no container via `-itsoffset`, **stream copy total** (trilha dublada intacta,
     sem re-encode). Com **drift** (janelas divergem): re-encode via
     `-filter_complex` (AAC mono/estéreo, AC3 multicanal para preservar o layout
     surround).

## Requisitos

- Python 3.11+
- Node 18+ (para buildar o frontend React; `python main.py` builda sozinho)
- [ffmpeg](https://ffmpeg.org/) (ffmpeg + ffprobe no PATH). As opções avançadas
  de conversão só oferecem os codecs que o seu ffmpeg tem encoder (detectado em
  runtime).
- Opcional: [mkvtoolnix](https://mkvtoolnix.download/) (`mkvpropedit`) — usado
  para reinjetar metadados HDR10 no container quando o encoder os descarta no
  re-encode (comum em encoders de hardware). Sem ele, a conversão avisa.
- [Jackett](https://github.com/Jackett/Jackett) rodando com indexadores configurados
- [qBittorrent](https://www.qbittorrent.org/) com a Web UI habilitada
  (Ferramentas → Opções → Web UI)
- Chave de API do [TMDB](https://www.themoviedb.org/settings/api) (gratuita)

## Instalação

```sh
cd pasta_onde_baixou
pip install -r requirements.txt
cp .env.example .env
# edite o .env com suas chaves/URLs
```

## Uso

```sh
python main.py        # produção: builda o frontend (se mudou) e serve tudo em :8008
python main.py dev    # dev: API com reload em :8008 + Vite em watch em :5173
```

Em produção, abra http://127.0.0.1:8008. Em dev, use http://127.0.0.1:5173 (o Vite
faz proxy de `/api` para o backend e recarrega o frontend a cada mudança).

### Senha de acesso

No primeiro acesso o serviço pede para criar uma senha; depois disso a API exige
login. O setup também gera uma `API_KEY` que vale como token permanente via
header `Authorization: Bearer <api_key>` (para scripts).

O login gera um token de sessão no `sessionStorage`: fechar o navegador ou
reiniciar o servidor derruba a sessão. Trocar a senha desconecta as outras.

## Docker

Sobe tudo (backend + frontend buildado + ffmpeg) num container só:

```sh
cp .env.example .env      # edite com suas chaves/URLs
docker compose up -d --build
```

Abra http://localhost:8008. Imagem multi-stage (Node builda o front, runtime só
Python + ffmpeg).

Pontos de atenção do `.env`/`docker-compose.yml`:

- **qBittorrent / Jackett na sua máquina**: use `host.docker.internal` nas URLs
  (`QBIT_URL`, `JACKETT_URL`) — dentro do container `localhost` é o container. O
  compose já mapeia esse nome no Linux via `extra_hosts`.
- **Pastas de download e destino**: monte-as no compose e cadastre os destinos na
  UI com os caminhos *de dentro do container* (`/downloads`, `/output`). Ajuste
  `DOWNLOADS_DIR`/`OUTPUT_DIR_HOST` no `.env`; o "caminho local" do destino de
  torrents deve ser `/downloads`.
- **Persistência**: `jobs.db` fica no volume `downloader-data` (`/data`, via
  `DB_DIR`).

Parar: `docker compose down`. Logs: `docker compose logs -f`.

## Downloads

Busque um filme, clique nele, escolha o idioma e o **destino**. Resultados que
já estão na coleção ganham um selo *✓ Na coleção* (ver *Cache da coleção*).

Opções de download:

- **Baixar e fazer merge** — original + dublada, junta num MKV (fluxo acima).
- **🎥 Só original** / **🔊 Só dublado** — baixa uma versão só e entrega por
  hardlink (fallback cópia) numa subpasta do destino, sem merge.
- **Apenas baixar** — só baixa pelo qBittorrent e conclui; sem merge/hardlink/
  cópia. Os arquivos ficam onde o qBittorrent os deixou (destino final não se
  aplica).

O modo manual e o destino dos torrents funcionam igual em todas. O andamento
aparece na aba *Downloads*.

### Opções avançadas de conversão

Bloco **"Opções avançadas"** (no modal de download e no de conversão manual) para
converter o resultado em vez de copiá-lo. Desabilitado por padrão (sai em stream
copy). Opções:

- **Codec de vídeo**: manter / VVC / AV1 / HEVC / H.264 (só os que o ffmpeg tem
  encoder aparecem). Em **AV1 por software** (SVT-AV1) o lookahead é limitado a
  um teto derivado da RAM da máquina — o default do encoder segura frames demais
  em buffer e, em 4K 10-bit, o pico de RAM passa de 12 GB e o kernel mata o
  ffmpeg (OOM). O teto é calculado no 1º uso (mais RAM → lookahead maior,
  melhor qualidade); num servidor de 16 GB fica no piso. Um encode que ainda
  tende a não caber gera uma nota de aviso no job; se o ffmpeg é morto por
  SIGKILL, o erro explica que foi falta de memória. Para **desligar o teto** e
  usar o default do SVT-AV1, defina `IGNORE_AV1_LOOKAHEAD_LIMITS=true` (só com
  RAM de sobra ou encodando abaixo de 4K — senão o OOM volta).
- **Encoder**: software (CPU) / **NVENC** (GPU NVIDIA) / **Quick Sync** (GPU
  Intel/Arc), para H.264/HEVC/AV1. A disponibilidade é testada com um encode
  real na GPU; a faixa de qualidade (CQ/ICQ, 1–51) e o 10-bit (não existe em
  H.264 por hardware) seguem o encoder escolhido. Na GPU o modo qualidade
  constante liga lookahead estendido (`-extbrc`/`-look_ahead_depth` no QSV,
  `-multipass`/`-rc-lookahead`/AQ no NVENC) e GOP de ~10 s; o decode também
  acontece na GPU quando possível (testado com o arquivo real, com fallback
  para software) — sem filtros de CPU no meio, os frames ficam na VRAM do
  decode ao encode. Fontes HDR10 têm a sinalização de cor reaplicada e os
  metadados estáticos (mastering display/MaxCLL) conferidos após o encode —
  reinjetados no container via `mkvpropedit` se o encoder os descartar.
  Dolby Vision não sobrevive a re-encode (fica o HDR10 base).
- **Preset**: muito rápido → muito lento (mapeado por encoder; ex.: p1–p7 no
  NVENC).
- **Resolução**: 8K/4K/Full HD/HD/SD. Corta por **largura** com 8% de tolerância
  e **nunca aumenta** a resolução da fonte.
- **Qualidade**: bitrate alvo (100 kbps–150 Mbps) ou **CRF**.
- **Profundidade de cor**: manter / 10-bit / 8-bit.
- **Áudios**: manter todos, ou **apenas original + dublagem** (idioma original vem
  do TMDB; faixas de idioma desconhecido são sempre mantidas).
- **Codec de áudio**: manter / AC3 / FLAC / Opus / OGG Vorbis / AAC.
- **Canais**: manter / máx. 5.1 / estéreo.
- **Bitrate de áudio** por faixa e **legendas** (padrão / todas / nenhuma).

A validação roda **no servidor, com o arquivo real**, e nunca "converte para
cima": se o bitrate pedido excede o da fonte, mantém o stream original; se o
re-encode é inevitável por outro motivo, rebaixa o alvo ao teto estimado da
fonte. Se o plano inteiro vira cópia, a saída volta a ser hardlink. Em jobs de
torrent o arquivo só existe **depois** do download, então o resultado dessas
regras aparece nos eventos do job.

### Conversão manual (Catálogo → Adicionar filme)

O botão **"+ Adicionar filme"** cria uma conversão a partir de dois arquivos já
no disco do servidor (filme do TMDB + caminhos de vídeo e áudio dublado + destino
+ opcionalmente as opções avançadas). Mesmo pipeline dos jobs normais, sem busca
nem torrents. Arquivos inexistentes ou não-mídia são recusados na hora (ffprobe).

### Cache da coleção

O selo *✓ Na coleção* na busca vem de um scan das pastas dos destinos, mantido em
memória por 30 min. Refeito sob demanda na próxima busca após vencer, ou quando
algo muda (job conclui, pasta removida, destino editado).

### Modo manual

Marcando **"Escolher torrents manualmente"**, o job faz as buscas e para em
*Aguardando escolha*: **Escolher** abre as tabelas de candidatos (áudio e vídeo,
com corte e score) para você decidir. Cortes diferentes geram aviso antes de
confirmar.

### Watchdog de download travado

Download parado por `STALL_TIMEOUT_MINUTES` (padrão 15) é trocado pelo próximo
candidato **do mesmo corte**. Sem reserva, avisa e continua esperando.

### Validação do alinhamento

O offset é medido em duas janelas (início e ~60% do filme). Se divergirem (corte
diferente ou drift), o merge continua com o offset do início e registra o aviso.

### Limpeza pós-merge (QBIT_CLEANUP)

`keep` (padrão) mantém seedando; `remove` remove os torrents mantendo os arquivos;
`remove_data` apaga também os dados. Quando o merge é pulado (hardlink), o dado
sobrevive pelo hardlink mesmo com `remove_data`.

### Cancelar / repetir

Cada card tem **✕** (remove o job; pergunta se apaga os torrents) e, para
erro/cancelado, **↻** (recria com os mesmos parâmetros). Cancelar durante a
conversão mata o ffmpeg e apaga o arquivo parcial.

### Detalhes de um job

O botão de lupa abre um painel ao vivo: barras de download (velocidade/ETA/seeds),
todos os candidatos avaliados por busca (com score e motivo de rejeição), o que
foi escolhido, e a timeline de eventos com o log do merge. Durante a conversão,
uma barra de progresso do ffmpeg mostra posição no filme, velocidade, fps,
tamanho, bitrate e ETA.

## Configurações

A aba **Configurações** reúne dois cadastros, ambos guardados no `jobs.db` e
gerenciáveis pela interface (adicionar/editar/remover, marcar padrão).

### Destinos do arquivo final

Pastas onde o filme finalizado pode ser salvo. Cada filme sai numa subpasta
própria (para o Jellyfin/Plex parsearem o nome). O destino fica registrado no
job (repetir um job mantém a mesma pasta). O `OUTPUT_DIR` do `.env` só cria o
destino "Padrão" na primeira execução.

Cada destino mostra o **uso do disco** do volume (barra `usado / total · livre`
nas Configurações; espaço livre ao lado do seletor no download). Se o caminho
ainda não existe, sobe pelos pais até achar um volume montado.

### Destinos dos torrents (qBittorrent)

Cada destino tem duas partes:

- **Caminho no qBittorrent** (`save_path`): onde o qBittorrent grava, do ponto de
  vista dele (vazio = pasta padrão). Enviado no `add`, com auto-management
  desligado só nesses torrents.
- **Caminho local**: onde essa pasta está montada *nesta máquina* — traduz o
  `content_path` que a API reporta para achar o arquivo. Vazio se o qBittorrent
  roda na mesma máquina/mesmo caminho.

Na criação do job você escolhe o destino dos torrents; sem nenhum cadastrado, usa
a pasta padrão do qBittorrent e o `QBIT_PATH_MAP` global do `.env` como fallback.
`QBIT_SAVE_PATH`/`QBIT_PATH_MAP` só semeiam o destino "Padrão (.env)" na primeira
execução. Dentro do caminho local, pega o maior arquivo de vídeo (ignorando
"sample").

### Catálogo

Lista os filmes de uma pasta de destino (cada subpasta é um filme). Cada arquivo
é um dropdown com o `ffprobe` parseado: container, bitrate, capítulos e cada
track (vídeo: resolução/FPS/HDR/10-bit/cor/perfil; áudio: canais/sample
rate/bitrate; legendas: idioma/forced/SDH). Dá para **renomear**/**remover** um
arquivo ou remover a **pasta inteira** (caminhos validados contra traversal).
Também é aqui que fica o **"+ Adicionar filme"** (conversão manual).

**Recomprimir** (ícone no arquivo de vídeo) converte um filme que já está na
coleção com as mesmas opções avançadas dos downloads — sem torrent, sem merge.
A saída vai para um arquivo temporário na mesma pasta e só substitui o original
quando o ffmpeg termina; cancelar ou falhar deixa o filme intacto, e um
resultado maior que o original é descartado. Também dá para **manter os dois**
(grava como `[recomprimido]` ao lado).

### Identificação no Jellyfin (tmdbid)

Pastas novas saem como `Filme (Ano) [tmdbid-N]` — o Jellyfin usa o id para
identificar o filme sem depender do título (remakes, títulos localizados). Para
a coleção antiga, o botão **"Marcar ID do TMDB"** no catálogo renomeia a pasta
com o id do match já exibido na tela. O `[tmdbid-N]` não entra no nome do
arquivo nem nas chaves do cache da coleção.

## merge.py avulso

O merge também roda como ferramenta independente:

```sh
python merge.py "filme.1080p.mkv" "filme.dublado.mkv" "resultado.mkv" --audio-lang pt
```

Mesma lógica do merge interno: escolhe o melhor vídeo, melhor áudio por língua
(tags como `pob`/`pt-br` normalizadas), hardlink se o vídeo já tem o idioma alvo,
e offset por GCC-PHAT em duas janelas (stream copy se constante; re-encode dos
áudios do outro arquivo se houver drift).

Opções extras do CLI:

- `--series`: os dois argumentos viram **diretórios**. Escaneia recursivamente,
  casa arquivos por `SxxExx` e faz o merge de cada par (nome de saída = episódio).
  Retomável (pula saídas existentes); erro de um episódio não para o lote.
- `--segments`: alinhamento **por segmentos** em vez de um offset único. Detecta
  cortes (preto validado por silêncio) e alinha cada segmento — útil quando as
  versões têm quebras de comercial/intervalo diferentes. A validação cruzada pode
  ser afinada (`--black-*`, `--silence-*`) ou reduzida a um modo (`--disable-black`
  / `--disable-silence`).

## Arquitetura

Backend FastAPI serve a API e o frontend React buildado (`python main.py` roda
`npm install`/`npm run build` quando o `dist` está desatualizado). Jobs e eventos
ficam em SQLite (`jobs.db`); se o servidor reinicia durante um download, ele
retoma o acompanhamento sozinho.

## Estrutura

| Arquivo | Função |
| --- | --- |
| `main.py` | API FastAPI + serve o frontend |
| `services/tmdb.py` | busca de filmes e títulos traduzidos |
| `services/jackett.py` | busca de torrents |
| `services/selector.py` | pontuação/escolha do melhor torrent (vídeo vs. áudio) |
| `services/qbittorrent.py` | cliente da Web API do qBittorrent |
| `services/jobs.py` | orquestração: busca → download → merge |
| `services/store.py` | persistência SQLite (jobs, eventos e destinos) |
| `services/catalog.py` | catálogo: lista filmes do destino + ffprobe parseado |
| `services/merger.py` | merge + alinhamento GCC-PHAT (usado pelos jobs) |
| `services/transcode.py` | opções avançadas: capacidades do ffmpeg, validação, planos de vídeo/áudio |
| `services/merger_segments.py` | alinhamento por segmentos (`merge.py --segments`) |
| `merge.py` | CLI avulso em cima do `services/merger.py` (`--series`, `--segments`) |
| `frontend/` | frontend React (TS + Tailwind + Iconoir + Router) |
| `Dockerfile` | build multi-stage (Node builda o front, runtime Python + ffmpeg) |
| `docker-compose.yml` | sobe o serviço local com volume p/ o `jobs.db` e mounts |
