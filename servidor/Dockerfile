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

# Muchas más fuentes para el catálogo de la sección Fuentes ("como en Word"). Se instala solo lo que exista en esta
# versión de Debian (un paquete que falte no rompe el build). Las de Microsoft (Arial, Times New Roman, Comic Sans,
# Impact, Georgia, Verdana...) vienen de contrib y se bajan al instalar; si esa descarga falla, se sigue sin ellas.
ARG FUENTES_EXTRA="fonts-roboto fonts-open-sans fonts-lato fonts-croscore fonts-crosextra-carlito \
    fonts-crosextra-caladea fonts-urw-base35 fonts-texgyre fonts-cantarell fonts-firacode fonts-jetbrains-mono \
    fonts-comic-neue fonts-lobster fonts-league-spartan fonts-quicksand fonts-cabin fonts-paratype \
    fonts-freefont-ttf fonts-hack fonts-inconsolata fonts-ebgaramond fonts-linuxlibertine fonts-humor-sans \
    fonts-dkg-handwriting fonts-breip fonts-komika fonts-yanone-kaffeesatz fonts-alegreya-sans fonts-oldstandard \
    fonts-ibm-plex fonts-overpass fonts-fantasque-sans fonts-mononoki fonts-courier-prime fonts-sil-gentiumplus \
    fonts-sil-charis fonts-sil-andika fonts-montserrat fonts-oswald fonts-anton fonts-bebas-neue fonts-pacifico \
    fonts-dancing-script fonts-nunito fonts-raleway fonts-ubuntu fonts-adf-accanthis fonts-adf-gillius \
    fonts-adf-baskervald fonts-adf-berenis fonts-adf-tribun fonts-adf-verana fonts-adf-universalis"
RUN sed -i 's/^Components: main$/Components: main contrib/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        $(for p in $FUENTES_EXTRA; do apt-cache show "$p" >/dev/null 2>&1 && echo "$p"; done) \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
    && (apt-get install -y --no-install-recommends ttf-mscorefonts-installer || echo "Sin las fuentes de Microsoft") \
    && fc-cache -f >/dev/null 2>&1 || true \
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
