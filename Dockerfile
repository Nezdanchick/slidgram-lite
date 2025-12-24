ARG PYTHONVERSION=3.13
ARG DEBIANVERSION=trixie

# Builder
# *******
FROM ghcr.io/astral-sh/uv:python$PYTHONVERSION-$DEBIANVERSION-slim AS builder

# install git for setuptools scm
RUN apt-get update -y && \
    apt-get install \
        git \
        make \
        libglib2.0-dev \
        gcc \
        -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /build
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV PATH="/venv/bin:/root/.cargo/bin:$PATH"
RUN uv venv $UV_PROJECT_ENVIRONMENT
COPY pyproject.toml uv.lock  README.md .
COPY slidgram slidgram
ARG SLIDGE_USE_LOCKFILE=
RUN [ -z "$SLIDGE_USE_LOCKFILE" ] && rm uv.lock || true
# install dependencies in /venv
# .git/ needs to be mounted for setuptools-scm to set the version
RUN --mount=source=.git,target=/build/.git,type=bind \
    uv sync --no-dev
ARG SLIDGE_PRERELEASE
RUN [ ! -z "$SLIDGE_PRERELEASE" ] && uv pip install slidge --upgrade --prerelease allow || true

# CI container
# ************
FROM builder AS ci

# We won't use this venv in CI, but this populates the uv cache in the container,
# minimizing (or even nulling) downloads.
RUN --mount=source=.git,target=/build/.git,type=bind \
    uv sync --all-groups --no-install-project
ENV UV_PROJECT_ENVIRONMENT="/woodpecker/src/codeberg.org/slidge/slidgram/.venv"
ENV UV_LINK_MODE=copy
ENV PATH="$UV_PROJECT_ENVIRONMENT/bin:$PATH"

# Dev container
# *************
FROM builder AS dev

# copy "localhost" certs from the prosody slidge dev container, so
COPY --from=codeberg.org/slidge/prosody-slidge-dev:latest \
  /etc/prosody/certs/localhost.crt \
  /usr/local/share/ca-certificates/
RUN update-ca-certificates
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
        libmagic1 \
        media-types \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
RUN uv pip install watchdog[watchmedo]
COPY --from=ci /venv /venv
ENTRYPOINT ["watchmedo", "auto-restart", \
  "--pattern", "*.py", \
  "--directory", "/build/slidgram", \
  "--recursive", \
  "slidgram", "--", \
  "--jid", "slidge.localhost", \
  "--secret", "secret", \
  "--debug", \
  "--upload-service", "upload.localhost", \
  "--admins", "test@localhost", \
  "--dev-mode"]

# Prod container
# **************
FROM docker.io/python:$PYTHONVERSION-slim-$DEBIANVERSION AS slidgram

ARG PYTHONVERSION
ENV PYTHONUNBUFFERED=1
ENV PATH="/venv/bin:$PATH"
STOPSIGNAL SIGINT
WORKDIR /var/lib/slidge
# libmagic1: to guess mime type from files
# media-types: to determine file name suffix based on file type
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
        libmagic1 \
        media-types \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
RUN addgroup --system --gid 10000 slidge
RUN adduser --system --uid 10000 --ingroup slidge --home /var/lib/slidge slidge
USER slidge
COPY --from=builder /venv /venv
COPY --from=builder /build/slidgram /venv/lib/python$PYTHONVERSION/site-packages/slidgram
ENTRYPOINT ["slidgram"]
