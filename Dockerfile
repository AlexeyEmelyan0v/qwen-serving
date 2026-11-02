FROM vllm/vllm-openai-cpu:v0.27.0-x86_64@sha256:ad2d073b2ff1b755e0d0b9e33a581bfcbadde22c13a073af1dfff1d553119d38

USER root
RUN groupadd --gid 10001 app \
    && useradd --create-home --uid 10001 --gid 10001 app \
    && mkdir -p /app/scripts /models \
    && chown app:app /app /app/scripts /models

ENV HOME=/home/app \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VLLM_LOGGING_CONFIG_PATH=/app/config/logging.json \
    VLLM_LOGGING_LEVEL=INFO \
    VLLM_NO_USAGE_STATS=1 \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY scripts/verify_dependencies.py /app/scripts/verify_dependencies.py
# All packages come from the immutable base. Do not re-resolve CPU wheels.
RUN python /app/scripts/verify_dependencies.py /app/requirements.txt

COPY config/ /app/config/
COPY runtime/ /app/runtime/
COPY scripts/download_model.py scripts/entrypoint.sh /app/scripts/
RUN chmod +x /app/scripts/entrypoint.sh
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["--served-model-name", "qwen", "--host", "0.0.0.0", "--port", "8000", "--dtype", "float32", "--max-model-len", "1024", "--max-num-seqs", "1", "--enforce-eager"]
