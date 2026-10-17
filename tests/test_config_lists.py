"""Listas da configuração em tabelas (language_markers, subtitle_markers,
search_rules) em vez de JSON em languages.markers_* e settings — e a etapa do
merge que sobra no banco de um job que parou no meio dele.

Cobre: gravação/leitura dos marcadores na ordem, banco novo semeado com os
padrões, migração do formato antigo (função pura e banco antigo, idempotente:
init() duas vezes não duplica), as rotas devolvendo o mesmo formato, e o
rótulo da etapa de um job em erro relido do banco.
"""
import importlib
import json
import sqlite3

import config
from services import jobs, store
from services.jobs import runtime


def _rows(sql: str, args=()) -> list[tuple]:
    with store._lock:
        return store._conn.execute(sql, args).fetchall()


def _langs(**over) -> list[dict]:
    base = [
        {"code": "pt", "label": "Português", "tmdb": "pt-BR",
         # fora de ordem alfabética de propósito: a ordem é a da tela
         "markers_strong": ["nacional", "dublado", "áudio dual"],
         "markers_weak": ["dual"]},
        {"code": "es", "label": "Espanhol", "tmdb": "es-ES",
         "markers_strong": ["latino", "castellano"],
         "markers_weak": ["multi", "dual"]},
    ]
    for lg in base:
        lg.update(over.get(lg["code"], {}))
    return base


# -------------------- marcadores --------------------

def test_marcadores_gravados_e_lidos_na_ordem(temp_db):
    store.save_languages(_langs(), ["vose", "legendado", "subs"])
    got = store.list_languages()
    assert [lg["code"] for lg in got] == ["pt", "es"]
    assert got[0]["markers_strong"] == ["nacional", "dublado", "áudio dual"]
    assert got[1]["markers_weak"] == ["multi", "dual"]
    assert got[1]["sort_order"] == 1
    assert store.get_subtitle_markers() == ["vose", "legendado", "subs"]
    # o runtime (seletor) enxerga o que foi salvo
    assert config.LANGUAGES["es"]["markers_strong"] == ["latino", "castellano"]
    assert config.SUBTITLE_MARKERS == ["vose", "legendado", "subs"]
    # linhas, não JSON: as colunas antigas ficam sem uso
    assert _rows("SELECT strength, pos, marker FROM language_markers "
                 "WHERE language = 'pt' ORDER BY strength, pos") == [
        ("strong", 0, "nacional"), ("strong", 1, "dublado"),
        ("strong", 2, "áudio dual"), ("weak", 0, "dual")]
    assert set(_rows("SELECT markers_strong, markers_weak FROM languages")) \
        == {("[]", "[]")}


def test_salvar_de_novo_substitui_sem_sobra(temp_db):
    store.save_languages(_langs(), ["leg"])
    store.save_languages(
        [{"code": "it", "label": "Italiano", "tmdb": "it-IT",
          "markers_strong": ["ita"], "markers_weak": []}], [])
    assert [lg["code"] for lg in store.list_languages()] == ["it"]
    assert _rows("SELECT language, marker FROM language_markers") == [("it", "ita")]
    # lista de legenda vazia é escolha do usuário: não volta aos padrões
    assert store.get_subtitle_markers() == []
    store.init()        # (já aberto: não faz nada)
    assert store.get_subtitle_markers() == []


def test_banco_novo_semeado_com_os_padroes(temp_db):
    got = {lg["code"]: lg for lg in store.list_languages()}
    assert list(got) == list(config._DEFAULT_LANGUAGES)
    for code, info in config._DEFAULT_LANGUAGES.items():
        assert got[code]["markers_strong"] == info["markers_strong"]
        assert got[code]["markers_weak"] == info["markers_weak"]
    assert store.get_subtitle_markers() == config._DEFAULT_SUBTITLE_MARKERS
    assert store.get_extra_search_rules() == {}


# -------------------- regras de busca extra --------------------

def test_regras_de_busca_round_trip_na_ordem(temp_db):
    rules = {"pt": {"roman": ["idx-b", "idx-a"], "no_year": ["idx-c"]},
             "es": {"no_year": ["idx-a"]}}
    store.set_extra_search_rules(rules)
    got = store.get_extra_search_rules()
    assert got == rules
    assert list(got) == ["pt", "es"] and list(got["pt"]) == ["roman", "no_year"]
    assert _rows("SELECT language, rule, tracker FROM search_rules ORDER BY pos")[:2] \
        == [("pt", "roman", "idx-b"), ("pt", "roman", "idx-a")]
    # as regras não dependem da tabela de idiomas (que é regravada inteira)
    store.save_languages(_langs(), [])
    assert store.get_extra_search_rules() == rules
    store.set_extra_search_rules({})
    assert store.get_extra_search_rules() == {}


