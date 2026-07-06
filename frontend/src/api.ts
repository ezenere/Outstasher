// Tipos espelhando a API do backend (main.py / services/jobs.py)

export interface Movie {
  id: number
  title: string | null
  original_title: string | null
  year: string
  overview: string | null
  poster: string | null
  rating: number | null
}

export interface Language {
  code: string
  label: string
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
}

export interface TorrentInfo {
  title: string
  seeders: number
  size: number
  score: number
  edition?: string | null
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
}

export interface Job {
  id: string
  tmdb_id: number
  language: string
  mode: string
  status: string
  detail: string
  movie: MovieRef | null
  video_torrent: TorrentInfo | null
  audio_torrent: TorrentInfo | null
  progress: { video: Progress | number | null; audio: Progress | number | null }
  output: string | null
  destination_id?: number | null
  destination_label?: string | null
  destination_path?: string | null
  torrent_target_id?: number | null
  torrent_target_label?: string | null
  torrent_save_path?: string | null
  torrent_local_path?: string | null
  created_at: string
  search?: { audio: Candidate[]; video: Candidate[] } | null
  events?: JobEvent[]
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

export async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(path, opts)
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
