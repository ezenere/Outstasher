# TODO

## Integrações

### Prowlarr
Coopera com o Jackett (não substitui).
- [ ] Configurações → "Indexadores": URL + API key do Prowlarr, teste de conexão,
  lista dos indexadores dele com escolha de quais usar (e categorias filme/TV).
- [ ] `services/prowlarr.py` com o mesmo shape de resultado do `jackett.py`; as
  buscas de filmes e séries rodam nos dois em paralelo e deduplicam por
  infohash/título; o candidato mostra a origem.
- [ ] Links `.torrent`/magnet passam pelo mesmo resolvedor do Jackett.
- [ ] Futuro: plugin "Outstasher" no Prowlarr (hoje o Prowlarr só conhece Sonarr/Radarr).

### Sonarr / Radarr
Fluxo: uma solicitação com a **tag do Outstasher** chega no Sonarr/Radarr, que
baixa o release na qualidade original (fluxo normal deles). O Outstasher:
- [ ] detecta o item com a tag (polling/webhook), **já busca a versão dublada**
  assim que o job aparece (não precisa esperar o original terminar);
- [ ] quando o original termina, faz o **merge** (mesmo pipeline dos jobs) e
  entrega no lugar/nome que o Sonarr/Radarr esperam;
- [ ] **monitora upgrades**: se o Sonarr/Radarr trocarem o release (qualidade
  melhor), refaz o merge com o áudio dublado sobre o novo original.
- [ ] Configurações: URL + API key de cada um, tag a observar, teste de conexão.

## Seleção de torrents
- [ ] "Dual Audio" com outro idioma nomeado no título (Hindi/French/Latino…) não
  pode contar como dublado no idioma alvo — regra no `marker_strength`.
- [ ] Persistir os candidatos rejeitados (com motivo) nos jobs de série, como os
  filmes já fazem em `search`.

## Interface
- [ ] Mostrar as legendas externas encontradas por episódio/filme (hoje só nos
  eventos).

## Alinhamento
- [ ] Fronteiras de corte vindas do vídeo ainda ficam na grade de 0,25 s —
  bissecção por áudio + snap no silêncio em todas, não só na junção "dublado a
  mais".
- [ ] Rótulo do "miolo X/64" em plano escuro/parado: dizer "hash pouco confiável"
  em vez de "diferente".
- [ ] Recap no caso não-fundido (gap_dub de 20–120 s no início) → aceitar
  automaticamente como regra padrão.
