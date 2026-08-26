// Tipos espelhando a API do backend (main.py / services/jobs.py)

export interface Movie {
  id: number
  title: string | null
  original_title: string | null
  year: string
  overview: string | null
  poster: string | null
  rating: number | null
  /** Já existe na coleção (pasta em algum destino) — cache de 30 min no backend. */
  in_catalog?: boolean
  /** 'tv' nos cards vindos de /api/series; ausente = filme. */
  media_type?: 'movie' | 'tv'
}

/** Página de resultados de filmes do TMDB (/api/movies). */
export interface MoviePage {
  results: Movie[]
  page: number
  total_pages: number
  total_results: number
}

// ---------- séries (TMDB TV) ----------

/** Temporada na lista do detalhe da série (/api/series/{id}). */
export interface SeasonInfo {
  season: number
  name: string | null
  episode_count: number | null
  air_date: string | null
}

/** Detalhe da série + temporadas, para o modal de seleção. */
export interface SeriesDetail {
  id: number
  title: string | null
  original_title: string | null
  year: string
  overview: string | null
  poster: string | null
  seasons: SeasonInfo[]
}

/** Episódio de uma temporada (/api/series/{id}/season/{n}). */
export interface EpisodeInfo {
  episode: number
  name: string | null
  air_date: string | null
  runtime: number | null
  overview: string | null
}

export interface SeasonDetail {
  season: number
  name: string | null
  episodes: EpisodeInfo[]
}

/** Subestado de um episódio dentro de um job de série. */
export type EpisodeState =
  | 'pending' | 'downloading' | 'downloaded' | 'aligning' | 'review'
  | 'merging' | 'done' | 'failed' | 'skipped_future' | 'skipped_missing'
  | 'skipped_mismatch'

export const EPISODE_STATE_LABEL: Record<EpisodeState, string> = {
  pending: 'Aguardando',
  downloading: 'Baixando',
  downloaded: 'Baixado',
  aligning: 'Alinhando',
  review: 'Revisão',
  merging: 'Convertendo',
  done: 'Concluído',
  failed: 'Falhou',
  skipped_future: 'Não lançado',
  skipped_missing: 'Pulado (sem torrent)',
  skipped_mismatch: 'Ignorado (desalinhado)',
}

/** Episódio dentro de job["episodes"] (chave "S01E02"). */
export interface JobEpisode {
  season: number
  episode: number
  name: string | null
  air_date: string | null
  runtime: number | null
  state: EpisodeState
  src: Partial<Record<'original' | 'dubbed', string>>
  output: string | null
  error: string | null
}

/** Torrent do plano de um job de série (as duas línguas na mesma lista). */
export interface SeriesTorrent {
  n: number
  role: 'original' | 'dubbed'
  tag: string
  title: string
  tracker: string | null
  seeders: number | null
  size: number | null
  quality: string | null
  coverage_label: string | null
  coverage: string[]
  state: 'pending' | 'downloading' | 'done' | 'abandoned'
  hash: string | null
  progress: Progress | null
  selected_files: string[] | null
  content_path: string | null
}

/** Candidato de série (mesmo shape do Candidate + cobertura). */
export interface SeriesCandidate {
  id: string
  title: string
  tracker: string | null
  seeders: number
  size: number
  quality: string | null
  coverage: string | null
  score: number | null
  tier?: number
}

export type GateReason =
  | 'manual_pick' | 'gaps_confirm' | 'incompatible_torrents'
  | 'alignment_review' | 'drift' | 'mismatched_pairs'

/** Candidato na visão invertida do manual: torrent + episódios que o TÍTULO
 *  dele cobre (o match fino pelo arquivo acontece após o download). */
export interface TorrentChoice extends SeriesCandidate {
  matches: string[]
}

