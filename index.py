import speech_recognition as sr
import requests
import subprocess
import sys
import os
import glob
import random
import tempfile
import threading
import logging
import time
import yaml
import argparse
from google_interface import process_audio, cloud_function_url, auth_headers, set_shared_secret

# Enable passing arguments to set Recognizer properties
def parse_args():
    parser = argparse.ArgumentParser(description='Jack-O-Lantern')
    parser.add_argument('--config', type=str, default='config.yml', help='Path to the config file')
    return parser.parse_args()

# load config from yaml file. Bail out immediately on a bad/missing file rather
# than limping on with `config` undefined and failing with a NameError below.
_config_path = parse_args().config
try:
    with open(_config_path, 'r') as f:
        config = yaml.safe_load(f)
except (OSError, yaml.YAMLError) as e:
    sys.exit(f"Error loading config file {_config_path}: {e}")
if not isinstance(config, dict):
    sys.exit(f"Config file {_config_path} is empty or malformed")

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
CHUNK_SIZE = config['general']['chunk_size']

# Optional shared secret for the cloud function. The Cloud Run endpoint is public,
# so without this anyone who finds the URL can run STT + Gemini on our bill.
set_shared_secret((config.get('google_cloud') or {}).get('shared_secret'))

# Hard cap on how long a single utterance is recorded before we cut it off.
# pause_threshold (the silence gap) is what normally ends a phrase; this is just
# a safety limit for someone who never stops talking. Falls back to 5s.
PHRASE_TIME_LIMIT = config['general'].get('phrase_time_limit', 5)

# Dim orange glow (0-255) the lantern holds between interactions. 0 = fully off
# to save battery; higher = brighter idle glow but more continuous current draw.
IDLE_GLOW_BRIGHTNESS = config['general'].get('idle_glow_brightness', 20)

# PIR (passive infrared) motion sensor settings. When someone approaches while
# Jack is idle, he speaks a proactive greeting and warms the cloud function for
# the conversation that's likely to follow. See the `pir:` block in config.yml.
_pir_cfg = config.get('pir', {}) or {}
PIR_ENABLED = _pir_cfg.get('enabled', False)
PIR_GPIO = _pir_cfg.get('gpio', 16)
GREETINGS_DIR = _pir_cfg.get('greetings_dir', 'greetings')
PIR_STARTUP_IGNORE = _pir_cfg.get('startup_ignore_seconds', 60)
PIR_GREETING_COOLDOWN = _pir_cfg.get('greeting_cooldown', 45)
PIR_LISTEN_POLL_TIMEOUT = _pir_cfg.get('listen_poll_timeout', 1.0)

# Request raw PCM from ElevenLabs (no MP3 decode on playback). 16-bit signed LE, mono.
# Lower sample rate = less data to transfer/buffer; 22050 is plenty for speech.
TTS_SAMPLE_RATE = 22050
TTS_OUTPUT_FORMAT = f"pcm_{TTS_SAMPLE_RATE}"

# (connect, read) timeouts for ElevenLabs. Without a read timeout a stalled
# stream hangs the main loop indefinitely with the mic paused and the LEDs stuck.
TTS_TIMEOUT = (5, 30)

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


def _close_player(player_proc):
    """Close the player's stdin and wait for it, tolerating an already-dead player."""
    try:
        player_proc.stdin.close()
    except OSError:
        pass
    player_proc.wait()


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
                                json=data, headers=headers, stream=True,
                                timeout=TTS_TIMEOUT)
    logger.info(f"[TIMING] TTS response headers received: {time.perf_counter() - t_request:.3f}s")

    # An error body (401, 429, ...) is JSON, not PCM: piping it to the player
    # would make Jack spit out a burst of static. Fail loudly instead.
    response.raise_for_status()

    # Pipe the raw PCM straight to the platform's audio player (no decode step)
    player_proc = subprocess.Popen(build_player_cmd(), stdin=subprocess.PIPE)
    try:
        first_chunk = True
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                if first_chunk:
                    logger.info(f"[TIMING] TTS first audio chunk piped to player: {time.perf_counter() - t_request:.3f}s")
                    first_chunk = False
                player_proc.stdin.write(chunk)
    except BrokenPipeError:
        # Player died mid-stream (no audio device, killed process). Drop the
        # rest of the audio rather than taking the interaction down with it.
        logger.warning("Audio player exited early; dropping the rest of the speech")
    finally:
        _close_player(player_proc)
    

# The LED script runs as root (via sudo) while index.py runs as a normal user,
# so we can't reliably signal it. Instead a SINGLE LED process drives the whole
# interaction and reads its current mode from a state file we write here. One
# process means two animations can never fight over the LED hardware, no matter
# how fast the response comes back.
_LIGHTS_STATEFILE = os.path.join(tempfile.gettempdir(), "jack_lights_state")


