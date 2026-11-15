"""The control files and the drop, without a call: python -I tests/test_control.py -v

control.py's rules for a drop file, and a Bridge built for real with a fake room, audio source and speech engine, so
text-before-voice, the leave sequence, the cut line, the sticky marker, --rejoin and the stop file run in-process.
The join itself runs too (Bridge.connect, with a fake room and a fake campus): its drop checks, the account route's
fall back to the guest link, and the ends of a call that count as a drop (removed by the host, a second join under the
same identity). The room's stats can count packets, so a line can come out "spoken", "not_spoken" or "unknown".
Every rejection sits beside an acceptance, so a check that stopped working turns a test red.
"""
import argparse, asyncio, io, json, logging, os, shutil, signal, socket, stat, sys, tempfile, threading, time, types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np  # noqa: E402
import bridge  # noqa: E402
import control  # noqa: E402
from livekit import rtc  # noqa: E402

VIA = "file test"


async def until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def readf(p):
    with open(p) as f:
        return f.read()


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TempDir(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="control-test-")
        os.chmod(self.d, 0o700)
        self.cap = Capture()
        logging.getLogger("bridge").addHandler(self.cap)
        logging.getLogger("bridge").setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger("bridge").removeHandler(self.cap)
        for root, dirs, _ in os.walk(self.d):
            for x in dirs:
                os.chmod(os.path.join(root, x), 0o700)
        shutil.rmtree(self.d, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------- control.py
class Printable(unittest.TestCase):
    def test_printable_caps_and_cleans(self):
        self.assertEqual(control.printable(b"drop"), "drop")
        self.assertEqual(len(control.printable(b"x" * 500)), 64)
        self.assertEqual(control.printable("a\x1bb\ncé"), "a?b?c?")


class Files(TempDir):
    def test_a_small_regular_file_is_a_drop_and_is_taken(self):
        p = os.path.join(self.d, "drop")
        open(p, "w").close()
        self.assertEqual(control.take_drop_file(p), (True, None))
        self.assertFalse(os.path.lexists(p))
        self.assertEqual(control.take_drop_file(p), (False, None))

    def test_links_folders_fifos_and_big_files_are_left_alone(self):
        target = os.path.join(self.d, "target")
        with open(target, "w") as f:
            f.write("someone's own file")
        link = os.path.join(self.d, "drop")
        os.symlink(target, link)
        self.assertEqual(control.take_drop_file(link), (False, "not a regular file"))
        self.assertTrue(os.path.islink(link))
        self.assertEqual(readf(target), "someone's own file")
        os.unlink(link)
        os.mkdir(link)
        self.assertEqual(control.take_drop_file(link), (False, "not a regular file"))
        os.rmdir(link)
        os.mkfifo(link)
        t = time.monotonic()
        self.assertEqual(control.take_drop_file(link), (False, "not a regular file"))
        self.assertLess(time.monotonic() - t, 1.0)          # never opened, so never blocked
        os.unlink(link)
        with open(link, "wb") as f:
            f.write(b"x" * (control.MAX_DROP_FILE + 1))
        self.assertEqual(control.take_drop_file(link), (False, "bigger than %d bytes" % control.MAX_DROP_FILE))
        with open(link, "wb") as f:
            f.write(b"x" * control.MAX_DROP_FILE)
        self.assertEqual(control.take_drop_file(link), (True, None))

    def test_a_drop_it_cant_remove_still_counts(self):
        sub = os.path.join(self.d, "ro")
        os.mkdir(sub)
        open(os.path.join(sub, "drop"), "w").close()
        os.chmod(sub, 0o500)
        found, note = control.take_drop_file(os.path.join(sub, "drop"))
        self.assertTrue(found)
        self.assertIn("can't remove it", note)


# ---------------------------------------------------------------------------------------- the bridge, with fakes
class FakeLP:
    def __init__(self, ev, hang=False):
        self.ev, self.hang, self.identity = ev, hang, "ada"
        self.chat_delay = 0.0

    async def unpublish_track(self, sid):
        self.ev.append(("unpublish", sid))
        if self.hang:
            await asyncio.sleep(30)

    async def send_text(self, text, topic=None):
        await asyncio.sleep(self.chat_delay)
        self.ev.append(("chat", len(text)))


class FakeStat:
    """One entry of the publisher's stats, shaped as livekit's: HasField, then the outbound audio packet count."""

    def __init__(self, sent):
        self.outbound_rtp = types.SimpleNamespace(stream=types.SimpleNamespace(kind="audio"),
                                                  sent=types.SimpleNamespace(packets_sent=sent))

    def HasField(self, name):
        return name == "outbound_rtp"


class FakeRoom:
    def __init__(self, ev, hang_disconnect=False, hang_unpublish=False, connect_s=0.0):
        self.ev, self.hang, self.name, self.connect_s = ev, hang_disconnect, "fake-class", connect_s
        self.local_participant = FakeLP(ev, hang_unpublish)
        self.remote_participants = {}   # nobody else in the call (test_relay.py has a call with people in it)
        self.handlers = {}
        self.packets = None             # a function giving the audio packets sent so far; None: no audio stats yet
        self.stats_fail = False

    def on(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    def emit(self, event, *args):
        for cb in self.handlers.get(event, []):
            cb(*args)

    async def connect(self, url, token):
        self.ev.append(("connect", url, token))
        await asyncio.sleep(self.connect_s)

    async def disconnect(self):
        self.ev.append("disconnect")
        if self.hang:
            await asyncio.sleep(30)

    async def get_rtc_stats(self):
        if self.stats_fail:
            raise RuntimeError("the room isn't connected")      # what livekit 1.2.1 raises once the room is gone
        return types.SimpleNamespace(publisher_stats=[FakeStat(self.packets())] if self.packets else [])


class FakeSource:
    def __init__(self, ev):
        self.ev, self.frames = ev, 0

    async def capture_frame(self, frame):
        if self.frames == 0:
            self.ev.append("first_frame")
        self.frames += 1
        await asyncio.sleep(0.01)

    def clear_queue(self):
        self.ev.append("clear_queue")

    async def wait_for_playout(self):
        pass


class FakeSpeech:
    """Speech that takes `delay` and gives `seconds` of a loud tone; `fail` sentences raise first (no model files, a
    voice Kokoro doesn't have); remote=False takes the bridge's make-the-whole-line path."""

    def __init__(self, delay=0.0, seconds=0.5, fail=0, remote=True):
        self.delay, self.seconds, self.fail, self.remote = delay, seconds, fail, remote

    def remote_up(self):
        return self.remote

    async def say(self, text):
        await asyncio.sleep(self.delay)
        if self.fail:
            self.fail -= 1
            raise FileNotFoundError("no model files")
        t = np.arange(int(bridge.SR * self.seconds)) / bridge.SR
        return (np.sin(2 * np.pi * 200 * t) * 6000).astype(np.int16), "remote"


class FakeCampus:
    def __init__(self, ev):
        self.ev, self.mx_token = ev, "mx-token"

    def sign_out(self):
        self.ev.append(("sign_out", self.mx_token is not None))
        self.mx_token = None

    def post_line(self, room, text, msgtype="m.text", extra=None):
        self.ev.append(("matrix", len(text)))


class FakeAccount(FakeCampus):
    """The account route's campus for the real connect(): a sign-in that may wait or fail, and a class join."""

    def __init__(self, ev, fail=None, sign_in_s=0.0):
        super().__init__(ev)
        self.fail, self.sign_in_s, self.mx_token = fail, sign_in_s, None

    def sign_in(self, user, password):
        time.sleep(self.sign_in_s)
        self.mx_token = "mx-token"          # the campus has opened a Matrix device by now, even when it then refuses
        self.ev.append("sign_in")
        if self.fail == "sign_in":
            raise bridge.CampusError("campus sign-in", 403)
        return "@ada:stub"

    def join_class(self, host_id):
        self.ev.append("join_class")
        return {"livekitUrl": "wss://account.example", "token": "account-lk-token", "roomName": "scratch-class",
                "matrixRoomId": "!class:stub", "matrixStatus": "joined"}

    def join_room(self, room):
        self.ev.append("join_room")


class DrippingHomeserver:
    """A homeserver on loopback that takes a request and then answers it a byte at a time, each byte well inside the
    campus's per-operation timeout, so the answer never finishes (found in review). It records each request's method and
    path."""

    def __init__(self, every_s=0.2):
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.srv.settimeout(0.2)
        self.url = "http://127.0.0.1:%d" % self.srv.getsockname()[1]
        self.every_s, self.stop, self.requests = every_s, threading.Event(), []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                continue
            threading.Thread(target=self.drip, args=(c,), daemon=True).start()

    def drip(self, c):
        with c:
            try:
                c.settimeout(5)
                self.requests.append(tuple(c.recv(65536).split(b" ", 2)[:2]))
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
                while not self.stop.wait(self.every_s):
                    c.sendall(b" ")
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.srv.close()


def dripping_campus(test, timeout=1):
    """A real Campus, signed in as far as sign_out can tell, whose homeserver drips; closed when the test ends."""
    hs = DrippingHomeserver()
    test.addCleanup(hs.close)
    campus = bridge.Campus(hs.url, timeout=timeout)
    campus.mx_token, campus.homeserver = "scratch-mx-token", hs.url
    return campus, hs


def args(run_dir, **over):
    a = dict(campus=None, host_id=None, cred_file=None, url="ws://127.0.0.1:1", token_file="unused",
             guest_link_file=None, guest_name=None, voice="af_heart", tts_socket=None,
             models=os.path.join(ROOT, "models"), local_threads=1, fast_cpu=False, run_dir=run_dir, control_dir=[],
             rejoin=False)
    a.update(over)
    return argparse.Namespace(**a)


class BridgeDrop(TempDir):
    def setUp(self):
        super().setUp()
        self.run = os.path.join(self.d, "run")
        self.marker = os.path.join(self.run, "dropped")

    def make(self, connect_s=0.0, speech=None, room=None, real_connect=False, **over):
        """A Bridge with a fake room, source and speech. connect() is replaced by a stand-in unless real_connect, in
        which case the real one runs against the fake room (and whatever campus the test puts in), and only the
        track's publishing is faked."""
        b = bridge.Bridge(args(self.run, **over))
        self.ev = []
        b.room = room or FakeRoom(self.ev, connect_s=connect_s)
        b.source = FakeSource(self.ev)
        b.speech = speech or FakeSpeech()
        self.joins = 0

        async def connect():
            self.joins += 1
            self.ev.append("join")
            await asyncio.sleep(connect_s)
            b.in_call = True
            b.audio_pub = types.SimpleNamespace(sid="TR_audio")
            self.ev.append("joined")

        async def publish_audio():
            self.ev.append("joined")
            return types.SimpleNamespace(sid="TR_audio")

        if real_connect:
            b.publish_audio = publish_audio
        else:
            b.connect = connect
        return b

    def token_file(self):
        """A LiveKit token in an owner-only file, for the real connect()'s guest route."""
        p = os.path.join(self.d, "lk_token")
        if not os.path.exists(p):
            with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write("scratch-guest-lk-token\n")
        return p

    def connects(self):
        return [e for e in self.ev if isinstance(e, tuple) and e[0] == "connect"]

    def rows(self):
        p = os.path.join(self.run, "status.jsonl")
        return [json.loads(x) for x in readf(p).splitlines()] if os.path.exists(p) else []

    def row(self, **match):
        return [r for r in self.rows() if all(r.get(k) == v for k, v in match.items())]

    def say(self, text, line_id):
        p = os.path.join(self.run, "say", "%.3f_%s.json" % (time.time(), line_id))
        with open(p, "w") as f:
            json.dump({"id": line_id, "text": text}, f)

    async def test_text_first_then_voice_and_no_voice_without_text(self):
        b = self.make()
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        self.say("Posted first, then spoken.", "order1")
        self.assertTrue(await until(lambda: bool(self.row(id="order1")), 5))
        chats = [i for i, e in enumerate(self.ev) if isinstance(e, tuple) and e[0] == "chat"]
        self.assertTrue(chats and "first_frame" in self.ev and chats[0] < self.ev.index("first_frame"), self.ev)

        async def refuse(text, topic=None):
            raise ConnectionError("chat down")
        b.room.local_participant.send_text = refuse
        frames = b.source.frames
        self.say("This one can't be posted, so it isn't spoken.", "nochat1")
        self.assertTrue(await until(lambda: bool(self.row(id="nochat1")), 5))
        self.assertEqual(self.row(id="nochat1")[0]["verdict"], "chat_failed")
        self.assertEqual(b.source.frames, frames)
        self.assertNotIn("posted", json.dumps(self.rows()), "the status log holds no line's text")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    async def test_a_drop_while_idle_leaves_in_order_and_writes_the_marker(self):
        b = self.make()
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        b.request_drop(VIA)
        b.request_drop(VIA)                                         # a second drop is only logged
        self.assertEqual(await asyncio.wait_for(task, 2), bridge.DROP_EXIT)
        self.assertEqual(self.ev[self.ev.index("joined") + 1:], ["clear_queue", ("unpublish", "TR_audio"), "disconnect"])
        d = self.row(id="drop", event="drop")
        self.assertEqual(len(d), 1)
        self.assertEqual((d[0]["via"], d[0]["in_call"]), (VIA, True))
        left = self.row(id="drop", event="left")[0]
        self.assertEqual((left["disconnect"], left["unpublished"]), ("ok", 1))
        self.assertLess(left["left_s"], 1.0)
        st = os.stat(self.marker)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(json.loads(readf(self.marker))["via"], VIA)
        await b.shutdown()

    async def test_a_drop_while_a_sentence_is_being_made_does_not_wait_for_it(self):
        b = self.make(speech=FakeSpeech(delay=30))
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        self.say("A line whose speech takes thirty seconds to make.", "slow1")
        self.assertTrue(await until(lambda: b.current_line == "slow1"))
        await asyncio.sleep(0.2)
        t = time.monotonic()
        b.request_drop(VIA)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertEqual(self.row(id="slow1")[0]["verdict"], "dropped")
        self.assertEqual(self.row(id="drop", event="left")[0]["cut_line"], "slow1")
        self.assertIn("disconnect", self.ev)
        await b.shutdown()

    async def test_a_drop_while_speaking_cuts_the_sound(self):
        b = self.make(speech=FakeSpeech(seconds=5.0))
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        self.say("One long sentence that plays for five seconds, more or less, if nothing stops it.", "long1")
        self.assertTrue(await until(lambda: b.source.frames > 20))
        b.request_drop(VIA)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        frames = b.source.frames
        await asyncio.sleep(0.3)
        self.assertEqual(b.source.frames, frames)                  # nothing more after the drop
        self.assertLess(frames, 200)                               # 5 s would be 500 frames
        self.assertEqual(self.row(id="long1")[0]["verdict"], "dropped")
        await b.shutdown()

    async def test_a_hanging_disconnect_or_unpublish_is_bounded(self):
        ev = []
        room = FakeRoom(ev, hang_disconnect=True, hang_unpublish=True)
        b = self.make(room=room)
        self.ev = ev
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        t = time.monotonic()
        b.request_drop(VIA)
        self.assertEqual(await asyncio.wait_for(task, 6), bridge.DROP_EXIT)
        self.assertLess(time.monotonic() - t, bridge.UNPUBLISH_WAIT_S + bridge.DISCONNECT_WAIT_S + 1.0)
        left = self.row(id="drop", event="left")[0]
        self.assertEqual((left["disconnect"], left["unpublished"]), ("timeout", 0))
        await b.shutdown()

    async def test_a_drop_file_in_run_control(self):
        b = self.make()
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        p = os.path.join(self.run, "control", "drop")
        open(p, "w").close()
        self.assertEqual(await asyncio.wait_for(task, 2), bridge.DROP_EXIT)
        self.assertFalse(os.path.lexists(p))
        self.assertEqual(self.row(id="drop", event="drop")[0]["via"], "file " + p)
        await b.shutdown()

    async def test_a_control_dir_link_is_ignored_and_a_real_file_there_drops(self):
        cdir = os.path.join(self.d, "other-control")
        os.mkdir(cdir)
        target = os.path.join(self.d, "precious")
        with open(target, "w") as f:
            f.write("keep me")
        os.symlink(target, os.path.join(cdir, "drop"))
        b = self.make(control_dir=[cdir])
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        await asyncio.sleep(0.4)
        self.assertFalse(task.done())
        self.assertFalse(b.dropped.is_set())
        self.assertEqual(readf(target), "keep me")
        self.assertEqual(len([r for r in self.cap.records if "ignoring" in r.getMessage()]), 1)     # said once
        os.unlink(os.path.join(cdir, "drop"))
        open(os.path.join(cdir, "drop"), "w").close()
        self.assertEqual(await asyncio.wait_for(task, 2), bridge.DROP_EXIT)
        await b.shutdown()

    async def test_after_a_drop_it_stays_out_until_rejoin(self):
        os.makedirs(self.run, mode=0o700, exist_ok=True)
        with open(self.marker, "w") as f:
            json.dump({"at": "2026-11-10T14:05:00-0800", "via": VIA}, f)
        b = self.make()
        self.assertEqual(await asyncio.wait_for(b.run(), 2), bridge.DROP_EXIT)
        self.assertEqual(self.joins, 0)
        self.assertTrue(any("stays out until it is started with --rejoin" in r.getMessage() for r in self.cap.records))
        await b.shutdown()
        queued = os.path.join(self.run, "say", "0001-before-the-drop.txt")
        with open(queued, "w") as f:
            f.write("a line queued before the drop")
        b = self.make(rejoin=True)
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        self.assertFalse(os.path.lexists(self.marker))
        self.assertFalse(os.path.exists(queued))                   # held, not spoken
        held = os.listdir(os.path.join(self.run, "say_held"))
        self.assertEqual(len(held), 1)
        self.assertEqual(os.listdir(os.path.join(self.run, "say_held", held[0])), ["0001-before-the-drop.txt"])
        self.assertEqual(self.row(id="rejoin", event="held")[0]["lines"], 1)
        b.request_drop(VIA)
        self.assertEqual(await asyncio.wait_for(task, 2), bridge.DROP_EXIT)
        await b.shutdown()

    async def test_control_dirs_others_could_change_are_refused(self):
        ok = os.path.join(self.d, "ok")
        os.mkdir(ok, 0o700)
        self.assertIsNone(bridge.unsafe_dir(ok))
        loose = os.path.join(self.d, "loose")
        os.mkdir(loose)
        os.chmod(loose, 0o777)
        self.assertIn("group or others can write it", bridge.unsafe_dir(loose))
        grp_only = os.path.join(self.d, "grp")
        os.mkdir(grp_only)
        os.chmod(grp_only, 0o770)
        real = control.group_is_ours
        try:
            control.group_is_ours = lambda gid: True              # the user's own group, with no other member
            self.assertIsNone(bridge.unsafe_dir(grp_only))
            control.group_is_ours = lambda gid: False             # a group with another account in it
            self.assertIn("group or others can write it", bridge.unsafe_dir(grp_only))
        finally:
            control.group_is_ours = real
        link = os.path.join(self.d, "link")
        os.symlink(ok, link)
        self.assertIn("link", bridge.unsafe_dir(link))
        f = os.path.join(self.d, "file")
        open(f, "w").close()
        self.assertEqual(bridge.unsafe_dir(f), "not a folder")
        self.assertIn("can't look at it", bridge.unsafe_dir(os.path.join(self.d, "missing")))
        inner = os.path.join(loose, "inner")
        os.mkdir(inner, 0o700)
        self.assertIn("can be changed by group or others", bridge.unsafe_dir(inner))
        os.chmod(loose, 0o1777)                                    # sticky, as /tmp is
        self.assertIsNone(bridge.unsafe_dir(inner))

    async def test_a_drop_file_waiting_at_the_start_keeps_it_out(self):
        os.makedirs(os.path.join(self.run, "control"), mode=0o700, exist_ok=True)
        p = os.path.join(self.run, "control", "drop")
        open(p, "w").close()
        b = self.make()
        self.assertEqual(await asyncio.wait_for(b.run(), 2), bridge.DROP_EXIT)
        self.assertEqual(self.joins, 0)
        self.assertTrue(os.path.exists(self.marker))
        await b.shutdown()
        os.unlink(self.marker)
        open(p, "w").close()
        b = self.make(rejoin=True)
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        self.assertFalse(os.path.lexists(p))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    async def test_a_drop_during_the_join_waits_for_it_then_leaves(self):
        b = self.make(connect_s=0.8)
        task = asyncio.create_task(b.run())
        await until(lambda: "join" in self.ev)
        b.request_drop(VIA)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        self.assertEqual(self.ev.count("disconnect"), 1)
        self.assertEqual(self.joins, 1)
        await b.shutdown()

    async def test_the_account_route_is_signed_out_by_a_drop(self):
        b = self.make()
        b.campus = FakeCampus(self.ev)
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        b.request_drop(VIA)
        self.assertEqual(await asyncio.wait_for(task, 2), bridge.DROP_EXIT)
        self.assertIn(("sign_out", True), self.ev)
        self.assertGreater(self.ev.index(("sign_out", True)), self.ev.index("disconnect"))     # the call first
        self.assertTrue(self.row(id="drop", event="left")[0]["matrix_signed_out"])
        await b.shutdown()

    async def test_stop_still_cuts_only_the_line(self):
        b = self.make(speech=FakeSpeech(seconds=5.0))
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        self.say("A long line that a stop request cuts, and only this line.", "cut1")
        self.assertTrue(await until(lambda: b.source.frames > 20))
        open(os.path.join(self.run, "control", "stop"), "w").close()
        self.assertTrue(await until(lambda: bool(self.row(id="cut1")), 5))
        self.assertEqual(self.row(id="cut1")[0]["verdict"], "interrupted")
        b.speech.seconds = 0.3
        self.say("And the next one plays.", "next1")
        self.assertTrue(await until(lambda: bool(self.row(id="next1")), 5))
        self.assertNotIn(self.row(id="next1")[0]["verdict"], ("dropped", "interrupted"))
        self.assertFalse(b.dropped.is_set())
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()


    # --- the real join (Bridge.connect) -------------------------------------------------------------------------------
    async def test_the_real_join_publishes_and_logs_no_private_room_name(self):
        b = self.make(real_connect=True, token_file=self.token_file())
        b.room.name = "private-0123456789ab"
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        self.assertEqual(self.connects(), [("connect", "ws://127.0.0.1:1", "scratch-guest-lk-token")])
        self.assertTrue(b.in_call)
        logged = " ".join(r.getMessage() for r in self.cap.records)
        self.assertIn("joined room private-...", logged)
        self.assertNotIn("0123456789ab", logged, "a private call's room is named for the start of its link")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    async def test_a_drop_during_the_sign_in_never_joins_and_signs_out(self):
        b = self.make(real_connect=True, campus="https://campus.example", host_id="h", cred_file="unused")
        account = FakeAccount(self.ev, sign_in_s=0.6)
        with mock.patch.object(bridge, "Campus", lambda url: account), \
                mock.patch.object(bridge, "read_credentials", lambda p: ("ada", "pw")):
            task = asyncio.create_task(b.run())
            await asyncio.sleep(0.2)                                # the sign-in is under way
            b.request_drop(VIA)
            self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        self.assertEqual(self.connects(), [], "no call is joined after a drop during the sign-in")
        self.assertNotIn("join_class", self.ev, "nor the class")
        self.assertIn(("sign_out", True), self.ev)
        await b.shutdown()

    async def test_a_drop_during_the_livekit_join_leaves_without_publishing(self):
        b = self.make(real_connect=True, connect_s=0.6, token_file=self.token_file())
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: bool(self.connects())))
        b.request_drop(VIA)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        self.assertNotIn("joined", self.ev, "nothing is published after a drop during the join")
        self.assertEqual(self.ev.count("disconnect"), 1)
        await b.shutdown()

    async def test_the_account_route_falls_back_to_the_guest_route(self):
        b = self.make(real_connect=True, campus="https://campus.example", host_id="h", cred_file="unused",
                      token_file=self.token_file())
        account = FakeAccount(self.ev, fail="sign_in")
        with mock.patch.object(bridge, "Campus", lambda url: account), \
                mock.patch.object(bridge, "read_credentials", lambda p: ("ada", "pw")):
            task = asyncio.create_task(b.run())
            self.assertTrue(await until(lambda: "joined" in self.ev))
        self.assertEqual(self.connects(), [("connect", "ws://127.0.0.1:1", "scratch-guest-lk-token")])
        self.assertLess(self.ev.index(("sign_out", True)), self.ev.index(self.connects()[0]),
                        "the account's Matrix device is signed out before the guest route joins")
        self.assertEqual((b.chat, b.chat_room), ("lk.chat", None))
        self.say("Posted to LiveKit's chat, not the class's Matrix room.", "fallback1")
        self.assertTrue(await until(lambda: bool(self.row(id="fallback1")), 5))
        self.assertEqual(self.row(id="fallback1")[0]["chat"], "lk.chat")
        self.assertFalse(any(isinstance(e, tuple) and e[0] == "matrix" for e in self.ev))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    async def test_the_account_route_with_no_guest_link_ends_the_run(self):
        b = self.make(real_connect=True, campus="https://campus.example", host_id="h", cred_file="unused",
                      url=None, token_file=None)
        account = FakeAccount(self.ev, fail="sign_in")
        with mock.patch.object(bridge, "Campus", lambda url: account), \
                mock.patch.object(bridge, "read_credentials", lambda p: ("ada", "pw")):
            with self.assertRaises(bridge.CampusError):
                await asyncio.wait_for(b.run(), 3)
        self.assertEqual(self.connects(), [])
        await b.shutdown()

    # --- the ends of a call ------------------------------------------------------------------------------------------
    async def kicked(self, reason, via):
        b = self.make(real_connect=True, token_file=self.token_file())
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        b.room.emit("disconnected", reason)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        self.assertEqual(json.loads(readf(self.marker))["via"], via)
        self.assertEqual(self.row(id="drop", event="drop")[0]["via"], via)
        await b.shutdown()
        b = self.make(real_connect=True, token_file=self.token_file())         # a restart without --rejoin
        self.assertEqual(await asyncio.wait_for(b.run(), 2), bridge.DROP_EXIT)
        self.assertEqual(self.connects(), [], "after a removal, nothing but --rejoin brings it back")
        await b.shutdown()

    async def test_removed_by_the_host_is_a_drop_that_sticks(self):
        await self.kicked(rtc.DisconnectReason.PARTICIPANT_REMOVED, "removed by the host")

    async def test_a_second_join_under_its_identity_is_a_drop_too(self):
        await self.kicked(rtc.DisconnectReason.DUPLICATE_IDENTITY, "another join under this identity")

    async def test_any_other_end_of_the_call_exits_3_and_leaves_no_marker(self):
        b = self.make(real_connect=True, token_file=self.token_file())
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        b.room.emit("disconnected", rtc.DisconnectReason.SIGNAL_CLOSE)
        self.assertEqual(await asyncio.wait_for(task, 3), 3)
        self.assertFalse(os.path.lexists(self.marker))
        self.assertEqual(self.row(id="disconnect", event="disconnected")[0]["reason"], "SIGNAL_CLOSE")
        await b.shutdown()

    async def test_the_end_of_the_call_cuts_the_line_being_spoken(self):
        b = self.make(real_connect=True, token_file=self.token_file(), speech=FakeSpeech(seconds=5.0))
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        self.say("A long line, and the call ends while it plays.", "ended1")
        self.assertTrue(await until(lambda: b.source.frames > 20))
        t = time.monotonic()
        b.room.emit("disconnected", rtc.DisconnectReason.SIGNAL_CLOSE)
        self.assertEqual(await asyncio.wait_for(task, 3), 3)
        self.assertLess(time.monotonic() - t, 1.5, "not the rest of the line played into a call that has ended")
        r = self.row(id="ended1")[0]
        self.assertEqual((r["verdict"], r["error"]), ("call_ended", "the call ended while the line played"))
        self.assertIsNotNone(r["t_chat"], "a cut row says whether the text reached the chat")
        self.assertIsNotNone(r["t_seen"])
        self.assertLess(b.source.frames, 200)                      # 5 s would be 500 frames
        self.assertFalse(os.path.lexists(self.marker))
        await b.shutdown()

    async def test_a_call_that_ends_during_the_chat_post_says_so(self):
        b = self.make(real_connect=True, token_file=self.token_file())
        b.room.local_participant.chat_delay = 1.0
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        self.say("The call ends while this is being posted.", "early2")
        self.assertTrue(await until(lambda: b.current_line == "early2"))
        b.room.emit("disconnected", rtc.DisconnectReason.SIGNAL_CLOSE)
        self.assertEqual(await asyncio.wait_for(task, 3), 3)
        r = self.row(id="early2")[0]
        self.assertEqual((r["verdict"], r["error"], r["t_chat"]),
                         ("call_ended", "the call ended before the chat post returned", None))
        self.assertNotIn("first_frame", self.ev)
        await b.shutdown()

    async def test_a_removal_mid_line_is_a_drop_not_the_end_of_the_call(self):
        b = self.make(real_connect=True, token_file=self.token_file(), speech=FakeSpeech(seconds=5.0))
        task = asyncio.create_task(b.run())
        self.assertTrue(await until(lambda: "joined" in self.ev))
        self.say("A long line, and the host removes the bridge while it plays.", "kick1")
        self.assertTrue(await until(lambda: b.source.frames > 20))
        b.room.emit("disconnected", rtc.DisconnectReason.PARTICIPANT_REMOVED)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        r = self.row(id="kick1")[0]
        self.assertEqual(r["verdict"], "dropped", r)
        self.assertIsNotNone(r["t_chat"])
        await b.shutdown()

    async def test_an_error_in_a_line_ends_the_bridge_after_its_row(self):
        b = self.make()

        async def broken(frame):
            raise RuntimeError("the audio source failed")
        b.source.capture_frame = broken
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        self.say("The audio source fails under this one.", "err1")
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.row(id="err1")[0]["verdict"], "error")
        await b.shutdown()

    async def test_a_call_that_ends_during_the_join_exits_3(self):
        class EndsAtJoin(FakeRoom):
            async def connect(self, url, token):
                await super().connect(url, token)
                self.emit("disconnected", rtc.DisconnectReason.SIGNAL_CLOSE)
        b = self.make(real_connect=True, token_file=self.token_file())
        b.room = EndsAtJoin(self.ev)
        self.assertEqual(await asyncio.wait_for(b.run(), 3), 3)
        self.assertNotIn("joined", self.ev, "no track published into a call that has ended")
        self.assertEqual(self.row(id="disconnect", event="disconnected")[0]["reason"], "SIGNAL_CLOSE")
        await b.shutdown()

    async def test_a_call_that_ends_while_the_track_is_published_exits_3(self):
        b = self.make(real_connect=True, token_file=self.token_file())

        async def publish_into_an_ended_call():
            b.room.emit("disconnected", rtc.DisconnectReason.SIGNAL_CLOSE)
            raise RuntimeError("the call has ended")
        b.publish_audio = publish_into_an_ended_call
        self.assertEqual(await asyncio.wait_for(b.run(), 3), 3)
        self.assertEqual(self.row(id="disconnect", event="disconnected")[0]["reason"], "SIGNAL_CLOSE")
        await b.shutdown()

    async def test_a_line_file_that_cant_be_set_aside_is_left_and_not_spoken(self):
        b = self.make()
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        said = os.path.join(self.run, "said")
        os.rmdir(said)
        open(said, "w").close()                         # a file where the folder was: every move fails
        self.say("This one can't be moved to said/.", "stuck1")
        self.assertTrue(await until(lambda: bool(self.row(id="stuck1", event="not_taken")), 3))
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.row(id="stuck1", event="not_taken")), 1, "taken once, not on every pass")
        self.assertEqual([e for e in self.ev if isinstance(e, tuple) and e[0] == "chat"], [], "nothing posted")
        self.assertEqual(len(os.listdir(os.path.join(self.run, "say"))), 1, "left in say/")
        self.assertFalse(task.done(), "the bridge goes on")
        os.unlink(said)
        os.mkdir(said, 0o700)
        self.say("And the next one goes through.", "next1")
        self.assertTrue(await until(lambda: bool(self.row(id="next1")), 5))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    # --- stopped by a signal: amain's handlers, with a real signal to this process ----------------------------------
    def amain_with(self, b):
        """amain(), running the Bridge b (patched in until the test ends); its signal handlers go when this test's loop
        closes."""
        patch = mock.patch.object(bridge, "Bridge", lambda a: b)
        patch.start()
        self.addCleanup(patch.stop)
        return asyncio.create_task(bridge.amain(args(self.run)))

    async def test_a_line_cut_by_a_signal_gets_a_row(self):
        b = self.make(speech=FakeSpeech(seconds=5.0))
        task = self.amain_with(b)
        self.assertTrue(await until(lambda: "joined" in self.ev))
        self.say("A long line, and the bridge is stopped while it plays.", "sig1")
        self.assertTrue(await until(lambda: b.source.frames > 20))
        os.kill(os.getpid(), signal.SIGTERM)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        r = self.row(id="sig1")[0]
        self.assertEqual((r["verdict"], r["error"]), ("stopped", "the bridge was stopped (SIGTERM)"))
        self.assertIsNotNone(r["t_chat"])
        self.assertLess(b.source.frames, 200)

    async def test_a_signal_while_it_leaves_is_ignored_and_the_exit_code_stands(self):
        b = self.make()
        b.campus = FakeCampus(self.ev)
        signing_out = asyncio.Event()
        close = b.close

        async def slow_close():
            signing_out.set()
            await asyncio.sleep(0.5)                    # the sign-out is under way when the signal comes
            return await close()
        b.close = slow_close
        task = self.amain_with(b)
        self.assertTrue(await until(lambda: "joined" in self.ev))
        open(os.path.join(self.run, "control", "drop"), "w").close()
        await asyncio.wait_for(signing_out.wait(), 3)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT, "the drop's exit code stands")
        self.assertIn(("sign_out", True), self.ev, "and the sign-out finished")
        self.assertTrue(any("already leaving" in r.getMessage() for r in self.cap.records))

    async def test_a_signal_after_the_call_ended_is_ignored_too(self):
        b = self.make(real_connect=True, token_file=self.token_file())
        b.campus = FakeCampus(self.ev)
        signing_out = asyncio.Event()
        close = b.close

        async def slow_close():
            signing_out.set()
            await asyncio.sleep(0.5)                    # shutdown()'s sign-out is under way when the signal comes
            return await close()
        b.close = slow_close
        task = self.amain_with(b)
        self.assertTrue(await until(lambda: "joined" in self.ev))
        b.room.emit("disconnected", rtc.DisconnectReason.SIGNAL_CLOSE)
        await asyncio.wait_for(signing_out.wait(), 3)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(await asyncio.wait_for(task, 3), 3, "the call's exit code stands")
        self.assertIn(("sign_out", True), self.ev)

    async def test_a_second_signal_while_it_shuts_down_is_ignored_and_the_sign_out_finishes(self):
        # No drop and no call end here, so only shutdown()'s leaving flag stops the second signal. (A line's error
        # and a failed join reach shutdown() the same way.)
        b = self.make()
        b.campus = FakeCampus(self.ev)
        signing_out = asyncio.Event()
        close = b.close

        async def slow_close():
            signing_out.set()
            await asyncio.sleep(0.5)                    # shutdown()'s sign-out is under way when the second comes
            return await close()
        b.close = slow_close
        task = self.amain_with(b)
        self.assertTrue(await until(lambda: "joined" in self.ev))
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(signing_out.wait(), 3)
        self.assertFalse(b.dropped.is_set() or b.disconnected.is_set(), "neither a drop nor a call end")
        os.kill(os.getpid(), signal.SIGHUP)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertIn(("sign_out", True), self.ev, "the sign-out finished")
        self.assertEqual(b.stop_signal, "SIGTERM", "the first signal is the one recorded")
        self.assertTrue(any("SIGHUP: already leaving" in r.getMessage() for r in self.cap.records))

    async def test_a_dripping_homeserver_outlasts_the_campus_timeout(self):
        # The control for the next test: the per-operation timeout (1 s here) never fires, so without a deadline of
        # its own the sign-out is still running at three times that.
        campus, hs = dripping_campus(self)
        t = threading.Thread(target=campus.sign_out, daemon=True)
        t.start()
        t.join(3.0)
        self.assertTrue(t.is_alive(), "still signing out after 3 s")
        self.assertEqual(hs.requests, [(b"POST", b"/_matrix/client/v3/logout")])

    async def test_a_dripping_homeserver_cant_hold_the_shutdown(self):
        b = self.make()
        b.campus, hs = dripping_campus(self)
        with mock.patch.object(bridge, "SIGN_OUT_S", 1.0):
            task = self.amain_with(b)
            self.assertTrue(await until(lambda: "joined" in self.ev))
            t0 = time.monotonic()
            os.kill(os.getpid(), signal.SIGTERM)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            took = time.monotonic() - t0
        self.assertLess(took, 3.0, "SIGN_OUT_S ends it, however slowly the homeserver answers")
        self.assertEqual(hs.requests, [(b"POST", b"/_matrix/client/v3/logout")], "the sign-out was asked for")
        self.assertTrue(any("Matrix sign-out: no answer within" in r.getMessage() for r in self.cap.records))
        left = [t for t in threading.enumerate() if t.name == "sign_out"]
        self.assertTrue(left and all(t.daemon for t in left), "left in a daemon thread, which the exit won't wait for")
        t0 = time.monotonic()
        bridge.sign_out_late(b)                         # the token went before the ask, so not that device again
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertFalse(any("signing out a Matrix device" in r.getMessage() for r in self.cap.records))

    async def test_a_drop_with_a_dripping_homeserver_leaves_on_time_and_says_so(self):
        b = self.make()
        b.campus, hs = dripping_campus(self)
        with mock.patch.object(bridge, "SIGN_OUT_S", 1.0):
            task = asyncio.create_task(b.run())
            self.assertTrue(await until(lambda: "joined" in self.ev))
            b.request_drop(VIA)
            self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT)
        self.assertIsNone(self.row(id="drop", event="left")[0]["matrix_signed_out"], "not known to be signed out")
        await b.shutdown()

    async def test_a_prompt_sign_out_still_reports_its_device(self):
        b = self.make()
        b.campus = FakeCampus(self.ev)
        t0 = time.monotonic()
        self.assertIs(await b.close(), True)
        self.assertIs(await b.close(), False, "idempotent: no device the second time")
        self.assertLess(time.monotonic() - t0, 1.0, "a prompt answer is taken at once, not at the deadline")
        self.assertEqual(self.ev, [("sign_out", True), ("sign_out", False)])

        def broken():
            raise RuntimeError("not a CampusError")
        b.campus.sign_out = broken
        with self.assertRaises(RuntimeError):
            await b.close()                             # an error sign_out doesn't expect still reaches the caller

    async def test_the_account_route_masks_its_room_like_the_guest_route(self):
        # The campus names a class's room class-<host>-<time>, so the account route
        # can't reach a private call today; this stands in for a change there.
        self.addCleanup(setattr, bridge.MaskingFormatter, "secrets", bridge.MaskingFormatter.secrets)
        for room, masked in (("private-0123456789ab", True), ("class-h-mh9x2k1", False)):
            bridge.MaskingFormatter.secrets = ()
            b = self.make(real_connect=True, campus="https://campus.example", host_id="h", cred_file="unused")
            account = FakeAccount(self.ev)
            account.join_class = lambda host_id, room=room: {"livekitUrl": "wss://account.example",
                                                             "token": "account-lk-token", "roomName": room,
                                                             "matrixRoomId": None, "matrixStatus": None}
            with mock.patch.object(bridge, "Campus", lambda url: account), \
                    mock.patch.object(bridge, "read_credentials", lambda p: ("ada", "pw")):
                await b.account_route()
            self.assertEqual("0123456789ab" in bridge.MaskingFormatter.secrets, masked, room)
            logged = " ".join(r.getMessage() for r in self.cap.records)
            self.assertNotIn("0123456789ab", logged)
        self.assertIn("in class class-h-mh9x2k1", logged, "any other room's name is shown as it is")

    def test_stopped_by_cancels_once_and_then_ignores(self):
        task = mock.Mock()
        b = types.SimpleNamespace(leaving=False, stop_signal=None, dropped=asyncio.Event(),
                                  disconnected=asyncio.Event())
        bridge.stopped_by(signal.SIGHUP, task, b)
        self.assertEqual((task.cancel.call_count, b.stop_signal), (1, "SIGHUP"))
        b.leaving = True
        bridge.stopped_by(signal.SIGTERM, task, b)
        self.assertEqual((task.cancel.call_count, b.stop_signal), (1, "SIGHUP"))

    def test_stopped_by_counts_a_drop_or_a_call_end_as_leaving(self):
        for seen in ("dropped", "disconnected"):
            task = mock.Mock()
            b = types.SimpleNamespace(leaving=False, stop_signal=None, dropped=asyncio.Event(),
                                      disconnected=asyncio.Event())
            getattr(b, seen).set()
            bridge.stopped_by(signal.SIGTERM, task, b)
            self.assertEqual((task.cancel.call_count, b.stop_signal), (0, None), seen)

    async def test_a_signal_in_the_wait_after_a_drop_during_the_join_is_ignored(self):
        b = self.make(connect_s=0.8)
        task = self.amain_with(b)
        self.assertTrue(await until(lambda: "join" in self.ev))
        b.request_drop(VIA)
        await asyncio.sleep(0.2)                        # run() now waits for the join to finish (CONNECT_WAIT_S)
        self.assertFalse(b.leaving, "leave() hasn't begun yet: this is the wait a review found")
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(await asyncio.wait_for(task, 3), bridge.DROP_EXIT, "the drop's exit code stands")
        self.assertTrue(self.row(id="drop", event="left"), "and the drop's row is written")
        self.assertTrue(any("already leaving" in r.getMessage() for r in self.cap.records))

    def test_held_lines_go_into_private_folders_whatever_the_umask(self):
        b = self.make()
        with open(os.path.join(self.run, "say", "a.txt"), "w") as f:
            f.write("queued before the drop\n")
        old = os.umask(0o022)
        try:
            b.hold_queued_lines()
        finally:
            os.umask(old)
        held = os.path.join(self.run, "say_held")
        (sub,) = os.listdir(held)
        for p in (held, os.path.join(held, sub)):
            self.assertEqual(stat.S_IMODE(os.lstat(p).st_mode), 0o700, p)
        self.assertEqual(os.listdir(os.path.join(held, sub)), ["a.txt"])

    # --- verdicts ----------------------------------------------------------------------------------------------------
    async def test_spoken_needs_the_packets_and_a_quiet_track_is_not_spoken(self):
        b = self.make()
        b.room.packets = lambda: b.source.frames // 2              # every voiced 20 ms went out
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        self.say("Every packet of this one went out.", "all1")
        self.assertTrue(await until(lambda: bool(self.row(id="all1")), 5))
        r = self.row(id="all1")[0]
        self.assertEqual(r["verdict"], "spoken", r)
        self.assertGreater(r["pkts_sent"], 0)
        b.room.packets = lambda: b.source.frames // 4              # half of them
        chats = len([e for e in self.ev if isinstance(e, tuple) and e[0] == "chat"])
        self.say("Only half of this one went out.", "half1")
        self.assertTrue(await until(lambda: bool(self.row(id="half1")), 5))
        self.assertEqual(self.row(id="half1")[0]["verdict"], "not_spoken")
        self.assertTrue(await until(lambda: len([e for e in self.ev if isinstance(e, tuple) and e[0] == "chat"])
                                    == chats + 2), "the line, then the note that its voice didn't reach the call")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    async def test_no_stats_make_the_verdict_unknown_and_the_bridge_goes_on(self):
        b = self.make()
        b.room.packets = lambda: b.source.frames // 2
        b.room.stats_fail = True                                    # the room is gone: livekit raises
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        self.say("Its call ended while it played.", "gone1")
        self.assertTrue(await until(lambda: bool(self.row(id="gone1")), 5))
        r = self.row(id="gone1")[0]
        self.assertEqual((r["verdict"], r["pkts_sent"]), ("unknown", None))
        self.assertFalse(task.done(), "a line without stats doesn't end the bridge")
        b.room.stats_fail = False
        self.say("And the next one is counted again.", "back1")
        self.assertTrue(await until(lambda: bool(self.row(id="back1")), 5))
        self.assertEqual(self.row(id="back1")[0]["verdict"], "spoken")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    async def test_a_synthesis_failure_is_not_spoken_and_the_bridge_goes_on(self):
        for remote in (True, False):        # a sentence at a time, and the whole line first
            with self.subTest(remote=remote):
                b = self.make(speech=FakeSpeech(fail=1, remote=remote))
                b.room.packets = lambda: b.source.frames // 2
                task = asyncio.create_task(b.run())
                await until(lambda: "joined" in self.ev)
                frames = b.source.frames
                lid = "nosynth%d" % remote
                self.say("No speech can be made for this one.", lid)
                self.assertTrue(await until(lambda: bool(self.row(id=lid)), 5))
                r = self.row(id=lid)[0]
                self.assertEqual((r["verdict"], r["error"]), ("not_spoken", "synthesis failed (FileNotFoundError)"))
                self.assertEqual(b.source.frames, frames)
                self.assertTrue(await until(lambda: len([e for e in self.ev if isinstance(e, tuple)
                                                         and e[0] == "chat"]) == 2), "the line, then the note")
                lid2 = "after%d" % remote
                self.say("The next line is spoken.", lid2)
                self.assertTrue(await until(lambda: bool(self.row(id=lid2)), 5))
                self.assertEqual(self.row(id=lid2)[0]["verdict"], "spoken")
                self.assertFalse(task.done())
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await b.shutdown()

    async def test_a_stop_sent_while_the_line_is_posted_cuts_it(self):
        b = self.make()
        b.room.local_participant.chat_delay = 0.5
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        frames = b.source.frames
        self.say("A stop lands while this is being posted.", "early1")
        self.assertTrue(await until(lambda: b.current_line == "early1"))
        open(os.path.join(self.run, "control", "stop"), "w").close()
        self.assertTrue(await until(lambda: bool(self.row(id="early1")), 5))
        self.assertEqual(self.row(id="early1")[0]["verdict"], "interrupted")
        self.assertEqual(b.source.frames, frames, "not a frame of it was played")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await b.shutdown()

    async def test_a_fifo_in_say_neither_blocks_the_drop_nor_holds_the_queue(self):
        b = self.make()
        task = asyncio.create_task(b.run())
        await until(lambda: "joined" in self.ev)
        os.mkfifo(os.path.join(self.run, "say", "0001_fifo.json"))
        await asyncio.sleep(0.3)
        self.assertTrue(os.path.lexists(os.path.join(self.run, "said", "0001_fifo.json")), "refused and set aside")
        t = time.monotonic()
        open(os.path.join(self.run, "control", "drop"), "w").close()
        self.assertEqual(await asyncio.wait_for(task, 2), bridge.DROP_EXIT)
        self.assertLess(time.monotonic() - t, 1.0)
        await b.shutdown()

    def test_run_folders_others_could_change_are_refused(self):
        run = os.path.join(self.d, "run-check")
        for sub in ("say", "said", "control"):
            os.makedirs(os.path.join(run, sub), mode=0o700)
        os.chmod(run, 0o700)
        self.assertIsNone(bridge.run_dir_problem(run))
        os.chmod(os.path.join(run, "say"), 0o777)
        self.assertIn("say: group or others can write it", bridge.run_dir_problem(run))
        os.chmod(os.path.join(run, "say"), 0o700)
        os.chmod(os.path.join(run, "control"), 0o772)
        self.assertIn("control", bridge.run_dir_problem(run))