/** Gate ativo de um job de série (job.awaiting). */
export interface JobGate {
  reason: GateReason
  payload: {
    /** gaps_confirm */
    missing?: { episode: string; name: string | null; missing: string[] }[]
    /** gaps_confirm no MEIO do download: os arquivos reais de um pack
     *  revelaram que ele não tinha episódios atribuídos pelo título. */
    mid_download?: boolean
    /** incompatible_torrents */
    torrents?: {
      n: number
      role: 'original' | 'dubbed'
      title: string
      coverage: string[]
      signals: string[]
      alternatives: SeriesCandidate[]
    }[]
    episode_groups?: { id: string; name: string; type: number; episode_count: number }[]
    /** mismatched_pairs: episódios que falharam por conflito de alinhamento */
    mismatched?: Record<string, { original: string; dubbed: string; error: string }>
    attempts?: number
    /** manual_pick */
    candidates?: Record<'original' | 'dubbed', Record<string, SeriesCandidate[]>>
    by_torrent?: Record<'original' | 'dubbed', TorrentChoice[]>
    requested?: string[]
    preselected?: { n: number; role: string; title: string; quality: string | null; coverage: string[] }[]
  }
}

/** Troca manual de um torrent de série ("tentar próximo(s)" com candidateId nulo). */
export function switchSeriesTorrent(jobId: string, torrentN: number,
                                    candidateId: string | null = null) {
  return post<Job>(`/api/jobs/${jobId}/switch-torrent`,
    { torrent_n: torrentN, candidate_id: candidateId })
}

/** Candidatos compatíveis (mesma cobertura) para trocar um torrent do plano. */
export function seriesTorrentCandidates(jobId: string, n: number) {
  return api<{ candidates: SeriesCandidate[] }>(
    `/api/jobs/${jobId}/torrent-candidates?n=${n}`)
}

/** Resumo de série no card da lista (/api/jobs/list). */
export interface SeriesSummary {
  episodes_total: number
  by_state: Partial<Record<EpisodeState, number>>
  download_pct: number | null
  awaiting_reason: GateReason | null
}

/** Resolve o gate ativo de um job de série. */
export function resolveGate(jobId: string, reason: GateReason | 'force_continue',
                            decision: object = {}) {
  return post<Job>(`/api/jobs/${jobId}/resolve`, { reason, decision })
}

// ---------- EDL (revisão de alinhamento) ----------

export type SegmentKind = 'match' | 'gap_dub' | 'gap_orig' | 'replaced' | 'pal' | 'drift'
export type ReviewAction = 'fill_original' | 'silence' | 'use_dub' | 'cut_video' | 'accept'

/** Segmento da EDL (tempos em s; a = dublado, b = original). */
export interface EdlSegment {
  kind: SegmentKind
  a_start: number
  a_end: number
  b_start: number | null
  b_end: number | null
  offset: number | null
  slope: number | null
  residual: number
  confidence: number
  note: string
  /** Decisão de revisão já aplicada (explícita ou por regra). */
  action?: ReviewAction
}

export interface Edl {
  version: number
  episode: string
  source_dub: { path: string; duration: number }
  source_orig: { path: string; duration: number }
  segments: EdlSegment[]
  confidence_profile: number[] | null
  review: { required: boolean; flagged: { a_start: number; a_end: number; reason: string }[] }
}

/** Regra de revisão reaplicável ("aplicar a todos os episódios"). */
export interface ReviewRule {
  when: { kind?: SegmentKind; position?: 'start' | 'end' | 'middle' | 'any'; min_len?: number; max_len?: number }
  action: ReviewAction
}

/** Busca um frame de comparação como blob-URL (o <img> puro não manda o
 *  header de Authorization — buscamos via fetch autenticado). O chamador é
 *  dono do URL e deve dar URL.revokeObjectURL ao descartar. */
export async function fetchFrame(jobId: string, episode: string,
                                 side: 'a' | 'b', t: number): Promise<string> {
  const r = await fetch(
    `/api/jobs/${jobId}/frame?episode=${episode}&side=${side}&t=${t.toFixed(2)}`,
    { headers: { Authorization: `Bearer ${getToken() ?? ''}` } })
  if (!r.ok) throw new Error(`frame ${r.status}`)
  return URL.createObjectURL(await r.blob())
}

export interface Language {
  code: string
  label: string
}

// ---- cadastro de idiomas (editável) ----

export interface LanguageEntry {
  code: string
  label: string
  tmdb: string
  markers_strong: string[]
  markers_weak: string[]
}

export interface LanguageConfig {
  languages: LanguageEntry[]
  subtitle_markers: string[]
}

// ---- buscas extras (idioma x variante x indexers) ----

export interface JackettIndexer {
  id: string
  name: string
  language?: string
  configured: boolean
}

// regras: { "<lang>": { "no_year": ["indexerId", ...], "roman": [...], "roman_no_year": [...] } }
export type ExtraSearchRules = Record<string, Record<string, string[]>>

