# ---- estagio 1: builda o frontend React (node so aqui, nao na imagem final) ----
FROM node:20-slim AS frontend
WORKDIR /app/frontend

# instala deps com o lockfile primeiro (cache melhor entre builds)
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-fund --no-audit

# builda
COPY frontend/ ./
RUN npm run build


# ---- estagio 2: runtime Python com ffmpeg ----
FROM python:3.12-slim AS runtime
WORKDIR /app

# ffmpeg + ffprobe sao exigidos pelo merger (services/merger.py);
# mkvtoolnix (mkvpropedit) reinjeta metadados HDR10 que encoders descartam.
#
# Encoders de HARDWARE Intel (Quick Sync / QSV — GPUs Arc, iGPUs recentes):
#   - intel-media-va-driver-non-free: driver VA-API "iHD" (OBRIGATORIO nas Arc;
#     o i965 antigo NAO suporta DG2/Arc). Sem ele, *_qsv nao abre a GPU.
#   - libmfx-gen1.2 / libvpl2: runtime oneVPL que o ffmpeg QSV usa nas Arc.
#   - vainfo: util para diagnostico (docker exec ... vainfo).
# Os pacotes non-free vem do componente non-free do Debian, habilitado abaixo.
# Sem uma GPU Intel isto so ocupa espaco — a deteccao em runtime (transcode.py)
# testa a GPU de verdade e simplesmente nao oferece o QSV se ele nao funcionar.
RUN sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg mkvtoolnix \
        intel-media-va-driver-non-free libmfx-gen1.2 libvpl2 vainfo \
    && rm -rf /var/lib/apt/lists/*
# o ffmpeg QSV procura o driver iHD por este nome (default no Debian ja e iHD,
# mas fixamos para nao depender de auto-deteccao)
ENV LIBVA_DRIVER_NAME=iHD

# deps python primeiro (camada cacheada enquanto requirements nao muda)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# codigo do backend
COPY main.py config.py merge.py ./
COPY services/ ./services/

# frontend ja buildado, vindo do estagio 1 (imagem final nao precisa de node/npm)
COPY --from=frontend /app/frontend/dist ./frontend/dist

# bind em 0.0.0.0 dentro do container; jobs.db vai para /data (volume)
ENV HOST=0.0.0.0 \
    PORT=8008 \
    DB_DIR=/data

EXPOSE 8008

CMD ["python", "main.py"]
