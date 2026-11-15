#!/usr/bin/env python3
"""Hand the bridge a line: say.py "text" [--id ID] [--run-dir DIR].

Writes RUN/say/<time>_<id>.json, created exclusively under a hidden name and then renamed, so the bridge never reads
half a line. An agent can also write plain UTF-8 .txt files into RUN/say/ itself (one file per line); see bridge.py.
"""
import argparse, json, os, sys, time, uuid

RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run")


def write_line(run_dir, text, line_id=None):
    """Put one line in RUN/say/ for the bridge; returns (line id, the file's path). relay.py uses this too, so a line
    from the chat lands exactly as one from here does."""
    line_id = line_id or uuid.uuid4().hex[:8]
    d = os.path.join(run_dir, "say")
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = os.path.join(d, f".tmp_{line_id}")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump({"id": line_id, "text": text}, f)
    path = os.path.join(d, f"{time.time():.3f}_{line_id}.json")
    os.rename(tmp, path)
    return line_id, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--id", default=None, help="the line's id in the status log (default: random)")
    ap.add_argument("--run-dir", default=RUN, help="the bridge's run folder (default: run/ beside this file)")
    a = ap.parse_args()
    print(write_line(a.run_dir, a.text, a.id)[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
