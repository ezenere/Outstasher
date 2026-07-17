# Testes

Suíte em `pytest`. **Roda no ambiente completo da aplicação** — o mesmo onde o
servidor roda: as dependências de runtime instaladas (`numpy`, `httpx`,
`python-dotenv`, `fastapi`) **e** `ffmpeg`/`ffprobe` no `PATH`. Nada é stubado.

Se algum requisito faltar, a suíte não é silenciosamente pulada: o `conftest.py`
aborta a coleção com a mensagem do que instalar/configurar.

## Como rodar

Num ambiente com as dependências de `requirements-dev.txt` instaladas e o
`ffmpeg`/`ffprobe` no `PATH`:

```sh
pip install -r requirements-dev.txt   # (num ambiente que também tenha ffmpeg)
pytest
```

O `ffmpeg` precisa ser visível pelo mesmo interpretador que roda o `pytest`
(ele o invoca como subprocesso). Se o ffmpeg estiver em outro sistema/subsistema
que o das dependências Python, use o interpretador do lado que enxerga os dois.

## O que roda onde

- **Sem `ffmpeg` real** (lógica pura, rápido): `test_selector_year.py`,
  `test_catalog_rename.py`, `test_catalog_naming.py`, `test_library_cache.py`,
  `test_jobs_convert.py`, `test_jobs_download_only.py`, `test_jobs_manual.py`,
  `test_cli_series.py`, `test_merger_segments.py`. A maior parte de
  `test_transcode.py` também é lógica pura (planejamento), com uma exceção
  marcada `ffmpeg` (o round-trip de metadados HDR).
  (Ainda exigem o ffmpeg no PATH: o `conftest.py` valida o ambiente inteiro
  antes de qualquer teste, e `services.transcode` lê os encoders reais.)
- **Com `ffmpeg` real** (marcados `@pytest.mark.ffmpeg`, mais lentos):
  `test_convert_ffmpeg.py` (conversão), `test_align_ffmpeg.py` (alinhamento
  GCC-PHAT medindo um offset conhecido), `test_cancel_ffmpeg.py` (cancelar no
  meio de uma conversão de verdade), `test_jobs_recompress.py` (recompressão de
  um filme da coleção, com a origem gerada por ffmpeg) e o
  `test_hdr_metadata_roundtrip` de `test_transcode.py`.

Pular os lentos (ainda exige ffmpeg instalado, por causa da checagem de
ambiente e das capacidades reais):

```sh
pytest -m "not ffmpeg"
```

## Fixtures (em `conftest.py`)

- `temp_db` — `DB_DIR` isolado num tmp + `services.store` reinicializado, sem
  tocar o `jobs.db` real; zera também o estado in-memory de `jobs`/`catalog`.
- `real_encoders` — encoders reais do ffmpeg deste ambiente (sem falsear).
- `make_media` — gera `.mkv` de teste (vídeo h264 + N áudios AC3).
- `ffprobe_streams` — lê as streams de um tipo de um arquivo.

Testes assíncronos usam `asyncio.run()` diretamente (a suíte não depende de
`pytest-asyncio`).
