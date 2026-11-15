#!/usr/bin/env python3
"""A stand-in for the campus and its Matrix homeserver, on this machine's loopback, for the bridge's tests.
Never the real campus: it answers only the calls campus.py makes, with the shapes production returns (campus.py's
header), and hands out whatever LiveKit token it is given (--lk-token-file).

    campus_stub.py --port P --record FILE --cred-file FILE --lk-token-file FILE [--fault F] [--no-matrix-room]
                   [--livekit-url ws://127.0.0.1:17880]

Faults, for the checks that must fail on purpose:
  signin     the sign-in is refused (401)
  roomjoin   joining the Matrix room is refused (403)
  send1      the first line's send fails (500); later lines go through
  429once    the first send is rate-limited (429); its retry must carry the same transaction id
  notlinked  the right username and password, but no campus student is linked to the Matrix user (403
             MATRIX_NOT_LINKED, as production answers; production has signed in to Matrix by then and keeps that device)
  logoutfail the homeserver refuses the sign-out (500), so the device stays signed in
  edge1010   every call from urllib's default User-Agent ("Python-urllib/...") is refused the way the campus's
             Cloudflare edge refuses it (403, text/plain "error code: 1010"); any other agent gets through
  edgeall    every call is refused that way, whatever the agent
  edgechallenge  every call gets Cloudflare's challenge (403 with the header cf-mitigated: challenge, an HTML body)
  garbled-signin / garbled-logout  that call is answered with a line that isn't HTTP, and the connection closed
  truncated-signin  the sign-in reply (chunked) is cut off inside its chunk, and the connection closed
  cut-signin the sign-in reply stops short of its Content-Length, and the connection closed
  partial    the sign-in says 200 with the Matrix credentials but without the campus's own token
  redirect-logout  the sign-out is answered 302 to /elsewhere on the stub; a request there is recorded as "redirected"
  http-homeserver  the sign-in reply names a plain-http homeserver that isn't loopback (http://matrix.example.invalid)
  slow-signin  the sign-in is recorded as "signin_start" when it arrives and answered (as "none" does) 2 s later
  slow-signin-logout  the same, and the sign-out too ("logout_start", then answered 2 s later)
  messages401  every forward read of the room (dir=f) is answered 401 M_UNKNOWN_TOKEN, the way the homeserver answers
             once the device was signed out elsewhere; the sign-out itself is still accepted, so a test sees it tried
For relay.py the stub keeps the room's timeline: every accepted send goes into it, POST /_stub/inject {sender,
content} puts someone else's message there (tests only, no sign-in), and GET .../rooms/ROOM/messages pages through it
the way the Matrix client API does (tokens "t<position>"; dir=b with no from gives the end now).
Every call goes to --record as one JSON row: the time, the event, the User-Agent it came with, and for a send the room,
transaction id and text (test lines only). A send repeated with the same transaction id after it was accepted is
answered with the first event id and recorded as a repeat, the way a homeserver treats it; a retry after a refusal is
recorded as a retry.
"""
import argparse, json, os, re, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ROOM = "!class:stub"
HOST = "scratch-host"
FAULTS = ["none", "signin", "roomjoin", "send1", "429once", "notlinked", "logoutfail", "edge1010", "edgeall",
          "edgechallenge", "garbled-signin", "garbled-logout", "truncated-signin", "cut-signin", "partial",
          "redirect-logout", "http-homeserver", "slow-signin",
          "slow-signin-logout", "messages401"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--record", required=True)
    ap.add_argument("--cred-file", required=True)
    ap.add_argument("--lk-token-file", required=True)
    ap.add_argument("--fault", default="none", choices=FAULTS)
    ap.add_argument("--no-matrix-room", action="store_true")
    ap.add_argument("--livekit-url", default="ws://127.0.0.1:17880", help="the LiveKit address a class join answers with")
    a = ap.parse_args()
    cred = json.load(open(a.cred_file))
    lk_token = open(a.lk_token_file).read().strip()
    state = {"bearer": None, "mx": None, "joined": set(), "sends": 0, "txns": {}, "timeline": []}
    lock = threading.Lock()

    def record(**row):
        row["t"] = time.time()
        with lock, open(a.record, "a") as f:
            f.write(json.dumps(row) + "\n")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, obj, headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def raw(self, code, ctype, body, headers=None):
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def garbled(self):
            self.wfile.write(b"NOT-HTTP\r\n\r\n")
            self.close_connection = True

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def bearer(self):
            h = self.headers.get("Authorization", "")
            return h[7:] if h.startswith("Bearer ") else None

        def ua(self):
            return self.headers.get("User-Agent", "")

        def edge(self):
            """Cloudflare in front of the campus, as the faults ask: True if it refused this call."""
            if a.fault == "edgechallenge":
                record(ev="edge_refused", ok=False, code=403, ua=self.ua(), path=self.path)
                self.raw(403, "text/html; charset=UTF-8", b"<!DOCTYPE html><title>Just a moment...</title>",
                         {"cf-mitigated": "challenge"})
                return True
            if a.fault == "edgeall" or (a.fault == "edge1010" and self.ua().startswith("Python-urllib")):
                record(ev="edge_refused", ok=False, code=403, ua=self.ua(), path=self.path)
                self.raw(403, "text/plain; charset=UTF-8", b"error code: 1010")
                return True
            return False

        def do_GET(self):
            if self.edge():
                return
            if self.path == "/elsewhere":
                record(ev="redirected", ok=False, bearer=self.bearer() is not None, ua=self.ua())
                return self.reply(200, {})
            u = urlsplit(self.path)
            m = re.fullmatch(r"/_matrix/client/v3/rooms/([^/]+)/messages", u.path)
            if m:                       # the Matrix client API's /messages, for relay.py: tokens are "t<position>"
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if self.bearer() != state["mx"] or state["mx"] is None:
                    return self.reply(401, {"errcode": "M_UNKNOWN_TOKEN"})
                if unquote(m.group(1)) not in state["joined"]:
                    return self.reply(403, {"errcode": "M_FORBIDDEN"})
                if a.fault == "messages401" and q.get("dir") == "f":
                    record(ev="messages", dir="f", code=401, ua=self.ua())
                    return self.reply(401, {"errcode": "M_UNKNOWN_TOKEN"})
                with lock:
                    tl = list(state["timeline"])
                record(ev="messages", dir=q.get("dir"), ua=self.ua())
                if q.get("dir") == "b" and "from" not in q:
                    return self.reply(200, {"start": "t%d" % len(tl), "chunk": tl[-1:], "end": "t%d" % (len(tl) - 1)})
                at, limit = int(q.get("from", "t0")[1:]), int(q.get("limit", "10"))
                chunk = tl[at:at + limit]
                out = {"start": "t%d" % at, "chunk": chunk}
                if chunk:
                    out["end"] = "t%d" % (at + len(chunk))
                return self.reply(200, out)
            self.reply(404, {"errcode": "M_UNRECOGNIZED"})

        def do_POST(self):
            b = self.body()
            if self.edge():
                return
            if self.path == "/api/auth/login/matrix":
                if a.fault in ("slow-signin", "slow-signin-logout"):
                    record(ev="signin_start", ua=self.ua())
                    time.sleep(2.0)
                if a.fault == "garbled-signin":
                    record(ev="signin", ok=False, code=None, ua=self.ua())
                    return self.garbled()
                if a.fault == "signin" or b.get("username") != cred["username"] or b.get("password") != cred["password"]:
                    record(ev="signin", ok=False, ua=self.ua())
                    return self.reply(401, {"success": False, "error": "Invalid username or password"})
                if a.fault == "notlinked":
                    record(ev="signin", ok=False, code=403, ua=self.ua())
                    return self.reply(403, {"success": False, "code": "MATRIX_NOT_LINKED",
                                            "error": "This Matrix account is not linked to a campus student."})
                state["bearer"], state["mx"] = "campus-" + uuid.uuid4().hex, "mx-" + uuid.uuid4().hex
                port = self.server.server_address[1]
                hs = "http://matrix.example.invalid" if a.fault == "http-homeserver" else "http://127.0.0.1:%d" % port
                data = {"student": {}, "tokens": {"accessToken": state["bearer"], "refreshToken": "r"},
                        "matrixCredentials": {"matrixUserId": "@ada:stub", "accessToken": state["mx"],
                                              "homeserver": hs}}
                if a.fault == "partial":
                    data["tokens"] = {}
                record(ev="signin", ok=a.fault not in ("partial", "truncated-signin", "cut-signin"), ua=self.ua())
                if a.fault in ("truncated-signin", "cut-signin"):
                    whole = json.dumps({"success": True, "data": data}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    if a.fault == "truncated-signin":      # chunked, cut inside its chunk: http.client's IncompleteRead
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        self.wfile.write(b"%x\r\n" % len(whole) + whole[:20])
                    else:                                  # a Content-Length that never arrives: a short body
                        self.send_header("Content-Length", str(len(whole)))
                        self.end_headers()
                        self.wfile.write(whole[:20])
                    self.close_connection = True
                    return
                return self.reply(200, {"success": True, "data": data})
            m = re.fullmatch(r"/api/class/([^/]+)/join", self.path)
            if m:
                if self.bearer() != state["bearer"]:
                    return self.reply(401, {"success": False, "error": "Sign in to join the class."})
                if unquote(m.group(1)) != HOST:
                    return self.reply(404, {"success": False, "error": "That class isn't running right now."})
                room = None if a.no_matrix_room else ROOM
                record(ev="join_class", matrix_room=room, ua=self.ua())
                return self.reply(200, {"success": True, "data": {
                    "livekitUrl": a.livekit_url, "token": lk_token, "roomName": "scratch-class",
                    "title": "Scratch class", "hostName": "Scratch", "matrixRoomId": room, "matrixJoined": False,
                    "matrixStatus": "no-account" if room else None}})
            m = re.fullmatch(r"/_matrix/client/v3/join/([^/]+)", self.path)
            if m:
                if self.bearer() != state["mx"]:
                    return self.reply(401, {"errcode": "M_UNKNOWN_TOKEN"})
                if a.fault == "roomjoin":
                    record(ev="room_join", ok=False, ua=self.ua())
                    return self.reply(403, {"errcode": "M_FORBIDDEN"})
                state["joined"].add(unquote(m.group(1)))
                record(ev="room_join", ok=True, room=unquote(m.group(1)), ua=self.ua())
                return self.reply(200, {"room_id": unquote(m.group(1))})
            if self.path == "/_matrix/client/v3/logout":
                if a.fault == "slow-signin-logout":
                    record(ev="logout_start", ua=self.ua())
                    time.sleep(2.0)
                ok = self.bearer() == state["mx"] and state["mx"] is not None
                if ok and a.fault == "logoutfail":
                    record(ev="logout", ok=False, code=500, ua=self.ua())
                    return self.reply(500, {"errcode": "M_UNKNOWN"})
                if ok and a.fault == "garbled-logout":
                    record(ev="logout", ok=False, code=None, ua=self.ua())
                    return self.garbled()
                if ok and a.fault == "redirect-logout":
                    record(ev="logout", ok=False, code=302, ua=self.ua())
                    return self.reply(302, {}, {"Location": "/elsewhere"})
                record(ev="logout", ok=ok, ua=self.ua())
                state["mx"] = None
                return self.reply(200 if ok else 401, {})
            if self.path == "/elsewhere":
                record(ev="redirected", ok=False, bearer=self.bearer() is not None, ua=self.ua())
                return self.reply(200, {})
            if self.path == "/_stub/inject":    # the test puts someone else's message in the room's timeline
                with lock:
                    ev = dict(b, type=b.get("type", "m.room.message"),
                              event_id="$in" + str(len(state["timeline"])))
                    state["timeline"].append(ev)
                return self.reply(200, {"event_id": ev["event_id"]})
            self.reply(404, {"errcode": "M_UNRECOGNIZED"})

        def do_PUT(self):
            if self.edge():
                return
            m = re.fullmatch(r"/_matrix/client/v3/rooms/([^/]+)/send/m\.room\.message/([^/]+)", self.path)
            if not m:
                return self.reply(404, {"errcode": "M_UNRECOGNIZED"})
            room, txn = unquote(m.group(1)), unquote(m.group(2))
            b = self.body()
            if self.bearer() != state["mx"]:
                return self.reply(401, {"errcode": "M_UNKNOWN_TOKEN"})
            if room not in state["joined"]:
                return self.reply(403, {"errcode": "M_FORBIDDEN"})
            with lock:
                seen = state["txns"].get(txn, "new")     # "new", None (refused once) or the event id
                if seen not in ("new", None):
                    kind, ev, code = "repeat", seen, 200
                else:
                    if seen == "new":
                        state["sends"] += 1
                    if seen == "new" and state["sends"] == 1 and a.fault in ("send1", "429once"):
                        kind, ev, code = "refused", None, (500 if a.fault == "send1" else 429)
                        state["txns"][txn] = None
                    else:
                        kind, ev, code = ("retry" if seen is None else "accepted"), "$ev" + uuid.uuid4().hex, 200
                        state["txns"][txn] = ev
                        state["timeline"].append({"type": "m.room.message", "sender": "@ada:stub", "event_id": ev,
                                                  "content": b})
            record(ev="send", ok=code == 200, code=code, kind=kind, room=room, txn=txn,
                   text=b.get("body") if code == 200 else None, msgtype=b.get("msgtype"), ua=self.ua())
            if code != 200:
                return self.reply(code, {"errcode": "M_LIMIT_EXCEEDED" if code == 429 else "M_UNKNOWN",
                                         "retry_after_ms": 500})
            self.reply(200, {"event_id": ev})

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print("ready", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
