#!/usr/bin/env python3
"""campus.py against the stand-in campus (tests/campus_stub.py), no LiveKit.

    python -I tests/test_campus.py

Synthetic test credentials only, written to a private temp folder that is removed at the end. Prints one PASS/FAIL row
per check; exits 1 on any FAIL. The faults are the checks failing on purpose: a refused sign-in, a refused room join,
a refused send, a 429, and the campus's Cloudflare edge refusing urllib's default agent,
replies that aren't HTTP or are cut off, a sign-in reply missing the campus's token, a redirected sign-out and a
plain-http homeserver. Last, bridge.py itself on the account route, stopped by SIGTERM and by SIGHUP while its LiveKit
join waits: it must sign its Matrix device out and exit 0.
"""
import json, os, shutil, signal, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import campus  # noqa: E402
from campus import Campus, CampusError, read_credentials  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        fails.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rows(path):
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except FileNotFoundError:
        return []


class Stub:
    def __init__(self, tmp, fault="none", no_room=False, livekit_url=None, name=None):
        self.port, self.record = free_port(), os.path.join(tmp, "stub-%s.jsonl" % (name or fault))
        args = [sys.executable, "-I", os.path.join(HERE, "campus_stub.py"), "--port", str(self.port), "--record",
                self.record, "--cred-file", os.path.join(tmp, "cred.json"), "--lk-token-file",
                os.path.join(tmp, "lk.token"), "--fault", fault] + (["--no-matrix-room"] if no_room else []) + (
                ["--livekit-url", livekit_url] if livekit_url else [])
        self.p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        assert self.p.stdout.readline().strip() == "ready"
        self.url = "http://127.0.0.1:%d" % self.port

    def events(self):
        return [r["ev"] for r in rows(self.record)]

    def close(self):
        self.p.kill()
        self.p.wait()


def main():
    tmp = tempfile.mkdtemp(prefix="campus-test-")
    os.chmod(tmp, 0o700)
    try:
        tests(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)      # it held a synthetic credential
    print("\n%s" % ("all passed" if not fails else "%d FAILED: %s" % (len(fails), ", ".join(fails))))
    return 1 if fails else 0


