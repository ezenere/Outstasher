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

# ffmpeg + ffprobe sao exigidos pelo merger (services/merger.py)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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
