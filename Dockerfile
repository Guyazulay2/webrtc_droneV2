FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-nice \
    gstreamer1.0-libav \
    gstreamer1.0-rtsp \
    libnss-mdns \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i \
        's/^hosts:.*/hosts: files mdns4_minimal [NOTFOUND=return] dns mdns4/' \
        /etc/nsswitch.conf

COPY backend/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision="${GIT_COMMIT}"

COPY backend/ /app/backend/
COPY ui/ /app/ui/

WORKDIR /app/backend

EXPOSE 8080 5004/udp 5005/udp 8554

ENV USE_GPU=0
ENV LIBNICE_NOUPNP=1

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')"

CMD ["python3", "main.py"]
