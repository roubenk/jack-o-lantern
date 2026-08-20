import speech_recognition as sr
import requests
import subprocess
# from elevenlabs import stream, VoiceSettings
import sys
import os
import tempfile
import logging
import time
import yaml
import argparse
from google_interface import process_audio

# Enable passing arguments to set Recognizer properties
def parse_args():
    parser = argparse.ArgumentParser(description='Jack-O-Lantern')
    parser.add_argument('--config', type=str, default='config.yml', help='Path to the config file')
    return parser.parse_args()

# load config from yaml file
with open(parse_args().config, 'r') as f:
    try:
        config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"Error loading config file: {e}")

# initialize logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")

# A child process (audio player / sudo) can leave the TTY in raw mode, dropping
# the newline->CR-LF mapping so log lines march diagonally down the screen.
# Emit an explicit CR+LF so every line returns to column 0 regardless.
for _handler in logging.getLogger().handlers:
    _handler.terminator = "\r\n"

VOICE_ID = config['eleven_labs']['voice_id']
URL = config['eleven_labs']['url']
ELEVENLABS_API_KEY = config['eleven_labs']['api_key']
GOOGLE_CLOUD_KEY = config['google_cloud']['api_key']
CHUNK_SIZE = config['general']['chunk_size']
# Hard cap on how long a single utterance is recorded before we cut it off.
# pause_threshold (the silence gap) is what normally ends a phrase; this is just
# a safety limit for someone who never stops talking. Falls back to 5s.
PHRASE_TIME_LIMIT = config['general'].get('phrase_time_limit', 5)

# Request raw PCM from ElevenLabs (no MP3 decode on playback). 16-bit signed LE, mono.
# Lower sample rate = less data to transfer/buffer; 22050 is plenty for speech.
TTS_SAMPLE_RATE = 22050
TTS_OUTPUT_FORMAT = f"pcm_{TTS_SAMPLE_RATE}"

# Initialize speech recognizer
r = sr.Recognizer()
r.dynamic_energy_adjustment_damping = config['recognizer_properties']['dynamic_energy_adjustment_damping']
r.dynamic_energy_threshold = config['recognizer_properties']['dynamic_energy_threshold']
r.pause_threshold = config['recognizer_properties']['pause_threshold']
r.non_speaking_duration = config['recognizer_properties']['non_speaking_duration']
r.energy_threshold = config['recognizer_properties']['energy_threshold']
r.dynamic_energy_ratio = config['recognizer_properties']['dynamic_energy_ratio']

m = sr.Microphone(chunk_size=config['microphone_properties']['chunk_size'])

# Reuse one HTTPS connection to ElevenLabs to avoid a TLS handshake per interaction
tts_session = requests.Session()

def build_player_cmd():
    """Pick a raw-PCM player for the current platform.

    aplay (ALSA) on the Pi is the lowest-latency option; on Windows we fall back
    to ffplay reading raw PCM, which still avoids the MP3 decoder spin-up.
    """
    if sys.platform.startswith("win"):
        return ["ffplay", "-hide_banner", "-loglevel", "error", "-nodisp", "-autoexit",
                "-f", "s16le", "-ar", str(TTS_SAMPLE_RATE), "-ch_layout", "mono", "-i", "-"]
    return ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(TTS_SAMPLE_RATE), "-c", "1", "-"]


def elevenlabs_stream(text):
    headers = {
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }

    data = {
        "text": text,
        "model_id": "eleven_flash_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.5
        }
    }
    
    logger.info("Sending text-to-speech request...")
    t_request = time.perf_counter()
    response = tts_session.post(URL, params={"output_format": TTS_OUTPUT_FORMAT},
                                json=data, headers=headers, stream=True)
    logger.info(f"[TIMING] TTS response headers received: {time.perf_counter() - t_request:.3f}s")

    # Pipe the raw PCM straight to the platform's audio player (no decode step)
    player_proc = subprocess.Popen(build_player_cmd(), stdin=subprocess.PIPE)
    chunk_progress = 0
    first_chunk = True
    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
        if chunk:
            if first_chunk:
                logger.info(f"[TIMING] TTS first audio chunk piped to player: {time.perf_counter() - t_request:.3f}s")
                first_chunk = False
            player_proc.stdin.write(chunk)
            chunk_progress += len(chunk)
            # logger.info(f"Received {chunk_progress} bytes of audio data.")

    # close the player process when finished
    player_proc.stdin.close()
    player_proc.wait()
    

