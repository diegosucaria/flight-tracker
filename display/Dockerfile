# Display driver — drives the 64×32 HUB75 panel via hzeller rpi-rgb-led-matrix.
#
# On the Pi 5 this uses hzeller's **PIO backend** (RGBMatrixOptions.rp1_pio = 1, via the RP1
# PIO char device /dev/pio0): the RP1 hardware-times the panel, which is immune to the dim-pixel
# flicker the CPU-driven path shows under USB/PCIe bus contention. /dev/pio0 needs a recent Pi 5
# EEPROM. Set MATRIX_RP1_PIO=0 to fall back to the RIO backend (mmap /dev/mem). Needs: a
# privileged container (for /dev/pio0 + GPIO, provided by docker-compose); init failure falls
# back to log-only so it never crash-loops. (Adafruit's separate "PioMatter" library isn't used.)
FROM python:3.12-slim

# The rgbmatrix binding builds via scikit-build-core/CMake (per its pyproject.toml), which
# needs cmake + a compiler; cython/scikit-build-core come in pip's build isolation.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git \
    && rm -rf /var/lib/apt/lists/*

# One step builds the C lib (incl. the RP1 RIO backend) + the `rgbmatrix` Python module.
# We stub out the Pillow C-shim (shims/pillow.c) so it compiles without Pillow's Imaging.h
# (that header isn't in the binary wheel). We only call SetImage(unsafe=False), which uses
# the pure SetPixel path and never invokes the shim's get_image32 — plenty fast at 64x32.
RUN git clone --depth=1 https://github.com/hzeller/rpi-rgb-led-matrix.git /opt/rgb \
    && printf '#include "pillow.h"\nint** get_image32(void* im) { (void)im; return 0; }\n' \
         > /opt/rgb/bindings/python/rgbmatrix/shims/pillow.c \
    && pip install --no-cache-dir /opt/rgb

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY display.py .
CMD ["python", "display.py"]