export interface AdvancedMergeConfig {
  /** trecho sem dublagem: sai do vídeo | fica mudo | recebe o áudio original */
  undubbed: 'cut' | 'silence' | 'fill'
  /** com 'cut': corta a partir de N s; abaixo disto o trecho fica mudo */
  cut_min_s: number
  /** re-encode do vídeo nos cortes (corte exato no frame) */
  reencode: 'auto' | 'av1_qsv' | 'libsvtav1' | 'none'
  /** CRF / ICQ do re-encode */
  quality: number
}

export interface AdvancedMergeInfo {
  config: AdvancedMergeConfig
  encoders: { av1_qsv: boolean; libsvtav1: boolean }
}

export interface ExtraSearchConfig {
  rules: ExtraSearchRules
  variants: string[]
  languages: Language[]
}

export const VARIANT_LABEL: Record<string, string> = {
  no_year: 'Sem o ano',
  roman: 'Trocando romanos (II → 2)',
  roman_no_year: 'Trocando romanos e sem o ano',
}

// ---- opções avançadas de conversão ----

/** Payload de opções avançadas enviado em /api/jobs e /api/jobs/manual. */
export interface ConvertOptions {
  video_codec: string // keep | vvc | av1 | hevc | h264
  hw_accel: string // none (software) | nvenc | qsv
  preset: string // veryfast | fast | default | slow | veryslow
  resolution: string // keep | 4320 | 2160 | 1080 | 720 | 480
  quality_mode: 'bitrate' | 'crf'
  video_bitrate: number | null // kbps
  crf: number | null
  bit_depth: string // keep | 10 | 8
  audio_tracks: string // all | target
  audio_codec: string // keep | ac3 | flac | opus | vorbis | aac
  audio_bitrate: number | null // kbps por faixa
  channels: string // keep | surround51 | stereo
  subtitles: string // default | all | none
}

export interface HwEncoderCap {
  id: string // nvenc | qsv
  encoder: string
  ten_bit: boolean
  crf: { min: number; max: number; default: number }
}

export interface VideoCodecCap {
  id: string
  label: string
  encoder: string | null
  available: boolean
  hw: HwEncoderCap[]
  crf: { min: number; max: number; default: number }
}

export interface AudioCodecCap {
  id: string
  label: string
  available: boolean
  max_channels: number
  lossless: boolean
  default_kbps: number | null
}

/** Conjunto de ConvertOptions salvo com um nome (/api/convert-presets). */
export interface ConvertPreset {
  id: number
  name: string
  options: ConvertOptions
}

/** O que o ffmpeg do servidor sabe encodar (/api/capabilities). */
export interface Capabilities {
  video_codecs: VideoCodecCap[]
  audio_codecs: AudioCodecCap[]
  presets: string[]
  hw_accels: { id: string; label: string; available: boolean }[]
  video_bitrate_kbps: [number, number]
  audio_bitrate_kbps: [number, number]
}

export interface DiskInfo {
  total: number
  used: number
  free: number
}

export interface Destination {
  id: number
  label: string
  path: string
  /** Biblioteca a que o destino pertence (filmes e séries são separadas). */
  media?: 'movie' | 'tv'
  is_default: boolean
  disk?: DiskInfo | null
}

export interface TorrentTarget {
  id: number
  label: string
  save_path: string
  local_path: string
  is_default: boolean
  disk?: DiskInfo | null
}

export interface Progress {
  pct: number
  speed?: number
  eta?: number | null
  state?: string | null
  seeds?: number | null
  name?: string | null
  size?: number | null        // tamanho total dos arquivos a baixar (bytes)
  downloaded?: number | null  // bytes já baixados
}

/** Progresso do ffmpeg durante o merge (parseado de -progress pipe:1). */
export interface MergeProgress {
  pct: number         // ESCRITA: out_time/duração — quanto já saiu codificado
  read_pct?: number   // LEITURA: frames lidos/total — quanto já entrou no encoder
  out_s: number       // tempo do filme já processado (s)
  duration_s: number  // duração total esperada (s)
  frame?: number      // frames lidos até agora
  size: number        // bytes escritos até agora
  bitrate: number     // bits/s
  speed: number       // multiplicador (1.35 = 1.35x tempo real)
  fps: number
  eta: number | null  // segundos restantes
  // presentes SÓ no fingerprint do alinhamento (não é conversão): qual dos
  // dois arquivos está sendo lido
  step?: number       // 1 = primeiro arquivo, 2 = segundo
  label?: string      // 'dublado' (fonte do áudio) | 'original' (fonte do vídeo)
  phase?: MergePhase  // etapa (o backend rotula; ver MERGE_PHASE)
}