def tests(tmp):
    cred = os.path.join(tmp, "cred.json")
    with os.fdopen(os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump({"username": "ada-test", "password": "scratch-only-" + os.urandom(6).hex()}, f)
    open(os.path.join(tmp, "lk.token"), "w").write("scratch-lk-token")
    user, pw = read_credentials(cred)

    loose = os.path.join(tmp, "loose.json")
    with open(loose, "w") as f:
        json.dump({"username": "u", "password": "p"}, f)
    os.chmod(loose, 0o644)
    try:
        read_credentials(loose)
        check("a credential file others can read is refused", False)
    except CampusError as e:
        check("a credential file others can read is refused", True, str(e))

    s = Stub(tmp)
    try:
        c = Campus(s.url)
        check("sign-in returns the Matrix user", c.sign_in(user, pw) == "@ada:stub")
        info = c.join_class("scratch-host")
        check("class join gives the LiveKit token and the Matrix room",
              info["token"] == "scratch-lk-token" and info["matrixRoomId"] == "!class:stub"
              and info["matrixStatus"] == "no-account", json.dumps({k: info[k] for k in ("matrixRoomId", "matrixStatus")}))
        try:
            c.join_class("not-running")
            check("a class that isn't running is an error", False)
        except CampusError as e:
            check("a class that isn't running is an error", e.status == 404, str(e))
        c.join_room(info["matrixRoomId"])
        ev = c.post_line(info["matrixRoomId"], "First line, two sentences. Here's the second.")
        check("a line posts and comes back with an event id", ev.startswith("$ev"))
        check("sign-out reports that the homeserver logged the device out", c.sign_out() is True)
        check("a second sign-out has nothing to log out, and says so", c.sign_out() is False)
        r = rows(s.record)
        sends = [x for x in r if x["ev"] == "send"]
        check("the stand-in got the text as sent", sends and sends[-1]["text"] == "First line, two sentences. Here's the second."
              and sends[-1]["msgtype"] == "m.text")
        check("sign-out logs the device out", any(x["ev"] == "logout" and x["ok"] for x in r))
        agents = sorted({x.get("ua") for x in r})
        ua = getattr(campus, "USER_AGENT", None) or ""
        check("every call names the bridge in its User-Agent, and imitates neither urllib's default nor a browser",
              agents == [ua] and ua.startswith("agent-voice-bridge/") and "Mozilla" not in ua and "urllib" not in ua,
              str(agents))
    finally:
        s.close()

    # --- the campus's Cloudflare edge (review_campus_1 B1): it refuses urllib's default agent with "error code: 1010"
    s = Stub(tmp, "edge1010")
    try:
        try:
            got = Campus(s.url).sign_in(user, pw)
            check("edge: an edge that refuses urllib's default agent lets the bridge's own agent sign in",
                  got == "@ada:stub" and "edge_refused" not in s.events(), str(s.events()))
        except CampusError as e:
            check("edge: an edge that refuses urllib's default agent lets the bridge's own agent sign in", False,
                  str(e))
    finally:
        s.close()
    for fault, words in (("edgeall", "error code 1010"), ("edgechallenge", "a challenge page")):
        s = Stub(tmp, fault)
        try:
            c = Campus(s.url)
            try:
                c.sign_in(user, pw)
                check("edge: a refusal by the edge (%s) is named as one, never as the campus's own 403" % fault, False)
            except CampusError as e:
                check("edge: a refusal by the edge (%s) is named as one, never as the campus's own 403" % fault,
                      getattr(e, "edge", None) is True and e.status == 403 and "Cloudflare edge" in e.step
                      and words in e.step and c.mx_token is None and pw not in str(e), str(e))
        finally:
            s.close()

    # --- answers that aren't HTTP, or are cut off (review_campus_0 S2): a CampusError, never http.client's own
    for fault, what, status in (("garbled-signin", "a sign-in answered with a line that isn't HTTP", None),
                                ("truncated-signin", "a chunked sign-in reply cut off (IncompleteRead)", None),
                                ("cut-signin", "a sign-in reply short of its Content-Length", 200)):
        s = Stub(tmp, fault)
        try:
            c = Campus(s.url)
            try:
                c.sign_in(user, pw)
                check("garbled: %s is a CampusError" % what, False)
            except CampusError as e:
                check("garbled: %s is a CampusError" % what, e.status == status and c.mx_token is None, str(e))
            except Exception as e:
                check("garbled: %s is a CampusError" % what, False, "got %s" % type(e).__name__)
        finally:
            s.close()
    s = Stub(tmp, "garbled-logout")
    try:
        c = Campus(s.url)
        c.sign_in(user, pw)
        try:
            out = c.sign_out()
            check("garbled: a sign-out answered with a line that isn't HTTP reports False", out is False, str(out))
        except Exception as e:
            check("garbled: a sign-out answered with a line that isn't HTTP reports False", False, type(e).__name__)
    finally:
        s.close()
    for bad, what in (("no-scheme/x", "an address Request() refuses (ValueError, raised outside the try before)"),
                      ("http://127.0.0.1:abc/x",
                       "an address http.client refuses (InvalidURL, a port that isn't a number)")):
        try:
            Campus("http://127.0.0.1:1")._call("a step", "POST", bad, {})
            check("garbled: %s is a CampusError" % what, False)
        except CampusError as e:
            check("garbled: %s is a CampusError" % what, e.status is None, str(e))
        except Exception as e:
            check("garbled: %s is a CampusError" % what, False, type(e).__name__)

    # --- a 200 without the campus's token (review_campus_0 S3): the Matrix device it opened is still signed out
    s = Stub(tmp, "partial")
    try:
        c = Campus(s.url)
        try:
            c.sign_in(user, pw)
            check("partial: a 200 without the campus's token is an error that keeps the Matrix token", False)
        except CampusError as e:
            check("partial: a 200 without the campus's token is an error that keeps the Matrix token",
                  e.status == 200 and "unexpected reply" in e.step and c.mx_token is not None and c.bearer is None,
                  str(e))
        check("partial: sign_out() then logs that device out (one logout at the stand-in)",
              c.sign_out() is True and s.events().count("logout") == 1, str(s.events()))
    finally:
        s.close()

    # --- redirects and the homeserver's scheme (review_campus_0 N4)
    s = Stub(tmp, "redirect-logout")
    try:
        c = Campus(s.url)
        c.sign_in(user, pw)
        out = c.sign_out()
        red = [x for x in rows(s.record) if x["ev"] == "redirected"]
        check("redirect: a sign-out answered 302 is not followed, and reports False", out is False and not red,
              "sign_out=%s; requests at the redirect target: %d" % (out, len(red)))
    finally:
        s.close()
    s = Stub(tmp, "http-homeserver")
    try:
        c = Campus(s.url)
        try:
            c.sign_in(user, pw)
            check("homeserver: a plain-http homeserver off loopback is refused and never kept", False)
        except CampusError as e:
            check("homeserver: a plain-http homeserver off loopback is refused and never kept",
                  "isn't https" in e.step and c.mx_token is None and c.homeserver is None and c.sign_out() is False,
                  str(e))
    finally:
        s.close()

    # --- the campus address itself (review_campus_1 N3): https, or plain http to loopback only
    for url, ok in (("https://campus.themultiverse.school", True), ("http://127.0.0.1:5", True),
                    ("http://localhost:5/", True), ("http://campus.themultiverse.school", False),
                    ("https://user:pw@campus.themultiverse.school", False), ("ftp://127.0.0.1", False),
                    ("https://campus.themultiverse.school/?a=1", False), ("campus.themultiverse.school", False)):
        try:
            Campus(url)
            got = True
        except CampusError:
            got = False
        check("address: Campus(%r) is %s" % (url, "taken" if ok else "refused, before any request"), got == ok)

    s = Stub(tmp, "signin")
    try:
        try:
            Campus(s.url).sign_in(user, pw)
            check("fault: a refused sign-in is an error", False)
        except CampusError as e:
            check("fault: a refused sign-in is an error", e.status == 401 and pw not in str(e), str(e))
    finally:
        s.close()

    s = Stub(tmp, "notlinked")
    try:
        c = Campus(s.url)
        try:
            c.sign_in(user, pw)
            check("fault: a Matrix user not linked to a student (403) is an error", False)
        except CampusError as e:
            check("fault: a Matrix user not linked to a student (403) is an error",
                  e.status == 403 and not getattr(e, "edge", False) and c.mx_token is None and pw not in str(e), str(e))
    finally:
        s.close()

    s = Stub(tmp, "logoutfail")
    try:
        c = Campus(s.url)
        c.sign_in(user, pw)
        check("fault: a refused sign-out is reported (False) and the token is dropped anyway",
              c.sign_out() is False and c.mx_token is None and c.bearer is None)
    finally:
        s.close()

    s = Stub(tmp, "429once")
    try:
        c = Campus(s.url)
        c.RETRY_429_S = 0.2
        c.sign_in(user, pw)
        c.join_room("!class:stub")
        c.post_line("!class:stub", "rate limited once")
        sends = [x for x in rows(s.record) if x["ev"] == "send"]
        check("fault: a 429 is retried once with the same transaction id",
              [x["kind"] for x in sends] == ["refused", "retry"] and sends[0]["txn"] == sends[1]["txn"],
              str([(x["kind"], x["code"]) for x in sends]))
        check("fault: after the 429 the line is in the room once",
              sum(1 for x in sends if x["ok"]) == 1)
    finally:
        s.close()

    s = Stub(tmp, "send1")
    try:
        c = Campus(s.url)
        c.sign_in(user, pw)
        c.join_room("!class:stub")
        try:
            c.post_line("!class:stub", "this one fails")
            check("fault: a refused send is an error, not retried", False)
        except CampusError as e:
            n = sum(1 for x in rows(s.record) if x["ev"] == "send")
            check("fault: a refused send is an error, not retried", e.status == 500 and n == 1, f"{e}; {n} attempt(s)")
        c.post_line("!class:stub", "the next one goes")
        check("fault: the next line after a refused send goes through",
              [x["kind"] for x in rows(s.record) if x["ev"] == "send"] == ["refused", "accepted"])
    finally:
        s.close()

    s = Stub(tmp, "roomjoin")
    try:
        c = Campus(s.url)
        c.sign_in(user, pw)
        try:
            c.join_room("!class:stub")
            check("fault: a refused room join is an error", False)
        except CampusError as e:
            check("fault: a refused room join is an error", e.status == 403, str(e))
    finally:
        s.close()


    # --- a guest link file the bridge won't use stops it at the start, before it signs in to the campus at all
    s = Stub(tmp, name="badlink")
    try:
        tts = os.path.join(tmp, "tts-badlink")
        os.mkdir(tts, 0o700)
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(ROOT, "bridge.py"), "--campus", s.url, "--host-id",
                            "scratch-host", "--cred-file", cred, "--url", "ws://127.0.0.1:9", "--guest-link-file",
                            os.path.join(tmp, "no-such-link"), "--guest-name", "Ada", "--run-dir",
                            os.path.join(tmp, "run-badlink"), "--tts-socket", os.path.join(tts, "tts.sock")],
                           capture_output=True, text=True, timeout=60)
        check("a missing guest link file ends the bridge with 2 before any campus call",
              p.returncode == 2 and s.events() == [] and "no-such-link" in p.stderr,
              "rc=%s events=%s" % (p.returncode, s.events()))
    finally:
        s.close()

    # --- SIGTERM and SIGHUP (systemd, kill, a closed ssh session) skipped the shutdown: the call wasn't left and the
    # account route's Matrix device stayed signed in. A socket that takes the LiveKit connection and never answers
    # keeps the bridge in its join, after the campus sign-in, until the signal comes.
    hang = socket.socket()
    hang.bind(("127.0.0.1", 0))
    hang.listen(8)
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            s = Stub(tmp, livekit_url="ws://127.0.0.1:%d" % hang.getsockname()[1], name=sig.name)
            tts = os.path.join(tmp, "tts-" + sig.name)
            os.mkdir(tts, 0o700)
            p = subprocess.Popen([sys.executable, "-I", "-B", os.path.join(ROOT, "bridge.py"), "--campus", s.url,
                                  "--host-id", "scratch-host", "--cred-file", cred, "--run-dir",
                                  os.path.join(tmp, "run-" + sig.name), "--tts-socket", os.path.join(tts, "tts.sock")],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                end = time.monotonic() + 30
                while time.monotonic() < end and "join_class" not in s.events():
                    time.sleep(0.1)
                time.sleep(1.0)                         # the LiveKit join is under way, and waits on the silent socket
                waiting = p.poll() is None
                p.send_signal(sig)
                out, _ = p.communicate(timeout=30)
                r = rows(s.record)
                ev = [x["ev"] for x in r]
                out_ok = any(x["ev"] == "logout" and x["ok"] for x in r) and "join_class" in ev and \
                    ev.index("logout") > ev.index("join_class")
                check("%s: the bridge leaves its join, signs the Matrix device out and exits 0" % sig.name,
                      waiting and p.returncode == 0 and out_ok and "Traceback" not in out and sig.name in out,
                      "waiting=%s rc=%s events=%s" % (waiting, p.returncode, ev))
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                s.close()
    finally:
        hang.close()

    # --- SIGTERM while the campus sign-in is still waiting for its answer: cancelling the run can't stop the sign-in's
    # thread, which opens a Matrix device after shutdown() looked for one. main() signs that device out once the loop
    # has closed. The stand-in answers the sign-in 2 s after it arrives.
    s = Stub(tmp, "slow-signin")
    tts = os.path.join(tmp, "tts-slow")
    os.mkdir(tts, 0o700)
    p = subprocess.Popen([sys.executable, "-I", "-B", os.path.join(ROOT, "bridge.py"), "--campus", s.url, "--host-id",
                          "scratch-host", "--cred-file", cred, "--run-dir", os.path.join(tmp, "run-slow"),
                          "--tts-socket", os.path.join(tts, "tts.sock")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        end = time.monotonic() + 30
        while time.monotonic() < end and "signin_start" not in s.events():
            time.sleep(0.05)
        waiting = p.poll() is None
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        r = rows(s.record)
        ev = [x["ev"] for x in r]
        late_ok = "signin" in ev and "logout" in ev and ev.index("logout") > ev.index("signin") and \
            any(x["ev"] == "logout" and x["ok"] for x in r)
        check("SIGTERM during the sign-in: the device it opens afterwards is signed out, no class joined, exit 0",
              waiting and p.returncode == 0 and late_ok and "join_class" not in ev and "Traceback" not in out,
              "waiting=%s rc=%s events=%s" % (waiting, p.returncode, ev))
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        s.close()

    # --- and another SIGTERM while that late sign-out waits for its answer: the loop's handlers are gone by then, and
    # SIGTERM's default would end the bridge before the device is signed out, so signals are held off until it is.
    # The stand-in answers both the sign-in and the sign-out 2 s after they arrive.
    s = Stub(tmp, "slow-signin-logout")
    tts = os.path.join(tmp, "tts-slow2")
    os.mkdir(tts, 0o700)
    p = subprocess.Popen([sys.executable, "-I", "-B", os.path.join(ROOT, "bridge.py"), "--campus", s.url, "--host-id",
                          "scratch-host", "--cred-file", cred, "--run-dir", os.path.join(tmp, "run-slow2"),
                          "--tts-socket", os.path.join(tts, "tts.sock")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        end = time.monotonic() + 30
        while time.monotonic() < end and "signin_start" not in s.events():
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)
        end = time.monotonic() + 30
        while time.monotonic() < end and "logout_start" not in s.events():
            time.sleep(0.05)
        late = "logout_start" in s.events() and p.poll() is None
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        r = rows(s.record)
        check("a second SIGTERM during that late sign-out is held off: the device is signed out, exit 0",
              late and p.returncode == 0 and any(x["ev"] == "logout" and x["ok"] for x in r) and "Traceback" not in out,
              "late=%s rc=%s events=%s" % (late, p.returncode, [x["ev"] for x in r]))
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        s.close()


if __name__ == "__main__":
    sys.exit(main())
