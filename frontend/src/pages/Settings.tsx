import { useEffect, useId, useRef, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import {
  Check, ControlSlider, EditPencil, Folder, HardDrive, Language, Lock, NavArrowDown,
  NavArrowRight, Plus, Refresh, Search, Star, StarSolid, Trash, WarningTriangle, Xmark,
} from 'iconoir-react'
import {
  type AdvancedMergeConfig, type AdvancedMergeInfo,
  api, changePassword, convertSummary, del, post, put, VARIANT_LABEL,
  type ConvertOptions, type ConvertPreset, type Destination, type ExtraSearchConfig,
  type ExtraSearchRules, type JackettIndexer, type LanguageConfig, type LanguageEntry,
  type TorrentTarget,
} from '../api'
import { DiskBar, Empty } from '../components/ui'
import { useDialog } from '../components/Dialog'
import AdvancedOptions, { CONVERT_DEFAULTS } from '../components/AdvancedOptions'
import AdvancedMergePolicy from '../components/AdvancedMergePolicy'

// abas da tela de Configurações (cada uma é uma sub-rota)
const SETTINGS_TABS: { to: string; label: string; icon: typeof Folder }[] = [
  { to: 'destinations', label: 'Destinos', icon: Folder },
  { to: 'torrents', label: 'Torrents', icon: HardDrive },
  { to: 'presets', label: 'Presets', icon: ControlSlider },
  { to: 'languages', label: 'Idiomas', icon: Language },
  { to: 'searches', label: 'Buscas extras', icon: Search },
  { to: 'merge', label: 'Merge avançado', icon: ControlSlider },
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

  // troca a lista de indexers de um (idioma, variante). Lista vazia sai do
  // objeto (o backend também a descarta) para o resumo contar só regra ativa.
  function setRule(lang: string, variant: string, ids: string[]) {
    setRules((prev) => {
      const next: ExtraSearchRules = structuredClone(prev)
      const langRules = next[lang] ?? (next[lang] = {})
      if (ids.length) langRules[variant] = ids
      else delete langRules[variant]
      if (Object.keys(langRules).length === 0) delete next[lang]
      return next
    })
  }

  // Sem lista de indexers (Jackett fora do ar, ou nenhum configurado) as
  // regras SALVAS ainda aparecem: é o único jeito de ver e tirar um indexer
  // que sumiu do Jackett. Olha cfg.rules (o que veio do banco), não o estado
  // editado — senão remover o último esconderia o botão de salvar.
  const hasSaved = Object.keys(cfg?.rules ?? {}).length > 0
  const showRules = !!cfg && ((indexers?.length ?? 0) > 0
    || ((indexers !== null || idxError !== null) && hasSaved))

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
        (ex.: buscar <i>Filme Exemplo II 1989</i> falha, mas <i>Filme Exemplo 2</i> funciona).
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

      {cfg && showRules && (
        <div className="mt-4 flex flex-col gap-4">
          {cfg.languages.map((lang) => {
            const open = openLang === lang.code
            const count = langSummary(lang.code)
            return (
              // sem overflow-hidden: a lista do "+ adicionar" sai para fora do card
              <div key={lang.code} className="rounded-xl border border-zinc-700 bg-zinc-900">
                <button
                  onClick={() => setOpenLang(open ? null : lang.code)}
                  className={`flex w-full items-center gap-2 px-4 py-3 text-left hover:bg-zinc-800/50 ${
                    open ? 'rounded-t-xl' : 'rounded-xl'
                  }`}
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
                    {cfg.variants.map((variant) => (
                      <div key={variant}>
                        <div className="mb-1.5 text-sm text-zinc-300">
                          {VARIANT_LABEL[variant] ?? variant}
                        </div>
                        <IndexerChips
                          selected={rules[lang.code]?.[variant] ?? []}
                          indexers={indexers}
                          onChange={(ids) => setRule(lang.code, variant, ids)}
                        />
                      </div>
                    ))}
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

// Indexers de uma regra de busca extra, no molde do MarkerChips: só os
// escolhidos aparecem, cada um com "×", e "+ adicionar" abre a lista dos que
// faltam, com filtro. Antes eram todos os indexers do Jackett (~30) como
// chips, os ligados em azul, vezes três regras por idioma: poluição demais
// para ver o que estava ligado.
// A ordem da lista é a ordem em que as buscas são montadas
// (services/jobs/search.py), por isso o novo entra sempre no fim.
function IndexerChips({ selected, indexers, onChange }: {
  selected: string[]
  /** null = lista do Jackett indisponível (fora do ar): os chips ficam só com o
   *  id, sem marcar como removido, e não dá para adicionar */
  indexers: JackettIndexer[] | null
  onChange: (ids: string[]) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const wrap = useRef<HTMLDivElement>(null)
  const addBtn = useRef<HTMLButtonElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLUListElement>(null)
  // fechar pelo teclado (Esc) devolve o foco ao "+ adicionar"; clicar fora, não
  const refocus = useRef(false)
  const listId = useId()

  const byId = new Map((indexers ?? []).map((i) => [i.id, i]))
  const q = query.trim().toLowerCase()
  const options = (indexers ?? []).filter(
    (idx) =>
      !selected.includes(idx.id) &&
      (!q ||
        idx.name.toLowerCase().includes(q) ||
        (idx.language ?? '').toLowerCase().includes(q) ||
        idx.id.toLowerCase().includes(q)),
  )
  const cur = Math.min(active, options.length - 1)

  // fecha ao clicar fora (mesmo padrão do ProcessMenu)
  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent) {
      if (wrap.current && !wrap.current.contains(e.target as Node)) close(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  useEffect(() => {
    if (!open && refocus.current) {
      refocus.current = false
      addBtn.current?.focus()
    }
  }, [open])

  // o destacado (setas) sempre à vista na lista com rolagem
  useEffect(() => {
    if (open && cur >= 0) list.current?.children[cur]?.scrollIntoView({ block: 'nearest' })
  }, [open, cur])

  function close(withFocus: boolean) {
    refocus.current = withFocus
    setOpen(false)
    setQuery('')
    setActive(0)
  }

  function add(id: string) {
    onChange([...selected, id])
    setQuery('')
    setActive(0)
    // continua aberto: dá para adicionar vários seguidos
    input.current?.focus()
  }

  const emptyText = !indexers?.length
    ? 'Nenhum indexer configurado no Jackett.'
    : q
      ? 'Nenhum indexer com esse filtro.'
      : 'Todos os indexers já estão nesta busca.'

  return (
    <div className="relative flex flex-wrap items-center gap-1.5 rounded-lg border border-zinc-700 bg-zinc-800 p-2">
      {selected.length === 0 && (
        <span className="px-0.5 text-xs text-zinc-600">nenhum indexer, essa busca não roda</span>
      )}
      {selected.map((id) => {
        const idx = byId.get(id)
        // na regra salva, mas o Jackett não lista mais: a busca nele vai falhar
        const missing = indexers !== null && !idx
        return (
          <span
            key={id}
            title={missing
              ? `${id}: não está mais configurado no Jackett, a busca extra nele vai falhar. Remova, ou configure de novo no Jackett.`
              : `${id}${idx?.language ? ` · ${idx.language}` : ''}`}
            className={`flex items-center gap-1 rounded-md px-2 py-0.5 text-xs ${
              missing ? 'bg-amber-950/60 text-amber-300 ring-1 ring-amber-800/70' : 'bg-zinc-700'
            }`}
          >
            {missing && <WarningTriangle width={11} height={11} className="shrink-0" />}
            {idx?.name ?? id}
            {idx?.language && <span className="text-zinc-400">{idx.language}</span>}
            {missing && <span className="text-amber-500/80">fora do Jackett</span>}
            <button
              onClick={() => onChange(selected.filter((x) => x !== id))}
              className="text-zinc-400 hover:text-red-400"
              title="Remover"
              aria-label={`Remover ${idx?.name ?? id}`}
            >
              <Xmark width={11} height={11} />
            </button>
          </span>
        )
      })}

      {/* sem position: a lista se ancora na caixa inteira (largura certa no celular) */}
      <div ref={wrap}>
        <button
          ref={addBtn}
          // sem roubar o foco do filtro: o clique só abre/fecha
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => (open ? close(true) : setOpen(true))}
          disabled={indexers === null}
          aria-haspopup="listbox"
          aria-expanded={open}
          title={indexers === null
            ? 'Lista de indexers do Jackett indisponível: tente o botão Indexers'
            : 'Adicionar indexer a esta busca'}
          className={`flex items-center gap-0.5 rounded-md px-1.5 py-0.5 text-xs transition-colors disabled:opacity-40 ${
            open ? 'bg-zinc-700 text-zinc-200' : 'text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200'
          }`}
        >
          <Plus width={12} height={12} /> adicionar
        </button>
        {open && (
          <div className="absolute top-full left-0 z-30 mt-1 w-full max-w-sm overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 shadow-xl">
            <div className="relative border-b border-zinc-800">
              <Search width={14} height={14} className="absolute top-1/2 left-3 -translate-y-1/2 text-zinc-500" />
              <input
                ref={input}
                autoFocus
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value)
                  setActive(0)
                }}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown') {
                    e.preventDefault()
                    setActive(Math.min(cur + 1, options.length - 1))
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    setActive(Math.max(cur - 1, 0))
                  } else if (e.key === 'Enter') {
                    e.preventDefault()
                    if (cur >= 0) add(options[cur].id)
                  } else if (e.key === 'Escape') {
                    e.preventDefault()
                    close(true)
                  } else if (e.key === 'Tab') {
                    close(false)
                  }
                }}
                role="combobox"
                aria-expanded
                aria-controls={listId}
                aria-activedescendant={cur >= 0 ? `${listId}-${cur}` : undefined}
                placeholder="Filtrar (nome ou idioma, ex.: pt)..."
                className="w-full bg-transparent py-2 pr-3 pl-9 text-sm outline-none"
              />
            </div>
            {options.length === 0 ? (
              <div className="px-3 py-2.5 text-xs text-zinc-500">{emptyText}</div>
            ) : (
              <ul
                ref={list}
                id={listId}
                role="listbox"
                // clicar na lista não tira o foco do filtro (Enter/setas seguem valendo)
                onMouseDown={(e) => e.preventDefault()}
                className="max-h-64 overflow-auto py-1"
              >
                {options.map((idx, i) => (
                  <li
                    key={idx.id}
                    id={`${listId}-${i}`}
                    role="option"
                    aria-selected={i === cur}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => add(idx.id)}
                    title={idx.id}
                    className={`flex cursor-pointer items-baseline gap-1.5 px-3 py-1.5 text-sm ${
                      i === cur ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-300'
                    }`}
                  >
                    <span className="truncate">{idx.name}</span>
                    {idx.language && <span className="shrink-0 text-xs text-zinc-500">{idx.language}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
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
  media: 'movie' | 'tv'
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
      const body = { label: form.label.trim(), path: form.path.trim(),
        media: form.media, is_default: form.is_default }
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
    // o padrão é POR mídia (um padrão de filmes + um de séries)
    await put(`/api/destinations/${d.id}`, {
      label: d.label, path: d.path, media: d.media ?? 'movie', is_default: true,
    })
    void reload()
  }

  return (
    <section>
      <div className="flex items-center gap-3">
        <h1 className="flex-1 text-lg font-semibold">Destinos do arquivo final</h1>
        <button
          onClick={() => setForm({ id: null, label: '', path: '', media: 'movie', is_default: !dests?.length })}
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
        >
          <Plus width={16} height={16} /> Novo
        </button>
      </div>
      <p className="mt-1 text-sm text-zinc-400">
        Pastas onde o arquivo finalizado é salvo. Filmes e séries têm bibliotecas
        SEPARADAS: cada destino pertence a uma mídia, com um padrão para cada.
      </p>

      {form && (
        <div className="mt-4 rounded-xl border border-zinc-700 bg-zinc-900 p-4">
          <h2 className="mb-3 font-semibold">{form.id == null ? 'Novo destino' : 'Editar destino'}</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Nome" value={form.label} onChange={(v) => setForm({ ...form, label: v })} placeholder="Ex.: HD Frio (filmes)" />
            <Field label="Caminho" value={form.path} onChange={(v) => setForm({ ...form, path: v })} placeholder="Ex.: /mnt/hd/filmes" mono />
          </div>
          {/* mídia: define em qual biblioteca (e em quais seletores) o destino aparece */}
          <div className="mt-3 flex items-center gap-4 text-sm text-zinc-300">
            <span className="text-zinc-400">Mídia:</span>
            <label className="flex items-center gap-1.5">
              <input type="radio" checked={form.media === 'movie'}
                onChange={() => setForm({ ...form, media: 'movie' })} />
              Filmes
            </label>
            <label className="flex items-center gap-1.5">
              <input type="radio" checked={form.media === 'tv'}
                onChange={() => setForm({ ...form, media: 'tv' })} />
              Séries
            </label>
          </div>
          <label className="mt-3 flex items-center gap-2 text-sm text-zinc-300">
            <input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
            Usar como padrão {form.media === 'tv' ? 'de séries' : 'de filmes'}
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
                <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                  (d.media ?? 'movie') === 'tv'
                    ? 'bg-sky-950 text-sky-300' : 'bg-blue-950 text-blue-300'}`}
                >
                  {(d.media ?? 'movie') === 'tv' ? 'Séries' : 'Filmes'}
                </span>
                {d.is_default && <DefaultBadge />}
              </div>
              <div className="truncate font-mono text-xs text-zinc-400">{d.path}</div>
              <div className="mt-2 max-w-md"><DiskBar disk={d.disk} /></div>
            </div>
            {!d.is_default && (
              <IconBtn title="Tornar padrão da mídia" onClick={() => makeDefault(d)}><Star width={15} height={15} /></IconBtn>
            )}
            <IconBtn title="Editar" onClick={() => setForm({ id: d.id, label: d.label, path: d.path, media: d.media ?? 'movie', is_default: d.is_default })}>
              <EditPencil width={15} height={15} />
            </IconBtn>
            <IconBtn title="Remover" onClick={() => remove(d)}><Trash width={15} height={15} /></IconBtn>
          </div>
        ))}
      </div>
    </section>
  )
}

// ---------------- presets de conversão ----------------

export function ConvertPresetsSection() {
  const dialog = useDialog()
  const [presets, setPresets] = useState<ConvertPreset[] | null>(null)
  // form aberto: null = fechado. name '' + options = novo; name preenchido = edição
  const [form, setForm] = useState<{ name: string; options: ConvertOptions | null } | null>(null)
  const [saving, setSaving] = useState(false)

  async function reload() {
    try {
      setPresets(await api<ConvertPreset[]>('/api/convert-presets'))
    } catch {
      /* proximo tick */
    }
  }
  useEffect(() => void reload(), [])

  async function save() {
    if (!form) return
    if (!form.name.trim())
      return dialog.alert({ title: 'Nome obrigatório', message: 'Dê um nome ao preset.' })
    if (!form.options)
      return dialog.alert({
        title: 'Opções desabilitadas',
        message: 'Marque "Habilitar opções avançadas" para definir o que o preset faz.',
      })
    setSaving(true)
    try {
      await post('/api/convert-presets', { name: form.name.trim(), options: form.options })
      setForm(null)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  async function remove(p: ConvertPreset) {
    if (!(await dialog.confirm({
      title: 'Remover preset',
      message: `Remover o preset "${p.name}"? Jobs já criados não são afetados.`,
      confirmText: 'Remover', tone: 'danger',
    }))) return
    try {
      await del(`/api/convert-presets/${p.id}`)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    }
  }

  return (
    <section>
      <div className="flex items-center gap-3">
        <h1 className="flex-1 text-lg font-semibold">Presets de conversão</h1>
        <button
          onClick={() => setForm({ name: '', options: CONVERT_DEFAULTS })}
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
        >
          <Plus width={16} height={16} /> Novo
        </button>
      </div>
      <p className="mt-1 text-sm text-zinc-400">
        Conjuntos de opções avançadas salvos com um nome. Nos modais de conversão
        você escolhe o preset na lista em vez de remontar tudo à mão.
      </p>

      {form && (
        <div className="mt-4 rounded-xl border border-zinc-700 bg-zinc-900 p-4">
          <h2 className="mb-3 font-semibold">
            {presets?.some((p) => p.name === form.name.trim()) ? 'Editar preset' : 'Novo preset'}
          </h2>
          <Field
            label="Nome"
            value={form.name}
            onChange={(v) => setForm({ ...form, name: v })}
            placeholder="Ex.: QSV AV1 CRF 20"
          />
          {/* reusa o mesmo bloco dos modais: um só lugar define os controles */}
          <AdvancedOptions
            value={form.options}
            onChange={(options) => setForm({ ...form, options })}
            hidePresets
            hideButtton
            hideTitle
          />
          <p className="mt-2 text-xs text-zinc-500">
            Salvar com o nome de um preset existente substitui as opções dele.
          </p>
          <FormButtons saving={saving} onSave={save} onCancel={() => setForm(null)} />
        </div>
      )}

      <div className="mt-5 flex flex-col gap-2">
        {presets === null && <Empty>Carregando...</Empty>}
        {presets?.length === 0 && <Empty>Nenhum preset salvo. Crie o primeiro.</Empty>}
        {presets?.map((p) => {
          const summary = convertSummary(p.options)
          return (
            <div key={p.id} className="flex items-start gap-3 rounded-xl bg-zinc-900 px-4 py-3">
              <ControlSlider width={18} height={18} className="mt-0.5 shrink-0 text-zinc-500" />
              <div className="min-w-0 flex-1">
                <span className="font-semibold">{p.name}</span>
                <div className="text-xs text-zinc-400">
                  {summary.length ? summary.join(' · ') : 'tudo em stream copy (nada a converter)'}
                </div>
              </div>
              <IconBtn
                title="Editar"
                onClick={() => setForm({ name: p.name, options: p.options })}
              >
                <EditPencil width={15} height={15} />
              </IconBtn>
              <IconBtn title="Remover" onClick={() => remove(p)}><Trash width={15} height={15} /></IconBtn>
            </div>
          )
        })}
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


// ---------------- merge avançado (alinhamento por conteúdo) ----------------

export function AdvancedMergeSection() {
  const [cfg, setCfg] = useState<AdvancedMergeConfig | null>(null)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    api<AdvancedMergeInfo>('/api/advanced-merge')
      .then((i) => setCfg(i.config))
      .catch((e) => setMsg({ ok: false, text: (e as Error).message }))
  }, [])

  async function save() {
    if (!cfg) return
    setSaving(true)
    setMsg(null)
    try {
      const r = await put<{ ok: boolean; config: AdvancedMergeConfig }>('/api/advanced-merge', { config: cfg })
      setCfg(r.config)
      setMsg({ ok: true, text: 'Configuração salva. Vale para os próximos merges por alinhamento.' })
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  if (!cfg) return <div className="text-sm text-zinc-500">{msg?.text ?? 'Carregando…'}</div>
  return (
    <div className="max-w-2xl">
      <h2 className="mb-1 font-semibold">Merge avançado</h2>
      <p className="mb-4 text-sm text-zinc-500">
        Padrão global para quando os dois arquivos têm montagens diferentes e o merge passa
        pelo alinhamento por conteúdo. Cada job pode trocar no modal de download (série e
        filme) ou na hora de pedir o merge avançado; decisões da revisão sempre vencem.
      </p>
      <AdvancedMergePolicy value={cfg} onChange={(v) => v && setCfg(v)} forceCustom />
      <div className="mt-4 flex items-center gap-3">
        <button onClick={save} disabled={saving}
          className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50">
          {saving ? 'Salvando…' : 'Salvar'}
        </button>
        {msg && <span className={`text-sm ${msg.ok ? 'text-emerald-400' : 'text-red-400'}`}>{msg.text}</span>}
      </div>
    </div>
  )
}
