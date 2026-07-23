FROM python:3.11.12-slim-bookworm

ARG NODE_VERSION=20.19.4
ARG NODE_X64_SHA256=7a488a09e2fc02fbd1bc4ae084bea8a589314f741c182fc02c5f3f07c79a29d4

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        docker.io \
        git \
        libdbus-1-3 \
        libegl1 \
        libfontconfig1 \
        libgl1 \
        libglib2.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb-cursor0 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-randr0 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-xfixes0 \
        libxkbcommon0 \
        libxkbcommon-x11-0 \
        procps \
        tini \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --location --silent --show-error \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        --output /tmp/node.tar.xz \
    && echo "${NODE_X64_SHA256}  /tmp/node.tar.xz" | sha256sum --check --strict \
    && tar --extract --xz --file /tmp/node.tar.xz \
        --directory /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && node --version \
    && npm --version

COPY controller-entrypoint.sh /usr/local/bin/sixbench-controller-entrypoint
COPY runtime_smoke.py /opt/sixbench/runtime_smoke.py
COPY runtime_smoke_worker.py /opt/sixbench/runtime_smoke_worker.py

RUN chmod 0755 /usr/local/bin/sixbench-controller-entrypoint

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sixbench-controller-entrypoint"]
