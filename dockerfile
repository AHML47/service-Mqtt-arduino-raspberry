FROM python:3.11-slim

# System dependencies for compilation
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    patchelf \
    ccache \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Nuitka + zstandard (fixes onefile warnings too)
RUN pip install --no-cache-dir "nuitka[onefile]"

WORKDIR /app

COPY . /app

# Install Python deps if present
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

# Build with explicit output directory (THIS is the key fix)
RUN python -m nuitka service/__main__.py \
    --standalone \
    --follow-imports \
    --include-package=service \
    --include-data-files=service/config.yaml=service/config.yaml \
    --assume-yes-for-downloads \
    --output-dir=/app/build \
    --output-filename=service.bin

# Show output so we can debug if needed
RUN ls -R /app/build