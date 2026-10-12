#!/usr/bin/env python3
"""Converts a `script -m classic -T timing -O typescript` recording to an asciicast v2 file
(stdlib only, Python 3.9+), so agg can render it and asciinema-player can replay it.

    cast.py TYPESCRIPT TIMING OUT.cast --cols 110 --rows 30 --title "..."

The classic timing file has one line per chunk: the delay in seconds before it, then its size in
bytes. The typescript starts with a "Script started on ..." line that the timing does not count.
"""
import argparse
import codecs
import json
import os
import sys


def convert(typescript, timing, out, cols, rows, title):
    with open(typescript, "rb") as f:
        data = f.read()
    if data.startswith(b"Script started"):
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    header = {"version": 2, "width": cols, "height": rows, "title": title,
              "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"}}
    pos, t, events = 0, 0.0, 0
    with open(timing, encoding="ascii") as tf, open(out, "w", encoding="utf-8") as of:
        of.write(json.dumps(header) + "\n")
        for line in tf:
            parts = line.split()
            if len(parts) != 2:
                continue
            delay, size = float(parts[0]), int(parts[1])
            t += delay
            chunk = decoder.decode(data[pos:pos + size])
            pos += size
            if chunk:
                of.write(json.dumps([round(t, 6), "o", chunk], ensure_ascii=False) + "\n")
                events += 1
        rest = decoder.decode(data[pos:], final=True)
        # The trailing "Script done ..." line is not output of the command.
        rest = rest.split("\nScript done", 1)[0] if "Script done" in rest else rest
        if rest.strip():
            of.write(json.dumps([round(t, 6), "o", rest], ensure_ascii=False) + "\n")
            events += 1
    return events, t


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("typescript")
    ap.add_argument("timing")
    ap.add_argument("out")
    ap.add_argument("--cols", type=int, default=110)
    ap.add_argument("--rows", type=int, default=30)
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    if not os.path.exists(a.typescript) or not os.path.exists(a.timing):
        sys.exit("cast.py: the typescript or the timing file is missing")
    events, seconds = convert(a.typescript, a.timing, a.out, a.cols, a.rows, a.title)
    print(f"cast.py: {a.out}, {events} events, {seconds:.1f} s")


if __name__ == "__main__":
    main()
