FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    patchelf \
    ccache \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Make sure compiler invocations are cached via ccache symlinks (gcc/g++ wrappers)
ENV PATH="/usr/lib/ccache:${PATH}"

RUN pip install --no-cache-dir "nuitka[onefile]"

WORKDIR /app

# Install Python deps first for better Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

COPY service /app/service

RUN python -m nuitka service/__main__.py \
    --standalone \
    --follow-imports \
    --include-package=service \
    --include-data-files=service/config.yaml=service/config.yaml \
    --assume-yes-for-downloads \
    --jobs=$(nproc) \
    --output-dir=/app/build \
    --output-filename=service.bin

RUN ls -R /app/build