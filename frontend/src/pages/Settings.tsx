import { useEffect, useState } from 'react'
import {
  Check, EditPencil, Folder, HardDrive, Lock, Plus, Star, StarSolid, Trash, Xmark,
} from 'iconoir-react'
import {
  api, changePassword, del, post, put, type Destination, type TorrentTarget,
} from '../api'
import { DiskBar, Empty } from '../components/ui'

export default function Settings() {
  return (
    <div className="flex flex-col gap-10">
      <DestinationsSection />
      <TorrentTargetsSection />
      <SecuritySection />
    </div>
  )
}

// ---------------- senha ----------------

function SecuritySection() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  async function save() {
    setMsg(null)
    if (next !== confirm) return setMsg({ ok: false, text: 'As senhas novas não conferem.' })
    if (next.length < 4) return setMsg({ ok: false, text: 'A nova senha precisa ter ao menos 4 caracteres.' })
    setBusy(true)
    try {
      await changePassword(current, next)
      setCurrent(''); setNext(''); setConfirm('')
      setMsg({ ok: true, text: 'Senha alterada. As outras sessões foram desconectadas.' })
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <div className="flex items-center gap-2">
        <Lock width={18} height={18} className="text-zinc-500" />
        <h1 className="text-lg font-semibold">Senha de acesso</h1>
      </div>
      <p className="mt-1 text-sm text-zinc-400">
        Troca a senha usada para entrar no serviço. Ao salvar, as outras sessões abertas caem.
      </p>
      <div className="mt-4 max-w-md rounded-xl border border-zinc-700 bg-zinc-900 p-4">
        <div className="grid gap-3">
          <Field label="Senha atual" value={current} onChange={setCurrent} type="password" />
          <Field label="Nova senha" value={next} onChange={setNext} type="password" />
          <Field label="Confirmar nova senha" value={confirm} onChange={setConfirm} type="password" />
        </div>
        {msg && (
          <div className={`mt-3 text-sm ${msg.ok ? 'text-emerald-400' : 'text-red-400'}`}>{msg.text}</div>
        )}
        <div className="mt-4">
          <button
            onClick={save}
            disabled={busy || !current || !next}
            className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            <Check width={16} height={16} /> Alterar senha
          </button>
        </div>
      </div>
    </section>
  )
}

function IconBtn({ title, onClick, children }: {
  title: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
    >
      {children}
    </button>
  )
}