/** Etapas de uma conversão. O backend rotula cada uma em progress.merge.phase
 *  e as expõe em summary().phase / lista.progress.merge_phase — o dropdown, a
 *  lista e o detalhe do job leem TODOS daqui, para dizerem a mesma coisa. */
export type MergePhase = 'align' | 'edl' | 'convert'

export const MERGE_PHASE: Record<MergePhase, { label: string; amber: boolean }> = {
  // etapas intermediárias (âmbar): ainda não é o arquivo final saindo
  align: { label: 'Buscando alinhamento', amber: true },
  edl: { label: 'Gerando áudio da EDL', amber: true },
  // conversão de verdade: juntando/convertendo o arquivo final
  convert: { label: 'Convertendo', amber: false },
}

export function mergePhase(phase?: string | null) {
  return MERGE_PHASE[(phase as MergePhase)] ?? null
}

export interface TorrentInfo {
  title: string
  seeders: number
  size: number
  score: number
  edition?: string | null
  /** id/tracker do candidato escolhido — casam a linha exata em uso na tabela. */
  id?: string | null
  tracker?: string | null
}

export interface Candidate {
  id?: string
  title: string
  tracker?: string | null
  /** null num torrent informado à mão (não passou pelo indexador). */
  seeders: number | null
  size: number
  edition?: string | null
  /** Rótulo da qualidade estilo Radarr ("4K Remux", "1080p WEB-DL", "Desconhecida"). */
  quality?: string | null
  score: number | null
  rejected?: string | null
  chosen?: boolean
  /** Modo áudio: o nome traz o ano do filme (identificação confiável — vem antes no rank). */
  year_match?: boolean
}

export interface JobEvent {
  ts: string
  kind: string
  message: string
  data?: { role?: string; query?: string; candidates?: Candidate[] }
}

export interface MovieRef {
  original_title: string
  localized_title?: string | null
  year: string
  poster?: string | null
  overview?: string | null
}

export interface Job {
  id: string
  tmdb_id: number
  language: string
  mode: string
  kind?: string // both | original | dubbed
  /** Só baixa pelo qBittorrent e conclui — sem conversão, hardlink ou cópia. */
  download_only?: boolean
  /** Opções avançadas de conversão do job (null/ausente = pipeline clássico). */
  convert?: ConvertOptions | null
  status: string
  detail: string
  movie: MovieRef | null
  video_torrent: TorrentInfo | null
  audio_torrent: TorrentInfo | null
  progress: {
    video: Progress | number | null
    audio: Progress | number | null
    merge?: MergeProgress | null
  }
  output: string | null
  destination_id?: number | null
  destination_label?: string | null
  destination_path?: string | null
  torrent_target_id?: number | null
  torrent_target_label?: string | null
  torrent_save_path?: string | null
  torrent_local_path?: string | null
  created_at: string
  /** ISO de quando a conversão/cópia começou (para o tempo decorrido). */
  merge_started_at?: string | null
  search?: { audio: Candidate[]; video: Candidate[] } | null
  events?: JobEvent[]
  /** Presente quando a conversão pausou por offsets divergentes (possível
   *  versão/corte diferente) e espera o usuário clicar em Continuar. */
  drift_confirm?: {
    video_file: string; audio_file: string; tau1_ms: number; tau2_ms: number
    /** offset medido a cada 5 min (mesmo scanner das séries) e a forma do
     *  perfil: drift (muda aos poucos) | cut (salto entre patamares) | flat |
     *  mixed | unknown; ausentes enquanto o perfil ainda está sendo medido */
    profile?: { t: number; offset_ms: number; quality: number }[]
    verdict?: 'drift' | 'cut' | 'flat' | 'mixed' | 'unknown'
  } | null
  /** Candidato ativo por papel (o que está no qBittorrent agora). */
  current?: { video?: Candidate | null; audio?: Candidate | null } | null
  /** Conversão manual (mode 'files'): os dois arquivos locais de origem. */
  manual_files?: { video: string; audio: string } | null
  // ---- campos de job de SÉRIE (media_type 'tv') ----
  media_type?: JobMedia
  request?: { seasons: number[]; episodes: Record<string, number[]> }
  episodes?: Record<string, JobEpisode>
  torrents?: SeriesTorrent[]
  awaiting?: JobGate | null
  report?: { attempted: number; succeeded: number; failed: string[]; skipped?: string[] } | null
}

