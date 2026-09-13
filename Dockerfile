# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

# tzdata so TZ=... makes the log timestamps match your local clock.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Dependencies first so this layer stays cached until requirements.txt changes.
# `tapo` publishes manylinux_2_28 wheels for aarch64 and armv7l, and bookworm
# is glibc 2.36, so the Rust extension installs prebuilt - no toolchain needed.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py dashboard.html ./

# The history database lives here; bind-mount it from the host to keep it
# across image updates. Owned by the app user so the container can write it.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

# The dashboard.
EXPOSE 8080

# `docker run IMAGE`                -> runs the watcher (main.py) + dashboard
# `docker run IMAGE check.py`       -> one-off status report
# `docker run IMAGE check.py 1.2.3.4`
ENTRYPOINT ["python", "-u"]
CMD ["main.py"]
