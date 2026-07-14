# syntax=docker/dockerfile:1.6
ARG RUSTWRIGHT_DOCKER_BASE_IMAGE=python:3.13-slim-bookworm
FROM ${RUSTWRIGHT_DOCKER_BASE_IMAGE}

USER root

ARG PLAYWRIGHT_REFERENCE_VERSION=1.59.0
ARG INSTALL_PUPPETEER=0

ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:/workspace/.local/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_ROOT_USER_ACTION=ignore \
    RUSTWRIGHT_CHROMIUM=/usr/local/bin/rustwright-chromium \
    RUSTWRIGHT_BROWSERS_PATH=/ms-rustwright \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_REFERENCE_PATH=/opt/playwright-reference \
    PUPPETEER_PACKAGE_PATH=/opt/puppeteer-benchmark/node_modules/puppeteer-core

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        pkg-config \
        unzip \
        xz-utils \
    && if ! command -v python >/dev/null 2>&1 || ! python -m pip --version >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends \
          python3 \
          python3-dev \
          python3-pip \
          python3-venv \
          python-is-python3; \
       fi \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --profile minimal \
    && chmod -R a+w "$CARGO_HOME" "$RUSTUP_HOME"

COPY pyproject.toml Cargo.toml Cargo.lock ./
RUN touch README.md
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel "maturin>=1.5,<2" \
    && python -m pip install "pytest>=8" "pytest-benchmark>=4"

COPY src ./src
COPY python ./python
# The root Cargo.toml declares `node` as a workspace member, so cargo must be
# able to load node/Cargo.toml (and its lib source) to resolve the workspace
# before maturin builds the Python extension — even though this image never
# builds the Node addon. Copy just the crate manifest and sources, not
# node_modules.
COPY node/Cargo.toml node/build.rs ./node/
COPY node/src ./node/src

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/workspace/target \
    python -m pip install --no-build-isolation -e ".[dev]"

RUN --mount=type=cache,target=/var/cache/rustwright-browsers \
    python -m rustwright.cli install-deps chromium \
    && mkdir -p "$RUSTWRIGHT_BROWSERS_PATH" \
    && case "$(uname -m)" in \
        aarch64|arm64) echo "Using Playwright's Linux arm64 Chromium for Rustwright runtime in this image." ;; \
        *) RUSTWRIGHT_BROWSERS_PATH=/var/cache/rustwright-browsers python -m rustwright.cli install chromium \
           && cp -a /var/cache/rustwright-browsers/. "$RUSTWRIGHT_BROWSERS_PATH"/ ;; \
       esac

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/var/cache/playwright-browsers \
    mkdir -p "$PLAYWRIGHT_REFERENCE_PATH" "$PLAYWRIGHT_BROWSERS_PATH" \
    && python -m pip install --target "$PLAYWRIGHT_REFERENCE_PATH" "playwright==${PLAYWRIGHT_REFERENCE_VERSION}" \
    && for attempt in 1 2 3; do \
        PLAYWRIGHT_BROWSERS_PATH=/var/cache/playwright-browsers PYTHONPATH="$PLAYWRIGHT_REFERENCE_PATH" python -m playwright install chromium && break; \
        if [ "$attempt" = "3" ]; then exit 1; fi; \
        sleep "$((attempt * 5))"; \
    done \
    && cp -a /var/cache/playwright-browsers/. "$PLAYWRIGHT_BROWSERS_PATH"/

RUN for root in "$RUSTWRIGHT_BROWSERS_PATH" "$PLAYWRIGHT_BROWSERS_PATH"; do \
        if [ -d "$root" ]; then \
          chmod -R a+rX "$root"; \
          find "$root" -type f \( \
            -name chrome -o \
            -name chrome_crashpad_handler -o \
            -name chromium_headless_shell -o \
            -name headless_shell \
          \) -exec chmod a+rx {} +; \
        fi; \
    done

RUN case "$(uname -m)" in \
        aarch64|arm64) browser="$(find "$PLAYWRIGHT_BROWSERS_PATH" -path '*/chrome-linux/chrome' -type f | head -n 1)" ;; \
        *) browser="$(find "$RUSTWRIGHT_BROWSERS_PATH" -path '*/chrome-linux64/chrome' -type f | head -n 1)" ;; \
    esac \
    && test -n "$browser" \
    && ln -sf "$browser" "$RUSTWRIGHT_CHROMIUM"

RUN --mount=type=cache,target=/root/.npm \
    if [ "$INSTALL_PUPPETEER" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends nodejs npm \
        && rm -rf /var/lib/apt/lists/* \
        && mkdir -p /opt/puppeteer-benchmark \
        && npm install --prefix /opt/puppeteer-benchmark --omit=dev puppeteer-core; \
    fi

COPY benchmarks ./benchmarks
COPY tests ./tests
COPY tools ./tools
COPY docs ./docs
COPY README.md ./README.md

ENTRYPOINT ["/workspace/tools/docker_verify.sh"]
CMD ["sampled"]
