# ReelDeck: the app, ffmpeg for cutting and mixing, node for projects' own render scripts.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home reeldeck \
 && mkdir /data && chown reeldeck /data

WORKDIR /app
# publik-app-token.txt is optional: release builds have it, a local build may not (see .dockerignore)
COPY . .

# /data holds everything that lasts: reels, the project registry, and
# ~/.config (Instagram token, AI settings, publik sign-in), since HOME is /data
ENV REELDECK_HOST=0.0.0.0 \
    REELDECK_PORT=8810 \
    REELDECK_DATA=/data \
    REELDECK_PROJECTS=/data/projects \
    HOME=/data \
    PYTHONUNBUFFERED=1
USER reeldeck
VOLUME /data
EXPOSE 8810
CMD ["python3", "server.py"]
