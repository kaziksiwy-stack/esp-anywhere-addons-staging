FROM ghcr.io/esphome/esphome:2026.6.5

ARG BUILD_VERSION
ARG BUILD_ARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir rfc8785==0.1.4

WORKDIR /app
COPY builder/ /app/esp-anywhere-builder/
COPY components/ /app/esp-anywhere-esphome/components/
COPY run.py /app/run.py

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

ENTRYPOINT ["python3", "/app/run.py"]
