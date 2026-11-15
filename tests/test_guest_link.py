"""A guest link (a class's or a jam's) resolved into the guest route's LiveKit token, against a stand-in campus on
loopback; no LiveKit, no real campus.

    python -I -B tests/test_guest_link.py -v

The stand-in answers POST /api/invite/TOKEN/guest-join with the shapes and refusals of a Multiverse campus
(as read from its code in November 2026). Each refusal sits beside an
acceptance, so a check that stopped working turns a test red. Test values only: the invite tokens are made up here, and
the "LiveKit token" is a JWT signed with a throwaway key that no server holds.
"""
import io, json, logging, os, re, shutil, stat, subprocess, sys, tempfile, threading, time, unittest, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import jwt  # noqa: E402  (pyjwt, in the bridge's venv)
import bridge  # noqa: E402
from campus import Campus, CampusError, parse_guest_link  # noqa: E402

JAM_ROOM = "jam-" + str(uuid.uuid4())
THROWAWAY_KEY = os.urandom(32).hex()


def livekit_token(identity, name, room):
    """What createGuestCallToken mints: identity guest:<hex>, the display name, one room, publish and subscribe, the
    server SDK's default 6 h lifetime. No canPublishData (LiveKit then follows canPublish), no canUpdateOwnMetadata."""
    now = int(time.time())
    return jwt.encode({"iss": "APIstub", "sub": identity, "name": name, "nbf": 0, "exp": now + 6 * 3600,
                       "video": {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True}},
                      THROWAWAY_KEY, algorithm="HS256")


class StandIn:
    """invite_tokens rows by token: kind, the room stored when the link was made, whether anyone is in it, expiry."""

    def __init__(self):
        self.links = {}
        self.edge = None                # None, "1010-default-ua" or "1010-all": the campus's Cloudflare edge
        self.agents = []                # the User-Agent of every request
        self.seen = []                  # one row per request: token, display name, bearer or not, guestToken given
        self.credentials = {}           # every guest credential handed out -> its guest id
        self.lock = threading.Lock()
        stand_in = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                ua = self.headers.get("User-Agent", "")
                with stand_in.lock:
                    stand_in.agents.append(ua)
                if stand_in.edge == "1010-all" or (stand_in.edge == "1010-default-ua" and ua.startswith("Python-urllib")):
                    body = b"error code: 1010"
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain; charset=UTF-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                m = re.fullmatch(r"/api/invite/([^/]+)/guest-join", self.path)
                if not m:
                    return self.reply(404, {"success": False, "error": "Not found"})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    b = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    b = {}
                token = m.group(1)
                with stand_in.lock:
                    stand_in.seen.append({"token": token, "name": b.get("displayName"),
                                          "bearer": "Authorization" in self.headers, "guestToken": b.get("guestToken")})
                name = b.get("displayName")
                if not (isinstance(name, str) and 1 <= len(name) <= 40):
                    return self.reply(400, {"success": False, "error": "A display name is required"})
                link = stand_in.links.get(token)
                if link is None:
                    return self.reply(404, {"success": False, "error": "Invite not found"})
                if link.get("expired"):
                    return self.reply(410, {"success": False, "error": "This invite has expired"})
                if link["kind"] == "campus":
                    return self.reply(400, {"success": False, "error": "That link opens campus, not a call"})
                if link["kind"] == "private":
                    room = "private-" + token[:12]
                elif not link.get("room"):
                    return self.reply(409, {"success": False, "code": "link-made-outside-a-call",
                                            "error": "The host wasn't in a call when they made this link"})
                elif not link.get("live"):
                    return self.reply(409, {"success": False, "code": "call-ended", "error": "That call has ended."})
                else:
                    room = link["room"]
                presented = b.get("guestToken")       # the campus checks it: ours, or a new guest
                with stand_in.lock:
                    known = stand_in.credentials.get(presented)
                    guest_id = known or os.urandom(8).hex()
                    credential = presented if known else guest_id + "." + os.urandom(32).hex()[:43]
                    stand_in.credentials[credential] = guest_id
                if link.get("banned") or guest_id in link.get("banned_ids", ()):
                    return self.reply(403, {"success": False, "code": "guest-banned", "error": "You were removed"})
                identity = "guest:" + guest_id
                lk = livekit_token(identity, name, room)
                link.setdefault("issued", []).append(lk)
                link.setdefault("identities", []).append(identity)
                self.reply(200, {"success": True, "data": {
                    "livekitToken": lk, "roomName": room, "identity": identity, "guestToken": credential,
                    "linkKind": link["kind"], "inviterName": "Sam"}})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def add(self, kind, **kw):
        token = str(uuid.uuid4())
        self.links[token] = dict(kind=kind, **kw)
        return token

    def link(self, token):
        return "%s/call/%s" % (self.base, token)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# A name, and (in setUp) a TTS socket nobody listens on, in a private folder (no model files are needed: nothing is
