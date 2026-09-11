import logging
import time

import requests

logger = logging.getLogger(__name__)

# Cloud Run URL of the speech -> LLM function.
cloud_function_url = "https://jack-o-lantern-function-172068380765.us-west1.run.app"

# Optional shared secret sent with every request. The endpoint is public, so
# without it anyone who finds the URL can burn STT + Gemini quota on our bill.
# Set `google_cloud.shared_secret` in config.yml and the matching
# JACK_SHARED_SECRET env var on the Cloud Run service to turn it on.
SECRET_HEADER = "X-Jack-Secret"
_shared_secret = None

# (connect, read) timeouts. Without these a hung cloud call blocks the main loop
# forever with the mic paused and the LEDs stuck mid-animation.
REQUEST_TIMEOUT = (5, 30)

# Reuse one HTTPS connection across requests to avoid a TLS handshake per interaction
session = requests.Session()


def set_shared_secret(secret):
    """Install the shared secret read from config.yml (None/empty disables it)."""
    global _shared_secret
    _shared_secret = secret or None


def auth_headers():
    """Headers that authenticate us to the cloud function; empty if unconfigured."""
    return {SECRET_HEADER: _shared_secret} if _shared_secret else {}


def build_data(audio_data) -> bytes:
    flac_data = audio_data.get_flac_data(
        convert_rate=to_convert_rate(audio_data.sample_rate),
        convert_width=2,  # audio samples must be 16-bit
    )
    return flac_data


def to_convert_rate(sample_rate: int) -> int:
    """Target rate for the FLAC conversion, or None to keep the mic's own rate.

    Google needs at least 8 kHz, so anything slower is upsampled; anything at or
    above it is passed through untouched (the rate is carried in the FLAC header).
    """
    return None if sample_rate >= 8000 else 8000


def process_audio(audio_data):
    """Send captured audio to the cloud function and return Jack's reply text.

    Returns None if the audio could not be transcribed or the call failed; the
    caller treats that as "say nothing".
    """
    try:
        logger.info("Converting audio to FLAC.")
        t_start = time.perf_counter()
        flac_audio_data = build_data(audio_data)
        t_flac = time.perf_counter()
        logger.info(f"[TIMING] FLAC conversion: {t_flac - t_start:.3f}s")

        headers = {"Content-Type": "audio/x-flac"}
        headers.update(auth_headers())

        logger.info("Sending audio to the cloud... ☁️")
        response = session.post(
            cloud_function_url,
            data=flac_audio_data,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        t_cloud = time.perf_counter()
        logger.info(f"[TIMING] Cloud round-trip (upload + STT + LLM): {t_cloud - t_flac:.3f}s")

        if response.status_code == 200:
            llm_response = response.text
            logger.info(f"✅ LLM Response: {llm_response}")
            return llm_response

        logger.error(f"❌ Error: {response.status_code}")
        logger.error(f"Message: {response.text}")
        return None

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return None
