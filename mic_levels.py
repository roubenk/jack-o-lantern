#!/usr/bin/env python3
"""Measure mic levels so energy_threshold can be set from real numbers, not guesses.

Records a few seconds and reports the RMS amplitude in the SAME units as
SpeechRecognition's energy_threshold, plus how close you are to clipping.

    python3 mic_levels.py                 # 5s from the default capture device
    python3 mic_levels.py -d plughw:1,0   # a specific device (see: arecord -l)
    python3 mic_levels.py -s 8            # record 8 seconds
    python3 mic_levels.py -f some.wav     # analyse a file you already have
    python3 mic_levels.py -m -t 1500      # LIVE meter against a threshold

Use -m while physically moving the mic. It shows the level as it changes and
counts how many windows cross -t, so you can see at a glance whether ambient
alone would trigger. Watch it in silence first, then talk, then move the mic:
you want the gap between those two numbers to grow, not just both to rise.

Run it three times: silence, someone at a normal voice where visitors stand,
and the loudest background noise you get. Put energy_threshold between the
noise floor and normal speech.
"""
import argparse, array, collections, math, os, subprocess, sys, tempfile, wave

WINDOW = 0.02  # 20ms, same granularity Jack uses


def record(device, seconds):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = ["arecord", "-f", "S16_LE", "-r", "16000", "-c", "1",
           "-d", str(seconds), path]
    if device:
        cmd[1:1] = ["-D", device]
    print(f"Recording {seconds}s... speak now", file=sys.stderr)
    try:
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        os.unlink(path)
        sys.exit("arecord not found. Install it with: sudo apt install alsa-utils")
    except subprocess.CalledProcessError as e:
        os.unlink(path)
        detail = (e.stderr or b"").decode(errors="replace").strip()
        sys.exit(f"arecord failed{': ' + detail if detail else ''}\n"
                 f"List your capture devices with: arecord -l")
    return path


def analyse(path):
    try:
        w = wave.open(path, "rb")
    except FileNotFoundError:
        sys.exit(f"no such file: {path}")
    except (OSError, wave.Error) as e:
        sys.exit(f"could not read {path} as a WAV file: {e}")
    with w:
        if w.getsampwidth() != 2:
            sys.exit(f"need 16-bit audio, got {w.getsampwidth() * 8}-bit")
        rate = w.getframerate()
        data = w.readframes(w.getnframes())
    per = max(1, int(rate * WINDOW))
    samples = array.array("h")
    samples.frombytes(data)
    levels, peak = [], 0
    for i in range(0, len(samples) - per + 1, per):
        window = samples[i:i + per]
        total = 0
        for s in window:
            total += s * s
            if abs(s) > peak:
                peak = abs(s)
        levels.append(math.sqrt(total / per))
    if not levels:
        sys.exit("no audio captured")
    levels.sort()
    n = len(levels)
    return {
        "quietest": levels[0],
        "median": levels[n // 2],
        "loud_10pct": levels[int(n * 0.9)],
        "loudest": levels[-1],
        "peak_pct": peak / 32767 * 100,
        "seconds": n * WINDOW,
    }


def monitor(device, threshold):
    """Live level meter. Prints the current level and counts threshold crossings
    so you can see, while moving the mic, whether ambient alone would trigger."""
    cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"]
    if device:
        cmd[1:1] = ["-D", device]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    except FileNotFoundError:
        sys.exit("arecord not found. Install it with: sudo apt install alsa-utils")

    per = int(16000 * WINDOW)
    nbytes = per * 2
    lo, hi, crossings, windows = None, 0, 0, 0
    recent = []
    # Rolling window of the last ~3s. The live crossing rate must reflect what is
    # happening NOW, so that watching in silence tells you about silence rather
    # than being diluted by whatever you did earlier.
    rolling = collections.deque(maxlen=150)
    print("Ctrl-C to stop.  Watch this in silence first, then speak.\n",
          file=sys.stderr)
    try:
        while True:
            chunk = proc.stdout.read(nbytes)
            if not chunk or len(chunk) < nbytes:
                break
            samples = array.array("h")
            samples.frombytes(chunk)
            total = 0
            for v in samples:
                total += v * v
            level = math.sqrt(total / len(samples))

            windows += 1
            lo = level if lo is None else min(lo, level)
            hi = max(hi, level)
            if threshold and level >= threshold:
                crossings += 1
            rolling.append(level)
            recent.append(level)
            if len(recent) < 10:          # update display ~5x/second
                continue
            shown = max(recent)
            recent = []

            bar_max = max(2000, (threshold or 0) * 1.5)
            filled = int(min(1.0, shown / bar_max) * 28)
            mark = ""
            if threshold:
                over = sum(1 for v in rolling if v >= threshold)
                pct = over / len(rolling) * 100
                mark = (f"  {'OVER ' if shown >= threshold else 'under'} {threshold:.0f}"
                        f"   last 3s: {pct:4.1f}% over")
            sys.stdout.write(
                f"\r  level {shown:6.0f}  [{'#' * filled}{'.' * (28 - filled)}]"
                f"  min {lo:5.0f}  max {hi:5.0f}{mark}   ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait()
    print("\n")
    if windows:
        print(f"{windows * WINDOW:.1f}s watched.  quietest {lo:.0f}, loudest {hi:.0f}")
        if threshold:
            print(f"crossed {threshold:.0f} in {crossings / windows * 100:.1f}% "
                  f"of all windows watched")
            print("Read the live 'last 3s' figure instead for a verdict: in "
                  "silence you want 0%, while speaking you want it high.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-d", "--device")
    p.add_argument("-s", "--seconds", type=int, default=5)
    p.add_argument("-f", "--file")
    p.add_argument("-m", "--monitor", action="store_true",
                   help="live meter instead of a fixed recording")
    p.add_argument("-t", "--threshold", type=float,
                   help="your energy_threshold, to see what would trigger")
    a = p.parse_args()

    if a.monitor:
        monitor(a.device, a.threshold)
        return
    path = a.file or record(a.device, a.seconds)
    try:
        r = analyse(path)
    finally:
        if not a.file:
            try:
                os.unlink(path)
            except OSError:
                pass

    print(f"\n{r['seconds']:.1f}s analysed, in energy_threshold units:")
    print(f"  quietest 20ms window   : {r['quietest']:8.0f}   <- your noise floor")
    print(f"  median                 : {r['median']:8.0f}")
    print(f"  loudest 10% of windows : {r['loud_10pct']:8.0f}   <- what speech reaches")
    print(f"  loudest window         : {r['loudest']:8.0f}")
    print(f"  peak sample            : {r['peak_pct']:7.1f}% of full scale")
    if r["peak_pct"] > 95:
        print("\n  CLIPPING. Turn the capture gain DOWN; distorted audio "
              "transcribes worse than quiet audio.")
    elif r["peak_pct"] < 10:
        print("\n  Very quiet. Raise the capture gain in alsamixer before "
              "touching energy_threshold.")


if __name__ == "__main__":
    main()
