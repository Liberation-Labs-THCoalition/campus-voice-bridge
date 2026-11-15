"""relay.py, and the bridge's participant notes, without a call: python -I tests/test_relay.py -v

What each room message does (spoken, or why not), the notices and captions, the files the relay follows, its
arguments, and the bridge's RUN/participants.jsonl, both alone and from a real Bridge.connect() with a fake room. Last,
relay.py itself in both modes against the stand-in campus (tests/campus_stub.py): it signs in, reads only what comes
after its start, queues one line, posts a notice, and on SIGTERM signs its Matrix device out and exits 0, with no
message text, caption or name in its log. Every rejection sits beside an acceptance, so a check that stopped working
turns a test red. Synthetic test credentials only, in private temp folders removed at the end.
"""
import argparse, asyncio, json, logging, os, shutil, signal, socket, stat, subprocess, sys, tempfile, time, types
import unittest, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import bridge  # noqa: E402
import campus  # noqa: E402
import relay  # noqa: E402

ROWAN, ADA, OTHER = "@rowan:stub", "@ada:stub", "@someone:matrix.example"   # ADA: the relay's own account
logging.getLogger("relay").addHandler(logging.NullHandler())   # the refusals the tests make on purpose stay quiet


def msg(sender, body, msgtype="m.text", eid=None, **content):
    return {"type": "m.room.message", "sender": sender, "event_id": eid or "$" + body[:6],
            "content": dict(content, msgtype=msgtype, body=body)}


