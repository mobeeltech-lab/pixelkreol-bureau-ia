# Standard téléphonique IA d'ABI Holding — service SÉPARÉ de l'orchestrateur (voir STANDARD-LISEZMOI.md, « Pourquoi un service à part »).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Paris \
    HF_HUB_DISABLE_TELEMETRY=1 OMP_NUM_THREADS=2
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY abi_standard ./abi_standard
RUN useradd -u 1000 -m pk && mkdir -p /app/data /models && chown -R pk /app /models
USER pk
CMD ["python", "-m", "abi_standard", "servir"]
