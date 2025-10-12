import requests
import logging

# initialize logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")

# 1. Set your Cloud Function URL
# This is the trigger URL you get from the Google Cloud Console.
cloud_function_url = "https://jack-o-lantern-function-172068380765.us-west1.run.app"

# 2. Specify the path to your audio file
# This could be a file you've just saved from the microphone.
audio_file_path = "output.mp3"

def build_data(audio_data) -> bytes:
    flac_data = audio_data.get_flac_data(
        convert_rate=to_convert_rate(audio_data.sample_rate),
        convert_width=2,  # audio samples must be 16-bit
    )
    return flac_data

def to_convert_rate(sample_rate: int) -> int:
    """Audio samples must be at least 8 kHz

    >>> RequestBuilder.to_convert_rate(16_000)
    >>> RequestBuilder.to_convert_rate(8_000)
    >>> RequestBuilder.to_convert_rate(7_999)
    8000
    """
    return None if sample_rate >= 8000 else 8000

def process_audio(audio_data):
    try:
        # 3. Read the audio file and convert to FLAC
        logger.info("Converting audio to FLAC.")
        flac_audio_data = build_data(audio_data)

        # 4. Set headers to specify the content type
        # This tells your function that you're sending raw binary data.
        headers = {
            'Content-Type': 'audio/x-flac; rate=16000'
        }

        # 5. Send the POST request
        logger.info("Sending audio to the cloud... ☁️")
        response = requests.post(cloud_function_url, data=flac_audio_data, headers=headers)

        # 6. Handle the response from the Cloud Function
        if response.status_code == 200:
            # Success! The response.text contains the final text from the LLM.
            llm_response = response.text
            logger.info(f"✅ LLM Response: {llm_response}")
            return llm_response
        
        else:
            # If something went wrong, print the error.
            logger.error(f"❌ Error: {response.status_code}")
            logger.error(f"Message: {response.text}")

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")