FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    fluxbox \
    git \
    python3 \
    python3-pip \
    x11vnc \
    xdotool \
    xvfb \
  && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir playwright==1.54.0
RUN python3 -m playwright install --with-deps chromium

COPY worker-entrypoint.sh /usr/local/bin/worker-entrypoint.sh
RUN chmod +x /usr/local/bin/worker-entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/worker-entrypoint.sh"]
