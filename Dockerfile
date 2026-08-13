FROM python:3.12-slim-bookworm

# Prevent Python from buffering stdout/stderr (useful for Docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Install system dependencies for WeasyPrint (cairo, pango) and PostgreSQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Run the application as an unprivileged user in every service container.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Install uv (fast Python package manager).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# Install Python dependencies outside /app so the development source mount does
# not hide the image's virtual environment.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-install-project

# Copy backend source
COPY backend/ .

RUN chown -R app:app /app /opt/venv

USER app

EXPOSE 8000
