FROM public.ecr.aws/docker/library/postgres:16-bookworm

# The postgres:bookworm image is Debian-based; configure its DEB822/list sources.
RUN set -eux; \
    for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        if [ -f "${source_file}" ]; then \
            sed -i \
                -e 's|http://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' \
                -e 's|https://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' \
                -e 's|http://security.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' \
                -e 's|https://security.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' \
                "${source_file}"; \
        fi; \
    done

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
