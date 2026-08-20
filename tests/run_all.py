#!/usr/bin/env python3
"""Run every scheming check: each component's --selftest (isolated SCHEMING_HOME, no real
`claude`) plus the end-to-end pipeline test. Exit 1 if any fail. Stdlib only."""
import os, subprocess, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LIB = os.path.join(ROOT, "lib")

CHECKS = [
    ("scheming_core", [os.path.join(LIB, "scheming_core.py")]),
    ("search",   [os.path.join(LIB, "search.py"), "--selftest"]),
    ("ingest",   [os.path.join(LIB, "ingest.py"), "--selftest"]),
    ("triage",   [os.path.join(LIB, "triage.py"), "--selftest"]),
    ("groups",   [os.path.join(LIB, "groups.py"), "--selftest"]),
    ("setup",    [os.path.join(LIB, "setup.py"), "--selftest"]),
    ("label",    [os.path.join(LIB, "label.py"), "--selftest"]),
    ("catchup",  [os.path.join(LIB, "catchup.py"), "--selftest"]),
    ("pipeline", [os.path.join(ROOT, "tests", "test_pipeline.py")]),
]


def main():
    failed = []
    for name, argv in CHECKS:
        p = subprocess.run([sys.executable, *argv], capture_output=True, text=True)
        ok = p.returncode == 0
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed.append(name)
            sys.stderr.write(p.stdout + p.stderr + "\n")
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