// ---- shapes enxutos das rotas de polling granular ----

/** Dimensão de mídia de um job. Jobs anteriores à feature de séries não têm o
 *  campo — o backend preenche 'movie' por padrão. */
export type JobMedia = 'movie' | 'tv'

/** Item do dropdown de processos (/api/jobs/summary). Só o mínimo. */
/** Uma pasta do servidor, como o navegador a devolve (GET /api/browse). */
export interface BrowseDir {
  path: string
  parent: string | null
  dirs: { name: string; path: string }[]
  files: { name: string; path: string; size: number }[]
  truncated: boolean
  shortcuts: { label: string; path: string }[]
}

/** Um lado (original/dublado) de uma temporada no scan do merge manual. */
export interface ManualSide {
  dir: string          // pasta DOMINANTE (a que mais cobre a temporada)
  order: string        // "SxxEyy (E01–E24, 2 fundido(s))" | "absoluta (…)" | …
  files: number
  episodes: number
  dirs: number         // >1 = a raiz mistura pastas; só a dominante pareia
}

/** Ordem de episódios da série (episode group do TMDB). A de exibição é o
 *  padrão e não vem da API — a tela a oferece como primeira opção. */
export interface EpisodeOrder {
  id: string
  name: string | null
  type: number          // 1=exibição 2=absoluta 3=DVD 4=digital … 7=TV
  group_count: number
  episode_count: number
}

export interface ManualRow {
  season: number
  episode: number
  name: string | null
  /** Onde este episódio fica na ordem de EXIBIÇÃO (só em ordem alternativa). */
  aired: { season: number; episode: number } | null
  original: string | null
  dubbed: string | null
  orig_duration: number | null
  dub_duration: number | null
  include: boolean
}

export interface ManualSeason {
  season: number
  original: ManualSide
  dubbed: ManualSide
  rows: ManualRow[]
  unmatched: { original: string[]; dubbed: string[] }
}

/** Releitura de UMA temporada com a pasta escolhida à mão para ela. */
export interface ManualSeasonScan {
  season: ManualSeason
  files: { original: { path: string; name: string }[]
           dubbed: { path: string; name: string }[] }
}

export interface ManualScan {
  original: { root: string; seasons: Record<string, { files: { path: string; name: string }[] }> }
  dubbed: { root: string; seasons: Record<string, { files: { path: string; name: string }[] }> }
  seasons: ManualSeason[]
}

export interface JobSummary {
  /** etapa da conversão (só com state 'converting'); ver MERGE_PHASE */
  phase?: MergePhase | null
  id: string
  tmdb_id: number
  title: string
  status: string
  state: MovieState
  pct: number | null
  media_type?: JobMedia
  /** Recompressão: reusa o tmdb_id de um filme já baixado, então não define o
   *  estado do card na tela de Filmes. */
  recompress?: boolean
}

/** Contagem por grupo (/api/jobs/counts) para os badges do filtro. */
export interface JobCounts {
  all: number
  active: number
  error: number
  done: number
}

/** Progresso de um torrent no card da lista: % + baixado/total (velocidade,
 *  ETA e seeds só no detalhe do job). */
export interface SlimProgress {
  pct: number
  downloaded: number | null
  size: number | null
  state: string | null
}

/** Card enxuto da lista de Downloads (/api/jobs/list). */
export interface JobListItem {
  id: string
  tmdb_id: number
  language: string
  media_type?: JobMedia
  /** Resumo de série (contagens por estado + % agregado), só em media_type tv. */
  series?: SeriesSummary
  mode: string
  kind: string
  download_only?: boolean
  /** true quando o job tem opções avançadas de conversão. */
  convert?: boolean
  status: string
  detail: string
  movie: MovieRef | null
  created_at: string
  destination_label?: string | null
  video_torrent: TorrentInfo | null
  audio_torrent: TorrentInfo | null
  output: string | null
  progress: {
    video: SlimProgress | null
    audio: SlimProgress | null
    merge: number | null
    merge_read?: number | null
    /** etapa da conversão (ver MERGE_PHASE) — o card usa nome e cor daqui */
    merge_phase?: MergePhase | null
  }
}