def set_lights(state):
    """Write the current LED state ('thinking', 'speaking', 'idle'). Non-blocking;
    the running LED process picks it up on its next cycle (~150ms)."""
    try:
        with open(_LIGHTS_STATEFILE, "w") as f:
            f.write(state)
    except OSError as e:
        logger.warning(f"Could not set LED state: {e}")


# The LED process keeps running for a moment after we write 'idle' while it
# fades down. Hold on to it so a fast follow-up interaction waits for it to exit
# instead of starting a second process against the same DMA channel.
_lights_proc = None


def start_lights(initial="thinking"):
    """Spawn the single LED process for one interaction, starting in `initial`
    ('thinking' for a normal turn, 'speaking' for a proactive greeting that has
    no thinking phase). It exits on its own once the state becomes 'idle',
    settling to the glow."""
    global _lights_proc
    if _lights_proc is not None and _lights_proc.poll() is None:
        # Previous interaction is still fading down. Wait it out so only one
        # process ever drives the strip.
        try:
            _lights_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning("Previous LED process did not exit; terminating it")
            _lights_proc.terminate()
            try:
                _lights_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _lights_proc.kill()
    set_lights(initial)
    _lights_proc = subprocess.Popen(
        ["sudo", "python", "led_animations.py",
         "--statefile", _LIGHTS_STATEFILE, "--glow", str(IDLE_GLOW_BRIGHTNESS)]
    )
    return _lights_proc


def set_idle_glow():
    """One-shot: leave the dim idle glow on the strip (used at startup, before the
    first visitor)."""
    subprocess.Popen(
        ["sudo", "python", "led_animations.py", "--glow", str(IDLE_GLOW_BRIGHTNESS)]
    )


# Recognize the captured audio and speak the response
def handle_audio(audio):
    t_phrase_end = time.perf_counter()

    try:
        start_lights()  # single process, starts in 'thinking'

        logger.info("Recognizing audio...")
        text = process_audio(audio)
        logger.info(f"AI response: {text}")

        # Call ElevenLabs to speak. An empty body (not just None) means STT or the
        # LLM gave us nothing usable, so there is nothing to say.
        if text:
            set_lights("speaking")
            logger.info("Speaking response...")
            logger.info(f"[TIMING] End of speech to TTS start: {time.perf_counter() - t_phrase_end:.3f}s")
            elevenlabs_stream(text)
        else:
            logger.info("Couldn't understand speech.")

    except Exception:
        # Jack runs unattended all night: a TTS error, a dead audio player or a
        # network blip must never take the listen loop down with it.
        logger.exception("Interaction failed; staying alive for the next visitor")
    finally:
        # Interaction over (success, garbled speech, or error): the LED process
        # reads 'idle', settles to the dim glow, and exits.
        set_lights("idle")


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


# --- PIR motion sensor -------------------------------------------------------
# The sensor runs in its own thread (gpiozero's callback). To keep the audio
# device single-owner, that thread NEVER touches the mic/speaker/LEDs: it only
# sets an event asking the main loop to greet. The main loop owns all hardware
# and services the greeting inline between listens. Cross-thread state is limited
# to `_greeting_request` (an Event).
#
# The cloud warmup is fired by the main loop when it actually greets, not on
# every motion edge: a greeting is the moment a conversation is likely, and
# Cloud Run stays warm for minutes, so a single warmup then covers the visit.
# Warming on bare motion would just re-ping an already-warm instance (or warm
# for a false trigger).
_greeting_request = threading.Event()
_pir_start = 0.0        # monotonic time the sensor was armed (for startup settle)
_pir_sensor = None      # keep a reference so gpiozero doesn't close the device
_warmup_session = requests.Session()


def _warmup():
    """Ping the cloud function so an instance is warm before the visitor speaks.
    Fire-and-forget on its own thread; failures are harmless (worst case the
    first real request pays the cold start it always did)."""
    try:
        headers = {"X-Warmup": "1"}
        headers.update(auth_headers())
        _warmup_session.post(cloud_function_url, headers=headers, timeout=8)
        logger.info("PIR: warmed cloud function")
    except Exception as e:
        logger.warning(f"PIR warmup failed: {e}")


def _on_motion():
    """gpiozero callback (sensor thread) fired on each rising edge of the PIR."""
    # Ignore the settling period after power-on, when PIRs emit false triggers.
    if time.monotonic() - _pir_start < PIR_STARTUP_IGNORE:
        return
    # Ask the main loop to greet; it decides whether cooldowns allow it.
    _greeting_request.set()


