FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (recommended JS runtime for yt-dlp)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV PATH="/usr/local/bin:/root/.deno/bin:${PATH}"

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m pip install --no-cache-dir -U --pre "yt-dlp[default]"

COPY . .

CMD ["python", "-m", "tunedrop"]
