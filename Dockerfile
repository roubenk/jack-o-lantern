# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# System dependencies:
#   gcc, portaudio19-dev  - build PyAudio
#   alsa-utils            - provides aplay, the raw-PCM player index.py pipes to
#   flac                  - SpeechRecognition's FLAC encoder on platforms where
#                           it ships no bundled binary (e.g. arm64)
#   ffmpeg                - ffplay fallback player
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ffmpeg \
    flac \
    alsa-utils \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so edits to the source don't invalidate the layer
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy all files from current directory into the container at /app
COPY . /app

# Make sure scripts in .local are usable:
ENV PATH=/root/.local/bin:$PATH

# Run index.py when the container launches
CMD ["python", "index.py", "--config", "config.yml"]
