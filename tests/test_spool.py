"""The line spool, without a call: python -I tests/test_spool.py -v

An agent writes one plain UTF-8 .txt file per line (over ssh, say); say.py writes JSON. Each rejection sits beside an
acceptance, so a check that stopped working turns a test red.
"""
import json, os, shutil, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge  # noqa: E402


class Spool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="spool-test-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def put(self, name, data, age=5.0):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_txt_is_read_as_plain_text(self):
        p = self.put("1762560000.000_ada.txt", "﻿Hello, class. 🌱\n")
        self.assertEqual(bridge.read_line_file(p), ("1762560000.000_ada", "Hello, class. 🌱"))

    def test_json_from_say_py(self):
        p = self.put("1.000_ab.json", json.dumps({"id": "ab", "text": " hi "}))
        self.assertEqual(bridge.read_line_file(p), ("ab", "hi"))

    def test_bad_files_give_no_text(self):
        self.assertIsNone(bridge.read_line_file(self.put("a.txt", b"\xff\xfe bad utf8"))[1])
        self.assertIsNone(bridge.read_line_file(self.put("b.txt", "   \n"))[1])
        self.assertIsNone(bridge.read_line_file(self.put("c.txt", "x" * (bridge.MAX_FILE + 1)))[1])
        self.assertEqual(bridge.read_line_file(self.put("c2.txt", "x" * bridge.MAX_FILE))[1], "x" * bridge.MAX_LINE)
        self.assertIsNone(bridge.read_line_file(self.put("d.json", "[1, 2]"))[1])
        self.assertIsNone(bridge.read_line_file(self.put("e.json", '{"id": "e"}'))[1])

    def test_symlink_is_not_followed(self):
        target = self.put("target.dat", "secret")
        link = os.path.join(self.d, "f.txt")
        os.symlink(target, link)
        self.assertIsNone(bridge.read_line_file(link)[1])
        self.assertEqual(bridge.read_line_file(self.put("g.txt", "fine"))[1], "fine")

    def test_ready_lines_order_settle_and_hidden(self):
        self.put("2.000_b.txt", "b")
        self.put("1.000_a.json", json.dumps({"text": "a"}))
        self.put(".3.000_c.tmp", "c")                 # being written: hidden
        self.put("4.000_d.txt", "d", age=0.0)         # just written: not settled yet
        self.put("5.000_e.dat", "e")                   # not a line file
        self.assertEqual(bridge.ready_lines(self.d), ["1.000_a.json", "2.000_b.txt"])
        self.assertEqual(bridge.ready_lines(self.d, now=time.time() + 1), ["1.000_a.json", "2.000_b.txt", "4.000_d.txt"])

    def test_a_fifo_or_a_link_is_taken_at_once_and_refused_without_blocking(self):
        os.mkfifo(os.path.join(self.d, "6.000_f.json"))
        os.mkfifo(os.path.join(self.d, "7.000_g.txt"))                 # no settle wait: it never settles into a line
        os.symlink(self.put("target.dat", "secret"), os.path.join(self.d, "8.000_h.txt"))
        self.put("9.000_i.txt", "fresh", age=0.0)
        self.assertEqual(bridge.ready_lines(self.d), ["6.000_f.json", "7.000_g.txt", "8.000_h.txt"])
        for n in ("6.000_f.json", "7.000_g.txt", "8.000_h.txt"):
            t = time.monotonic()
            self.assertEqual(bridge.read_line_file(os.path.join(self.d, n)), (n.rsplit(".", 1)[0], None))
            self.assertLess(time.monotonic() - t, 1.0, n + ": a FIFO with no writer must not block the read")
        self.assertEqual(bridge.read_line_file(os.path.join(self.d, "9.000_i.txt"))[1], "fresh")


if __name__ == "__main__":
    unittest.main()
