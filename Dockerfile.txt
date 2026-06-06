# ── Stage 1: Build Flutter Web ───────────────────────────────────────────────
FROM ubuntu:22.04 AS flutter-build

ENV DEBIAN_FRONTEND=noninteractive
ENV FLUTTER_VERSION=3.22.0
ENV FLUTTER_HOME=/flutter
ENV PATH="${FLUTTER_HOME}/bin:${PATH}"

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    unzip \
    xz-utils \
    zip \
    libglu1-mesa \
    && rm -rf /var/lib/apt/lists/*

# Install Flutter
RUN git clone https://github.com/flutter/flutter.git \
    --branch ${FLUTTER_VERSION} \
    --depth 1 \
    ${FLUTTER_HOME}

# Pre-cache Flutter web tools
RUN flutter precache --web
RUN flutter config --enable-web

# Copy app source
WORKDIR /app
COPY . .

# Get dependencies and build for web
RUN flutter pub get
RUN flutter build web --release --no-tree-shake-icons

# ── Stage 2: Python FastAPI Backend ──────────────────────────────────────────
FROM python:3.11-slim AS backend-build

WORKDIR /backend
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# ── Stage 3: Final image — nginx serves Flutter, FastAPI runs alongside ───────
FROM python:3.11-slim

# Install nginx
RUN apt-get update && apt-get install -y nginx && rm -rf /var/lib/apt/lists/*

# Install Python deps in final image
COPY requirements.txt /backend/requirements.txt
RUN pip install --no-cache-dir -r /backend/requirements.txt

# Copy FastAPI backend
COPY --from=backend-build /backend/main.py /backend/main.py

# Copy Flutter web build output into nginx html directory
COPY --from=flutter-build /app/build/web /usr/share/nginx/html

# nginx config — serves Flutter on /, proxies /api/* to FastAPI
RUN cat > /etc/nginx/sites-available/default << 'EOF'
server {
    listen 80;
    server_name _;

    # Serve Flutter web app
    root /usr/share/nginx/html;
    index index.html;

    # Flutter routing — send all unknown paths to index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Proxy API calls to FastAPI backend
    location /api/ {
        rewrite ^/api/(.*) /$1 break;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

# Startup script — runs FastAPI and nginx together
RUN cat > /start.sh << 'EOF'
#!/bin/bash
# Start FastAPI backend in background on port 8000
cd /backend
uvicorn main:app --host 127.0.0.1 --port 8000 &

# Start nginx in foreground (keeps container alive)
nginx -g "daemon off;"
EOF

RUN chmod +x /start.sh

EXPOSE 80

CMD ["/start.sh"]
