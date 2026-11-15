#!/usr/bin/env python3
"""Kokoro text-to-speech over HTTP on a Unix socket: an optional speech server for bridge.py.

    tts_server.py [--socket run/tts/tts.sock] [--threads 4] [--models DIR]

    GET  /health  -> {"ok": true, "model": ..., "voices": n, "threads": t}
    POST /speak   {"text": "...", "voice": "af_heart", "speed": 1.0}
                  voice may also be a blend, "af_bella:60,am_adam:40": two or three voices, percents summing to
                  100, mixed by their style vectors
                  -> 200 audio/wav (16-bit mono, the model's rate) | 400 with {"error": ...}

Run it on a machine whose CPU makes speech faster than real time. It listens on a Unix socket, never on a network port,
in a folder only this account can enter (0700: made so if it is missing, refused if anyone else can enter or change
it), because each line's text goes through it and the audio that comes back is played as the agent's voice. On the
bridge's own machine, give the bridge the same path (--tts-socket). On another machine, forward this socket to the
bridge's machine with SSH (README.md). The bridge falls back to Kokoro on its own CPU, same voice, whenever this can't
be reached.

The log records sizes and timings, never the text: these are someone's words in a class.
"""
import argparse, io, json, logging, os, re, socket, socketserver, stat, sys, threading, time
from http.server import BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                # -I leaves the script's own folder off the path
from control import make_private_dirs, private_dir_problem  # noqa: E402

MAX_CHARS = 1500
MAX_BODY = 16384
log = logging.getLogger("tts_server")


class Engine:
    def __init__(self, models, threads):
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.model = os.path.join(models, "kokoro-v1.0.onnx")
        sess = ort.InferenceSession(self.model, so, providers=["CPUExecutionProvider"])
        self.k = Kokoro.from_session(sess, os.path.join(models, "voices-v1.0.bin"))
        self.voices = set(self.k.get_voices())
        self.threads = threads
        self.lock = threading.Lock()   # one synthesis at a time: bounds the CPU this takes on a shared machine
        self.k.create("Warm up.", voice="af_heart", speed=1.0, lang="en-us")

    BLEND_PART = re.compile(r"([a-z]{2}_[a-z]+):(\d{1,3})")

    def style(self, spec):
        """What Kokoro.create takes for a voice spec: a named voice as is, or a blend as the weighted sum of the
        voices' style vectors (wa * style(a) + (1 - wa) * style(b), then float32). ValueError if neither."""
        if spec in self.voices:
            return spec
        parts = [self.BLEND_PART.fullmatch(p.strip()) for p in spec.split(",")]
        if not (2 <= len(parts) <= 3 and all(parts)) or any(m.group(1) not in self.voices for m in parts):
            raise ValueError("unknown voice")
        weights = [int(m.group(2)) for m in parts]
        if sum(weights) != 100 or min(weights) <= 0:
            raise ValueError("unknown voice")
        import numpy as np
        return np.asarray(sum(w / 100 * self.k.get_voice_style(m.group(1)) for m, w in zip(parts, weights)), np.float32)

    def speak(self, text, voice, speed):
        import soundfile as sf
        with self.lock:
            t0 = time.perf_counter()
            samples, sr = self.k.create(text, voice=voice, speed=speed, lang="en-us")
            dt = time.perf_counter() - t0
        buf = io.BytesIO()
        sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue(), len(samples) / sr, dt


def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        server_version = "agent-voice-bridge-tts/1"

        def log_message(self, fmt, *args):   # the default logs the request line to stderr; ours logs below
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self._json(404, {"error": "not found"})
            self._json(200, {"ok": True, "model": os.path.basename(engine.model), "voices": len(engine.voices),
                             "threads": engine.threads})

        def do_POST(self):
            if self.path != "/speak":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                return self._json(400, {"error": f"body must be 1..{MAX_BODY} bytes"})
            try:
                req = json.loads(self.rfile.read(n))
                text = str(req["text"]).strip()
                voice = str(req.get("voice", "af_heart"))
                speed = float(req.get("speed", 1.0))
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "expected JSON with text, voice, speed"})
            if not text or len(text) > MAX_CHARS:
                return self._json(400, {"error": f"text must be 1..{MAX_CHARS} characters"})
            try:
                style = engine.style(voice)
            except ValueError:
                return self._json(400, {"error": "unknown voice"})
            if not 0.5 <= speed <= 2.0:
                return self._json(400, {"error": "speed must be 0.5..2.0"})
            try:
                wav, secs, dt = engine.speak(text, style, speed)
            except Exception as e:
                log.error("synthesis failed: %s", type(e).__name__)
                return self._json(500, {"error": "synthesis failed"})
            log.info("speak chars=%d audio=%.2fs compute=%.2fs voice=%s", len(text), secs, dt, voice)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.send_header("X-Audio-Seconds", f"{secs:.3f}")
            self.end_headers()
            self.wfile.write(wav)
    return H


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # One line, not socketserver's traceback: a bridge that gave up waiting has closed its end (BrokenPipeError).
        log.warning("a request ended early (%s)", sys.exc_info()[0].__name__)


def socket_problem(path):
    """Why the server can't listen at path, or None. Its folder is made 0700 if missing (and so is any missing folder
    above it) and must pass private_dir_problem. A socket of this account's left by an earlier run is removed; a live
    one (another server answering there) or anything that isn't this account's socket is left alone and refused."""
    folder = os.path.dirname(path)
    try:
        make_private_dirs([folder])
    except OSError as e:
        return "%s: can't make it (%s)" % (folder, type(e).__name__)
    why = private_dir_problem(folder)
    if why:
        return "%s: %s" % (folder, why)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        return "%s is there and isn't a socket of this account's" % path
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(2)
        probe.connect(path)
        live = True
    except OSError:
        live = False
    finally:
        probe.close()
    if live:
        return "%s: another server is answering there" % path
    try:
        os.unlink(path)                 # nobody answers: an earlier run's socket
    except OSError as e:
        return "%s: can't remove an earlier run's socket (%s)" % (path, type(e).__name__)
    return None


def listen(path, handler):
    """The server on its socket, which is owner-only (0600) from the moment it exists."""
    old = os.umask(0o177)
    try:
        return UnixHTTPServer(path, handler)
    finally:
        os.umask(old)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=os.path.join(HERE, "run", "tts", "tts.sock"),
                    help="the Unix socket to listen on, in a folder only this account can enter (default: run/tts/tts.sock)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--models", default=os.path.join(HERE, "models"))
    a = ap.parse_args()
    path = os.path.abspath(a.socket)
    why = socket_problem(path)
    if why:
        ap.error("--socket: " + why)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    engine = Engine(a.models, a.threads)
    srv = listen(path, make_handler(engine))
    log.info("listening on %s, %d voices, %d threads", path, len(engine.voices), a.threads)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            os.unlink(path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