/** Resposta paginada de /api/jobs/list. */
export interface JobListPage {
  items: JobListItem[]
  page: number
  per_page: number
  total: number
  pages: number
}

/** Tick de 1s do detalhe do job (/api/jobs/{id}/progress). */
export interface JobProgress {
  id: string
  status: string
  detail: string
  progress: Job['progress']
  output: string | null
  merge_started_at?: string | null
}

// ---------- catálogo ----------

export interface CatalogItem {
  folder: string
  title: string
  year: string | null
  /** 'series' = pasta com subpastas "Season NN" (layout Jellyfin). */
  type?: 'movie' | 'series'
  size: number
  size_human: string
  file_count: number
  has_video: boolean
}

export interface CatalogList {
  destination: Destination
  exists: boolean
  items: CatalogItem[]
}

export interface Stream {
  index: number
  type: 'video' | 'audio' | 'subtitle' | string
  codec: string | null
  codec_long: string | null
  profile: string | null
  language: string | null
  title: string | null
  default: boolean
  forced: boolean
  bitrate: string | null
  // video
  resolution?: string | null
  width?: number | null
  height?: number | null
  fps?: number | null
  pix_fmt?: string | null
  bit_depth?: string | null
  color_space?: string | null
  color_transfer?: string | null
  color_primaries?: string | null
  hdr?: boolean
  aspect_ratio?: string | null
  level?: number | null
  // audio
  channels?: number | null
  channel_layout?: string | null
  sample_rate?: string | null
  sample_fmt?: string | null
  // subtitle
  hearing_impaired?: boolean
  raw?: Record<string, unknown>
}

export interface CatalogFile {
  name: string
  rel: string
  ext: string
  size: number
  size_human: string
  category: 'video' | 'subtitle' | 'media' | 'other'
  container?: string | null
  duration?: string | null
  overall_bitrate?: string | null
  streams?: Stream[]
  counts?: { video: number; audio: number; subtitle: number }
  chapters?: number
  probe_error?: string
}

export interface CatalogDetail {
  destination: Destination
  folder: string
  title: string
  year: string | null
  type?: 'movie' | 'series'
  /** id do TMDB já marcado no nome da pasta ([tmdbid-N]); null = ainda não marcada. */
  tmdb_id: number | null
  size: number
  size_human: string
  files: CatalogFile[]
  /** Série: arquivos agrupados por temporada (null = soltos na raiz). */
  seasons?: { season: number | null; files: CatalogFile[] }[]
  tmdb: Movie | null
}

// ---------- sessão / token ----------
// Token no sessionStorage: some quando o navegador/aba fecha (login de novo),
// que é o comportamento pedido. Um evento 'auth-expired' avisa o App quando a
// API responde 401 (sessão caiu, servidor reiniciou etc.).

const TOKEN_KEY = 'downloader_token'

export const getToken = () => sessionStorage.getItem(TOKEN_KEY)
export const setToken = (t: string) => sessionStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => sessionStorage.removeItem(TOKEN_KEY)

export interface AuthStatus {
  password_set: boolean
  authenticated: boolean
}

export async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const token = getToken()
  const headers = new Headers(opts?.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const r = await fetch(path, { ...opts, headers })
  if (r.status === 401 && !path.startsWith('/api/auth/')) {
    clearToken()
    window.dispatchEvent(new Event('auth-expired'))
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}) as { detail?: string })
    throw new Error((body as { detail?: string }).detail || r.statusText)
  }
  return r.json() as Promise<T>
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

export const put = <T,>(path: string, body: unknown) =>
  api<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const del = <T,>(path: string) => api<T>(path, { method: 'DELETE' })

// ---------- ações de auth ----------

export const authStatus = () => api<AuthStatus>('/api/auth/status')

export async function login(password: string): Promise<void> {
  const { token } = await post<{ token: string }>('/api/auth/login', { password })
  setToken(token)
}

export async function setupPassword(password: string): Promise<void> {
  const { token } = await post<{ token: string }>('/api/auth/setup', { password })
  setToken(token)
}