def setup_pir():
    """Arm the PIR sensor if enabled and the hardware library is present.
    Safe to call on a dev machine: a missing gpiozero just leaves PIR off."""
    global _pir_sensor, _pir_start
    if not PIR_ENABLED:
        logger.info("PIR disabled in config")
        return
    try:
        from gpiozero import MotionSensor
    except Exception as e:
        logger.warning(f"gpiozero unavailable, PIR disabled: {e}")
        return
    try:
        _pir_sensor = MotionSensor(PIR_GPIO)
        # Start the settle clock BEFORE attaching the callback: a motion edge in
        # between would otherwise compare against _pir_start == 0.0, pass the
        # startup-ignore check, and queue a greeting from a power-on false trigger.
        _pir_start = time.monotonic()
        _pir_sensor.when_motion = _on_motion
        logger.info(
            f"PIR armed on GPIO {PIR_GPIO} "
            f"(ignoring motion for the first {PIR_STARTUP_IGNORE}s)"
        )
    except Exception as e:
        logger.warning(f"Could not init PIR on GPIO {PIR_GPIO}: {e}")


def play_greeting():
    """Play a random pre-rendered greeting clip (raw PCM) through the same player
    the streamed TTS uses. Returns False if there are no clips to play."""
    clips = sorted(glob.glob(os.path.join(GREETINGS_DIR, "*.pcm")))
    if not clips:
        logger.warning(f"No greeting clips in {GREETINGS_DIR}/; skipping greeting")
        return False
    with open(random.choice(clips), "rb") as f:
        pcm = f.read()
    player_proc = subprocess.Popen(build_player_cmd(), stdin=subprocess.PIPE)
    try:
        player_proc.stdin.write(pcm)
    except BrokenPipeError:
        logger.warning("Audio player exited early; greeting cut short")
    finally:
        _close_player(player_proc)
    return True


# monotonic timestamps gating proactive greetings (main thread only)
_last_greeting = 0.0
_last_interaction = 0.0


def service_greeting(source):
    """Play a proactive greeting if the PIR asked for one and cooldowns allow it.

    Runs on the main loop between listens, so all mic/speaker/LED work stays on
    one thread. A recent interaction suppresses greetings so we don't talk over
    a conversation already in progress.
    """
    global _last_greeting
    if not _greeting_request.is_set():
        return
    _greeting_request.clear()
    now = time.monotonic()
    if (now - _last_greeting < PIR_GREETING_COOLDOWN or
            now - _last_interaction < PIR_GREETING_COOLDOWN):
        return

    pause_capture(source)
    try:
        start_lights("speaking")  # no thinking phase for a greeting
        logger.info("PIR: speaking a proactive greeting")
        # A greeting is our best signal a conversation is imminent, so warm the
        # cloud now. Runs concurrently with the clip, so the instance is hot
        # before the visitor answers.
        threading.Thread(target=_warmup, daemon=True).start()
        play_greeting()
    finally:
        set_lights("idle")
        resume_capture(source)
        drain_mic(source)
        _last_greeting = time.monotonic()


# Settle the lantern into its dim idle glow while it waits for the first visitor.
set_idle_glow()
setup_pir()

# Listen in the foreground. While Jack thinks and speaks we stop the mic stream
# entirely so nothing is captured, then flush any residual and resume. This keeps
# Jack half-duplex: he ignores everything said while he is talking.
#
# listen() carries a short timeout so the loop also wakes ~1x/sec while idle to
# service a proactive greeting the PIR thread has requested. All mic/speaker/LED
# work stays on this thread; the sensor thread only sets the request event.
logger.info("Started listening")
try:
    with m as source:
        while True:
            try:
                try:
                    audio = r.listen(source, timeout=PIR_LISTEN_POLL_TIMEOUT,
                                     phrase_time_limit=PHRASE_TIME_LIMIT)
                except sr.WaitTimeoutError:
                    # No speech this window: a good moment to greet a passerby.
                    service_greeting(source)
                    continue

                pause_capture(source)
                try:
                    handle_audio(audio)
                finally:
                    resume_capture(source)
                    drain_mic(source)
                    # A real interaction resets the greeting timer: the visitor is
                    # already engaged, so don't greet them on top of it.
                    _last_interaction = time.monotonic()
            except KeyboardInterrupt:
                raise
            except Exception:
                # Last line of defence. Jack is unattended on a porch all night;
                # no single bad turn is allowed to end the loop. Pause briefly so
                # a persistent fault (e.g. the mic disappearing) doesn't spin.
                logger.exception("Listen loop error; continuing")
                time.sleep(1)
except KeyboardInterrupt:
    logger.info("Stopping.")
finally:
    # Turn the strip fully off when Jack shuts down (--glow 0), so a Ctrl-C
    # doesn't leave the idle glow stuck on. run() blocks until it's done.
    set_lights("idle")
    subprocess.run(["sudo", "python", "led_animations.py", "--glow", "0"])
