import speech_recognition as sr
import requests
import subprocess
import signal
import threading
# from elevenlabs import stream, VoiceSettings
import sys
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

VOICE_ID = config['eleven_labs']['voice_id']
URL = config['eleven_labs']['url']
ELEVENLABS_API_KEY = config['eleven_labs']['api_key']
GOOGLE_CLOUD_KEY = config['google_cloud']['api_key']
CHUNK_SIZE = config['general']['chunk_size']
LOOP_PAUSE_TIME = config['general']['loop_pause_time']
PHYSICAL_MIC_MUTE = config['general']['physical_mic_mute']

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
    

# Function to handle speech recognition
def listen_and_respond(r, audio):
    t_phrase_end = time.perf_counter()

    if PHYSICAL_MIC_MUTE:
        mute_mic = subprocess.run(["amixer", "sset", "'Capture'", "nocap"])
        logger.info(f"Muted mic: {mute_mic.stdout}")
    
    lights = subprocess.Popen(["sudo", "python", "led_pulse_test.py"])

    try:
        logger.info("Recognizing audio...")
        text = process_audio(audio)
        logger.info(f"AI response: {text}")
        
        lights.send_signal(signal.SIGINT)

        # Call ElevenLabs to speak
        if text is not None:
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
        if PHYSICAL_MIC_MUTE:
            unmute_mic = subprocess.run(["amixer", "sset", "'Capture'", "cap"])
            logger.info(f"Unmuted mic: {unmute_mic.stdout}")


# Start listening in the background
# with m as source:
#     r.listen(source)
stop_listening = r.listen_in_background(m, listen_and_respond, phrase_time_limit=5)
logger.info('Started listening')

# Keep the program running
try:
    while True:
        time.sleep(LOOP_PAUSE_TIME)
except KeyboardInterrupt:
    logger.info('Stopping.')
finally:
    stop_listening(wait_for_stop=False)