export async function logout(): Promise<void> {
  try {
    await post('/api/auth/logout')
  } finally {
    clearToken()
  }
}

export async function changePassword(current_password: string, new_password: string): Promise<void> {
  const { token } = await post<{ token: string }>('/api/auth/change-password', {
    current_password,
    new_password,
  })
  setToken(token)
}

// ---------- formatadores ----------

export function fmtSize(bytes: number | null | undefined): string {
  if (!bytes) return '?'
  const gb = bytes / 1024 ** 3
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(0)} MB`
}

/** Tamanho de disco: escala até TB, sem casas quando é grande. */
export function fmtDisk(bytes: number | null | undefined): string {
  if (bytes == null) return '?'
  const tb = bytes / 1024 ** 4
  if (tb >= 1) return `${tb.toFixed(tb >= 10 ? 0 : 1)} TB`
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${Math.round(gb)} GB`
  return `${Math.round(bytes / 1024 ** 2)} MB`
}

export function fmtSpeed(bps: number): string {
  const mb = bps / 1024 ** 2
  return mb >= 1 ? `${mb.toFixed(1)} MB/s` : `${(bps / 1024).toFixed(0)} kB/s`
}

/** Segundos -> "1:42:13" / "42:13" (posição no filme). Com forceHours=true
 *  sempre inclui a hora ("0:42:13"), para pares alinharem sem confundir. */
export function fmtTime(s: number, forceHours = false): string {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  const mm = String(m).padStart(2, '0')
  const ss = String(sec).padStart(2, '0')
  return h || forceHours ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

export function fmtEta(s: number): string {
  if (s >= 8640000) return '∞'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h) return `${h}h${String(m).padStart(2, '0')}m`
  if (m) return `${m}m${String(Math.floor(s % 60)).padStart(2, '0')}s`
  return `${Math.floor(s)}s`
}

/** progress pode ser número (jobs antigos) ou objeto */
export function prog(p: Progress | number | null | undefined): Progress | null {
  if (p == null) return null
  if (typeof p === 'number') return { pct: p }
  return p
}

export const STATUS_LABEL: Record<string, string> = {
  searching: 'Buscando',
  awaiting: 'Aguardando escolha',
  downloading: 'Baixando',
  merging: 'Convertendo',
  done: 'Concluído',
  error: 'Erro',
  cancelled: 'Cancelado',
}

// ---------- estados crus do qBittorrent -> rótulo legível + severidade ----------
// A API do qBittorrent devolve estados como "stalledDL", "metaDL",
// "checkingResumeData"... Aqui viram texto humano. `tone` colore o chip:
// ok (baixando/seedando), warn (parado/enfileirado), done (completo), err (erro).

export type QbitTone = 'ok' | 'warn' | 'done' | 'err' | 'neutral'

interface QbitStateInfo {
  label: string
  tone: QbitTone
}

const QBIT_STATE: Record<string, QbitStateInfo> = {
  downloading: { label: 'Baixando', tone: 'ok' },
  forcedDL: { label: 'Baixando (forçado)', tone: 'ok' },
  metaDL: { label: 'Obtendo metadados', tone: 'warn' },
  forcedMetaDL: { label: 'Obtendo metadados', tone: 'warn' },
  stalledDL: { label: 'Sem fonte (esperando seeds)', tone: 'warn' },
  queuedDL: { label: 'Na fila para baixar', tone: 'warn' },
  allocating: { label: 'Alocando espaço', tone: 'neutral' },
  checkingDL: { label: 'Verificando dados', tone: 'neutral' },
  checkingResumeData: { label: 'Verificando ao iniciar', tone: 'neutral' },
  moving: { label: 'Movendo arquivos', tone: 'neutral' },
  pausedDL: { label: 'Pausado', tone: 'warn' },
  stoppedDL: { label: 'Parado', tone: 'warn' },
  uploading: { label: 'Concluído (seedando)', tone: 'done' },
  forcedUP: { label: 'Concluído (seedando)', tone: 'done' },
  stalledUP: { label: 'Concluído (sem leechers)', tone: 'done' },
  queuedUP: { label: 'Concluído (na fila de upload)', tone: 'done' },
  checkingUP: { label: 'Concluído (verificando)', tone: 'done' },
  pausedUP: { label: 'Concluído', tone: 'done' },
  stoppedUP: { label: 'Concluído', tone: 'done' },
  error: { label: 'Erro', tone: 'err' },
  missingFiles: { label: 'Arquivos ausentes', tone: 'err' },
  unknown: { label: 'Desconhecido', tone: 'neutral' },
}

