FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    patchelf \
    ccache \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "nuitka[onefile]"

WORKDIR /app

COPY . /app

RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

RUN python -m nuitka service/__main__.py \
    --standalone \
    --follow-imports \
    --include-package=service \
    --include-data-files=service/config.yaml=service/config.yaml \
    --assume-yes-for-downloads \
    --output-dir=/app/build \
    --output-filename=service.bin

RUN ls -R /app/build