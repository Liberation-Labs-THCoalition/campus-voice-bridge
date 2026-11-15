"""A campus account for the bridge: sign in, join the class, chat in its Matrix room.

What the bridge expects of a Multiverse campus, as read from the campus's code in November 2026 (a campus that changes
these calls changes what works):
- The campus keeps no password for an invited account. The bridge signs in with the account's Matrix password:
  POST {campus}/api/auth/login/matrix {username, password} returns the campus tokens and the Matrix credentials
  {matrixUserId, accessToken, homeserver}. Each sign-in opens a new Matrix device; sign_out() logs it out.
- POST {campus}/api/class/{hostId}/join (Bearer) returns livekitUrl, token, roomName, matrixRoomId and matrixStatus.
  The class has to be running (its host started it in the last 12 hours).
- An auto-made account may come back with matrixStatus "no-account" (the campus looks the Matrix account up
  elsewhere), so the bridge joins the room itself: POST {homeserver}/_matrix/client/v3/join/{roomId}. That works for
  a public room; an invite-only one needs the campus admin's invite.
- A line goes to the room the way the web chat sends one: PUT .../rooms/{roomId}/send/m.room.message/{txnId},
  straight to the homeserver.
The credential file holds {"username": ..., "password": ...}, owner-only. It is read in-process and never logged, and
nothing here logs a token, a password, a reply body or a line's text. set_credential.py writes it.
Sign-in replies the bridge can meet:
- 401 whenever the homeserver didn't say yes to the campus's Matrix login: a wrong username or password, the
  homeserver's own rate limit, a deactivated account, or the homeserver itself failing (a 5xx from its proxy). No
  device is left.
- 403 when the Matrix user isn't linked to a campus student (code MATRIX_NOT_LINKED) or the student is banned or timed
  out. Both come after the campus has signed in to Matrix and drop that device's token, so a 403 means the password is
  right and leaves a "MultiverseCampus" device nobody can sign out.
- 429 past 30 tries in 15 minutes from one address, before any Matrix login.
- 500 for anything thrown: the fetch to the homeserver, or a database step after the Matrix login, so a 500 can leave
  a device too.
- A 403 whose body is "error code: 1010" is not the campus: Cloudflare, in front of it, refuses urllib's default
  User-Agent ("Python-urllib/3.12") before the app sees the request. So every
  call names this client honestly (USER_AGENT; it imitates no browser), and an edge refusal says so (CampusError.edge),
  so it can't be read as the campus's own 403. Allowing or blocking this agent is the campus team's setting.
Calls never follow a redirect (a 3xx is an error like any other), so a bearer token goes only where it was sent, and the
homeserver address in the sign-in reply must be https (plain http only on loopback, and only when the campus address
is loopback too: the stand-in, tests/campus_stub.py). A reply that isn't HTTP, or is cut off, is a CampusError too.

Guest links: a class's or a jam's guest link is https://CAMPUS/call/TOKEN, made by POST /api/invite with linkKind
"public" from inside the call. Its page asks POST {campus}/api/invite/TOKEN/guest-join {displayName, guestToken?} (no
sign-in), which answers {livekitToken, roomName, identity, guestToken, linkKind, inviterName}. No LiveKit address: the
page uses the one built into it, so the bridge still takes --url. The link is a credential (anyone holding it can
join), so it is read from an owner-only file and never logged.
The guestToken is the guest's own credential, guestId.HMAC, handed out at a first join and presented at the next one,
as the guest page keeps it: the guest then keeps its identity (guest:guestId), so a host's removal holds, because
guest-join refuses a guest the host removed from that room, with a 403. It is a credential too: whoever holds it can
join as that guest, and a second join under one identity disconnects the first. So it is kept owner-only and never
logged, and only the campus that issued it is shown it.
"""
import http.client, json, os, re, stat, time, urllib.error, urllib.parse, urllib.request, uuid

