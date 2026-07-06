# Movie Downloader & Merger

Serviço que baixa duas versões de um filme (original + dublada), escolhendo o melhor
torrent para cada uma, e junta tudo em um único arquivo: **melhor imagem + todos os
áudios**, alinhados automaticamente.

## Fluxo

1. Você escolhe um filme (busca ou populares do TMDB) e um idioma de áudio no frontend.
2. O serviço busca no **Jackett**:
   - o título traduzido / com marcadores de idioma (dublado, dual, castellano...) →
     melhor **qualidade de áudio** (TrueHD, DTS, EAC3...);
   - o título original → melhor **qualidade de vídeo** (resolução, remux/bluray, seeds),
     **restrito ao mesmo corte** da versão dublada: se o dublado é o corte normal, um
     "extended/director's cut" original é rejeitado (e vice-versa) — cortes diferentes
     não alinham. Se não houver vídeo com o corte do melhor áudio, tenta o próximo
     candidato de áudio.
3. Manda os dois torrents para o **qBittorrent** via Web API e acompanha o progresso.
4. Quando os dois terminam, faz o merge internamente:
   - se o arquivo de melhor vídeo **já tem áudio no idioma alvo**, não faz merge —
     cria um **symlink** no destino (fallback: hardlink → cópia);
   - senão: melhor áudio **por língua** entre os dois arquivos, legendas só das
     línguas com áudio, capítulos, offset medido por **GCC-PHAT** e corrigido via
     `-filter_complex` (áudios do segundo arquivo re-encodados em AAC/AC3; o resto
     é stream copy).

## Requisitos

