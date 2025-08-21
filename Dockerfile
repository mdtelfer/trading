# Usa la imagen oficial de Timescale ya preparada para PG 17
FROM timescale/timescaledb:pg17-latest

ENV TZ=UTC \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# (Opcional) Toolkit: muchas tags ya lo incluyen; si no, lo instalas
RUN apt-get update && apt-get install -y --no-install-recommends \
    timescaledb-toolkit-postgresql-17 || true \
    && rm -rf /var/lib/apt/lists/*

# (Opcional) carpeta de logs si vas a bind-mount
RUN mkdir -p /var/log/postgresql && chown -R postgres:postgres /var/log/postgresql