/** Estado cru do qBittorrent -> { label legível, tone p/ cor }. */
export function qbitState(state?: string | null): QbitStateInfo {
  if (!state) return { label: '', tone: 'neutral' }
  return QBIT_STATE[state] ?? { label: state, tone: 'neutral' }
}

/** True quando o torrent já terminou de baixar (qualquer variante de "UP"). */
export function qbitIsComplete(state?: string | null): boolean {
  return !!state && (state.endsWith('UP') || state.endsWith('up'))
}

// ---------- estado de um filme derivado dos jobs ----------
// A tela de Filmes deriva o estado de cada filme do summary compartilhado do
// cabeçalho (JobSummary[], via JobsSummaryContext), sem cruzar jobs no cliente.
// Aqui ficam só o tipo e os rótulos.

// nome legível de idioma a partir do código (fallback: código em maiúsculas).
// Espelha os labels do config.LANGUAGES do backend — estáveis o bastante para
// os cards não precisarem buscar /api/languages.
const LANG_NAME: Record<string, string> = {
  pt: 'Português', es: 'Espanhol', en: 'Inglês', it: 'Italiano',
  de: 'Alemão', fr: 'Francês', ja: 'Japonês', ko: 'Coreano',
}

export const langName = (code: string): string =>
  LANG_NAME[code] ?? code.toUpperCase()

/** Resumo curto das opções de conversão para exibir na descrição/eventos.
 *  Só as opções que diferem do padrão. Retorna [] quando tudo é padrão. */
export function convertSummary(c: ConvertOptions | null | undefined): string[] {
  if (!c) return []
  const hwShort: Record<string, string> = { nvenc: 'NVENC', qsv: 'QSV' }
  const hw = c.hw_accel && c.hw_accel !== 'none' ? (hwShort[c.hw_accel] ?? c.hw_accel) : null
  const out: string[] = []
  if (c.video_codec !== 'keep') out.push(c.video_codec.toUpperCase() + (hw ? ` (${hw})` : ''))
  if (c.resolution !== 'keep') {
    const r: Record<string, string> = { '4320': '8K', '2160': '4K', '1080': '1080p', '720': '720p', '480': '480p' }
    out.push(r[c.resolution] ?? c.resolution)
  }
  if (c.quality_mode === 'crf' && c.crf != null) out.push(`CRF ${c.crf}`)
  else if (c.video_codec !== 'keep' && c.video_bitrate != null)
    out.push(c.video_bitrate >= 1000 ? `${(c.video_bitrate / 1000).toFixed(1)} Mbps` : `${c.video_bitrate} kbps`)
  if (c.bit_depth !== 'keep') out.push(`${c.bit_depth}-bit`)
  // preset/HW só são relevantes quando há re-encode de vídeo
  const reencodesVideo = c.video_codec !== 'keep' || c.resolution !== 'keep' || c.bit_depth !== 'keep'
  if (reencodesVideo && hw && c.video_codec === 'keep') out.push(hw)
  if (reencodesVideo && c.preset !== 'default') {
    const p: Record<string, string> = {
      veryfast: 'muito rápido', fast: 'rápido', slow: 'lento', veryslow: 'muito lento',
    }
    out.push(`preset ${p[c.preset] ?? c.preset}`)
  }
  if (c.audio_codec !== 'keep') out.push(`áudio ${c.audio_codec.toUpperCase()}`)
  if (c.channels !== 'keep') out.push(c.channels === 'stereo' ? 'estéreo' : '5.1')
  if (c.audio_tracks === 'target') out.push('só orig+dub')
  if (c.subtitles === 'none') out.push('sem legendas')
  else if (c.subtitles === 'all') out.push('todas legendas')
  return out
}

export type MovieState = 'converting' | 'downloading' | 'searching' | 'awaiting' | 'done' | 'error'

export const MOVIE_STATE_LABEL: Record<MovieState, string> = {
  converting: 'Convertendo',
  downloading: 'Baixando',
  searching: 'Procurando',
  awaiting: 'Aguardando escolha',
  done: 'Baixado',
  error: 'Com erro',
}
