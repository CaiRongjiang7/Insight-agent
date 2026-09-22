FROM public.ecr.aws/docker/library/python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# Use Tsinghua University mirrors for Debian packages and Python dependencies.
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

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

EXPOSE 8000

CMD ["uvicorn", "repo_maintainer.api:app", "--host", "0.0.0.0", "--port", "8000"]
