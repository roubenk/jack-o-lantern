"""One-time generator for Jack's proactive greeting clips.

The PIR sensor triggers a greeting when someone approaches while Jack is idle.
Rather than pay a live LLM+TTS round trip (and a cold start) for every passerby,
we pre-render a handful of greeting lines once and play a random one instantly.

Clips are saved as raw PCM (pcm_22050, 16-bit signed LE, mono) so playback in
index.py is a straight pipe to the same audio player the streamed TTS uses --
no decode step, no latency.

Run this once (anywhere with the ElevenLabs key in config.yml):

    python generate_greetings.py

It (re)creates the greetings/ folder with greet_00.pcm, greet_01.pcm, ...
Edit GREETINGS below to taste, then re-run to regenerate.
"""

import argparse
import os
import sys

import requests
import yaml

# Spooky, in-character one-liners Jack calls out when someone wanders near.
# Keep them short and punchy -- they play unprompted, so brevity is a virtue.
# The bracketed [tags] are eleven_v3 audio tags (see TTS_MODEL below): they
# steer delivery -- laughter, whispers, tone -- and are NOT spoken aloud. They
# only work on v3; on an older model they'd be read literally, so keep TTS_MODEL
# and these tags in sync.
GREETINGS = [
    "[low and menacing] Well, well, well... [chuckles darkly] what do we have here?",
    "[whispers] Come closer... [ominous] if you dare.",
    "[theatrical] Ah, a fresh face for the harvest. [delighted] Step right up.",
    "[sniffs the air] I smell... a visitor. [coaxing] Don't be shy.",
    "[slow, wicked laugh] Heh heh heh... [gleeful] I've been waiting for you.",
    "[booming] Who dares disturb my slumber? [softening, inviting] Come, say hello.",
    "[delighted] A living soul approaches. [savoring] How delightful.",
    "[mock-scolding] Don't just lurk in the shadows. [warmly] Come chat with old Jack.",
]

# Raw PCM format the player in index.py expects (must match TTS_SAMPLE_RATE there).
TTS_SAMPLE_RATE = 22050
TTS_OUTPUT_FORMAT = f"pcm_{TTS_SAMPLE_RATE}"

# The live path in index.py uses a flash model for low latency, since a visitor
# is waiting on the reply. This generator runs once, offline, with nobody
# waiting -- so we spend that budget on quality. eleven_v3 is the most
# expressive model (built for character/emotional delivery, and it understands
# inline tags like [laughs] / [whispers]), which suits Jack's theatrical
# one-liners better than the steadier-but-flatter multilingual_v2. It's slightly
# less consistent take-to-take, but that's a non-issue here: this is a one-time
# render, so just re-run and keep the good takes. Same voice (that lives in the
# URL), so Jack still sounds like Jack -- just hammier.
TTS_MODEL = "eleven_v3"


def parse_args():
    parser = argparse.ArgumentParser(description="Pre-render Jack's greeting clips")
    parser.add_argument("--config", default="config.yml", help="Path to the config file")
    return parser.parse_args()


def main():
    args = parse_args()
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    api_key = config["eleven_labs"]["api_key"]
    url = config["eleven_labs"]["url"]
    out_dir = config.get("pir", {}).get("greetings_dir", "greetings")
    os.makedirs(out_dir, exist_ok=True)

    headers = {"Content-Type": "application/json", "xi-api-key": api_key}
    session = requests.Session()

    for i, text in enumerate(GREETINGS):
        data = {
            "text": text,
            "model_id": TTS_MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.5},
        }
        print(f"[{i + 1}/{len(GREETINGS)}] {text}")
        resp = session.post(
            url, params={"output_format": TTS_OUTPUT_FORMAT}, json=data, headers=headers
        )
        if resp.status_code != 200:
            print(f"  ! ElevenLabs error {resp.status_code}: {resp.text}", file=sys.stderr)
            sys.exit(1)

        out_path = os.path.join(out_dir, f"greet_{i:02d}.pcm")
        with open(out_path, "wb") as out:
            out.write(resp.content)
        print(f"  -> {out_path} ({len(resp.content)} bytes)")

    print(f"\nDone. Wrote {len(GREETINGS)} clips to {out_dir}/")


if __name__ == "__main__":
    main()
