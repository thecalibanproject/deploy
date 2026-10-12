#!/usr/bin/env python3
"""Builds the demo videos from the recorded parts (stdlib only, Python 3.9+; ffmpeg does the work).

    stitch.py [SCENARIO...]    default: every <scenario>.timeline.json in $DEMO_OUT

Per scenario, $DEMO_OUT/<scenario>.mp4: its title card, its console walkthrough (long waits cut
to their first and last seconds), its checks (term/check-<scenario>.mp4). Then
$DEMO_OUT/caliban-demo.mp4: the title card, the gateway host (term/infra-up.mp4), every scenario,
the health results segment, the telemetry segment and the end card, with a chapter marker per part
and per walkthrough chapter. Parts that are missing are left out and reported.

DEMO_FFMPEG is the ffmpeg command (default: the pinned image through docker, see images.env);
DEMO_MAX_IDLE_S (default 10) is the longest wait kept whole; DEMO_IDLE_KEEP_S (default 3) how much
of each end of a longer wait is kept.
"""
import glob
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("E2E_WORK_DIR") or os.path.join(os.path.dirname(HERE), "health-triage", ".work")
OUT = os.path.abspath(os.environ.get("DEMO_OUT") or os.path.join(WORK, "demo"))
MAX_IDLE = float(os.environ.get("DEMO_MAX_IDLE_S", "10"))
KEEP = float(os.environ.get("DEMO_IDLE_KEEP_S", "3"))
CARD_S = {"title": 7, "section": 4, "scenario": 5, "end": 6}
VF = ("scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0b1020,"
      "fps=30,format=yuv420p,setsar=1")
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an"]


def image(name):
    env = {}
    with open(os.path.join(HERE, "images.env"), encoding="utf-8") as f:
        for m in re.finditer(r'\$\{(\w+):=([^}]+)\}', f.read()):
            env[m.group(1)] = m.group(2)
    return os.environ.get(name) or env[name]


def ffmpeg_cmd():
    if os.environ.get("DEMO_FFMPEG"):
        return shlex.split(os.environ["DEMO_FFMPEG"])
    return ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{OUT}:{OUT}", "-w", OUT,
            image("DEMO_FFMPEG_IMAGE")]


def ff(*args):
    cmd = ffmpeg_cmd() + ["-hide_banner", "-loglevel", "error", "-y"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-1500:]}")


def duration(path):
    r = subprocess.run(ffmpeg_cmd() + ["-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        raise RuntimeError(f"no duration for {path}: {r.stderr.strip()[-400:]}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def card(name, seconds, out):
    ff("-loop", "1", "-t", str(seconds), "-i", os.path.join(OUT, "cards", f"{name}.png"), "-vf", VF, *ENC, out)


def normalize(src, out, cuts=()):
    vf = VF
    if cuts:
        expr = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in cuts)
        vf = f"fps=30,select='not({expr})',setpts=N/30/TB," + VF
    ff("-i", src, "-vf", vf, *ENC, out)


def idle_cuts(timeline):
    cuts = []
    for s in timeline.get("idle") or []:
        if s["end"] - s["start"] > MAX_IDLE:
            cuts.append((s["start"] + KEEP, s["end"] - KEEP))
    return cuts


def shift(t, cuts):
    """A time of the original recording in the cut recording."""
    out = t
    for a, b in cuts:
        if t >= b:
            out -= b - a
        elif t > a:
            out -= t - a
    return out


class Builder:
    def __init__(self, name):
        self.name = name
        self.parts = []          # (path, duration)
        self.chapters = []       # (start, end, title)
        self.t = 0.0

    def add(self, path, title=None, sub=()):
        d = duration(path)
        if title:
            self.chapters.append((self.t, self.t + d, title))
        for start, end, t in sub:
            self.chapters.append((self.t + start, self.t + min(end, d), t))
        self.parts.append(path)
        self.t += d

    def write(self, out):
        lst = os.path.join(OUT, "parts", f"{self.name}.txt")
        with open(lst, "w", encoding="utf-8") as f:
            f.writelines(f"file '{p}'\n" for p in self.parts)
        meta = os.path.join(OUT, "parts", f"{self.name}.chapters.txt")
        with open(meta, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\ntitle=Caliban demo\n")
            for start, end, title in self.chapters:
                clean = re.sub(r"([=;#\\\n])", r"\\\1", title)
                f.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(start * 1000)}\nEND={int(end * 1000)}\n"
                        f"title={clean}\n")
        joined = os.path.join(OUT, "parts", f"{self.name}.joined.mp4")
        ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined)
        ff("-i", joined, "-i", meta, "-map_metadata", "1", "-map_chapters", "1", "-c", "copy",
           "-movflags", "+faststart", out)
        print(f"stitch.py: {out} ({self.t / 60:.1f} min, {len(self.chapters)} chapters)", flush=True)


