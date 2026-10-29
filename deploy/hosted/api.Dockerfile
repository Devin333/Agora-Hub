# The hosted API: FastAPI behind the web BFF, never published to the internet.
# Build from the repository root:
#   docker build -f deploy/hosted/api.Dockerfile -t agora-api .

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/home/agora/.cache/huggingface

RUN groupadd --system --gid 10001 agora \
    && useradd --system --uid 10001 --gid agora --home-dir /home/agora --create-home --shell /usr/sbin/nologin agora

WORKDIR /app

# Dependencies first, so a source change does not reinstall them. torch comes
# from the CPU wheel index: the PyPI wheel pulls several GB of CUDA libraries
# the reranker never uses on a CPU server.
# Both indexes can point at mirrors where the public ones are blocked.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=$PIP_INDEX_URL
COPY deploy/hosted/api-constraints.txt /tmp/api-constraints.txt
RUN pip install --index-url "$TORCH_INDEX_URL" "torch==2.13.0" \
    && pip install -r /tmp/api-constraints.txt

COPY pyproject.toml README.md LICENSE ./
COPY framework ./framework
COPY backend ./backend
COPY interfaces ./interfaces
COPY infrastructure ./infrastructure
COPY configs ./configs
# The shared public paper catalog the guided search reads.
COPY frontend/data/papers/arxiv-papers.json ./frontend/data/papers/arxiv-papers.json

# Editable, so the code runs from /app where configs/ and .newsroom/ resolve;
# the constraints keep every already-installed version as tested.
RUN pip install -c /tmp/api-constraints.txt -e ".[api,rerank]" \
    && mkdir -p /app/.newsroom /home/agora/.cache/huggingface \
    && chown -R agora:agora /app/.newsroom /home/agora

USER agora

EXPOSE 8000
VOLUME ["/app/.newsroom", "/home/agora/.cache/huggingface"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status == 200 else 1)"]

CMD ["news", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
