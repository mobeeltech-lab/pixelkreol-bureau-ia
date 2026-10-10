FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py .
RUN useradd -u 1000 -m pk && mkdir -p /app/agents && chown -R pk /app
USER pk
EXPOSE 4001
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4001/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4001", "--proxy-headers", "--timeout-keep-alive", "75"]
