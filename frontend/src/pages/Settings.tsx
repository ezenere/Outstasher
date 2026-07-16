import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import {
  Check, EditPencil, Folder, HardDrive, Language, Lock, NavArrowDown, NavArrowRight,
  Plus, Refresh, Search, Star, StarSolid, Trash, Xmark,
} from 'iconoir-react'
import {
  api, changePassword, del, post, put, VARIANT_LABEL,
  type Destination, type ExtraSearchConfig, type ExtraSearchRules,
  type JackettIndexer, type LanguageConfig, type LanguageEntry, type TorrentTarget,
} from '../api'
import { DiskBar, Empty } from '../components/ui'
import { useDialog } from '../components/Dialog'

// abas da tela de Configurações (cada uma é uma sub-rota)
const SETTINGS_TABS: { to: string; label: string; icon: typeof Folder }[] = [
  { to: 'destinations', label: 'Destinos', icon: Folder },
  { to: 'torrents', label: 'Torrents', icon: HardDrive },
  { to: 'languages', label: 'Idiomas', icon: Language },
  { to: 'searches', label: 'Buscas extras', icon: Search },
  { to: 'security', label: 'Senha', icon: Lock },
]

export default function Settings() {
  return (
    <div className="flex flex-col gap-6 sm:flex-row sm:gap-8">
      <nav className="flex shrink-0 gap-1 overflow-x-auto sm:w-48 sm:flex-col">
        {SETTINGS_TABS.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            className={({ isActive }) =>
              `flex items-center gap-2 rounded-lg px-3 py-2 text-sm whitespace-nowrap transition-colors ${
                isActive ? 'bg-zinc-800 font-semibold text-zinc-100' : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200'
              }`
            }
          >
            <t.icon width={16} height={16} /> {t.label}
          </NavLink>
        ))}
      </nav>
      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  )
}

// ---------------- buscas extras (idioma x variante x indexers) ----------------

