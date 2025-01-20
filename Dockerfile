# install dependencies
FROM codeberg.org/slidge/slidge-builder AS builder

COPY uv.lock pyproject.toml /build/
RUN uv export --no-dev > requirements.txt
RUN uv venv /venv/
RUN uv pip install --requirement requirements.txt

# ci container
FROM builder AS woodpecker-slidgram
# In CI we copy /venv to .venv, then update it for the whole workflow.
ENV PATH=".venv/bin:$PATH"
RUN uv export > requirements.txt
RUN uv pip install --requirement requirements.txt

# main container
FROM codeberg.org/slidge/slidge-base AS slidgram

USER root
RUN apt update && \
    apt install --assume-yes ffmpeg && \
    rm -rf /var/lib/apt/lists/*
USER slidge

COPY --from=codeberg.org/slidge/lottie-converter /lottie-converter/* /usr/bin/
COPY --from=builder /venv /venv
COPY ./slidgram /venv/lib/python/site-packages/legacy_module

# dev container
FROM codeberg.org/slidge/slidge-dev AS dev

RUN apt update && \
    apt install --assume-yes ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY --from=codeberg.org/slidge/lottie-converter /lottie-converter/* /usr/bin/
COPY --from=builder /venv /venv