# ------------------------------------------------------------------------------------------- the log, the model files
ROOM = "private-AbC_12-xyZ9q"        # what privateRoomForToken makes: "private-" and 12 characters of a link


class LogMask(unittest.TestCase):
    def log_through(self, logger, stream, *a, **kw):
        h = bridge.log_handler(stream)
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        try:
            logger.log(logging.INFO, *a, **kw)
        finally:
            logger.removeHandler(h)

    def test_livekits_own_lines_are_masked(self):
        from livekit.rtc.log import logger as livekit_logger    # the logger LiveKit's Rust core logs through
        out = io.StringIO()
        self.log_through(livekit_logger, out, "%s:%s:%s - %s", "livekit::room", 1835, "livekit::room",
                         'Disconnected from room "%s" with reason: UnknownReason' % ROOM)
        self.log_through(logging.getLogger("livekit.rtc.room"), out, 'leaving "%s"', ROOM)
        try:
            raise RuntimeError("no room %s" % ROOM)
        except RuntimeError:
            self.log_through(logging.getLogger("bridge"), out, "failed", exc_info=True)
        text = out.getvalue()
        self.assertNotIn("AbC_12", text)
        self.assertEqual(text.count('"private-..."'), 2, text)
        self.assertIn("no room private-...", text)

    def test_words_like_private_call_are_left_alone(self):
        out = io.StringIO()
        self.log_through(logging.getLogger("bridge"), out, "guest link: a private-call link, private-%s", "12345678")
        self.assertIn("a private-call link, private-...", out.getvalue())

    def test_the_rooms_exact_part_is_masked_in_any_form(self):
        self.addCleanup(setattr, bridge.MaskingFormatter, "secrets", bridge.MaskingFormatter.secrets)
        bridge.mask_room(ROOM)
        part, out = ROOM[len("private-"):], io.StringIO()
        for shown in ("PRIVATE-" + part, "private%2D" + part, "x" + ROOM + "y"):     # shapes the pattern misses
            self.log_through(logging.getLogger("bridge"), out, "room %s", shown)
        self.assertNotIn(part, out.getvalue())
        bridge.mask_room("jam-0123456789abcdef")                # any other room's name is left as it is
        self.log_through(logging.getLogger("bridge"), out, "room %s", "jam-0123456789abcdef")
        self.assertIn("jam-0123456789abcdef", out.getvalue())

    def test_main_logs_through_the_mask(self):
        d = tempfile.mkdtemp(prefix="logmask-test-")
        os.chmod(d, 0o700)
        root = logging.getLogger()
        kept = root.handlers[:]
        try:
            tok = os.path.join(d, "token")
            with os.fdopen(os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write("scratch-token\n")
            os.mkdir(os.path.join(d, "tts"), 0o700)

            async def fake_amain(a, box=None):
                from livekit.rtc.log import logger as livekit_logger
                livekit_logger.info('Disconnected from room "%s"', ROOM)
                return 0
            err = io.StringIO()
            argv = ["bridge.py", "--url", "ws://127.0.0.1:1", "--token-file", tok, "--run-dir", os.path.join(d, "run"),
                    "--tts-socket", os.path.join(d, "tts", "tts.sock")]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stderr", err), \
                    mock.patch.object(bridge, "amain", fake_amain):
                self.assertEqual(bridge.main(), 0)
            self.assertIn('Disconnected from room "private-..."', err.getvalue())
            self.assertNotIn("AbC_12", err.getvalue())
        finally:
            root.handlers[:] = kept
            shutil.rmtree(d, ignore_errors=True)


class SignOutLate(unittest.TestCase):
    """sign_out_late, after the loop has closed: signals are ignored meanwhile, and the whole of it has a deadline."""

    def run_it(self, campus, limit):
        before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with mock.patch.object(bridge, "SIGN_OUT_LATE_S", limit), self.assertLogs(bridge.log, "INFO") as cm:
            t0 = time.monotonic()
            bridge.sign_out_late(types.SimpleNamespace(campus=campus))
            took = time.monotonic() - t0
        self.assertEqual({s: signal.getsignal(s) for s in before}, before, "the handlers are put back")
        return took, "\n".join(cm.output)

    def test_a_homeserver_that_never_finishes_is_given_up_on_time(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Dripping:
            mx_token = "scratch-mx-token"

            def sign_out(self):
                os.kill(os.getpid(), signal.SIGTERM)    # ignored meanwhile: the process lives on
                release.wait(30)                        # an answer that never finishes
                return True
        took, out = self.run_it(Dripping(), 0.5)
        self.assertLess(took, 5, "the deadline ends it, however slowly the homeserver answers")
        self.assertIn("given up", out)

    def test_a_prompt_sign_out_is_reported(self):
        class Prompt:
            mx_token = "scratch-mx-token"

            def sign_out(self):
                return True
        took, out = self.run_it(Prompt(), 5)
        self.assertLess(took, 5)
        self.assertIn("that device: signed out", out)


class Models(TempDir):
    def models(self, name="models", mode=0o700):
        p = os.path.join(self.d, name)
        os.mkdir(p, mode)
        os.chmod(p, mode)
        for n in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            with open(os.path.join(p, n), "wb") as f:
                f.write(b"x")
            os.chmod(os.path.join(p, n), 0o600)
        return p

    def test_a_private_models_folder_passes_and_loose_ones_are_refused(self):
        p = self.models()
        self.assertIsNone(control.models_problem(p))
        os.chmod(os.path.join(p, "voices-v1.0.bin"), 0o666)
        self.assertIn("voices-v1.0.bin: group or others can write it", control.models_problem(p))
        os.chmod(os.path.join(p, "voices-v1.0.bin"), 0o600)
        os.chmod(p, 0o777)
        self.assertIn("group or others can write it", control.models_problem(p))
        os.chmod(p, 0o700)
        link = os.path.join(self.d, "models-link")
        os.symlink(p, link)
        self.assertIn("link", control.models_problem(link))

    def test_a_model_file_may_link_into_a_private_folder_only(self):
        p, store = self.models(), self.models("store")
        os.unlink(os.path.join(p, "kokoro-v1.0.onnx"))
        os.symlink(os.path.join(store, "kokoro-v1.0.onnx"), os.path.join(p, "kokoro-v1.0.onnx"))
        self.assertIsNone(control.models_problem(p))
        os.chmod(store, 0o777)
        self.assertIn("links into", control.models_problem(p))

    def test_the_bridge_wont_start_on_a_loose_models_folder(self):
        p = self.models(mode=0o777)
        tok = os.path.join(self.d, "token")
        with os.fdopen(os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write("scratch-token\n")
        err = io.StringIO()
        argv = ["bridge.py", "--url", "ws://127.0.0.1:1", "--token-file", tok, "--run-dir",
                os.path.join(self.d, "run"), "--models", p]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(bridge, "amain", mock.Mock(side_effect=AssertionError("it started"))):
            with self.assertRaises(SystemExit) as cm:
                bridge.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--models", err.getvalue())

    def test_a_link_to_a_link_through_a_loose_folder_is_refused(self):
        # A reviewer's case: models/kokoro -> loose/k -> store/kokoro, with loose/ open to all; store/ is private
        p, store = self.models(), self.models("store")
        loose = os.path.join(self.d, "loose")
        os.mkdir(loose)
        os.chmod(loose, 0o777)
        os.symlink(os.path.join(store, "kokoro-v1.0.onnx"), os.path.join(loose, "k"))
        os.unlink(os.path.join(p, "kokoro-v1.0.onnx"))
        os.symlink(os.path.join(loose, "k"), os.path.join(p, "kokoro-v1.0.onnx"))
        self.assertIn("links into %s" % loose, control.models_problem(p))
        os.chmod(loose, 0o700)              # the same chain through private folders only passes
        self.assertIsNone(control.models_problem(p))

    def test_a_relative_link_is_read_from_its_own_folder_and_a_loop_stops(self):
        p, store = self.models(), self.models("store")
        os.unlink(os.path.join(p, "voices-v1.0.bin"))
        # two relative hops: models/voices -> ../store/hop, then store/hop -> voices-v1.0.bin, read from store/
        os.symlink(os.path.join("..", "store", "hop"), os.path.join(p, "voices-v1.0.bin"))
        os.symlink("voices-v1.0.bin", os.path.join(store, "hop"))
        self.assertIsNone(control.models_problem(p))
        os.unlink(os.path.join(p, "voices-v1.0.bin"))
        os.symlink("voices-v1.0.bin", os.path.join(p, "voices-v1.0.bin"))     # a link to itself
        self.assertIn("more than", control.models_problem(p))

    def test_model_files_missing_at_the_start_are_never_loaded_later(self):
        tok = os.path.join(self.d, "token")
        with os.fdopen(os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write("scratch-token\n")
        os.mkdir(os.path.join(self.d, "tts"), 0o700)
        empty = os.path.join(self.d, "empty-models")
        os.mkdir(empty, 0o700)
        seen = []

        async def fake_amain(a, box=None):
            seen.append(a.models)
            return 0
        argv = ["bridge.py", "--url", "ws://127.0.0.1:1", "--token-file", tok, "--run-dir", os.path.join(self.d, "run"),
                "--tts-socket", os.path.join(self.d, "tts", "tts.sock"), "--models", empty]
        root = logging.getLogger()
        kept = root.handlers[:]
        try:
            with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stderr", io.StringIO()), \
                    mock.patch.object(bridge, "amain", fake_amain):
                self.assertEqual(bridge.main(), 0)
        finally:
            root.handlers[:] = kept
        self.assertEqual(seen, [None], "unchecked at the start, so never a folder to load from")
        with self.assertRaises(FileNotFoundError):
            bridge.Speech("af_heart", None, None)._local_say("A line.")

    def test_private_folders_are_made_without_a_looser_one_above(self):
        old = os.umask(0o022)
        try:
            leaf = os.path.join(self.d, "a", "b", "c")
            control.make_private_dirs([leaf])
        finally:
            os.umask(old)
        for p in (os.path.join(self.d, "a"), os.path.join(self.d, "a", "b"), leaf):
            self.assertEqual(stat.S_IMODE(os.lstat(p).st_mode), 0o700, p)


if __name__ == "__main__":
    unittest.main()
