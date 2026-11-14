FROM python:3.12-slim

# Agent SDK запускает Claude Code CLI. Свежие колёса claude-agent-sdk несут CLI внутри,
# Node + глобальный пакет ставим для надёжности (если CLI в колесе нет — возьмётся этот).
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/workdir /data/logs && chown -R app:app /app /data
USER app

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