# -------------------- formato antigo --------------------

def test_normalizacao_legada_funcoes_puras():
    assert store._legacy_markers('["b", "a"]') == ["b", "a"]
    assert store._legacy_markers("") == [] and store._legacy_markers("{x") == []
    assert store._legacy_markers('{"a": 1}') == []
    assert store._legacy_search_rules(None) == []
    assert store._legacy_search_rules("não é json") == []
    raw = json.dumps({"pt": {"no_year": ["i2", "i1"], "roman": "x"}, "es": 3})
    assert store._legacy_search_rules(raw) == [("pt", "no_year", "i2"),
                                               ("pt", "no_year", "i1")]


def _banco_antigo(tmp_path, monkeypatch, subtitle="__padrao__"):
    """jobs.db de antes das tabelas: marcadores em JSON nas colunas de
    languages e listas em settings."""
    con = sqlite3.connect(tmp_path / "jobs.db")
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("""CREATE TABLE languages (
        code TEXT PRIMARY KEY, label TEXT NOT NULL, tmdb TEXT NOT NULL,
        markers_strong TEXT NOT NULL DEFAULT '[]',
        markers_weak TEXT NOT NULL DEFAULT '[]',
        sort_order INTEGER NOT NULL DEFAULT 0)""")
    con.execute("INSERT INTO languages VALUES ('pt', 'Português', 'pt-BR', ?, ?, 0)",
                (json.dumps(["nacional", "dublado"]), json.dumps(["dual"])))
    con.execute("INSERT INTO languages VALUES ('fr', 'Francês', 'fr-FR', ?, '[]', 1)",
                (json.dumps(["vff", "french"]),))
    if subtitle != "__padrao__":
        con.execute("INSERT INTO settings VALUES ('subtitle_markers', ?)", (subtitle,))
    con.execute("INSERT INTO settings VALUES ('extra_search_rules', ?)",
                (json.dumps({"fr": {"roman_no_year": ["i9", "i3"]}}),))
    con.commit()
    con.close()
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    importlib.reload(config)
    importlib.reload(store)


def _reabrir():
    store._conn.close()
    store._conn = None
    store.init()


def test_banco_antigo_migra_uma_vez(tmp_path, monkeypatch):
    _banco_antigo(tmp_path, monkeypatch,
                  subtitle=json.dumps(["vostfr", "legendado"]))
    store.init()
    esperado = {
        "pt": (["nacional", "dublado"], ["dual"]),
        "fr": (["vff", "french"], []),
    }

    def confere():
        got = {lg["code"]: (lg["markers_strong"], lg["markers_weak"])
               for lg in store.list_languages()}
        assert got == esperado
        assert store.get_subtitle_markers() == ["vostfr", "legendado"]
        assert store.get_extra_search_rules() == {"fr": {"roman_no_year": ["i9", "i3"]}}
        assert config.LANGUAGES["fr"]["markers_strong"] == ["vff", "french"]
        assert _rows("SELECT COUNT(*) FROM language_markers") == [(5,)]
        assert _rows("SELECT COUNT(*) FROM subtitle_markers") == [(2,)]
        assert _rows("SELECT COUNT(*) FROM search_rules") == [(2,)]

    confere()
    _reabrir()                  # init() de novo: não duplica
    confere()
    # depois de migrado o JSON antigo não é mais lido: editar pela tela vale
    # mesmo com o JSON velho ainda no banco
    store.save_languages([{"code": "pt", "label": "Português", "tmdb": "pt-BR",
                           "markers_strong": ["dublado"], "markers_weak": []}],
                         ["leg"])
    store.set_extra_search_rules({})
    _reabrir()
    assert [(lg["code"], lg["markers_strong"]) for lg in store.list_languages()] \
        == [("pt", ["dublado"])]
    assert store.get_subtitle_markers() == ["leg"]
    assert store.get_extra_search_rules() == {}


def test_banco_antigo_sem_chave_de_legenda_ganha_os_padroes(tmp_path, monkeypatch):
    _banco_antigo(tmp_path, monkeypatch)
    store.init()
    assert store.get_subtitle_markers() == config._DEFAULT_SUBTITLE_MARKERS
    # os idiomas existentes não são sobrescritos pelos padrões
    assert [lg["code"] for lg in store.list_languages()] == ["pt", "fr"]


# -------------------- API: mesmo formato --------------------

def _client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main.auth, "is_password_set", lambda: True)
    monkeypatch.setattr(main.auth, "validate_token", lambda tok: True)
    return TestClient(main.app)


