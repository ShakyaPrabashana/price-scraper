# Supplement Store Scraper: FastAPI app + CLI, talking to SQL Server via pyodbc.
#
#   docker build -t price-scraper .
#   docker run --rm -p 5000:5000 --env-file .env price-scraper
#   docker run --rm --env-file .env price-scraper scrape --category Protein
#
# Windows auth (DB_TRUSTED_CONNECTION=yes) does not work from a Linux container;
# use a SQL login. For a SQL Server running on the Docker host, set
# DB_SERVER=host.docker.internal (plus ,port if it is not 1433).

FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Microsoft ODBC Driver 18 for SQL Server (the DB_DRIVER in .env.example).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL -o /tmp/packages-microsoft-prod.deb \
        https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY main.py .

RUN useradd --create-home --uid 1000 appuser
USER appuser

# Listen on all interfaces inside the container; the app's default is 127.0.0.1.
ENV APP_HOST=0.0.0.0 \
    APP_PORT=5000 \
    DB_DRIVER="ODBC Driver 18 for SQL Server"

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${APP_PORT}/health', timeout=3)" || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["serve"]
