import speech_recognition as sr
import requests
import subprocess
import signal
import threading
from openai import OpenAI
# from elevenlabs import stream, VoiceSettings
import os
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
OPENAI_API_KEY = config['open_ai']['api_key']
GOOGLE_CLOUD_KEY = config['google_cloud']['api_key']
CHUNK_SIZE = config['general']['chunk_size']
LOOP_PAUSE_TIME = config['general']['loop_pause_time']
PHYSICAL_MIC_MUTE = config['general']['physical_mic_mute']

# Unmute system microphone in case it was left muted in a crash
unmute_cmd = ["amixer", "sset", "Capture", "cap"]
unmute_proc = subprocess.Popen(unmute_cmd, stdin=subprocess.PIPE)

# Initialize speech recognizer
r = sr.Recognizer()
r.dynamic_energy_adjustment_damping = config['recognizer_properties']['dynamic_energy_adjustment_damping']
r.dynamic_energy_threshold = config['recognizer_properties']['dynamic_energy_threshold']
r.pause_threshold = config['recognizer_properties']['pause_threshold']
r.non_speaking_duration = config['recognizer_properties']['non_speaking_duration']
r.energy_threshold = config['recognizer_properties']['energy_threshold']
r.dynamic_energy_ratio = config['recognizer_properties']['dynamic_energy_ratio']

m = sr.Microphone(chunk_size=config['microphone_properties']['chunk_size'])

def elevenlabs_stream(text):
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }

    data = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.5
        }
    }
    
    logger.info("Sending text-to-speech request...")
    response = requests.post(URL, json=data, headers=headers, stream=True)

    # use subprocess to pipe the audio to ffplay and play it
    ffplay_cmd = ["ffplay", "-nodisp", "-autoexit", "-"]
    ffplay_proc = subprocess.Popen(ffplay_cmd, stdin=subprocess.PIPE)
    chunk_progress = 0
    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
        if chunk:
            ffplay_proc.stdin.write(chunk)
            chunk_progress += len(chunk)
            # logger.info(f"Received {chunk_progress} bytes of audio data.")
    
    # close the ffplay process when finished
    ffplay_proc.stdin.close()
    ffplay_proc.wait()
    

# Function to handle speech recognition
def listen_and_respond(r, audio):

    if PHYSICAL_MIC_MUTE:
        mute_mic = subprocess.run(
            ["amixer", "sset", "'Capture'", "nocap"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
        logger.info(f"Muted mic.")
    
    thinking_lights = subprocess.Popen(["sudo", "python", "led_animations.py", "--thinking", "-c"])

    try:
        logger.info("Recognizing audio...")
        text = process_audio(audio)
        logger.info(f"AI response: {text}")
        
        thinking_lights.send_signal(signal.SIGINT)
        
        speaking_lights = subprocess.Popen(["sudo", "python", "led_animations.py", "--speaking", "-c"])

        # Call ElevenLabs to speak
        if text is not None:
            logger.info("Speaking response...")
            elevenlabs_stream(text)
            # stream(text_to_speech_stream(ai_text))
        else:
            logger.info("Couldn't understand speech.")
        
        speaking_lights.send_signal(signal.SIGINT)
 

    except sr.UnknownValueError:
        print("Could not understand audio")
        thinking_lights.send_signal(signal.SIGINT)
    except sr.RequestError as e:
        print("Could not request results; {0}".format(e))
        thinking_lights.send_signal(signal.SIGINT)
    finally: 
        if PHYSICAL_MIC_MUTE:
            unmute_mic = subprocess.run(["amixer", "sset", "'Capture'", "cap"],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
            )
            logger.info(f"Unmuted mic.")


# Initialize OpenAI API client
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
openai_client = OpenAI()

# Start listening in the background
# with m as source:
#     r.adjust_for_ambient_noise(source)
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
