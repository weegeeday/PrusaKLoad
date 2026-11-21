FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir cffi greenlet jinja2 pyserial pytest

RUN git clone --depth 1 https://github.com/Klipper3d/klipper.git /opt/klipper \
    && cd /opt/klipper \
    && cp test/configs/hostsimulator.config .config \
    && make olddefconfig \
    && make out/klipper.dict

WORKDIR /workspace
COPY . /workspace

RUN bash /workspace/install.sh -k /opt/klipper

ENV KLIPPER_DIR=/opt/klipper

CMD ["sh", "-c", "pytest -v && python3 tests/sim/run_klipper_sim.py"]
