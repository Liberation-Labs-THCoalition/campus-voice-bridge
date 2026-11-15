"""The voice spec: a Kokoro voice name, or a blend such as "af_bella:60,am_adam:40".

    python -I tests/test_voice.py -v

bridge.blend_parts / bridge.kokoro_voice (speech on the bridge's own CPU) and tts_server.Engine.style (the TTS server)
must agree on the mix, wa * style(a) + (1 - wa) * style(b) as float32, and refuse anything else. A fake Kokoro stands
in for the model, so no model files are needed."""
import os, sys, unittest
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bridge  # noqa: E402
import tts_server  # noqa: E402


class FakeKokoro:
    def __init__(self):
        rng = np.random.default_rng(7)
        self.styles = {n: rng.standard_normal((510, 1, 256)).astype(np.float32) for n in ("af_heart", "af_bella", "am_adam", "bf_emma")}

    def get_voices(self):
        return list(self.styles)

    def get_voice_style(self, name):
        return self.styles[name]


class Voice(unittest.TestCase):
    def setUp(self):
        self.k = FakeKokoro()
        self.server = tts_server.Engine.__new__(tts_server.Engine)
        self.server.k, self.server.voices = self.k, set(self.k.get_voices())

    def test_a_name_passes_through(self):
        self.assertEqual(bridge.kokoro_voice(self.k, "af_heart"), "af_heart")
        self.assertEqual(self.server.style("af_heart"), "af_heart")

    def test_a_two_voice_blend_is_the_weighted_mix(self):
        ref = (0.6 * self.k.get_voice_style("af_bella") + 0.4 * self.k.get_voice_style("am_adam")).astype(np.float32)
        for got in (bridge.kokoro_voice(self.k, "af_bella:60,am_adam:40"), self.server.style("af_bella:60,am_adam:40"),
                    bridge.kokoro_voice(self.k, " af_bella:60 , am_adam:40 ")):
            self.assertEqual(got.dtype, np.float32)
            self.assertTrue(np.array_equal(got, ref))

    def test_three_voices_and_uneven_weights(self):
        got = self.server.style("af_bella:30,am_adam:45,bf_emma:25")
        ref = 0.3 * self.k.styles["af_bella"] + 0.45 * self.k.styles["am_adam"] + 0.25 * self.k.styles["bf_emma"]
        self.assertTrue(np.allclose(got, ref, atol=1e-6))

    def test_bad_specs_are_refused_by_both(self):
        for bad in ("af_bella:60,am_adam:45", "af_bella:100", "af_bella:0,am_adam:100", "af_bella:70,am_adam",
                    "af_bella:40,af_bella:30,am_adam:20,bf_emma:10", "AF_BELLA:60,am_adam:40", "af_bella:-5,am_adam:105"):
            with self.assertRaises(ValueError, msg=bad):
                bridge.blend_parts(bad)
            with self.assertRaises(ValueError, msg=bad):
                self.server.style(bad)

    def test_the_server_refuses_voices_it_doesnt_have(self):
        for bad in ("nobody", "af_bella:70,zz_nobody:30"):
            with self.assertRaises(ValueError, msg=bad):
                self.server.style(bad)

    def test_the_bridge_refuses_a_bad_blend_at_start(self):
        import subprocess
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(ROOT, "bridge.py"), "--url", "ws://127.0.0.1:1",
                            "--token-file", "/nonexistent", "--voice", "af_bella:60,am_adam:45"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2, p.stderr[-300:])
        self.assertIn("--voice", p.stderr)


if __name__ == "__main__":
    unittest.main()
