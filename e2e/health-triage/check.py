#!/usr/bin/env python3
"""The health triage scenario's check for the recorded demo (e2e/demo/README.md): the e2e's own
run, lib/run.py, which sends R1 to R7 (and G1 to G4 with E2E_RAG=1) again and checks everything the
console set up. Run it through e2e/demo/run-demo.sh, or as `e2e.sh run`."""
import os
import sys

if __name__ == "__main__":
    lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "run.py")
    os.execv(sys.executable, [sys.executable, lib] + sys.argv[1:])