def private_tmp(test, prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    os.chmod(d, 0o700)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ---------------------------------------------------------------------------------------------- notes and routing
class Notes(unittest.TestCase):
    def test_note_lines_are_not_spoken_and_plain_lines_are(self):
        acts = relay.route_events([msg(ROWAN, "// captions please"), msg(ROWAN, "Hello, I'm Rowan."),
                                   msg(ROWAN, "//no space")], {ROWAN: "/run/rowan"}, ADA, 600)
        self.assertEqual(acts[0], ("skip", "note", ROWAN))
        self.assertEqual(acts[1], ("speak", ROWAN, "Hello, I'm Rowan."))
        self.assertEqual(acts[2], ("skip", "note", ROWAN), "no space is needed after the //")

    def test_note_marker_counts_only_at_the_start(self):
        self.assertTrue(relay.is_note("   // indented note"))
        self.assertFalse(relay.is_note("a line with // inside it"))
        self.assertFalse(relay.is_note("/ one slash"))


class Routing(unittest.TestCase):
    def test_only_allowlisted_text_from_others_is_spoken(self):
        events = [msg(OTHER, "not mine"), msg(ADA, "my own line"), msg(ROWAN, "(joined: Kestrel)", "m.notice"),
                  msg(ROWAN, "x" * 601), msg(ROWAN, "said once", eid="$sent1"), msg(ROWAN, "x" * 600, eid="$ok")]
        acts = relay.route_events(events, {ROWAN: "/run/rowan"}, ADA, 600, sent_ids={"$sent1"})
        self.assertEqual([a[1] for a in acts[:5]], ["not allowlisted", "own account", "not text", "too long", "own post"])
        self.assertTrue(all(a[0] == "skip" for a in acts[:5]))
        self.assertEqual(acts[5], ("speak", ROWAN, "x" * 600))

    def test_the_relays_own_account_is_never_spoken_even_when_allowlisted(self):
        acts = relay.route_events([msg(ADA, "a line the bridge posted"), msg(ADA, "(left: Kestrel)", "m.notice"),
                                   msg(ROWAN, "mine")], {ADA: "/run/ada", ROWAN: "/run/rowan"}, ADA, 600)
        self.assertEqual(acts, [("skip", "own account", ADA), ("skip", "not text", ADA), ("speak", ROWAN, "mine")])

    def test_a_line_a_bridge_posted_is_never_spoken_again(self):
        """The echo: a bridge on the account route, signed in as the agent's own mapped ID (not the relay's account),
        posts each line it speaks in the room, and the relay reads that post as the agent's message. The real
        Bridge.post_chat and Campus.post_line make the post; only the HTTP call is replaced."""
        sent = []
        c = object.__new__(campus.Campus)
        c.homeserver, c.mx_token = "https://matrix.example", "scratch-mx"
        c._call = lambda what, method, url, body, token=None: sent.append(body) or {"event_id": "$posted1"}
        asyncio.run(bridge.Bridge.post_chat(types.SimpleNamespace(chat_room="!class:stub", campus=c), "Hello", "l1"))
        self.assertEqual(sent, [{"msgtype": "m.text", "body": "Hello", campus.BRIDGE_LINE_KEY: "l1"}])
        echo = {"type": "m.room.message", "sender": ROWAN, "event_id": "$posted1", "content": sent[0]}
        acts = relay.route_events([echo, msg(ROWAN, "Hello")], {ROWAN: "/run/rowan"}, ADA, 600)
        self.assertEqual(acts, [("skip", "bridge line", ROWAN), ("speak", ROWAN, "Hello")],
                         "the bridge's post is skipped; the same words typed by the agent are still spoken")

    def test_an_edit_is_not_spoken_and_a_reply_loses_its_quote(self):
        reply = msg(ROWAN, "> <@someone:matrix.example> are you there?\n> second line\n\nYes, I'm here.",
                    **{"m.relates_to": {"m.in_reply_to": {"event_id": "$q"}}})
        edit = msg(ROWAN, "* Hello again", **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$h"}})
        own_quote = msg(ROWAN, "> a line I quote myself")             # no reply: the agent's own words
        acts = relay.route_events([reply, edit, own_quote], {ROWAN: "/run/rowan"}, ADA, 600)
        self.assertEqual(acts, [("speak", ROWAN, "Yes, I'm here."), ("skip", "edit", ROWAN),
                                ("speak", ROWAN, "> a line I quote myself")])


# ---------------------------------------------------------------------------------------------- notices and captions
class Notices(unittest.TestCase):
    def test_join_leave_present(self):
        line = lambda e, n: json.dumps({"t": 1.0, "event": e, "name": n})
        self.assertEqual(relay.parse_participant_line(line("joined", "Kestrel")), "(joined: Kestrel)")
        self.assertEqual(relay.parse_participant_line(line("left", " Juniper ")), "(left: Juniper)")
        self.assertEqual(relay.parse_participant_line(line("present", "Moss")), "(in the call: Moss)")

    def test_bad_lines_post_nothing(self):
        self.assertIsNone(relay.parse_participant_line("not json"))
        self.assertIsNone(relay.parse_participant_line(json.dumps({"event": "waved", "name": "Kestrel"})))
        self.assertIsNone(relay.parse_participant_line(json.dumps({"event": "joined", "name": "  "})))
        self.assertIsNone(relay.parse_participant_line(json.dumps(["joined", "Kestrel"])))
        self.assertEqual(relay.parse_participant_line(json.dumps({"event": "joined", "name": "Kestrel"})), "(joined: Kestrel)")

    def test_caption_line(self):
        self.assertEqual(relay.caption_text(json.dumps({"speaker": "Juniper", "text": " hi "})), "Juniper: hi")
        self.assertEqual(relay.caption_text(json.dumps({"text": "hi"})), "Someone: hi")
        self.assertIsNone(relay.caption_text(json.dumps({"speaker": "Juniper", "text": ""})))
        self.assertEqual(relay.caption_text(json.dumps({"speaker": "T", "text": "x" * 50}), 20), "T: " + "x" * 14 + "...")

    def test_the_same_notice_from_two_bridges_is_posted_once(self):
        n = relay.Notices(window=10)
        self.assertTrue(n.fresh("(joined: Kestrel)", now=100))
        self.assertFalse(n.fresh("(joined: Kestrel)", now=101), "the second bridge's copy")
        self.assertTrue(n.fresh("(left: Kestrel)", now=102))
        self.assertTrue(n.fresh("(joined: Kestrel)", now=111), "a later join is a new one")


# ---------------------------------------------------------------------------------------------- files
class Files(unittest.TestCase):
    def setUp(self):
        self.d = private_tmp(self, "relay-test-")

    def test_spool_writes_what_say_py_writes(self):
        line_id, p = relay.write_line(self.d, "Hello there", "abc12345")
        self.assertEqual((line_id, os.path.dirname(p)), ("abc12345", os.path.join(self.d, "say")))
        self.assertTrue(p.endswith("_abc12345.json"))
        with open(p) as f:
            self.assertEqual(json.load(f), {"id": "abc12345", "text": "Hello there"})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(os.path.join(self.d, "say")) if f.startswith(".")], [])
        self.assertEqual(bridge.read_line_file(p), ("abc12345", "Hello there"), "the bridge reads it as a line")

    def test_tail_reads_only_new_lines(self):
        f = os.path.join(self.d, "p.jsonl")
        with open(f, "w") as w:
            w.write('{"old": 1}\n')
        t = relay.Tail(f)
        self.assertEqual(t.lines(), [])
        with open(f, "a") as w:
            w.write('{"new": 2}\n')
        self.assertEqual(t.lines(), ['{"new": 2}'])
        self.assertEqual(t.lines(), [])

    def test_tail_waits_for_a_missing_file(self):
        t = relay.Tail(os.path.join(self.d, "later.jsonl"))
        self.assertEqual(t.lines(), [])
        with open(os.path.join(self.d, "later.jsonl"), "w") as w:
            w.write('{"a": 1}\n')
        self.assertEqual(t.lines(), ['{"a": 1}'])

    def test_tail_waits_for_a_whole_line_and_never_reads_a_link_or_a_fifo(self):
        f = os.path.join(self.d, "c.jsonl")
        t = relay.Tail(f)
        with open(f, "w") as w:
            w.write('{"a": 1}\n{"b": ')
        self.assertEqual(t.lines(), ['{"a": 1}'], "half a line waits")
        with open(f, "a") as w:
            w.write('2}\n')
        self.assertEqual(t.lines(), ['{"b": 2}'])
        secret = os.path.join(self.d, "secret.json")
        with open(secret, "w") as w:
            w.write('{"text": "not for the room"}\n')
        link, fifo = os.path.join(self.d, "link.jsonl"), os.path.join(self.d, "fifo.jsonl")
        os.symlink(secret, link)
        os.mkfifo(fifo)
        tl, tf = relay.Tail(link), relay.Tail(fifo)
        with open(secret, "a") as w:
            w.write('{"text": "nor this"}\n')
        started = time.monotonic()
        self.assertEqual((tl.lines(), tf.lines()), ([], []))
        self.assertLess(time.monotonic() - started, 1.0, "a FIFO with no writer must not block the read")


