# syntax = docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MW_DATA_DIR=/data

COPY --link mw_explorer ./mw_explorer
COPY --link static ./static
COPY --link station_overrides.json ./

# Cache and build output live on a mounted volume at /data, owned by this uid on the host.
RUN groupadd --system --gid 1000 app && \
    useradd app --uid 1000 --gid 1000 --create-home --shell /bin/bash && \
    mkdir -p /data && chown 1000:1000 /data
USER 1000:1000

EXPOSE 5000
CMD ["python3", "-m", "mw_explorer", "serve", "--host", "0.0.0.0"]