function Field({ label, value, onChange, placeholder, mono, type }: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  mono?: boolean
  type?: string
}) {
  return (
    <label className="text-sm">
      <span className="text-zinc-400">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-blue-500 ${mono ? 'font-mono' : ''}`}
      />
    </label>
  )
}

// ---------------- destinos do arquivo final ----------------

interface DestForm {
  id: number | null
  label: string
  path: string
  is_default: boolean
}

function DestinationsSection() {
  const [dests, setDests] = useState<Destination[] | null>(null)
  const [form, setForm] = useState<DestForm | null>(null)
  const [saving, setSaving] = useState(false)

  async function reload() {
    try {
      setDests(await api<Destination[]>('/api/destinations'))
    } catch {
      /* proximo tick */
    }
  }
  useEffect(() => void reload(), [])

  async function save() {
    if (!form) return
    if (!form.label.trim() || !form.path.trim()) return alert('Preencha nome e caminho.')
    setSaving(true)
    try {
      const body = { label: form.label.trim(), path: form.path.trim(), is_default: form.is_default }
      if (form.id == null) await post('/api/destinations', body)
      else await put(`/api/destinations/${form.id}`, body)
      setForm(null)
      void reload()
    } catch (e) {
      alert(`Erro: ${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  async function remove(d: Destination) {
    if (!confirm(`Remover o destino "${d.label}"?\n(Jobs já criados mantêm o caminho que usaram.)`)) return
    try {
      await del(`/api/destinations/${d.id}`)
      void reload()
    } catch (e) {
      alert(`Erro: ${(e as Error).message}`)
    }
  }

  async function makeDefault(d: Destination) {
    await put(`/api/destinations/${d.id}`, { label: d.label, path: d.path, is_default: true })
    void reload()
  }

  return (
    <section>
      <div className="flex items-center gap-3">
        <h1 className="flex-1 text-lg font-semibold">Destinos do arquivo final</h1>
        <button
          onClick={() => setForm({ id: null, label: '', path: '', is_default: !dests?.length })}
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
        >
          <Plus width={16} height={16} /> Novo
        </button>
      </div>
      <p className="mt-1 text-sm text-zinc-400">
        Pastas onde o filme finalizado é salvo. O destino padrão vem pré-selecionado ao criar um download.
      </p>

      {form && (
        <div className="mt-4 rounded-xl border border-zinc-700 bg-zinc-900 p-4">
          <h2 className="mb-3 font-semibold">{form.id == null ? 'Novo destino' : 'Editar destino'}</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Nome" value={form.label} onChange={(v) => setForm({ ...form, label: v })} placeholder="Ex.: HD Frio (filmes)" />
            <Field label="Caminho" value={form.path} onChange={(v) => setForm({ ...form, path: v })} placeholder="Ex.: /mnt/hd/filmes" mono />
          </div>
          <label className="mt-3 flex items-center gap-2 text-sm text-zinc-300">
            <input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
            Usar como padrão
          </label>
          <FormButtons saving={saving} onSave={save} onCancel={() => setForm(null)} />
        </div>
      )}

      <div className="mt-5 flex flex-col gap-2">
        {dests === null && <Empty>Carregando...</Empty>}
        {dests?.length === 0 && <Empty>Nenhum destino cadastrado. Crie o primeiro.</Empty>}
        {dests?.map((d) => (
          <div key={d.id} className="flex items-start gap-3 rounded-xl bg-zinc-900 px-4 py-3">
            <Folder width={18} height={18} className="mt-0.5 shrink-0 text-zinc-500" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{d.label}</span>
                {d.is_default && <DefaultBadge />}
              </div>
              <div className="truncate font-mono text-xs text-zinc-400">{d.path}</div>
              <div className="mt-2 max-w-md"><DiskBar disk={d.disk} /></div>
            </div>
            {!d.is_default && (
              <IconBtn title="Tornar padrão" onClick={() => makeDefault(d)}><Star width={15} height={15} /></IconBtn>
            )}
            <IconBtn title="Editar" onClick={() => setForm({ id: d.id, label: d.label, path: d.path, is_default: d.is_default })}>
              <EditPencil width={15} height={15} />
            </IconBtn>
            <IconBtn title="Remover" onClick={() => remove(d)}><Trash width={15} height={15} /></IconBtn>
          </div>
        ))}
      </div>
    </section>
  )
}

// ---------------- destinos dos torrents (qBittorrent) ----------------

interface TargetForm {
  id: number | null
  label: string
  save_path: string
  local_path: string
  is_default: boolean
}

function TorrentTargetsSection() {
  const [targets, setTargets] = useState<TorrentTarget[] | null>(null)
  const [form, setForm] = useState<TargetForm | null>(null)
  const [saving, setSaving] = useState(false)

  async function reload() {
    try {
      setTargets(await api<TorrentTarget[]>('/api/torrent-targets'))
    } catch {
      /* proximo tick */
    }
  }
  useEffect(() => void reload(), [])

  async function save() {
    if (!form) return
    if (!form.label.trim()) return alert('Preencha o nome.')
    setSaving(true)
    try {
      const body = {
        label: form.label.trim(),
        save_path: form.save_path.trim(),
        local_path: form.local_path.trim(),
        is_default: form.is_default,
      }
      if (form.id == null) await post('/api/torrent-targets', body)
      else await put(`/api/torrent-targets/${form.id}`, body)
      setForm(null)
      void reload()
    } catch (e) {
      alert(`Erro: ${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  async function remove(t: TorrentTarget) {
    if (!confirm(`Remover o destino de torrents "${t.label}"?`)) return
    try {
      await del(`/api/torrent-targets/${t.id}`)
      void reload()
    } catch (e) {
      alert(`Erro: ${(e as Error).message}`)
    }
  }

  async function makeDefault(t: TorrentTarget) {
    await put(`/api/torrent-targets/${t.id}`, {
      label: t.label, save_path: t.save_path, local_path: t.local_path, is_default: true,
    })
    void reload()
  }

  return (
    <section>
      <div className="flex items-center gap-3">
        <h1 className="flex-1 text-lg font-semibold">Destinos dos torrents (qBittorrent)</h1>
        <button
          onClick={() => setForm({ id: null, label: '', save_path: '', local_path: '', is_default: !targets?.length })}
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
        >
          <Plus width={16} height={16} /> Novo
        </button>
      </div>
      <p className="mt-1 text-sm text-zinc-400">
        Onde o qBittorrent salva os torrents e como esse caminho é visto por este serviço.
        O <b>caminho no qBittorrent</b> é onde ele grava (vazio = pasta padrão dele). O{' '}
        <b>caminho local</b> é onde a mesma pasta está montada nesta máquina (usado para achar
        o arquivo e fazer o merge). Deixe o local vazio se rodam na mesma máquina/mesmo caminho.
      </p>

      {form && (
        <div className="mt-4 rounded-xl border border-zinc-700 bg-zinc-900 p-4">
          <h2 className="mb-3 font-semibold">{form.id == null ? 'Novo destino de torrents' : 'Editar destino de torrents'}</h2>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Nome" value={form.label} onChange={(v) => setForm({ ...form, label: v })} placeholder="Ex.: SSD do seedbox" />
            <Field label="Caminho no qBittorrent" value={form.save_path} onChange={(v) => setForm({ ...form, save_path: v })} placeholder="Ex.: /downloads (vazio = padrão)" mono />
            <Field label="Caminho local (deste serviço)" value={form.local_path} onChange={(v) => setForm({ ...form, local_path: v })} placeholder="Ex.: /mnt/nas/downloads" mono />
          </div>
          <label className="mt-3 flex items-center gap-2 text-sm text-zinc-300">
            <input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
            Usar como padrão
          </label>
          <FormButtons saving={saving} onSave={save} onCancel={() => setForm(null)} />
        </div>
      )}

      <div className="mt-5 flex flex-col gap-2">
        {targets === null && <Empty>Carregando...</Empty>}
        {targets?.length === 0 && (
          <Empty>Nenhum destino de torrents. Sem isso, usa a pasta padrão do qBittorrent.</Empty>
        )}
        {targets?.map((t) => (
          <div key={t.id} className="flex items-start gap-3 rounded-xl bg-zinc-900 px-4 py-3">
            <HardDrive width={18} height={18} className="mt-0.5 shrink-0 text-zinc-500" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{t.label}</span>
                {t.is_default && <DefaultBadge />}
              </div>
              <div className="truncate font-mono text-xs text-zinc-400">
                {t.save_path || '(pasta padrão do qBittorrent)'}
                {t.local_path && <span className="text-zinc-500"> → {t.local_path}</span>}
              </div>
              {t.local_path ? (
                <div className="mt-2 max-w-md"><DiskBar disk={t.disk} /></div>
              ) : (
                <div className="mt-1 text-xs text-zinc-600">defina um caminho local para ver o uso do disco</div>
              )}
            </div>
            {!t.is_default && (
              <IconBtn title="Tornar padrão" onClick={() => makeDefault(t)}><Star width={15} height={15} /></IconBtn>
            )}
            <IconBtn title="Editar" onClick={() => setForm({ id: t.id, label: t.label, save_path: t.save_path, local_path: t.local_path, is_default: t.is_default })}>
              <EditPencil width={15} height={15} />
            </IconBtn>
            <IconBtn title="Remover" onClick={() => remove(t)}><Trash width={15} height={15} /></IconBtn>
          </div>
        ))}
      </div>
    </section>
  )
}

function DefaultBadge() {
  return (
    <span className="flex items-center gap-1 rounded-full bg-amber-950 px-2 py-0.5 text-xs font-semibold text-amber-400">
      <StarSolid width={11} height={11} /> padrão
    </span>
  )
}

function FormButtons({ saving, onSave, onCancel }: {
  saving: boolean
  onSave: () => void
  onCancel: () => void
}) {
  return (
    <div className="mt-4 flex gap-2">
      <button
        onClick={onSave}
        disabled={saving}
        className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
      >
        <Check width={16} height={16} /> Salvar
      </button>
      <button
        onClick={onCancel}
        className="flex items-center gap-1.5 rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800"
      >
        <Xmark width={16} height={16} /> Cancelar
      </button>
    </div>
  )
}
