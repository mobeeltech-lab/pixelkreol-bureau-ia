FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Indian/Reunion XDG_CACHE_HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 libjpeg62-turbo libopenjp2-7 fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Polices de la charte (OFL) : Outfit et Source Sans 3, via le CDN jsDelivr (paquets Fontsource)
ADD https://cdn.jsdelivr.net/npm/@fontsource/outfit@5.3.0/files/outfit-latin-500-normal.woff2 \
    https://cdn.jsdelivr.net/npm/@fontsource/outfit@5.3.0/files/outfit-latin-700-normal.woff2 \
    https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-400-normal.woff2 \
    https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-400-italic.woff2 \
    https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-600-normal.woff2 \
    https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-700-normal.woff2 \
    ./fonts/
COPY app ./app
RUN useradd -r -u 10001 atelier && mkdir -p /data && chown atelier /data && chmod 644 fonts/*
USER atelier
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