# spoken before the join).
NAMED = ("--guest-name", "Ada")


class Args:
    """The bridge's parsed arguments, for the guest route only."""

    def __init__(self, **kw):
        self.url, self.token_file, self.guest_link_file, self.guest_name = "wss://livekit.example:7443", None, None, "Ada"
        self.__dict__.update(kw)


class GuestLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = StandIn()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="guest-link-test-")
        os.chmod(self.d, 0o700)
        os.mkdir(os.path.join(self.d, "tts"), 0o700)
        self.named = NAMED + ("--tts-socket", os.path.join(self.d, "tts", "tts.sock"))
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        bridge.log.addHandler(self.handler)
        bridge.log.setLevel(logging.INFO)

    def tearDown(self):
        bridge.log.removeHandler(self.handler)
        shutil.rmtree(self.d, ignore_errors=True)

    def private_file(self, name, text, mode=0o600):
        p = os.path.join(self.d, name)
        with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w") as f:
            f.write(text + "\n")
        os.chmod(p, mode)
        return p

    # --- the link itself ---------------------------------------------------------------------------------------------

    def test_parse_accepts_a_call_link_and_refuses_the_rest(self):
        tok = str(uuid.uuid4())
        self.assertEqual(parse_guest_link("https://campus.example/call/%s" % tok), ("https://campus.example", tok))
        self.assertEqual(parse_guest_link(" https://campus.example/call/%s/ \n" % tok), ("https://campus.example", tok))
        self.assertEqual(parse_guest_link("http://127.0.0.1:8080/call/%s" % tok), ("http://127.0.0.1:8080", tok))
        for bad in ("https://campus.example/invite/%s" % tok,          # a campus link: the map, not a call
                    "http://campus.example/call/%s" % tok,             # plain http off loopback
                    "https://campus.example/call/short",                # under the campus's 8 characters
                    "https://campus.example/call/%s/more" % tok,
                    "https://campus.example/class/602",                 # the class page, which needs an account
                    "/call/%s" % tok, ""):
            with self.assertRaises(CampusError, msg=bad):
                parse_guest_link(bad)

    # --- the campus's answer -----------------------------------------------------------------------------------------

    def test_jam_link_gives_a_guest_token_for_the_jam_room(self):
        tok = self.s.add("public", room=JAM_ROOM, live=True)
        lk, room, kind, cred = Campus(self.s.base).guest_join(tok, "Ada")
        self.assertEqual((room, kind), (JAM_ROOM, "public"))
        self.assertRegex(cred, r"^[a-f0-9]{16}\.")
        self.assertEqual(lk, self.s.links[tok]["issued"][-1])
        claims = jwt.decode(lk, options={"verify_signature": False})
        self.assertTrue(claims["sub"].startswith("guest:"))
        self.assertEqual(claims["name"], "Ada")
        self.assertEqual(claims["video"], {"room": JAM_ROOM, "roomJoin": True, "canPublish": True, "canSubscribe": True})
        row = [r for r in self.s.seen if r["token"] == tok][-1]
        self.assertEqual(row["name"], "Ada")
        self.assertFalse(row["bearer"], "a guest join carries no campus sign-in")

    def test_refusals_name_the_reason_and_never_the_link(self):
        cases = [(self.s.add("public", room=JAM_ROOM, live=False), 409, "nobody is in that call now"),
                 (self.s.add("public", room=None), 409, "made outside a call"),
                 (self.s.add("public", room=JAM_ROOM, live=True, expired=True), 410, "expired"),
                 (self.s.add("campus"), 400, "not a call link"),
                 (self.s.add("public", room=JAM_ROOM, live=True, banned=True), 403, "removed"),
                 (str(uuid.uuid4()), 404, "no such link")]
        for tok, status, words in cases:
            with self.assertRaises(CampusError, msg=words) as cm:
                Campus(self.s.base).guest_join(tok, "Ada")
            self.assertEqual(cm.exception.status, status)
            self.assertIn(words, str(cm.exception))
            self.assertNotIn(tok, str(cm.exception))
        tok = self.s.add("public", room=JAM_ROOM, live=True)           # and the acceptance beside them
        self.assertEqual(Campus(self.s.base).guest_join(tok, "Ada")[1], JAM_ROOM)

    def test_the_edge_lets_the_bridge_through_and_its_refusal_is_never_read_as_the_campus(self):
        """Cloudflare in front of the campus refuses urllib's default agent with 403 "error code: 1010". The
        guest join names the bridge honestly, so it passes; an edge that refuses anyway is reported as the edge's 403,
        never as the campus's own 403 ("this guest was removed from the call")."""
        tok = self.s.add("public", room=JAM_ROOM, live=True)
        self.s.edge = "1010-default-ua"
        self.assertEqual(Campus(self.s.base).guest_join(tok, "Ada")[1], JAM_ROOM)
        self.assertTrue(self.s.agents[-1].startswith("agent-voice-bridge/"), self.s.agents[-1])
        self.s.edge = "1010-all"
        with self.assertRaises(CampusError) as cm:
            Campus(self.s.base).guest_join(tok, "Ada")
        self.assertTrue(cm.exception.edge)
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("Cloudflare", str(cm.exception))
        self.assertNotIn("removed", str(cm.exception))
        self.assertNotIn(tok, str(cm.exception))
        self.s.edge = None

    def test_unreachable_campus_is_an_error(self):
        with self.assertRaises(CampusError) as cm:
            Campus("http://127.0.0.1:9", timeout=2).guest_join(str(uuid.uuid4()), "Ada")
        self.assertIsNone(cm.exception.status)

    # --- the bridge's guest route ------------------------------------------------------------------------------------

    def test_bridge_guest_route_from_a_link_file(self):
        tok = self.s.add("public", room=JAM_ROOM, live=True)
        f = self.private_file("guest_link", self.s.link(tok))
        url, lk = bridge.guest_route(Args(guest_link_file=f))
        self.assertEqual(url, "wss://livekit.example:7443")
        self.assertEqual(lk, self.s.links[tok]["issued"][-1])
        out = self.log.getvalue()
        self.assertIn("into room " + JAM_ROOM, out)
        self.assertNotIn(tok, out)
        self.assertNotIn(lk, out)

    def test_bridge_refuses_a_link_file_others_can_read(self):
        tok = self.s.add("public", room=JAM_ROOM, live=True)
        f = self.private_file("guest_link_loose", self.s.link(tok), mode=0o644)
        n = len(self.s.seen)
        with self.assertRaises(bridge.FileRefused):
            bridge.guest_route(Args(guest_link_file=f))
        self.assertEqual(len(self.s.seen), n, "nothing may be sent for a loose link file")
        g = self.private_file("guest_link_ok", self.s.link(tok))
        self.assertEqual(bridge.guest_route(Args(guest_link_file=g))[1], self.s.links[tok]["issued"][-1])

    def test_bridge_warns_on_a_private_call_link(self):
        tok = self.s.add("private")
        f = self.private_file("guest_link_private", self.s.link(tok))
        _, lk = bridge.guest_route(Args(guest_link_file=f))
        self.assertTrue(lk)
        self.assertIn("private-call link", self.log.getvalue())
        self.assertNotIn(tok[:12], self.log.getvalue(), "a private room's name is the link's first 12 characters")
        self.log.truncate(0)
        self.log.seek(0)
        g = self.private_file("guest_link_public", self.s.link(self.s.add("public", room=JAM_ROOM, live=True)))
        bridge.guest_route(Args(guest_link_file=g))
        self.assertNotIn("private-call link", self.log.getvalue())

    def test_a_private_rooms_link_part_is_masked_from_then_on(self):
        self.addCleanup(setattr, bridge.MaskingFormatter, "secrets", bridge.MaskingFormatter.secrets)
        tok = self.s.add("private")
        f = self.private_file("guest_link_private_mask", self.s.link(tok))
        bridge.guest_route(Args(guest_link_file=f))
        self.assertIn(tok[:12], bridge.MaskingFormatter.secrets, "the room's name is the link's first 12 characters")

    def test_token_file_route_unchanged(self):
        f = self.private_file("token", "eyJ.unchanged.token")
        self.assertEqual(bridge.guest_route(Args(token_file=f)), ("wss://livekit.example:7443", "eyJ.unchanged.token"))

    # --- the guest's own credential (a host's removal holds) --------------------------------------------------------

    def test_the_guest_credential_is_kept_presented_and_a_removal_holds(self):
        tok = self.s.add("public", room=JAM_ROOM, live=True)
        f = self.private_file("guest_link_cred", self.s.link(tok))
        run = os.path.join(self.d, "run_cred")
        os.mkdir(run, 0o700)
        bridge.guest_route(Args(guest_link_file=f), run)
        first = [r for r in self.s.seen if r["token"] == tok][-1]
        self.assertIsNone(first["guestToken"], "a first join presents no credential")
        kept = os.path.join(run, "guest_token")
        self.assertEqual(stat.S_IMODE(os.lstat(kept).st_mode), 0o600)
        with open(kept) as fh:
            d = json.load(fh)
        self.assertEqual(d["campus"], self.s.base)
        bridge.guest_route(Args(guest_link_file=f), run)
        again = [r for r in self.s.seen if r["token"] == tok][-1]
        self.assertTrue(again["guestToken"] == d["guestToken"], "the next join presents it, as the guest page does")
        ids = self.s.links[tok]["identities"]
        self.assertEqual(ids[-1], ids[-2], "so the guest keeps its identity")
        self.assertIn("as a returning guest", self.log.getvalue())
        self.s.links[tok]["banned_ids"] = {ids[-1][len("guest:"):]}         # the host removes this guest
        with self.assertRaises(CampusError) as cm:
            bridge.guest_route(Args(guest_link_file=f), run)
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("removed", str(cm.exception))
        other = self.private_file("guest_link_other", self.s.link(self.s.add("public", room=JAM_ROOM, live=True)))
        fresh = os.path.join(self.d, "run_fresh")
        os.mkdir(fresh, 0o700)
        self.assertEqual(bridge.guest_route(Args(guest_link_file=other), fresh)[0], "wss://livekit.example:7443",
                         "and a bridge with no credential kept joins as a new guest")
        self.assertFalse(d["guestToken"].split(".")[1] in self.log.getvalue(), "the credential is never logged")

    def test_a_credential_is_shown_only_to_the_campus_that_issued_it(self):
        run = os.path.join(self.d, "run_other_campus")
        os.mkdir(run, 0o700)
        bridge.save_guest_credential(os.path.join(run, "guest_token"), "https://another-campus.example",
                                     "0123456789abcdef.c2lnbmF0dXJl")
        tok = self.s.add("public", room=JAM_ROOM, live=True)
        bridge.guest_route(Args(guest_link_file=self.private_file("guest_link_x", self.s.link(tok))), run)
        self.assertIsNone([r for r in self.s.seen if r["token"] == tok][-1]["guestToken"])
        with open(os.path.join(run, "guest_token")) as fh:
            self.assertEqual(json.load(fh)["campus"], self.s.base, "this campus's own credential replaces it")

    def test_a_kept_credential_others_could_read_is_not_used(self):
        run = os.path.join(self.d, "run_loose_cred")
        os.mkdir(run, 0o700)
        kept = os.path.join(run, "guest_token")
        bridge.save_guest_credential(kept, self.s.base, "0123456789abcdef.c2lnbmF0dXJl")
        os.chmod(kept, 0o644)
        self.assertIsNone(bridge.load_guest_credential(kept, self.s.base))
        os.chmod(kept, 0o600)
        self.assertEqual(bridge.load_guest_credential(kept, self.s.base), "0123456789abcdef.c2lnbmF0dXJl")

    def test_a_missing_or_loose_file_ends_the_bridge_with_2_and_no_traceback(self):
        loose = self.private_file("guest_link_loose_cli", self.s.link(str(uuid.uuid4())), mode=0o644)
        for f in (os.path.join(self.d, "no-such-file"), loose):
            for flag in ("--guest-link-file", "--token-file"):
                rc, out = self.run_bridge("--url", "ws://127.0.0.1:9", flag, f, *self.named)
                self.assertEqual(rc, 2, out[-600:])
                self.assertNotIn("Traceback", out)
                self.assertIn(f, out)

    # --- the whole program, up to the LiveKit connection -------------------------------------------------------------

    def run_bridge(self, *args, timeout=60):
        """bridge.py in its own process, in a copy of the code whose run/ is a temp folder."""
        code = tempfile.mkdtemp(prefix="code-", dir=self.d)
        for n in os.listdir(ROOT):                     # every top-level module, so a new import is never missed
            if n.endswith(".py"):
                shutil.copy(os.path.join(ROOT, n), code)
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(code, "bridge.py"), *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr

    def test_a_run_folder_others_can_change_is_refused_at_start(self):
        f = self.private_file("token_runcheck", "eyJ.scratch.token")
        loose = os.path.join(self.d, "run-loose")
        for sub in ("", "say", "said", "control"):
            os.makedirs(os.path.join(loose, sub), mode=0o700, exist_ok=True)
        os.chmod(os.path.join(loose, "say"), 0o777)
        rc, out = self.run_bridge("--url", "ws://127.0.0.1:9", "--token-file", f, "--run-dir", loose, *self.named)
        self.assertEqual(rc, 2, out[-600:])
        self.assertIn("--run-dir: %s: group or others can write it" % os.path.join(loose, "say"), out)

    def test_a_file_refused_after_the_start_also_ends_with_2(self):
        """The start-up check reads the file first; one that is gone or loosened by the time it is used is refused
        then, inside the join, and must still end the bridge with 2."""
        f = self.private_file("token_late", "eyJ.scratch.token")

        async def gone_since(a, box=None):
            raise bridge.FileRefused(f + ": can't open it (No such file or directory)")
        argv = ["bridge.py", "--url", "ws://127.0.0.1:9", "--token-file", f, "--run-dir", os.path.join(self.d, "run_late"),
                *self.named]
        with mock.patch.object(bridge, "amain", gone_since), mock.patch.object(sys, "argv", argv):
            self.assertEqual(bridge.main(), 2)

    def test_arguments(self):
        f = self.private_file("guest_link_args", self.s.link(str(uuid.uuid4())))
        rc, out = self.run_bridge("--url", "ws://127.0.0.1:9", "--token-file", f, "--guest-link-file", f)
        self.assertEqual(rc, 2)
        self.assertIn("not both", out)
        rc, out = self.run_bridge("--guest-link-file", f)
        self.assertEqual(rc, 2)
        self.assertIn("--url goes with", out)

    def test_a_refused_link_ends_the_bridge_cleanly(self):
        tok = self.s.add("public", room=JAM_ROOM, live=False)          # the jam emptied before the bridge joined
        f = self.private_file("guest_link_ended", self.s.link(tok))
        rc, out = self.run_bridge("--url", "ws://127.0.0.1:9", "--guest-link-file", f, *self.named)
        self.assertEqual(rc, 2, out[-800:])
        self.assertIn("guest join (nobody is in that call now", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn(tok, out)

    def test_a_resolved_link_reaches_the_livekit_connect_and_logs_no_secret(self):
        tok = self.s.add("public", room=JAM_ROOM, live=True)
        f = self.private_file("guest_link_live", self.s.link(tok))
        try:    # nothing listens on port 9: the connection must fail, after the link was resolved
            rc, out = self.run_bridge("--url", "ws://127.0.0.1:9", "--guest-link-file", f, *self.named, timeout=90)
        except subprocess.TimeoutExpired as e:
            text = lambda x: x.decode() if isinstance(x, bytes) else (x or "")  # noqa: E731
            rc, out = None, text(e.stdout) + text(e.stderr)
        self.assertIn("guest link: a public link, into room " + JAM_ROOM, out)
        self.assertNotEqual(rc, 0)
        self.assertNotIn(tok, out)
        for lk in self.s.links[tok]["issued"]:
            self.assertNotIn(lk, out)
        for cred in list(self.s.credentials):
            self.assertNotIn(cred.split(".")[1], out)


if __name__ == "__main__":
    unittest.main()
