#!/usr/bin/env python3
# NeoPixel library strandtest example
# Author: Tony DiCola (tony@tonydicola.com)
#
# Direct port of the Arduino NeoPixel library strandtest example.  Showcases
# various animations on a strip of NeoPixels.

import argparse
import signal
import time

from rpi_ws281x import Adafruit_NeoPixel, Color

# LED strip configuration:
LED_COUNT      = 29     # Number of LED pixels.
LED_PIN        = 21      # GPIO pin connected to the pixels (18 uses PWM!).
#LED_PIN        = 10      # GPIO pin connected to the pixels (10 uses SPI /dev/spidev0.0).
LED_FREQ_HZ    = 800000  # LED signal frequency in hertz (usually 800khz)
LED_DMA        = 10      # DMA channel to use for generating a signal (try 10)
LED_BRIGHTNESS = 65      # Set to 0 for darkest and 255 for brightest
LED_INVERT     = False   # True to invert the signal (when using NPN transistor level shift)
LED_CHANNEL    = 0       # set to '1' for GPIOs 13, 19, 41, 45 or 53


# Define functions which animate LEDs in various ways.
def colorWipe(strip, color, wait_ms=50):
    """Wipe color across display a pixel at a time."""
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, color)
        strip.show()
        time.sleep(wait_ms/1000.0)

def theaterChase(strip, color, wait_ms=50, iterations=10):
    """Movie theater light style chaser animation."""
    for j in range(iterations):
        for q in range(3):
            for i in range(0, strip.numPixels(), 3):
                strip.setPixelColor(i+q, color)
            strip.show()
            time.sleep(wait_ms/1000.0)
            for i in range(0, strip.numPixels(), 3):
                strip.setPixelColor(i+q, 0)

def wheel(pos):
    """Generate rainbow colors across 0-255 positions."""
    if pos < 85:
        return Color(pos * 3, 255 - pos * 3, 0)
    elif pos < 170:
        pos -= 85
        return Color(255 - pos * 3, 0, pos * 3)
    else:
        pos -= 170
        return Color(0, pos * 3, 255 - pos * 3)

def rainbow(strip, wait_ms=20, iterations=1):
    """Draw rainbow that fades across all pixels at once."""
    for j in range(256*iterations):
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, wheel((i+j) & 255))
        strip.show()
        time.sleep(wait_ms/1000.0)

def rainbowCycle(strip, wait_ms=20, iterations=5):
    """Draw rainbow that uniformly distributes itself across all pixels."""
    for j in range(256*iterations):
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, wheel((int(i * 256 / strip.numPixels()) + j) & 255))
        strip.show()
        time.sleep(wait_ms/1000.0)

def theaterChaseRainbow(strip, wait_ms=50):
    """Rainbow movie theater light style chaser animation."""
    for j in range(256):
        for q in range(3):
            for i in range(0, strip.numPixels(), 3):
                strip.setPixelColor(i+q, wheel((i+j) % 255))
            strip.show()
            time.sleep(wait_ms/1000.0)
            for i in range(0, strip.numPixels(), 3):
                strip.setPixelColor(i+q, 0)

def read_state(statefile):
    """Return the current animation state written by index.py, or 'idle'."""
    try:
        with open(statefile) as f:
            return f.read().strip()
    except OSError:
        return "idle"


# Main program logic follows:
if __name__ == '__main__':
    # Process arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--statefile', type=str, default=None,
                        help="poll this file for the current state "
                             "('thinking', 'speaking', or 'idle')")
    parser.add_argument('--glow', type=int, default=0,
                        help='idle-glow brightness 0-255 to leave on the strip on exit')
    args = parser.parse_args()

    # Create NeoPixel object with appropriate configuration.
    strip = Adafruit_NeoPixel(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA, LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL)
    # Intialize the library (must be called once before other functions).
    strip.begin()

    def settle_to_glow(fade=False):
        """Leave a dim orange idle glow (or dark if --glow is 0) and let the
        WS281x pixels hold it after we exit - no running process needed.

        With fade=True, ramp down from full-brightness orange to the glow for a
        smooth dim-down. Only safe from 'speaking' (colorWipe leaves the strip at
        full orange); other states are already dim, so they snap instead.
        """
        g = max(0, min(255, args.glow))
        if fade:
            level = 255
            while level > g:
                c = Color(level, int(level * 120 / 255), 0)
                for i in range(strip.numPixels()):
                    strip.setPixelColor(i, c)
                strip.show()
                time.sleep(0.015)
                level -= 8
        # Keep the 255:120:0 orange hue while scaling brightness.
        color = Color(g, int(g * 120 / 255), 0)
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, color)
        strip.show()

    # SIGTERM fallback for a direct kill/Ctrl-C; normal stops come via statefile.
    def _stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)

    # A single process drives the whole interaction so two processes never fight
    # over the LED hardware. index.py flips the state file (thinking -> speaking
    # -> idle); we read the current state each cycle and animate accordingly.
    # 'idle' (or a missing file) means the interaction is over: settle and exit.
    last_state = "idle"
    try:
        if args.statefile is None:
            # One-shot: just set the idle glow (used at startup) and exit.
            pass
        else:
            while True:
                state = read_state(args.statefile)
                if state == "thinking":
                    # One iteration per cycle so we re-check the state ~6x/second.
                    theaterChase(strip, Color(255, 120, 0), iterations=1)
                    last_state = state
                elif state == "speaking":
                    colorWipe(strip, Color(255, 120, 0), 5)
                    last_state = state
                else:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        # Fade down only when we were speaking (strip is at full orange); other
        # exits are already dim, so snap straight to the glow to avoid a flash-up.
        settle_to_glow(fade=(last_state == "speaking"))
