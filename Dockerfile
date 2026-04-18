# =========================
# DeepStream Base
# =========================
FROM nvcr.io/nvidia/deepstream:6.4-gc-triton-devel

ENV DEBIAN_FRONTEND=noninteractive

# =========================
# NVIDIA + GStreamer ENV
# =========================
ENV NVIDIA_DRIVER_CAPABILITIES=all
ENV GST_PLUGIN_PATH=/opt/nvidia/deepstream/deepstream/lib/gst-plugins
ENV LD_LIBRARY_PATH=/opt/nvidia/deepstream/deepstream/lib:$LD_LIBRARY_PATH

# =========================
# Base system tools
# =========================
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# =========================
# IMPORTANT FIX:
# Remove any broken librealsense repo leftovers
# =========================
RUN rm -f /etc/apt/sources.list.d/librealsense.list || true \
 && rm -f /etc/apt/sources.list.d/*realsense* || true \
 && rm -rf /var/lib/apt/lists/*
RUN /opt/nvidia/deepstream/deepstream/user_additional_install.sh

# =========================
# Install GStreamer + system deps (NO external repos)
# =========================
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-nice \
    gstreamer1.0-libav \
    gstreamer1.0-rtsp \
    libgstrtspserver-1.0-dev \
    ffmpeg \
    nginx \
    && rm -rf /var/lib/apt/lists/*

# =========================
# Python dependencies
# =========================
COPY backend/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# =========================
# App code
# =========================
COPY backend/ /app/backend/
COPY ui/ /var/www/html/

# =========================
# Nginx config
# =========================
COPY nginx/nginx.conf /etc/nginx/nginx.conf

WORKDIR /app

# =========================
# Ports
# =========================
EXPOSE 8080 8554 8443

# =========================
# GStreamer debug script
# =========================

# =========================
# Start services
# =========================
CMD ["bash", "-c", "/app/check_plugins.sh && nginx && python3 backend/main.py"]
