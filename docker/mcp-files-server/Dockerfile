FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install ".[mcp-server]"

WORKDIR /workspace/project

ENTRYPOINT ["orchestra-agent-mcp"]
CMD ["--config", "/workspace/project/orchestra-agent.toml", "--workspace", "/workspace/workspace", "--server", "files"]