def scenario_parts(name, missing):
    """The normalized parts of one scenario: (path, title, sub-chapters)."""
    parts = []
    tl_path = os.path.join(OUT, f"{name}.timeline.json")
    with open(tl_path, encoding="utf-8") as f:
        tl = json.load(f)
    title = tl.get("title") or name
    if os.path.exists(os.path.join(OUT, "cards", f"scenario-{name}.png")):
        p = os.path.join(OUT, "parts", f"card-{name}.mp4")
        card(f"scenario-{name}", CARD_S["scenario"], p)
        parts.append((p, f"{title}", ()))
    webm = os.path.join(OUT, f"{name}.webm")
    if os.path.exists(webm):
        cuts = idle_cuts(tl)
        p = os.path.join(OUT, "parts", f"console-{name}.mp4")
        normalize(webm, p, cuts)
        subs = [(shift(c["start"], cuts), shift(c["end"], cuts), f"{name}: {c['title']}") for c in tl["chapters"]]
        parts.append((p, None, subs))
    else:
        missing.append(webm)
    check = os.path.join(OUT, "term", f"check-{name}.mp4")
    if os.path.exists(check):
        p = os.path.join(OUT, "parts", f"check-{name}.mp4")
        normalize(check, p)
        parts.append((p, f"{name}: checks", ()))
    else:
        missing.append(check)
    return title, parts


def ordered(names):
    """Scenario names in DEMO_ORDER (run-demo.sh), the others after them in name order."""
    order = os.environ.get("DEMO_ORDER", "health-triage law developer fintech").split()
    return sorted(names, key=lambda n: (order.index(n) if n in order else len(order), n))


def main():
    names = sys.argv[1:] or ordered(os.path.basename(p)[:-len(".timeline.json")]
                                    for p in glob.glob(os.path.join(OUT, "*.timeline.json")))
    if not names:
        sys.exit(f"stitch.py: no <scenario>.timeline.json in {OUT}")
    os.makedirs(os.path.join(OUT, "parts"), exist_ok=True)
    missing = []
    full = Builder("caliban-demo")
    if os.path.exists(os.path.join(OUT, "cards", "title.png")):
        p = os.path.join(OUT, "parts", "card-title.mp4")
        card("title", CARD_S["title"], p)
        full.add(p, "Caliban")
    infra = os.path.join(OUT, "term", "infra-up.mp4")
    if os.path.exists(infra):
        card("infra", CARD_S["section"], os.path.join(OUT, "parts", "card-infra.mp4"))
        full.add(os.path.join(OUT, "parts", "card-infra.mp4"), "The gateway host")
        normalize(infra, os.path.join(OUT, "parts", "infra-up.mp4"))
        full.add(os.path.join(OUT, "parts", "infra-up.mp4"))
    else:
        missing.append(infra)
    for name in names:
        title, parts = scenario_parts(name, missing)
        if name == "health-triage":
            # The health e2e's results card belongs to its use case, right after its checks.
            src = os.path.join(OUT, "results.webm")
            if os.path.exists(src):
                p = os.path.join(OUT, "parts", "results.mp4")
                normalize(src, p)
                parts.append((p, "health-triage: the results", ()))
            else:
                missing.append(src)
        one = Builder(name)
        for path, t, subs in parts:
            one.add(path, t, subs)
            full.add(path, t, subs)
        if one.parts:
            one.write(os.path.join(OUT, f"{name}.mp4"))
    for seg, title in (("telemetry", "Telemetry"),):
        src = os.path.join(OUT, f"{seg}.webm")
        if os.path.exists(src):
            p = os.path.join(OUT, "parts", f"{seg}.mp4")
            normalize(src, p)
            full.add(p, title)
        else:
            missing.append(src)
    if os.path.exists(os.path.join(OUT, "cards", "end.png")):
        p = os.path.join(OUT, "parts", "card-end.mp4")
        card("end", CARD_S["end"], p)
        full.add(p, "End")
    full.write(os.path.join(OUT, "caliban-demo.mp4"))
    for m in missing:
        print(f"stitch.py: left out (missing) {m}", flush=True)


if __name__ == "__main__":
    main()
