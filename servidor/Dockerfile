# Hookline (API + worker). La misma imagen corre las dos cosas:
#   docker run ... hookline api      |      docker run ... hookline worker
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOOKLINE_DATOS=/datos HOOKLINE_CHROME=/usr/bin/chromium

# ffmpeg (videos), fuentes para los textos de los reels (antes salían de C:\Windows\Fonts),
# chromium (buscador de memes: opcional, se apaga con HOOKLINE_BUSCADOR=0)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-inter fonts-dejavu-core fonts-liberation2 chromium tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY hookline ./hookline
RUN pip install ".[buscador,heic]"

RUN useradd --create-home --uid 1000 hookline && mkdir -p /datos && chown hookline /datos
USER hookline
VOLUME ["/datos"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/salud', timeout=4).status == 200 else 1)" || exit 1

ENTRYPOINT ["tini", "--", "hookline"]
CMD ["api"]
