# Imagem da aplicacao (web + collector: mesmo codigo, comandos diferentes).
#
# ATENCAO: o contexto de build e a RAIZ do repositorio, nao a pasta app/.
# Precisamos de db/schema.sql, que esta fora de app/, e o COPY do Docker nao
# enxerga nada acima do contexto. Ver docker-compose.yml:
#     build: { context: ., dockerfile: app/Dockerfile }
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Sao_Paulo

# postgresql-client-17 vem do repositorio oficial da PGDG, NAO do Debian.
# O Debian bookworm empacota o pg_dump 15, e o pg_dump se recusa a operar
# contra um servidor mais novo ("server version mismatch") -- o botao
# "Baixar banco" quebraria contra o PostgreSQL 16/17.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg tzdata; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# /app/db/schema.sql e onde unifi/db.py::apply_schema procura o schema.
COPY db/schema.sql /app/db/schema.sql
COPY scripts/ /app/scripts/
COPY app/ /app/

# Usuario sem privilegios. /data e o volume gravavel (backups).
RUN useradd --system --create-home --uid 10001 gestaomac \
 && mkdir -p /data/backups \
 && chown -R gestaomac:gestaomac /app /data
USER gestaomac

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status==200 else 1)"

# gunicorn, NAO iniciar.py: o watchdog de heartbeat do desktop encerra o
# processo apos 90s sem navegador, o que aqui viraria restart loop.
#
# --workers 2 --threads 4: o trabalho e I/O-bound (PostgreSQL + HTTP na UniFi).
# --timeout 120: a coleta manual (/refresh) fala com o controller e pode passar
# dos 30s padrao do gunicorn.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", \
     "--workers", "2", "--threads", "4", \
     "--timeout", "120", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "app:app"]