- Python 3.11+
- Node 18+ (para buildar o frontend React; `python main.py` builda sozinho)
- [ffmpeg](https://ffmpeg.org/) (ffmpeg + ffprobe no PATH)
- [Jackett](https://github.com/Jackett/Jackett) rodando com indexadores configurados
- [qBittorrent](https://www.qbittorrent.org/) com a Web UI habilitada
  (Ferramentas → Opções → Web UI)
- Chave de API do [TMDB](https://www.themoviedb.org/settings/api) (gratuita)

## Instalação

```sh
cd pasta_onde_baixou
pip install -r requirements.txt
copy .env.example .env
# edite o .env com suas chaves/URLs
```

## Uso

```sh
python main.py        # produção: builda o frontend (se mudou) e serve tudo em :8008
python main.py dev    # dev: API com reload em :8008 + Vite em watch em :5173
```

Em produção, abra http://127.0.0.1:8008. Em dev, use http://127.0.0.1:5173 (o Vite
faz proxy de `/api` para o backend e recarrega o frontend a cada mudança).

Busque um filme, clique nele, escolha o idioma e o **destino**, e clique em
**Baixar e fazer merge**. O andamento aparece na aba *Downloads*. O arquivo final
vai para a pasta de destino escolhida (veja *Destinos* abaixo).

O frontend é React + TypeScript + Tailwind + Iconoir + React Router (`frontend/`),
com rotas `/` (filmes), `/jobs` (downloads), `/jobs/:id` (detalhe/lupa — é também
onde se escolhe os torrents no modo manual), `/catalog` (biblioteca já baixada) e
`/settings` (destinos do arquivo final e dos torrents). O `python main.py` roda
`npm install`/`npm run build` automaticamente quando o build está ausente ou desatualizado.

Os jobs ficam salvos em **SQLite** (`jobs.db`, WAL): o documento do job numa
tabela e os eventos em outra, append-only — escrita atômica, sem risco de
corromper o histórico num crash. Se o servidor
reiniciar durante um download, ele retoma o acompanhamento sozinho.

### Modo manual (toggle na barra de seleção)

Marcando **"Escolher torrents manualmente"**, o job faz as buscas e para em
*Aguardando escolha*: o botão **Escolher** abre as tabelas de candidatos viáveis
(áudio e vídeo, com corte e score) e você decide o que baixar. Se os cortes
escolhidos forem diferentes, a UI avisa antes de confirmar.

### Watchdog de download travado

Se um download ficar `STALL_TIMEOUT_MINUTES` (padrão 15) sem progresso, o serviço
remove o torrent e troca automaticamente pelo próximo candidato viável **do mesmo
corte**. Sem reserva disponível, ele avisa na timeline e continua esperando.

### Validação do alinhamento

O offset é medido em duas janelas (início e ~60% do filme). Se divergirem, é
sinal de corte diferente ou drift — o merge continua com o offset do início, mas
o aviso fica registrado na timeline e nas notas do job.

### Limpeza pós-merge (QBIT_CLEANUP)

`keep` (padrão) mantém tudo seedando; `remove` remove os torrents mantendo os
arquivos; `remove_data` apaga também os dados — exceto quando a saída é um
symlink para o próprio download (aí ele remove só o torrent, para não quebrar o
link).

### Cancelar / repetir

Cada card tem **✕** (remove o job, perguntando se apaga também os torrents) e,
para jobs com erro ou cancelados, **↻** (cria um novo job com os mesmos
parâmetros).

### Detalhes de um job (🔍)

Cada card de download tem um botão de lupa que abre um painel ao vivo (atualiza a
cada 2s) com: barras de download com velocidade/ETA/seeds, **todos os candidatos
avaliados** em cada busca (com score e motivo de rejeição — "sem seeders",
"sem marcador de idioma", "título não bate"...), qual foi escolhido e por quê,
e a timeline completa de eventos, incluindo o log do merge.

## Configurações

A aba **Configurações** reúne dois cadastros, ambos guardados no `jobs.db` e
gerenciáveis pela interface (adicionar/editar/remover, marcar padrão).

### Destinos do arquivo final

Pastas onde o filme finalizado pode ser salvo. Ao criar um download, o destino
padrão vem pré-selecionado e você pode trocar antes de iniciar. Cada filme sai
numa subpasta própria dentro do destino escolhido (bom para o Jellyfin/Plex
parsearem o nome). O destino fica registrado no job, então repetir um job (↻)
mantém a mesma pasta. O `OUTPUT_DIR` do `.env` é usado só para criar o destino
"Padrão" na primeira execução.

### Destinos dos torrents (qBittorrent)

Cada destino de torrents tem duas partes:

- **Caminho no qBittorrent** (`save_path`): onde o qBittorrent grava os torrents,
  do ponto de vista *dele* (vazio = pasta padrão do qBittorrent). Ao baixar, o
  serviço manda esse caminho no `add` e desliga o auto-management só nesses torrents.
- **Caminho local**: onde a mesma pasta está montada *nesta máquina* — usado para
  traduzir o `content_path` que a API do qBittorrent reporta e achar o arquivo
  para o merge. Deixe vazio se o qBittorrent roda na mesma máquina/mesmo caminho.

O par `save_path → caminho local` substitui o antigo `QBIT_PATH_MAP` por
download. Na criação do job você escolhe o **destino dos torrents** (padrão
pré-selecionado); sem nenhum cadastrado, usa a pasta padrão do qBittorrent e o
`QBIT_PATH_MAP` global do `.env` como fallback. `QBIT_SAVE_PATH`/`QBIT_PATH_MAP`
do `.env` só semeiam o destino de torrents "Padrão (.env)" na primeira execução.

Dentro do caminho local, o serviço pega o maior arquivo de vídeo (ignorando "sample").

### Catálogo

A aba **Catálogo** lê uma pasta de destino e lista os filmes já baixados (cada
subpasta é um filme). Clicando num item abre o detalhe: correspondência no TMDB
(pôster, título original, nota, sinopse), tamanho total, e a lista de arquivos.
Cada arquivo é um **dropdown** — expandindo, mostra o `ffprobe` parseado em
detalhe: container, bitrate total, capítulos e cada track (vídeo com resolução,
FPS, HDR/10-bit, espaço de cor, perfil/nível; áudio com layout de canais, sample
rate, bitrate; legendas com idioma/forced/SDH), além dos campos crus do ffprobe.
Dá para **remover um arquivo** individual ou a **pasta inteira** do filme.
Caminhos são validados contra traversal — nada fora do destino é tocado.

## merge.py avulso

O merge também funciona como ferramenta independente:

```sh
python merge.py "filme.1080p.mkv" "filme.dublado.mkv" "resultado.mkv" --audio-lang pt
```

- Escolhe automaticamente qual dos dois tem a melhor imagem.
- Melhor áudio por língua entre os dois arquivos (se ambos têm a mesma língua,
  ganha o de melhor codec/canais/bitrate); tags como `pob`/`pt-br` são normalizadas.
- Se o arquivo de melhor vídeo já tem o idioma alvo, sai com symlink em vez de merge.
- Offset detectado por GCC-PHAT (janela de 5 min a partir de 30s — música e efeitos
  coincidem mesmo com falas em idiomas diferentes); só os áudios do outro arquivo
  são re-encodados, o resto é stream copy.

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
| `merge.py` | CLI avulso em cima do `services/merger.py` |
| `frontend/` | frontend React (TS + Tailwind + Iconoir + Router) |