USER_AGENT = "agent-voice-bridge/1.0 (campus-voice-bridge)"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# The field bridge.py adds to each line it posts in a Matrix room (its value: the line's id). relay.py never speaks a
# message carrying it: a bridge signed in as the very ID the relay maps to it would otherwise hear its own line again.
BRIDGE_LINE_KEY = "org.thcoalition.avb.line"


class CampusError(Exception):
    """One named step that failed, with its HTTP status if there was one. Never a token or a reply body. edge is True
    when the campus's Cloudflare edge refused the call, so it never reached the campus."""

    def __init__(self, step, status=None, edge=False):
        super().__init__(step + " failed" + (" (HTTP %d)" % status if status else ""))
        self.step, self.status, self.edge = step, status, edge


def read_credentials(path):
    """(username, password) from an owner-only JSON file. Never follows a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise CampusError("reading the credential file") from None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise CampusError("the credential file (must be a regular file readable only by its owner)")
        try:
            d = json.load(f)
        except ValueError:
            raise CampusError("reading the credential file (not JSON)") from None
    if not (isinstance(d, dict) and isinstance(d.get("username"), str) and isinstance(d.get("password"), str)):
        raise CampusError("the credential file (needs username and password)")
    return d["username"], d["password"]


GUEST_PATH = re.compile(r"/call/([A-Za-z0-9_-]{8,128})/?")    # the campus's own check of a call link
GUEST_CREDENTIAL = re.compile(r"[a-f0-9]{16}\.[A-Za-z0-9_-]{1,128}")  # guestId.HMAC in base64url


def parse_guest_link(link):
    """(campus address, invite token) from a guest link, https://CAMPUS/call/TOKEN. Plain http only on loopback
    (tests). A campus link (/invite/TOKEN) brings someone to a spot on the map, not into a call, so it is refused."""
    u = urllib.parse.urlsplit(link.strip())
    m = GUEST_PATH.fullmatch(u.path)
    loopback = u.hostname in LOOPBACK
    if not (m and u.netloc and (u.scheme == "https" or (u.scheme == "http" and loopback))):
        raise CampusError("reading the guest link (want https://CAMPUS/call/TOKEN)")
    return "%s://%s" % (u.scheme, u.netloc), m.group(1)


def safe_base(url, allow_http):
    """url without its trailing slash if it is https (or, with allow_http, plain http to loopback) with a host and no
    user, password, query or fragment; else None."""
    try:
        p = urllib.parse.urlsplit(url)
        ok = bool(p.hostname) and not (p.username or p.password or p.query or p.fragment) and (
            p.scheme == "https" or (allow_http and p.scheme == "http" and p.hostname in LOOPBACK))
    except (ValueError, TypeError, AttributeError):
        ok = False
    return url.rstrip("/") if ok else None


def edge_refusal(e):
    """What the campus's Cloudflare edge said, if it and not the campus refused: a body starting "error code: 1xxx"
    (only the code is kept), or a challenge page (header cf-mitigated). Reads at most 64 bytes of the body."""
    try:
        if (e.headers.get("cf-mitigated") or "").strip().lower() == "challenge":
            return "a challenge page"
        m = re.match(rb"\s*error code: (1\d{3})\b", e.read(64) or b"")
        return "error code %s" % m.group(1).decode() if m else None
    except Exception:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                     # the 3xx then reaches the caller as an HTTPError


_OPENER = urllib.request.build_opener(_NoRedirect)


class Campus:
    RETRY_429_S = 2.0

    def __init__(self, campus_url, timeout=15):
        self.base = safe_base(campus_url, allow_http=True)
        if not self.base:
            raise CampusError("checking the campus address (https only, or http on this machine's loopback)")
        self.local = self.base.startswith("http:")      # the stand-in on loopback
        self.timeout = timeout
        self.bearer = self.mx_token = self.mx_user = self.homeserver = None
        self.last_status = None

    def _call(self, step, method, url, body, token=None):
        try:
            headers = {"User-Agent": USER_AGENT}
            if body is not None:                    # None: a GET, with no body
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                         headers=headers)
            if token:
                req.add_unredirected_header("Authorization", "Bearer " + token)
            with _OPENER.open(req, timeout=self.timeout) as r:
                code, raw = r.status, r.read(1 << 20)
        except urllib.error.HTTPError as e:
            edge = edge_refusal(e)
            try:
                e.close()
            except Exception:
                pass
            if edge:
                raise CampusError("%s (refused by the campus's Cloudflare edge, %s)" % (step, edge), e.code,
                                  edge=True) from None
            raise CampusError(step, e.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            raise CampusError(step) from None   # no answer, or not HTTP: unreachable, TLS, garbled, cut off
        self.last_status = code
        try:
            out = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise CampusError(step + " (reply not JSON)", code) from None
        if not isinstance(out, dict):
            raise CampusError(step + " (unexpected reply)", code)
        return out

    def sign_in(self, username, password):
        """Sign in with the account's Matrix password; returns the Matrix user id. The Matrix credentials in the reply
        are kept as soon as they are read, before the rest of it is checked, so a reply that fails later still leaves
        sign_out() the device to log out. A homeserver address that isn't https is never kept, nor sent the token."""
        out = self._call("campus sign-in", "POST", self.base + "/api/auth/login/matrix",
                         {"username": username, "password": password})
        code = self.last_status
        d = out.get("data") if isinstance(out.get("data"), dict) else {}
        mc = d.get("matrixCredentials") if isinstance(d.get("matrixCredentials"), dict) else {}
        user, token, hs = mc.get("matrixUserId"), mc.get("accessToken"), mc.get("homeserver")
        have_mx = all(isinstance(x, str) and x for x in (user, token, hs))
        if have_mx:
            hs = safe_base(hs, allow_http=self.local)
            if not hs:
                raise CampusError("campus sign-in (the homeserver address in the reply isn't https)", code)
            self.mx_user, self.mx_token, self.homeserver = user, token, hs
        tokens = d.get("tokens") if isinstance(d.get("tokens"), dict) else {}
        bearer = tokens.get("accessToken")
        if not (have_mx and isinstance(bearer, str) and bearer):
            raise CampusError("campus sign-in (unexpected reply)", code)
        self.bearer = bearer
        return user

    def join_class(self, host_id):
        out = self._call("class join", "POST",
                         "%s/api/class/%s/join" % (self.base, urllib.parse.quote(host_id, safe="")), {},
                         token=self.bearer)
        d = out.get("data")
        info = {k: d.get(k) for k in ("livekitUrl", "token", "roomName", "matrixRoomId", "matrixStatus")} \
            if isinstance(d, dict) else {}
        if not (isinstance(info.get("livekitUrl"), str) and isinstance(info.get("token"), str)
                and (info["matrixRoomId"] is None or isinstance(info["matrixRoomId"], str))):
            raise CampusError("class join (unexpected reply)")
        return info

    # What guest-join's refusals mean; the reply's own text is never read.
    GUEST_JOIN_REFUSALS = {400: "not a call link, or the name was refused", 403: "this guest was removed from the call",
                           404: "no such link", 409: "nobody is in that call now, or the link was made outside a call",
                           410: "the link has expired", 429: "too many joins from here; wait a few minutes"}

    def guest_join(self, invite_token, display_name, guest_credential=None):
        """Join by a guest link, as its page does: (LiveKit token, room name, link kind, guest credential or None). No
        sign-in and no bearer: the link is the credential. guest_credential is the guestToken this campus handed this
        guest before; presented, it keeps the guest's identity, so a host's removal still refuses it (403)."""
        url = "%s/api/invite/%s/guest-join" % (self.base, urllib.parse.quote(invite_token, safe=""))
        body = {"displayName": display_name}
        if guest_credential:
            body["guestToken"] = guest_credential
        try:
            out = self._call("guest join", "POST", url, body)
        except CampusError as e:
            if e.status in self.GUEST_JOIN_REFUSALS and not e.edge:     # an edge refusal keeps its own words
                raise CampusError("guest join (%s)" % self.GUEST_JOIN_REFUSALS[e.status], e.status) from None
            raise
        d = out.get("data")
        if not (isinstance(d, dict) and isinstance(d.get("livekitToken"), str) and d["livekitToken"]
                and isinstance(d.get("roomName"), str) and d["roomName"]):
            raise CampusError("guest join (unexpected reply)")
        cred = d.get("guestToken")
        cred = cred if isinstance(cred, str) and GUEST_CREDENTIAL.fullmatch(cred) else None
        return d["livekitToken"], d["roomName"], d.get("linkKind"), cred

    def _mx(self, path):
        return self.homeserver + "/_matrix/client/v3" + path

    def join_room(self, room_id):
        self._call("Matrix room join", "POST", self._mx("/join/" + urllib.parse.quote(room_id, safe="")), {},
                   token=self.mx_token)

    def post_line(self, room_id, text, msgtype="m.text", extra=None):
        """Post one line; returns its event id. A rate-limited send is tried once more with the same transaction id,
        which the homeserver treats as the same message, so a retry can't post it twice. relay.py posts its notices
        with msgtype "m.notice", which by Matrix's convention no bot answers, and which the relay never speaks.
        extra: more fields for the event's content (never msgtype or body). bridge.py marks each line it posts with
        {BRIDGE_LINE_KEY: line id}, and relay.py never speaks a message that carries that key."""
        url = self._mx("/rooms/%s/send/m.room.message/avb-%s" % (urllib.parse.quote(room_id, safe=""), uuid.uuid4().hex))
        body = dict(extra or {})
        body.update(msgtype=msgtype, body=text)
        try:
            out = self._call("Matrix send", "PUT", url, body, token=self.mx_token)
        except CampusError as e:
            if e.status != 429:
                raise
            time.sleep(self.RETRY_429_S)
            out = self._call("Matrix send", "PUT", url, body, token=self.mx_token)
        ev = out.get("event_id")
        if not isinstance(ev, str) or not ev:
            raise CampusError("Matrix send (no event id)")
        return ev

    def messages(self, room_id, from_token=None, direction="f", limit=50):
        """One page of a room's timeline, for relay.py: (events, start, end). The Matrix client API's own call (GET
        /_matrix/client/v3/rooms/{roomId}/messages), straight to the homeserver, not the campus's; the account must be
        in the room. With direction "b" and no from_token, start is the timeline's end now, so paging forward from it
        gives only what arrives later. end is None when there is nothing more yet."""
        q = {"dir": direction, "limit": str(limit)}
        if from_token:
            q["from"] = from_token
        out = self._call("Matrix read", "GET", self._mx("/rooms/%s/messages?%s" % (
            urllib.parse.quote(room_id, safe=""), urllib.parse.urlencode(q))), None, token=self.mx_token)
        chunk = out.get("chunk") if isinstance(out.get("chunk"), list) else []
        start, end = out.get("start"), out.get("end")
        return ([e for e in chunk if isinstance(e, dict)], start if isinstance(start, str) else None,
                end if isinstance(end, str) else None)

    def sign_out(self):
        """Log the Matrix device this sign-in opened out again. Best effort; the token is dropped either way.
        True only when the homeserver accepted the logout (set_credential.py --check reports it; the bridge
        doesn't look). A redirect, an error or a reply that isn't HTTP is False."""
        token, self.mx_token, self.bearer = self.mx_token, None, None
        if token and self.homeserver:
            try:
                self._call("Matrix sign-out", "POST", self._mx("/logout"), {}, token=token)
                return True
            except CampusError:
                pass
        return False
