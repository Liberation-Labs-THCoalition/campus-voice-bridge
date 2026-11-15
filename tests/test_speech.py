"""Speech through the TTS server's Unix socket, and every way the server can fail: python -I -B tests/test_speech.py -v

tts_server.py's own listener and bridge.Speech, both real, with a fake Kokoro in the server and a fake CPU engine in the
bridge (no model files). The audio comes back over the socket; a server that is gone, refuses, answers with something
that isn't 24 kHz mono WAV, cuts its answer off or doesn't speak HTTP sends the bridge to its own CPU instead. Folders
that others could enter are refused at both ends. Each refusal sits beside an acceptance, so a check that stopped
working turns a test red.
"""
import asyncio, io, os, shutil, socket, stat, subprocess, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bridge  # noqa: E402
import tts_server  # noqa: E402


class FakeKokoro:
    def __init__(self):
        self.calls = 0

    def get_voices(self):
        return ["af_heart", "af_bella", "am_adam"]

    def get_voice_style(self, name):
        return np.zeros((510, 1, 256), np.float32)

    def create(self, text, voice, speed, lang):
        self.calls += 1
        t = np.arange(int(24000 * 0.3)) / 24000
        return (np.sin(2 * np.pi * 220 * t) * 0.3).astype(np.float32), 24000


def fake_engine():
    e = tts_server.Engine.__new__(tts_server.Engine)
    e.k, e.lock, e.model, e.threads = FakeKokoro(), threading.Lock(), "fake.onnx", 1
    e.voices = set(e.k.get_voices())
    return e


def wav(sr=24000, channels=1, seconds=0.2):
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, np.zeros((int(sr * seconds), channels), np.int16), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def failing(kind, hits):
    """A server that answers /speak the wrong way: kind says how."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            hits.append(kind)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if kind == "not-http":
                self.wfile.write(b"NOT-HTTP\r\n\r\n")
                self.close_connection = True
                return
            body = {"refused": b'{"error": "synthesis failed"}', "not-wav": b"this is not a wav file",
                    "stereo": wav(channels=2), "8khz": wav(sr=8000), "cut": wav(), "no-length": wav()}[kind]
            self.send_response(500 if kind == "refused" else 200)
            self.send_header("Content-Type", "audio/wav")
            if kind == "cut":
                self.send_header("Content-Length", str(len(body) + 5000))
            elif kind != "no-length":
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
    return H


class Socket(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="speech-test-")
        os.chmod(self.d, 0o700)
        self.folder = os.path.join(self.d, "tts")
        self.path = os.path.join(self.folder, "tts.sock")
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.d, ignore_errors=True)

    def serve(self, handler):
        self.assertIsNone(tts_server.socket_problem(self.path))
        srv = tts_server.listen(self.path, handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        return srv

    def speech(self):
        sp = bridge.Speech("af_heart", tts_socket=self.path)
        self.local = 0

        def local_say(text):
            self.local += 1
            return np.zeros(2400, np.int16)
        sp._local_say = local_say
        return sp

    def test_speech_comes_back_over_the_socket(self):
        engine = fake_engine()
        self.serve(tts_server.make_handler(engine))
        self.assertEqual(stat.S_IMODE(os.lstat(self.folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        sp = self.speech()
        samples, backend = asyncio.run(sp.say("Hello, class."))
        self.assertEqual((backend, samples.dtype, len(samples)), ("remote", np.int16, int(24000 * 0.3)))
        self.assertEqual((engine.k.calls, self.local), (1, 0))

    def test_every_kind_of_failed_answer_falls_back_to_this_cpu(self):
        for kind in ("refused", "not-wav", "stereo", "8khz", "cut", "no-length", "not-http"):
            with self.subTest(kind=kind):
                hits = []
                srv = self.serve(failing(kind, hits))
                sp = self.speech()
                samples, backend = asyncio.run(sp.say("Hello."))
                self.assertEqual((backend, self.local, hits), ("local", 1, [kind]))
                self.assertGreater(sp.remote_down_until, time.monotonic())
                asyncio.run(sp.say("And again."))                       # inside REMOTE_RETRY_S: not asked again
                self.assertEqual((self.local, hits), (2, [kind]))
                srv.shutdown()
                srv.server_close()
                self.servers.remove(srv)
                os.unlink(self.path)

    def test_no_server_or_not_a_socket_falls_back_too(self):
        os.mkdir(self.folder, 0o700)
        sp = self.speech()
        self.assertEqual(asyncio.run(sp.say("Nobody there."))[1], "local")
        with open(self.path, "w") as f:
            f.write("a plain file where the socket should be")
        sp = self.speech()
        self.assertEqual(asyncio.run(sp.say("A file, not a socket."))[1], "local")

    def test_the_server_refuses_a_folder_others_can_enter_a_live_socket_and_anything_else(self):
        os.mkdir(self.folder, 0o750)
        self.assertIn("want 0700", tts_server.socket_problem(self.path))
        os.chmod(self.folder, 0o700)
        self.assertIsNone(tts_server.socket_problem(self.path))
        with open(self.path, "w") as f:
            f.write("x")
        self.assertIn("isn't a socket", tts_server.socket_problem(self.path))
        os.unlink(self.path)
        srv = self.serve(tts_server.make_handler(fake_engine()))
        self.assertIn("another server is answering", tts_server.socket_problem(self.path))
        srv.shutdown()
        srv.server_close()                                              # leaves its socket file behind
        self.servers.remove(srv)
        self.assertTrue(os.path.lexists(self.path))
        self.assertIsNone(tts_server.socket_problem(self.path), "an earlier run's socket is cleared")
        self.assertFalse(os.path.lexists(self.path))

    def test_the_bridge_refuses_a_socket_folder_others_can_enter(self):
        os.mkdir(self.folder, 0o750)

        def run(*extra):
            p = subprocess.run([sys.executable, "-I", "-B", os.path.join(ROOT, "bridge.py"), "--url", "ws://127.0.0.1:9",
                                "--token-file", os.path.join(self.d, "no-such-token"), "--tts-socket", self.path, *extra],
                               capture_output=True, text=True, timeout=60)
            return p.returncode, p.stderr.strip().splitlines()[-1]     # the error line, not argparse's usage
        rc, err = run()
        self.assertEqual(rc, 2, err[-300:])
        self.assertIn("--tts-socket", err)
        self.assertIn("want 0700", err)
        os.chmod(self.folder, 0o700)
        rc, err = run()
        self.assertEqual(rc, 2, err[-300:])
        self.assertNotIn("--tts-socket", err)           # past the socket's check, to the token file's
        self.assertIn("no-such-token", err)

    def test_the_server_makes_missing_folders_private_whatever_the_umask(self):
        path = os.path.join(self.d, "a", "b", "tts.sock")
        old = os.umask(0o022)
        try:
            self.assertIsNone(tts_server.socket_problem(path))
        finally:
            os.umask(old)
        for p in (os.path.join(self.d, "a"), os.path.join(self.d, "a", "b")):
            self.assertEqual(stat.S_IMODE(os.lstat(p).st_mode), 0o700, p)


if __name__ == "__main__":
    unittest.main()
