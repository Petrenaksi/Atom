# Atom trader — paper-trading engine and dashboard image.
# No secrets are baked in: configuration comes from the environment at runtime.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 atom
WORKDIR /app

COPY pyproject.toml README.md ./
COPY atomtrader ./atomtrader
RUN pip install ".[postgres,dashboard]" && rm -rf /root/.cache

USER atom
# Default: the engine (paper mode only). The dashboard service overrides the command.
CMD ["python", "-m", "atomtrader.engine.main"]
