FROM debian:trixie-slim
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl libglib2.0-0 libpulse0 libasound2t64 libgl1 libatomic1 \
    pulseaudio pulseaudio-utils python3 python3-zeroconf \
    && rm -rf /var/lib/apt/lists/*
# Fetch each user's copy from Spotify; do not redistribute built images/binaries.
RUN case "$TARGETARCH" in arm64) arch=arm64 ;; amd64) arch=x86_64 ;; *) exit 1 ;; esac \
    && curl --fail --location "https://soloist-builds.spotifycdn.com/soloist_release_${arch}.tar.gz" \
       -o /tmp/soloist.tar.gz \
    && tar -xzf /tmp/soloist.tar.gz -C /usr/local/bin \
    && rm /tmp/soloist.tar.gz \
    && chmod +x /usr/local/bin/soloist
RUN useradd --create-home --uid 1000 stacy
COPY pulse.pa /etc/pulse/stacy.pa
COPY entrypoint.sh /usr/local/bin/stacy-soloist
COPY pairing_proxy.py /usr/local/bin/pairing_proxy.py
USER stacy
RUN mkdir -p /home/stacy/.local/share/soloist
ENV XDG_RUNTIME_DIR=/tmp/stacy-runtime
ENV PULSE_SERVER=unix:/tmp/stacy-runtime/pulse/native
ENTRYPOINT ["sh", "/usr/local/bin/stacy-soloist"]