# ---------------------------------------------------------------------------------------------- arguments
class Arguments(unittest.TestCase):
    def test_map_gives_each_agent_its_own_bridge(self):
        got = relay.parse_map(["@rowan:stub=/srv/rowan/run", "@quill:stub"], "/srv/run")
        self.assertEqual(got, {"@rowan:stub": "/srv/rowan/run", "@quill:stub": "/srv/run"})
        for bad in (["rowan=/srv/rowan"], ["@rowan:stub", "@rowan:stub=/srv/x"], ["@rowan:stub=/srv/x", "@quill:stub=/srv/x"]):
            with self.assertRaises(ValueError, msg=bad):
                relay.parse_map(bad, "/srv/run")

    def test_until_is_the_next_hh_mm(self):
        t = time.localtime()
        evening = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 23, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(relay.parse_until("23:30", evening))[3:5], (23, 30))
        self.assertLess(relay.parse_until("23:30", evening) - evening, 3600)
        after_midnight = relay.parse_until("01:00", evening)
        self.assertEqual(time.localtime(after_midnight)[3:5], (1, 0))
        self.assertTrue(0 < after_midnight - evening <= 3 * 3600, "past midnight, not 22 hours ago")
        self.assertIsNone(relay.parse_until(""))
        for bad in ("25:00", "6pm", "18:5"):
            with self.assertRaises(ValueError):
                relay.parse_until(bad, evening)

    def test_a_run_folder_others_can_change_is_refused(self):
        d = private_tmp(self, "relay-args-")
        run = os.path.join(d, "run")
        argv = ["voice", "--campus", "http://127.0.0.1:9", "--cred-file", "unused", "--room", "!class:stub",
                "--map", ROWAN + "=" + run]
        ap = relay.build_parser()
        relay.check_args(ap, ap.parse_args(argv))           # its own folder: accepted
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(run, "say")).st_mode), 0o700)
        os.chmod(run, 0o777)
        try:
            with self.assertRaises(SystemExit) as e, open(os.devnull, "w") as quiet:
                sys.stderr, saved = quiet, sys.stderr
                try:
                    relay.check_args(ap, ap.parse_args(argv))
                finally:
                    sys.stderr = saved
            self.assertEqual(e.exception.code, 2)
        finally:
            os.chmod(run, 0o700)


# ---------------------------------------------------------------------------------------------- the bridge's notes
class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return " ".join(r.getMessage() for r in self.records)