export function ExtraSearchSection() {
  const [cfg, setCfg] = useState<ExtraSearchConfig | null>(null)
  const [rules, setRules] = useState<ExtraSearchRules>({})
  const [indexers, setIndexers] = useState<JackettIndexer[] | null>(null)
  const [idxError, setIdxError] = useState<string | null>(null)
  const [loadingIdx, setLoadingIdx] = useState(false)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [filter, setFilter] = useState('')
  // idioma aberto no accordion (só um por vez); null = todos fechados
  const [openLang, setOpenLang] = useState<string | null>(null)

  async function loadCfg() {
    try {
      const c = await api<ExtraSearchConfig>('/api/extra-search-rules')
      setCfg(c)
      setRules(c.rules ?? {})
      // abre por padrão o primeiro idioma que já tem regras (se houver)
      setOpenLang((cur) => cur ?? Object.keys(c.rules ?? {})[0] ?? null)
    } catch { /* proximo tick */ }
  }

  async function loadIndexers() {
    setLoadingIdx(true)
    setIdxError(null)
    try {
      setIndexers(await api<JackettIndexer[]>('/api/jackett/indexers'))
    } catch (e) {
      setIdxError((e as Error).message)
    } finally {
      setLoadingIdx(false)
    }
  }

  useEffect(() => {
    void loadCfg()
    void loadIndexers()
  }, [])

  // liga/desliga um indexer para um (idioma, variante)
  function toggle(lang: string, variant: string, id: string) {
    setRules((prev) => {
      const next: ExtraSearchRules = structuredClone(prev)
      const langRules = next[lang] ?? (next[lang] = {})
      const list = langRules[variant] ?? []
      langRules[variant] = list.includes(id) ? list.filter((x) => x !== id) : [...list, id]
      if (langRules[variant].length === 0) delete langRules[variant]
      if (Object.keys(langRules).length === 0) delete next[lang]
      return next
    })
  }

  const isOn = (lang: string, variant: string, id: string) =>
    (rules[lang]?.[variant] ?? []).includes(id)

  // nº de variantes com pelo menos um indexer marcado, para o resumo do idioma fechado
  const langSummary = (lang: string) => {
    const v = rules[lang] ?? {}
    return Object.values(v).filter((l) => l.length > 0).length
  }

  async function save() {
    setSaving(true)
    setMsg(null)
    try {
      await put('/api/extra-search-rules', { rules })
      setMsg({ ok: true, text: 'Regras salvas.' })
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <section>
      <div className="flex items-center gap-2">
        <Search width={18} height={18} className="text-zinc-500" />
        <h1 className="flex-1 text-lg font-semibold">Buscas extras no Jackett</h1>
        <button
          onClick={loadIndexers}
          disabled={loadingIdx}
          title="Recarregar a lista de indexers do Jackett"
          className="flex items-center gap-1.5 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
        >
          <Refresh width={15} height={15} /> Indexers
        </button>
      </div>
      <p className="mt-1 text-sm text-zinc-400">
        Alguns trackers BR não retornam nada para o título com numeral romano ou com o ano
        (ex.: buscar <i>De Volta para o Futuro II 1989</i> falha, mas <i>De Volta para o Futuro 2</i> funciona).
        Aqui você diz, por idioma e variante do título, em quais indexers rodar uma busca extra.
        Cada variante só dispara quando faz sentido para o filme (só tem efeito se o título tiver
        numeral romano, ou se tiver ano a remover). As buscas extras rodam em paralelo e só afetam
        a faixa dublada.
      </p>

      {idxError && (
        <div className="mt-4 rounded-xl border border-amber-900/60 bg-amber-950/20 p-3 text-sm text-amber-300">
          Não consegui listar os indexers do Jackett: {idxError}
        </div>
      )}
      {!idxError && indexers?.length === 0 && (
        <Empty>Nenhum indexer configurado no Jackett.</Empty>
      )}

      {cfg && indexers && indexers.length > 0 && (
        <div className="mt-4 flex flex-col gap-4">
          <div className="relative max-w-xs">
            <Search width={14} height={14} className="absolute top-1/2 left-3 -translate-y-1/2 text-zinc-500" />
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filtrar indexers (nome ou idioma, ex.: pt)..."
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 py-1.5 pr-8 pl-9 text-sm outline-none focus:border-blue-500"
            />
            {filter && (
              <button
                onClick={() => setFilter('')}
                className="absolute top-1/2 right-2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
                title="Limpar"
              >
                <Xmark width={14} height={14} />
              </button>
            )}
          </div>
          {cfg.languages.map((lang) => {
            const open = openLang === lang.code
            const count = langSummary(lang.code)
            return (
              <div key={lang.code} className="overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900">
                <button
                  onClick={() => setOpenLang(open ? null : lang.code)}
                  className="flex w-full items-center gap-2 px-4 py-3 text-left hover:bg-zinc-800/50"
                >
                  {open ? <NavArrowDown width={16} height={16} /> : <NavArrowRight width={16} height={16} />}
                  <span className="flex-1 font-semibold">{lang.label}</span>
                  {count > 0 && (
                    <span className="rounded-full bg-blue-950 px-2 py-0.5 text-xs font-semibold text-blue-300">
                      {count} variante{count === 1 ? '' : 's'} ativa{count === 1 ? '' : 's'}
                    </span>
                  )}
                </button>
                {open && (
                  <div className="flex flex-col gap-4 border-t border-zinc-800 p-4">
                    {cfg.variants.map((variant) => {
                      // mostra os que batem no filtro + os já selecionados (para não sumirem)
                      const q = filter.trim().toLowerCase()
                      const visible = indexers.filter(
                        (idx) =>
                          isOn(lang.code, variant, idx.id) ||
                          !q ||
                          idx.name.toLowerCase().includes(q) ||
                          (idx.language ?? '').toLowerCase().includes(q) ||
                          idx.id.toLowerCase().includes(q),
                      )
                      return (
                        <div key={variant}>
                          <div className="mb-1.5 text-sm text-zinc-300">
                            {VARIANT_LABEL[variant] ?? variant}
                          </div>
                          <div className="flex flex-wrap gap-1.5">
                            {visible.length === 0 && (
                              <span className="text-xs text-zinc-600">nenhum indexer com esse filtro</span>
                            )}
                            {visible.map((idx) => {
                              const on = isOn(lang.code, variant, idx.id)
                              return (
                                <button
                                  key={idx.id}
                                  onClick={() => toggle(lang.code, variant, idx.id)}
                                  className={`rounded-lg border px-2.5 py-1 text-xs transition-colors ${
                                    on
                                      ? 'border-blue-500 bg-blue-600 font-semibold text-white'
                                      : 'border-zinc-700 bg-zinc-800 text-zinc-300 hover:bg-zinc-700'
                                  }`}
                                  title={`${idx.id}${idx.language ? ` · ${idx.language}` : ''}`}
                                >
                                  {on && <Check width={11} height={11} className="mr-1 inline" />}
                                  {idx.name}
                                  {idx.language && (
                                    <span className={`ml-1 ${on ? 'text-blue-200' : 'text-zinc-500'}`}>
                                      {idx.language}
                                    </span>
                                  )}
                                </button>
                              )
                            })}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}

          {msg && (
            <div className={`text-sm ${msg.ok ? 'text-emerald-400' : 'text-red-400'}`}>{msg.text}</div>
          )}
          <div>
            <button
              onClick={save}
              disabled={saving}
              className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
            >
              <Check width={16} height={16} /> Salvar regras
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

// ---------------- cadastro de idiomas (dublagem) ----------------

// editor de lista de marcadores como "chips": digita e Enter/vírgula adiciona
function MarkerChips({ label, hint, markers, onChange }: {
  label: string
  hint?: string
  markers: string[]
  onChange: (m: string[]) => void
}) {
  const [draft, setDraft] = useState('')
  function add(raw: string) {
    const parts = raw.split(',').map((p) => p.trim().toLowerCase()).filter(Boolean)
    if (!parts.length) return
    const merged = [...markers]
    for (const p of parts) if (!merged.includes(p)) merged.push(p)
    onChange(merged)
    setDraft('')
  }
  return (
    <div>
      <div className="mb-1 text-sm text-zinc-400">{label}</div>
      {hint && <div className="mb-1.5 text-xs text-zinc-600">{hint}</div>}
      <div className="flex flex-wrap gap-1.5 rounded-lg border border-zinc-700 bg-zinc-800 p-2">
        {markers.map((m) => (
          <span key={m} className="flex items-center gap-1 rounded-md bg-zinc-700 px-2 py-0.5 text-xs">
            {m}
            <button
              onClick={() => onChange(markers.filter((x) => x !== m))}
              className="text-zinc-400 hover:text-red-400"
              title="Remover"
            >
              <Xmark width={11} height={11} />
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',') {
              e.preventDefault()
              add(draft)
            } else if (e.key === 'Backspace' && !draft && markers.length) {
              onChange(markers.slice(0, -1))
            }
          }}
          onBlur={() => add(draft)}
          placeholder={markers.length ? '+ marcador' : 'digite e Enter...'}
          className="min-w-24 flex-1 bg-transparent text-xs outline-none"
        />
      </div>
    </div>
  )
}

export function LanguagesSection() {
  const dialog = useDialog()
  const [langs, setLangs] = useState<LanguageEntry[] | null>(null)
  const [subs, setSubs] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  async function load() {
    try {
      const c = await api<LanguageConfig>('/api/language-config')
      setLangs(c.languages)
      setSubs(c.subtitle_markers)
    } catch { /* proximo tick */ }
  }
  useEffect(() => void load(), [])

  function patch(i: number, field: keyof LanguageEntry, value: string | string[]) {
    setLangs((prev) => prev && prev.map((l, j) => (j === i ? { ...l, [field]: value } : l)))
  }
  function addLang() {
    setLangs((prev) => [...(prev ?? []), {
      code: '', label: '', tmdb: '', markers_strong: [], markers_weak: [],
    }])
  }
  async function removeLang(i: number) {
    if (!(await dialog.confirm({
      title: 'Remover idioma', message: 'Remover este idioma?',
      confirmText: 'Remover', tone: 'danger',
    }))) return
    setLangs((prev) => prev && prev.filter((_, j) => j !== i))
  }

  async function save() {
    if (!langs) return
    setSaving(true)
    setMsg(null)
    try {
      const c = await put<LanguageConfig>('/api/language-config', {
        languages: langs, subtitle_markers: subs,
      })
      setLangs(c.languages)
      setSubs(c.subtitle_markers)
      setMsg({ ok: true, text: 'Idiomas salvos.' })
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <section>
      <div className="flex items-center gap-2">
        <Language width={18} height={18} className="text-zinc-500" />
        <h1 className="flex-1 text-lg font-semibold">Idiomas da dublagem</h1>
        <button
          onClick={addLang}
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
        >
          <Plus width={16} height={16} /> Idioma
        </button>
      </div>
      <p className="mt-1 text-sm text-zinc-400">
        Idiomas que aparecem ao escolher a faixa dublada. <b>Código TMDB</b> busca o título traduzido
        (ex.: pt-BR). <b>Marcadores fortes</b> confirmam dublagem no idioma e ganham bônus de score
        (dublado, bludv...). <b>Marcadores fracos</b> são ambíguos (dual, multi) e só contam por falta
        de opção. Comparados em minúsculas — mantenha acentos onde importam (<i>dual áudio</i> ≠ <i>dual audio</i>).
      </p>

      {langs === null && <Empty>Carregando...</Empty>}

      <div className="mt-4 flex flex-col gap-3">
        {langs?.map((l, i) => (
          <div key={i} className="rounded-xl border border-zinc-700 bg-zinc-900 p-4">
            <div className="flex items-start gap-3">
              <div className="grid flex-1 gap-3 sm:grid-cols-3">
                <Field label="Código" value={l.code} onChange={(v) => patch(i, 'code', v)} placeholder="pt" mono />
                <Field label="Nome" value={l.label} onChange={(v) => patch(i, 'label', v)} placeholder="Português" />
                <Field label="Código TMDB" value={l.tmdb} onChange={(v) => patch(i, 'tmdb', v)} placeholder="pt-BR" mono />
              </div>
              <IconBtn title="Remover idioma" onClick={() => removeLang(i)}><Trash width={15} height={15} /></IconBtn>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <MarkerChips
                label="Marcadores fortes"
                hint="confirmam dublagem — ganham bônus"
                markers={l.markers_strong}
                onChange={(m) => patch(i, 'markers_strong', m)}
              />
              <MarkerChips
                label="Marcadores fracos"
                hint="ambíguos (dual/multi) — sem bônus"
                markers={l.markers_weak}
                onChange={(m) => patch(i, 'markers_weak', m)}
              />
            </div>
          </div>
        ))}
      </div>

      {langs && (
        <div className="mt-6 rounded-xl border border-zinc-700 bg-zinc-900 p-4">
          <MarkerChips
            label="Marcadores de legenda (universais)"
            hint="se o título tem um destes e NENHUM marcador de dublagem, é só legendado (áudio original) e é descartado"
            markers={subs}
            onChange={setSubs}
          />
        </div>
      )}

      {msg && (
        <div className={`mt-4 text-sm ${msg.ok ? 'text-emerald-400' : 'text-red-400'}`}>{msg.text}</div>
      )}
      {langs && (
        <div className="mt-4">
          <button
            onClick={save}
            disabled={saving}
            className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            <Check width={16} height={16} /> Salvar idiomas
          </button>
        </div>
      )}
    </section>
  )
}

// ---------------- senha ----------------

export function SecuritySection() {
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

export function DestinationsSection() {
  const dialog = useDialog()
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
    if (!form.label.trim() || !form.path.trim())
      return dialog.alert({ title: 'Campos obrigatórios', message: 'Preencha nome e caminho.' })
    setSaving(true)
    try {
      const body = { label: form.label.trim(), path: form.path.trim(), is_default: form.is_default }
      if (form.id == null) await post('/api/destinations', body)
      else await put(`/api/destinations/${form.id}`, body)
      setForm(null)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  async function remove(d: Destination) {
    if (!(await dialog.confirm({
      title: 'Remover destino',
      message: `Remover o destino "${d.label}"? Jobs já criados mantêm o caminho que usaram.`,
      confirmText: 'Remover', tone: 'danger',
    }))) return
    try {
      await del(`/api/destinations/${d.id}`)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
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

export function TorrentTargetsSection() {
  const dialog = useDialog()
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
    if (!form.label.trim())
      return dialog.alert({ title: 'Campo obrigatório', message: 'Preencha o nome.' })
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
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  async function remove(t: TorrentTarget) {
    if (!(await dialog.confirm({
      title: 'Remover destino de torrents',
      message: `Remover o destino de torrents "${t.label}"?`,
      confirmText: 'Remover', tone: 'danger',
    }))) return
    try {
      await del(`/api/torrent-targets/${t.id}`)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
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
