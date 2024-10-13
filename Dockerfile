# Use an official Python runtime as a parent image
FROM python:3.8-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    portaudio19-dev \
    python3-pyaudio \
    && rm -rf /var/lib/apt/lists/*

# Copy all files from current directory into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make sure scripts in .local are usable:
ENV PATH=/root/.local/bin:$PATH

# Make port 80 available to the world outside this container
EXPOSE 80

# Define environment variable
ENV NAME Jack-o-Lantern

# Run index.py when the container launches
CMD ["python", "index.py", "--config", "config.yml"]