class BridgeNotes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.d = private_tmp(self, "relay-bridge-")
        self.cap = Capture()
        logging.getLogger("bridge").addHandler(self.cap)
        self.addCleanup(logging.getLogger("bridge").removeHandler, self.cap)

    def rows(self):
        with open(os.path.join(self.d, "participants.jsonl")) as f:
            return [json.loads(x) for x in f]

    def test_joined_and_left_lines(self):
        me = types.SimpleNamespace(run_dir=self.d)
        bridge.Bridge.note_participant(me, "joined", types.SimpleNamespace(name="Kestrel", identity="guest:1"))
        bridge.Bridge.note_participant(me, "left", types.SimpleNamespace(name="", identity="guest:2"))
        bridge.Bridge.note_participant(me, "joined", types.SimpleNamespace(name="  Mo\x07ss ", identity="x"))
        self.assertEqual([(r["event"], r["name"]) for r in self.rows()],
                         [("joined", "Kestrel"), ("left", "guest:2"), ("joined", "Moss")])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d, "participants.jsonl")).st_mode), 0o600)
        self.assertNotIn("Kestrel", self.cap.text(), "names are never logged")

    def test_a_failed_note_never_raises(self):
        bridge.Bridge.note_participant(types.SimpleNamespace(run_dir=os.path.join(self.d, "no", "such")), "joined",
                                       types.SimpleNamespace(name="Kestrel", identity="x"))
        os.mkfifo(os.path.join(self.d, "participants.jsonl"))
        started = time.monotonic()
        bridge.Bridge.note_participant(types.SimpleNamespace(run_dir=self.d), "joined",
                                       types.SimpleNamespace(name="Kestrel", identity="x"))
        self.assertLess(time.monotonic() - started, 1.0, "a FIFO there must not block the call")
        self.assertIn("participants note (joined) not written", self.cap.text())
        self.assertNotIn("Kestrel", self.cap.text())

    async def test_the_real_join_notes_who_is_there_and_who_comes_and_goes(self):
        run = os.path.join(self.d, "run")
        token = os.path.join(self.d, "lk_token")
        with os.fdopen(os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write("scratch-guest-lk-token")
        a = argparse.Namespace(campus=None, host_id=None, cred_file=None, url="ws://127.0.0.1:1", token_file=token,
                               guest_link_file=None, guest_name=None, voice="af_heart", tts_socket=None,
                               models=os.path.join(ROOT, "models"), local_threads=1, fast_cpu=False, run_dir=run,
                               control_dir=[], rejoin=False)
        b = bridge.Bridge(a)
        kestrel, moss = types.SimpleNamespace(name="Kestrel", identity="guest:0123"), types.SimpleNamespace(name="",
                                                                                                    identity="u-42")
        handlers = {}
        b.room = types.SimpleNamespace(name="scratch-class", local_participant=types.SimpleNamespace(identity="ada"),
                                       remote_participants={"guest:0123": kestrel, "u-42": moss},
                                       on=lambda ev, cb: handlers.setdefault(ev, []).append(cb))

        async def connect(url, tok):
            pass

        async def publish_audio():
            return types.SimpleNamespace(sid="TR_audio")
        b.room.connect, b.publish_audio = connect, publish_audio
        await b.connect()
        juniper = types.SimpleNamespace(name="Juniper", identity="user-7")
        for cb in handlers.get("participant_connected", []):
            cb(juniper)
        for cb in handlers.get("participant_disconnected", []):
            cb(juniper)
        self.d = run
        self.assertEqual([(r["event"], r["name"]) for r in self.rows()],
                         [("present", "Kestrel"), ("present", "u-42"), ("joined", "Juniper"), ("left", "Juniper")])
        self.assertNotIn("Juniper", self.cap.text())


# ---------------------------------------------------------------------------------------------- relay.py itself
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stub:
    def __init__(self, tmp, fault="none"):
        self.port, self.record, cred = free_port(), os.path.join(tmp, "stub.jsonl"), os.path.join(tmp, "cred.json")
        with os.fdopen(os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump({"username": "ada-test", "password": "scratch-only-" + os.urandom(6).hex()}, f)
        with open(os.path.join(tmp, "lk.token"), "w") as f:
            f.write("scratch-lk-token")
        self.cred, self.url = cred, "http://127.0.0.1:%d" % self.port
        self.p = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "campus_stub.py"), "--port",
                                   str(self.port), "--record", self.record, "--cred-file", cred, "--lk-token-file",
                                   os.path.join(tmp, "lk.token"), "--fault", fault], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        assert self.p.stdout.readline().strip() == "ready"

    def rows(self, ev=None):
        try:
            with open(self.record) as f:
                rows = [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []
        return [r for r in rows if ev is None or r["ev"] == ev]

    def inject(self, sender, body, msgtype="m.text"):
        req = urllib.request.Request(self.url + "/_stub/inject", method="POST", data=json.dumps(
            {"sender": sender, "content": {"msgtype": msgtype, "body": body}}).encode())
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()

    def close(self):
        self.p.kill()
        self.p.wait()
        self.p.stdout.close()


class RelayAgainstTheStandIn(unittest.TestCase):
    def setUp(self):
        self.tmp = private_tmp(self, "relay-e2e-")
        self.stub = Stub(self.tmp)
        self.addCleanup(self.stub.close)

    def spawn(self, *mode_args):
        p = subprocess.Popen([sys.executable, "-I", "-B", os.path.join(ROOT, "relay.py"), mode_args[0], "--campus",
                              self.stub.url, "--cred-file", self.stub.cred, "--room", "!class:stub", "--every", "0.5"]
                             + list(mode_args[1:]), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def start(self, *mode_args):
        p = self.spawn(*mode_args)
        self.assertTrue(wait_for(lambda: self.stub.rows("messages") or p.poll() is not None), "it read the room")
        self.assertIsNone(p.poll(), "it is running")
        return p

    def stop(self, p):
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([r["ok"] for r in self.stub.rows("logout")], [True], "its Matrix device is signed out")
        return err

    def notices(self):
        return [r["text"] for r in self.stub.rows("send") if r["msgtype"] == "m.notice"]

    def test_voice(self):
        run = os.path.join(self.tmp, "run")
        say = os.path.join(run, "say")
        self.stub.inject(ROWAN, "said before the relay started")             # history: never spoken
        p = self.start("voice", "--map", ROWAN + "=" + run)
        for sender, body, kind in ((ROWAN, "// a note for the chat", "m.text"), (OTHER, "not hers to say", "m.text"),
                                   (ROWAN, "a notice of hers", "m.notice"), (ROWAN, "Hello from Rowan.", "m.text")):
            self.stub.inject(sender, body, kind)
        with open(os.path.join(run, "participants.jsonl"), "a") as f:
            f.write(json.dumps({"t": 1.0, "event": "joined", "name": "Kestrel Test"}) + "\n")
        self.assertTrue(wait_for(lambda: os.listdir(say) and self.notices()), "a line queued and a notice posted")
        time.sleep(1.5)                                                     # a few more reads: nothing else comes
        err = self.stop(p)
        texts = [bridge.read_line_file(os.path.join(say, n))[1] for n in sorted(os.listdir(say))]
        self.assertEqual(texts, ["Hello from Rowan."])
        self.assertEqual(self.notices(), ["(joined: Kestrel Test)"], "its own notice is never read back as a line")
        for words in ("said before", "a note", "not hers", "notice of hers", "Hello from", "Kestrel Test"):
            self.assertNotIn(words, err, "no message text or name in the log")
        self.assertIn("queued as", err)

    def test_a_lost_matrix_sign_in_ends_it_with_exit_1_and_still_signs_out(self):
        """The room read answered 401 (the device was signed out elsewhere): exit 1, not a loop of warnings, and the
        sign-out is still tried. Beside it, test_voice's stand-in with no fault keeps reading and exits 0."""
        self.stub = Stub(private_tmp(self, "relay-401-"), fault="messages401")
        self.addCleanup(self.stub.close)
        p = self.spawn("voice", "--map", ROWAN + "=" + os.path.join(self.tmp, "run"))
        try:            # 15 s: here it exits within a second (this whole file ran in 4.8 s on the lab's CPU box)
            _, err = p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("still running after its room read was answered 401")
        self.assertEqual(p.returncode, 1, err)
        self.assertEqual([r.get("code") for r in self.stub.rows("messages") if r["dir"] == "f"], [401])
        self.assertEqual([r["ok"] for r in self.stub.rows("logout")], [True], "the sign-out was tried")
        self.assertIn("HTTP 401", err)

    def test_captions(self):
        heard = os.path.join(self.tmp, "heard.jsonl")
        with open(heard, "w") as f:
            f.write(json.dumps({"speaker": "Juniper", "text": "said before the start"}) + "\n")
        p = self.start("captions", "--heard", heard)
        with open(heard, "a") as f:
            f.write(json.dumps({"speaker": "Juniper", "text": "hi there"}) + "\n")
        self.assertTrue(wait_for(lambda: self.notices()), "a caption posted")
        time.sleep(1.0)
        err = self.stop(p)
        self.assertEqual(self.notices(), ["Juniper: hi there"])
        self.assertNotIn("hi there", err)
        self.assertNotIn("said before", err)


if __name__ == "__main__":
    unittest.main()
