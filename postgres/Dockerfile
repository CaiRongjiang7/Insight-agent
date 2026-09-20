FROM public.ecr.aws/docker/library/postgres:16-bookworm

ARG PGVECTOR_VERSION=0.8.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        postgresql-server-dev-16 \
    && curl -fsSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
        | tar -xz -C /tmp \
    && make -C "/tmp/pgvector-${PGVECTOR_VERSION}" OPTFLAGS="" \
    && make -C "/tmp/pgvector-${PGVECTOR_VERSION}" install \
    && apt-get purge -y --auto-remove build-essential curl postgresql-server-dev-16 \
    && rm -rf "/tmp/pgvector-${PGVECTOR_VERSION}" /var/lib/apt/lists/*
