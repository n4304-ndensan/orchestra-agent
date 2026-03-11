FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONPATH=/workspace/project/src:/app/src
ENV WORKSPACE_ROOT=/workspace/workspace
ENV EXCEL_MCP_AUDIT_DIR=/workspace/workspace/.orchestra_state/audit
ENV EXCEL_MCP_BACKUP_DIR=/workspace/workspace/.excel_mcp_backups
ENV EXCEL_MCP_REMOTE_ENABLED=false

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install ".[mcp-server]"

WORKDIR /workspace/project

ENTRYPOINT ["python", "-m", "orchestra_agent.mcp_server"]
CMD ["--config", "/workspace/project/orchestra-agent.toml", "--workspace", "/workspace/workspace", "--server", "excel"]
