import time
import functions_framework
from google.cloud import speech
from google import genai
from google.genai import types
import logging

# initialize logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")

# Initialize Vertex AI - happens once when the function instance starts
PROJECT_ID = "jack-o-lantern-474421"  # @param {type:"string"}
LOCATION = "us-west1"            # @param {type:"string"}

try:
    GENAI_CLIENT = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION
    )
    # The recommended model name for Vertex AI is still the clean name
    GEMINI_MODEL_NAME = "gemini-2.5-flash-lite"

except Exception as e:
    # A failure here means critical config error (e.g., bad project/location)
    logger.error(f"FATAL: Failed to initialize GenAI Client: {e}")
    GENAI_CLIENT = None

# Build the Speech client once at instance startup, not per request. Constructing
# it involves auth + gRPC channel setup, so reusing it removes that cost from the
# hot path of every interaction.
try:
    SPEECH_CLIENT = speech.SpeechClient()
except Exception as e:
    logger.error(f"FATAL: Failed to initialize Speech Client: {e}")
    SPEECH_CLIENT = None


@functions_framework.http
def respond_to_speech(request):
    """
    HTTP Cloud Function to process audio, transcribe it, and get an LLM response.
    Args:
        request (flask.Request): The request object.
                                 Expects audio data in the request body.
    Returns:
        The LLM's text response.
    """
    if not GENAI_CLIENT:
        return "Internal Error: GenAI Client failed to initialize.", 500
    if not SPEECH_CLIENT:
        return "Internal Error: Speech Client failed to initialize.", 500

    # 1. Get audio from the request
    audio_content = request.get_data()

    # 2. Call Speech-to-Text API
    try:
        t_stt_start = time.perf_counter()
        audio = speech.RecognitionAudio(content=audio_content)
        stt_config = speech.RecognitionConfig(
            language_code="en-US",
            model='latest_short'
        )
        response = SPEECH_CLIENT.recognize(config=stt_config, audio=audio)
        if not response.results:
            return "Could not transcribe audio.", 400
        transcript = response.results[0].alternatives[0].transcript
        logger.info(f"[TIMING] STT: {time.perf_counter() - t_stt_start:.3f}s")
        logger.info(f"User said: {transcript}")
    except Exception as e:
        return f"Error in transcription: {e}", 500

    # 3. Call the Gemini LLM via Vertex AI
    try:
        t_llm_start = time.perf_counter()
        system_prompt_1 = "You are a spooky and snarky Jack-o-lantern named Jack. Reply to people in the spirit of Halloween, with a dramatic vibe. Keep your replies short, dramatic, and fun. Reply in very short quips."
        system_prompt_2 = "Your response will be input to a text-to-speech model, so keep your text standard, don't use chat expressions like \"*squeals*\". If you laugh, only write it as \"heh heh heh\"."
        llm_config = types.GenerateContentConfig(
             system_instruction=[
                system_prompt_1,
                system_prompt_2
             ],
             thinking_config=types.ThinkingConfig(
                thinking_budget=0,
            )
        )
        llm_response = GENAI_CLIENT.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=transcript,
            config=llm_config
        )
        final_response = llm_response.text
        logger.info(f"[TIMING] LLM: {time.perf_counter() - t_llm_start:.3f}s")
        logger.info(f"Jack said: {final_response}")
    except Exception as e:
        return f"Error generating LLM response: {e}", 500

    # 4. Return the final text response
    return final_response, 200