# The LED script runs as root (via sudo) while index.py runs as a normal user,
# so we can't reliably signal it to stop. Instead each animation polls a stop
# file and exits its own loop when the file appears - which guarantees its
# clear-on-exit runs, no signal delivery required.
def start_lights(mode):
    """Launch an LED animation ('thinking' or 'speaking'). Returns a handle."""
    stopfile = os.path.join(tempfile.gettempdir(), f"jack_lights_{mode}")
    try:
        os.remove(stopfile)  # drop any stale sentinel so the new animation runs
    except OSError:
        pass
    proc = subprocess.Popen(
        ["sudo", "python", "led_animations.py", f"--{mode}", "--stopfile", stopfile]
    )
    return (proc, stopfile)


def stop_lights(handle):
    """Tell an animation to stop and clear the strip. Non-blocking - the process
    notices the stop file within ~150ms and clears itself; the next animation can
    start immediately (the strip tolerates the brief overlap)."""
    if handle is None:
        return
    _proc, stopfile = handle
    try:
        open(stopfile, "w").close()  # sentinel: animation sees it, exits, clears
    except OSError as e:
        logger.warning(f"Could not signal LED stop: {e}")


# Recognize the captured audio and speak the response
def handle_audio(audio):
    t_phrase_end = time.perf_counter()

    thinking_lights = None
    speaking_lights = None

    try:
        thinking_lights = start_lights("thinking")

        logger.info("Recognizing audio...")
        text = process_audio(audio)
        logger.info(f"AI response: {text}")

        # Recognition done: stop the thinking animation before anything else runs
        stop_lights(thinking_lights)
        thinking_lights = None

        # Call ElevenLabs to speak
        if text is not None:
            speaking_lights = start_lights("speaking")
            logger.info("Speaking response...")
            logger.info(f"[TIMING] End of speech to TTS start: {time.perf_counter() - t_phrase_end:.3f}s")
            elevenlabs_stream(text)
            # stream(text_to_speech_stream(ai_text))
        else:
            logger.info("Couldn't understand speech.")

    except sr.UnknownValueError:
        print("Could not understand audio")
    except sr.RequestError as e:
        print("Could not request results; {0}".format(e))
    finally:
        # Always stop any LED animation still running, on every exit path.
        for lights in (thinking_lights, speaking_lights):
            stop_lights(lights)


def pause_capture(source):
    """Stop capturing so nothing said while Jack is busy is ever buffered."""
    try:
        source.stream.pyaudio_stream.stop_stream()
    except Exception as e:
        # If we can't stop the stream, drain_mic on resume is the fallback.
        logger.warning(f"Could not pause mic capture: {e}")


def resume_capture(source):
    """Resume capturing once Jack has finished speaking."""
    try:
        if not source.stream.pyaudio_stream.is_active():
            source.stream.pyaudio_stream.start_stream()
    except Exception as e:
        logger.warning(f"Could not resume mic capture: {e}")


def drain_mic(source):
    """Discard any audio left buffered on the mic stream.

    Belt-and-suspenders after resume: with capture stopped during playback there
    should be little to nothing here, but this clears anything the driver latched
    across the stop/start so Jack never acts on his own voice or on speech that
    happened while he was talking.
    """
    try:
        pending = source.stream.pyaudio_stream.get_read_available()
        if pending > 0:
            source.stream.read(pending)
    except Exception as e:
        # A failed flush at worst causes an occasional self-trigger; never let it
        # take down the listen loop.
        logger.warning(f"Could not flush mic buffer: {e}")


# Listen in the foreground. While Jack thinks and speaks we stop the mic stream
# entirely so nothing is captured, then flush any residual and resume. This keeps
# Jack half-duplex: he ignores everything said while he is talking.
logger.info("Started listening")
try:
    with m as source:
        while True:
            audio = r.listen(source, phrase_time_limit=PHRASE_TIME_LIMIT)
            pause_capture(source)
            try:
                handle_audio(audio)
            finally:
                resume_capture(source)
                drain_mic(source)
except KeyboardInterrupt:
    logger.info("Stopping.")
