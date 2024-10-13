import speech_recognition as sr
import requests
import subprocess
import threading
from openai import OpenAI
# from elevenlabs import stream, VoiceSettings
import os
import logging
import time
import yaml

# load config from yaml file
with open('config.yml', 'r') as f:
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
CHUNK_SIZE = config['general']['chunk_size']
LOOP_PAUSE_TIME = config['general']['loop_pause_time']

# Initialize speech recognizer
r = sr.Recognizer()
r.dynamic_energy_adjustment_damping = 0.15
r.pause_threshold = 0.3
r.non_speaking_duration = 0.2

m = sr.Microphone(chunk_size=2048)

def elevenlabs_stream(text):
    def stream_audio():
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY
        }

        data = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "optimize_streaming_latency": "5",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.0
            }
        }
        
        logger.info("Sending text-to-speech request...")
        response = requests.post(URL, json=data, headers=headers, stream=True)

        # use subprocess to pipe the audio to ffplay and play it
        ffplay_cmd = ["ffplay", "-nodisp", "-autoexit", "-"]
        ffplay_proc = subprocess.Popen(ffplay_cmd, stdin=subprocess.PIPE)
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                ffplay_proc.stdin.write(chunk)
                logger.info(f"Received {len(chunk)} bytes of audio data.")
        
        # close the ffplay process when finished
        ffplay_proc.stdin.close()
        ffplay_proc.wait()
    
    # Start the audio streaming in a separate thread
    threading.Thread(target=stream_audio).start()


# Function to handle speech recognition
def listen_and_respond(r, audio):

    try:
        logger.info("Recognizing audio...")
        text = r.recognize_google(audio)
        logger.info(f"You said: {text}")

        # Send text to OpenAI API and get response
        logger.info("Sending text to OpenAI API...")
        completion = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": """
                    You are a spooky and sassy Jack-o-lantern named Jack. 
                    Reply to users in the spirit of Halloween and keep your replies short and sweet.  
                    Don't use too many hip phrases. Your replies should sound like the narrator of "Thriller".
                    """
                 },
                {
                    "role": "user",
                    "content": text
                }
            ]
        )

        # Get response from OpenAI
        ai_text = completion.choices[0].message.content
        logger.info(f"AI Response: {ai_text}")

        # Call ElevenLabs to speak
        logger.info("Speaking response...")
        elevenlabs_stream(ai_text)
        # stream(text_to_speech_stream(ai_text))

    except sr.UnknownValueError:
        print("Could not understand audio")
    except sr.RequestError as e:
        print("Could not request results; {0}".format(e))


# Initialize OpenAI API client
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
openai_client = OpenAI()

# Start listening in the background
with m as source:
    r.adjust_for_ambient_noise(source)
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