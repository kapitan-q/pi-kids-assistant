FROM python:3.12-slim

# claude-agent-sdk несёт Claude Code CLI внутри колеса (нативный бинарник _bundled/claude),
# поэтому Node и npm-пакет не нужны. Версия CLI закреплена версией SDK в requirements.txt.
RUN useradd -m app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/workdir /data/logs && chown -R app:app /app /data
USER app

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