def test_api_de_idiomas_e_regras_no_mesmo_formato(temp_db, monkeypatch):
    c = _client(monkeypatch)
    body = {"languages": _langs(), "subtitle_markers": ["Vose ", "legendado", "vose"]}
    r = c.put("/api/language-config", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["subtitle_markers"] == ["vose", "legendado"]   # normalizado
    assert out["languages"][0] == {
        "code": "pt", "label": "Português", "tmdb": "pt-BR",
        "markers_strong": ["nacional", "dublado", "áudio dual"],
        "markers_weak": ["dual"], "sort_order": 0}
    assert c.get("/api/language-config").json() == {
        "languages": out["languages"], "subtitle_markers": ["vose", "legendado"]}

    rules = {"pt": {"roman": ["idx-b", "idx-a"]}, "es": {"no_year": ["idx-a"]},
             "xx": {"roman": ["idx-a"]}}           # idioma desconhecido: sai
    r = c.put("/api/extra-search-rules", json={"rules": rules})
    assert r.json()["rules"] == {"pt": {"roman": ["idx-b", "idx-a"]},
                                 "es": {"no_year": ["idx-a"]}}
    got = c.get("/api/extra-search-rules").json()
    assert got["rules"] == r.json()["rules"]
    assert got["variants"] == list(store.EXTRA_SEARCH_VARIANTS)


# -------------------- etapa do merge de um job em erro --------------------

def _filme(jid: str, **kw) -> dict:
    job = {"id": jid, "tmdb_id": 900077, "language": "pt", "mode": "auto",
           "kind": "both", "skip_search": False, "advanced_merge": None,
           "download_only": False, "convert": None, "status": "merging",
           "detail": "", "movie": None,
           "progress": {"video": None, "audio": None},
           "output": None, "created_at": "2026-01-01T00:00:00",
           "search_queries": [], "search_candidates": [],
           "merge_started_at": "2026-01-01T01:00:00"}
    job.update(kw)
    return job


def test_job_que_falhou_no_alinhamento_relido_do_banco(temp_db):
    """O detalhe rotula a seção da conversão pela etapa (progress.merge.phase
    + qual arquivo o fingerprint lia). Um job que falhou buscando
    alinhamento dizia "Buscando alinhamento (Áudio)"; sem a barra no banco,
    passou a dizer "Conversão". A etapa volta; a barra não."""
    job = _filme("ea1")
    jobs._jobs[job["id"]] = job
    runtime._persist(job)
    _log, on_progress = runtime._ffmpeg_hooks(job, runtime.PHASE_ALIGN)
    on_progress({"pct": 37.0, "out_s": 10.0, "duration_s": 30.0, "size": 0,
                 "bitrate": 0, "speed": 2.0, "fps": 0, "eta": 5,
                 "step": 1, "label": "dublado"})
    runtime._set(job, "error", "Erro: fingerprint falhou")
    assert job["id"] not in jobs._jobs            # terminal: só no banco
    back = jobs.get_job(job["id"])
    assert back["progress"]["merge"] == {"phase": "align", "step": 1,
                                         "label": "dublado"}
    assert _rows("SELECT merge_phase, merge_step, merge_label FROM jobs "
                 "WHERE id = 'ea1'") == [("align", 1, "dublado")]


def test_etapa_so_volta_de_job_parado_no_meio(temp_db):
    # ativo (depois de um restart o merge recomeça): a etapa gravada é velha
    ativo = _filme("em1", progress={"video": None, "audio": None,
                                    "merge": {"pct": 5.0, "phase": "edl"}})
    store.upsert_job(ativo)
    assert "merge" not in store.get_job("em1")["progress"]
    # concluído zera a barra: nada a mostrar
    store.upsert_job(_filme("ed1", status="done"))
    assert "merge" not in store.get_job("ed1")["progress"]
    # cancelado no meio da conversão: a etapa volta (merge de série também)
    serie = _filme("ec1", status="cancelled", media_type="tv",
                   progress={"merge": {"pct": 80.0, "phase": "convert"}})
    store.upsert_job(serie)
    assert store.get_job("ec1")["progress"] == {"merge": {"phase": "convert"}}
    # etapa fora da forma: só não volta (a barra é efêmera)
    store.upsert_job(_filme("ex1", status="error",
                            progress={"video": None, "audio": None,
                                      "merge": {"phase": 3, "step": "um"}}))
    assert "merge" not in store.get_job("ex1")["progress"]


def test_documento_antigo_com_a_barra_guarda_a_etapa():
    doc = _filme("ol1", status="error",
                 progress={"video": None, "audio": None,
                           "merge": {"pct": 12.0, "phase": "align", "step": 2,
                                     "label": "original"}})
    out = store._legacy_scalars(doc)
    assert out["progress"]["merge"] == {"phase": "align", "step": 2,
                                        "label": "original"}
    out = store._legacy_scalars(dict(doc, status="done"))
    assert "merge" not in out["progress"]
