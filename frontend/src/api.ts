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
}

/** Página de resultados de filmes do TMDB (/api/movies). */
export interface MoviePage {
  results: Movie[]
  page: number
  total_pages: number
  total_results: number
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

export interface VideoCodecCap {
  id: string
  label: string
  encoder: string | null
  available: boolean
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

/** O que o ffmpeg do servidor sabe encodar (/api/capabilities). */
export interface Capabilities {
  video_codecs: VideoCodecCap[]
  audio_codecs: AudioCodecCap[]
  presets: string[]
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
  seeders: number
  size: number
  edition?: string | null
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
  drift_confirm?: { video_file: string; audio_file: string; tau1_ms: number; tau2_ms: number } | null
  /** Conversão manual (mode 'files'): os dois arquivos locais de origem. */
  manual_files?: { video: string; audio: string } | null
}

// ---- shapes enxutos das rotas de polling granular ----

/** Item do dropdown de processos (/api/jobs/summary). Só o mínimo. */
export interface JobSummary {
  id: string
  tmdb_id: number
  title: string
  status: string
  state: MovieState
  pct: number | null
}

/** Contagem por grupo (/api/jobs/counts) para os badges do filtro. */
export interface JobCounts {
  all: number
  active: number
  error: number
  done: number
}

/** Card enxuto da lista de Downloads (/api/jobs/list). Progresso já é % puro. */
export interface JobListItem {
  id: string
  tmdb_id: number
  language: string
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
  progress: { video: number | null; audio: number | null; merge: number | null; merge_read?: number | null }
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
  size: number
  size_human: string
  files: CatalogFile[]
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
  merging: 'Fazendo merge',
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
  const out: string[] = []
  if (c.video_codec !== 'keep') out.push(c.video_codec.toUpperCase())
  if (c.resolution !== 'keep') {
    const r: Record<string, string> = { '4320': '8K', '2160': '4K', '1080': '1080p', '720': '720p', '480': '480p' }
    out.push(r[c.resolution] ?? c.resolution)
  }
  if (c.quality_mode === 'crf' && c.crf != null) out.push(`CRF ${c.crf}`)
  else if (c.video_codec !== 'keep' && c.video_bitrate != null)
    out.push(c.video_bitrate >= 1000 ? `${(c.video_bitrate / 1000).toFixed(1)} Mbps` : `${c.video_bitrate} kbps`)
  if (c.bit_depth !== 'keep') out.push(`${c.bit_depth}-bit`)
  // preset só é relevante quando há re-encode de vídeo (e ≠ default)
  const reencodesVideo = c.video_codec !== 'keep' || c.resolution !== 'keep' || c.bit_depth !== 'keep'
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
