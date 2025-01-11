# install dependencies
FROM codeberg.org/slidge/slidge-builder AS builder

COPY poetry.lock pyproject.toml /build/
RUN poetry export --without-hashes >requirements.txt
RUN python3 -m pip install --requirement requirements.txt

# main container
FROM codeberg.org/slidge/slidge-base AS slidgram

USER root
RUN apt update && \
    apt install --assume-yes ffmpeg && \
    rm -rf /var/lib/apt/lists/*
USER slidge

COPY --from=docker.io/nicocool84/slidge-lottie /lottie-converter/* /usr/bin/
COPY --from=builder /venv /venv
COPY ./slidgram /venv/lib/python/site-packages/legacy_module

# dev container
FROM codeberg.org/slidge/slidge-dev AS dev

RUN apt update && \
    apt install --assume-yes ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY --from=docker.io/nicocool84/slidge-lottie /lottie-converter/* /usr/bin/
COPY --from=builder /venv /venv
