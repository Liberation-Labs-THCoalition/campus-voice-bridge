#!/usr/bin/env python3
"""The chat into a call, and the call into the chat, beside bridge.py: relay.py voice | relay.py captions.

    relay.py voice    --campus https://CAMPUS --cred-file run/campus.json --room '!ROOM:SERVER'
                      --map @agent:SERVER[=RUN_DIR] [--map ...] [--run-dir run] [--until HH:MM] [--every 3]
                      [--max-chars 600]
    relay.py captions --campus https://CAMPUS --cred-file run/campus.json --room '!ROOM:SERVER' --heard FILE
                      [--until HH:MM] [--every 2] [--max-chars 600]

voice: for an agent that types in a Matrix room (a class's chat) and speaks in the call through its own bridge. The
relay reads the room. Each new m.text message from a Matrix ID given with --map goes into that ID's bridge, as a line
in RUN_DIR/say/ written exactly as say.py writes one (say.write_line), so its text never sits on a command line. The
message is already in the room, so what is heard can still be read. Not spoken:
- a message whose first non-space characters are "//": a note, for the people in the chat only;
- an m.notice (by Matrix's convention a bot's message, which nothing answers), and the relay's own posts;
- anything from the account the relay signs in with, even if it is given with --map: on the account route a bridge
  posts each line as its account, so speaking that account's messages could go round for ever;
- a line a bridge posted: its content carries campus.BRIDGE_LINE_KEY. A bridge on the account route that signs in as
  the very ID it is mapped to here (not the relay's account) posts each line it speaks as that ID, so without the mark
  the relay would queue the line again, and again;
- an edit (its text is already in the room, and was spoken or skipped when it came), and the quote at the top of a
  reply, which is someone else's words;
- a message longer than --max-chars (at most bridge.py's 1500, where a line is cut).
Each Matrix ID speaks through its own bridge only: one RUN_DIR per ID, never two IDs in one voice. The relay also
follows each bridge's RUN_DIR/participants.jsonl (bridge.py writes it) and posts "(joined: NAME)", "(left: NAME)" and
"(in the call: NAME)" in the room as m.notice. When two bridges in one call see the same person join, the notice is
posted once.

captions: follows a file of JSON lines {"speaker": ..., "text": ...} that a caption listener writes, and posts each
new line in the room as an m.notice "Speaker: text" (cut at --max-chars). Campus NPCs read only m.text, so they don't
see these (README.md, Campus NPCs).

Both modes sign in through campus.py with the relay's own campus account, the way bridge.py's account route does (the
password read in-process from an owner-only file, and dropped), and log that Matrix device out when they stop:
Ctrl-C, SIGTERM or SIGHUP, or --until (the next HH:MM, local time). Only lines written after the relay starts are
read, in the room and in each file, so a restart never posts or speaks anything twice. The files it acts on must be in
folders only this account can change (control.py's unsafe_dir), since what lands in them is spoken in an agent's voice
or posted under this account. Links and FIFOs are never read.

The log keeps Matrix IDs, lengths, counts and kinds of notice, never a message's text, a caption or a name.
Exit codes: 0 stopped (a signal, or --until); 1 an error, or the Matrix sign-in stopped working; 2 bad arguments, a
file or folder it won't use, or the campus refused the sign-in or the room.
"""
import argparse, json, logging, os, re, signal, stat, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                # -I leaves the script's own folder off the path
from campus import BRIDGE_LINE_KEY, Campus, CampusError, read_credentials  # noqa: E402
from control import make_private_dirs, unsafe_dir  # noqa: E402
from say import write_line  # noqa: E402

MAX_LINE = 1500                         # bridge.py's MAX_LINE: a longer line would be cut there
NAME_CHARS = 80                         # bridge.py's PARTICIPANT_NAME
NOTICE = {"joined": "(joined: %s)", "left": "(left: %s)", "present": "(in the call: %s)"}
SAME_NOTICE_S = 10.0                    # the same notice from two bridges within this long is posted once
MAX_READ = 1 << 20                      # bytes read from a followed file in one go
PAGE = 50                               # room events asked for in one read
SIGN_OUT_S = 20.0                       # the whole sign-out at the end, however slowly the homeserver answers
QUIET_S = 60.0                          # the same failure is logged again only after this long
MXID = re.compile(r"@[^:\s]+:[^\s=]+")
ROOM_ID = re.compile(r"![^:\s/]+:[^\s/]+")
UNTIL = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
log = logging.getLogger("relay")


# ---------------------------------------------------------------------------------------------- what each event does
def is_note(body):
    """A note, never spoken: the first non-space characters are '//'."""
    return isinstance(body, str) and body.lstrip().startswith("//")


def relation(content):
    rel = content.get("m.relates_to")
    return rel if isinstance(rel, dict) else {}


def without_reply_quote(content):
    """A message's body without the quote of the message it replies to: Matrix's reply fallback, lines starting with
    '>' at the top, then a blank line. Those are someone else's words."""
    body = content.get("body")
    if not (isinstance(body, str) and isinstance(relation(content).get("m.in_reply_to"), dict)):
        return body
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    if i and i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:]) if i else body


def route_events(events, senders, me, max_chars, sent_ids=()):
    """What each new room message does: ("speak", sender, text), spoken through senders[sender]'s bridge, or
    ("skip", why, sender). Events that aren't messages (state, reactions, encrypted ones) are left out."""
    out = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "m.room.message":
            continue
        sender = ev.get("sender")
        content = ev.get("content") if isinstance(ev.get("content"), dict) else {}
        if ev.get("event_id") in sent_ids:
            out.append(("skip", "own post", sender))
        elif BRIDGE_LINE_KEY in content:
            out.append(("skip", "bridge line", sender))
        elif content.get("msgtype") != "m.text" or not isinstance(content.get("body"), str):
            out.append(("skip", "not text", sender))
        elif sender == me:
            out.append(("skip", "own account", sender))
        elif sender not in senders:
            out.append(("skip", "not allowlisted", sender))
        elif relation(content).get("rel_type") == "m.replace":
            out.append(("skip", "edit", sender))
        else:
            text = without_reply_quote(content).strip()
            if is_note(text):
                out.append(("skip", "note", sender))
            elif not text:
                out.append(("skip", "empty", sender))
            elif len(text) > max_chars:
                out.append(("skip", "too long", sender))
            else:
                out.append(("speak", sender, text))
    return out


def clean_name(name):
    return "".join(c for c in name if c.isprintable()).strip()[:NAME_CHARS] if isinstance(name, str) else ""


def notice_text(event, name):
    fmt, name = NOTICE.get(event), clean_name(name)
    return fmt % name if fmt and name else None


def parse_participant_line(line):
    """A row of RUN/participants.jsonl as the notice to post, or None."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return notice_text(d.get("event"), d.get("name")) if isinstance(d, dict) else None


def caption_text(line, max_chars=MAX_LINE):
    """A caption line, {"speaker": ..., "text": ...}, as "Speaker: text" (cut at max_chars), or None."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not (isinstance(d, dict) and isinstance(d.get("text"), str) and d["text"].strip()):
        return None
    msg = "%s: %s" % (clean_name(d.get("speaker")) or "Someone", " ".join(d["text"].split()))
    return msg if len(msg) <= max_chars else msg[:max_chars - 3] + "..."


class Notices:
    """The same notice from two bridges in one call (both see a person join) is posted once."""

    def __init__(self, window=SAME_NOTICE_S):
        self.window, self.last = window, {}

    def fresh(self, text, now=None):
        now = time.monotonic() if now is None else now
        self.last = {t: at for t, at in self.last.items() if now - at < self.window}
        if text in self.last:
            return False
        self.last[text] = now
        return True


class Tail:
    """Whole lines added to a file after this starts, never its history; a line still being written waits for its
    newline. The file may not exist yet. A link, a FIFO or anything but a regular file is never read (open with
    O_NOFOLLOW and O_NONBLOCK, then fstat), and is reported once. A file that shrinks or is replaced is read from its
    start."""

    def __init__(self, path):
        self.path, self.pos, self.ino, self.refused = path, 0, None, None
        st = self._stat()
        if st:
            self.pos, self.ino = st.st_size, st.st_ino

    def _stat(self):
        try:
            st = os.lstat(self.path)
        except OSError:
            return None
        return st if stat.S_ISREG(st.st_mode) else None

    def lines(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        except OSError as e:
            return self._refuse("can't open it (%s)" % type(e).__name__)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return self._refuse("not a regular file")
            self.refused = None
            if st.st_ino != self.ino or st.st_size < self.pos:
                self.pos, self.ino = 0, st.st_ino       # replaced or cut short: a new file
            f.seek(self.pos)
            data = f.read(MAX_READ)
        whole = data.rfind(b"\n") + 1
        if not whole and len(data) == MAX_READ:
            whole = len(data)                           # one line longer than MAX_READ: taken as it is
        self.pos += whole
        return [ln for ln in data[:whole].decode("utf-8", "replace").splitlines() if ln.strip()]

    def _refuse(self, why):
        if self.refused != why:
            log.warning("not reading %s: %s", self.path, why)
            self.refused = why
        return []


# ---------------------------------------------------------------------------------------------- arguments
def parse_until(s, now=None):
    """The next HH:MM (local time) as epoch seconds, today or else tomorrow; None for none. Raises ValueError."""
    if not s:
        return None
    m = UNTIL.fullmatch(s)
    if not m:
        raise ValueError("want HH:MM")
    now = time.time() if now is None else now
    t = time.localtime(now)
    at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
    if at <= now:
        t = time.localtime(at + 86400 + 3600)           # a day on, past any change of clocks
        at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
    return at


def parse_map(entries, default_run_dir):
    """{Matrix ID: RUN_DIR} from --map MXID[=RUN_DIR]. Raises ValueError: a bad ID, an ID given twice, or two IDs
    in one bridge (one voice speaking two agents' words)."""
    senders = {}
    for e in entries:
        mxid, eq, run_dir = e.partition("=")
        mxid = mxid.strip()
        if not MXID.fullmatch(mxid):
            raise ValueError("%r: want @user:server[=RUN_DIR]" % mxid)
        if mxid in senders:
            raise ValueError("%s is given twice" % mxid)
        run_dir = os.path.abspath(run_dir.strip() if eq and run_dir.strip() else default_run_dir)
        if run_dir in senders.values():
            raise ValueError("%s: one bridge per agent, and %s is already another one's" % (mxid, run_dir))
        senders[mxid] = run_dir
    return senders


def build_parser():
    ap = argparse.ArgumentParser(description="The chat into a call, and the call into the chat, beside bridge.py. "
                                             "See the module docstring and README.md.")
    sub = ap.add_subparsers(dest="mode", required=True)
    for mode, every in (("voice", 3.0), ("captions", 2.0)):
        p = sub.add_parser(mode)
        p.add_argument("--campus", required=True, help="the campus's address")
        p.add_argument("--cred-file", required=True,
                       help="owner-only JSON with the relay account's Matrix username and password "
                            "(set_credential.py writes it)")
        p.add_argument("--room", required=True, help="the Matrix room's id, !ROOM:SERVER (a class page shows it)")
        p.add_argument("--until", default="", help="stop at the next HH:MM, local time")
        p.add_argument("--every", type=float, default=every, help="seconds between reads (0.5 to 60)")
        p.add_argument("--max-chars", type=int, default=600,
                       help="voice: longer messages aren't spoken; captions: longer lines are cut (1 to %d)" % MAX_LINE)
        if mode == "voice":
            p.add_argument("--map", action="append", required=True, metavar="MXID[=RUN_DIR]",
                           help="a Matrix ID whose messages are spoken, and its bridge's --run-dir (repeatable)")
            p.add_argument("--run-dir", default=os.path.join(HERE, "run"),
                           help="the bridge's run folder for a --map without =RUN_DIR (default: run/)")
        else:
            p.add_argument("--heard", required=True, help="the caption file, JSON lines {speaker, text}")
    return ap


def check_args(ap, a):
    """Refuse what it won't use (exit 2) before anything signs in."""
    if not ROOM_ID.fullmatch(a.room):
        ap.error("--room: want a room id, !ROOM:SERVER")
    try:
        a.deadline = parse_until(a.until)
    except ValueError as e:
        ap.error("--until: %s" % e)
    if not 0.5 <= a.every <= 60:
        ap.error("--every: 0.5 to 60 seconds")
    if not 1 <= a.max_chars <= MAX_LINE:
        ap.error("--max-chars: 1 to %d" % MAX_LINE)
    if a.mode == "voice":
        try:
            a.senders = parse_map(a.map, a.run_dir)
        except ValueError as e:
            ap.error("--map %s" % e)
        for run_dir in a.senders.values():
            try:
                make_private_dirs([os.path.join(run_dir, "say")])
            except OSError as e:
                ap.error("%s: can't make its say/ (%s)" % (run_dir, type(e).__name__))
            for d in (run_dir, os.path.join(run_dir, "say")):
                why = unsafe_dir(d)
                if why:
                    ap.error("%s: %s (what lands there is spoken in an agent's voice)" % (d, why))
    else:
        a.heard = os.path.abspath(a.heard)
        why = unsafe_dir(os.path.dirname(a.heard))
        if why:
            ap.error("--heard: its folder %s: %s (what lands there is posted under this account)"
                     % (os.path.dirname(a.heard), why))


# ---------------------------------------------------------------------------------------------- the room, and the loops
class Room:
    """One Matrix room, through a signed-in Campus: read what is new, post a notice."""

    def __init__(self, campus, room_id):
        self.campus, self.room, self.sent, self.token = campus, room_id, set(), None

    def start(self):
        self.campus.join_room(self.room)            # the account may not be in it yet; a member's join changes nothing
        _, self.token, _ = self.campus.messages(self.room, direction="b", limit=1)
        if not self.token:
            raise CampusError("Matrix read (no place in the timeline)")

    def new_events(self):
        events, _, end = self.campus.messages(self.room, self.token, "f", PAGE)
        if end:
            self.token = end
        return events

    def notice(self, text):
        ev = self.campus.post_line(self.room, text, msgtype="m.notice")
        self.sent.add(ev)
        return ev


class Relay:
    def __init__(self, a, campus):
        self.a, self.campus, self.room = a, campus, Room(campus, a.room)
        self.me, self.stopping, self.counts = None, None, {}
        self.notices, self.failed, self.failed_at = Notices(), None, 0.0

    def stop(self, sig, *_):
        """A signal's handler: the loop ends at its next look, then the device is signed out. Another signal after
        the first is ignored, so the sign-out finishes."""
        name = signal.Signals(sig).name
        if self.stopping:
            log.warning("%s: already stopping; the sign-out finishes first", name)
            return
        log.warning("%s: stopping", name)
        self.stopping = name

    def done(self):
        if self.stopping:
            return True
        if self.a.deadline and time.time() >= self.a.deadline:
            log.info("--until %s: stopping", self.a.until)
            self.stopping = "until"
            return True
        return False

    def nap(self, seconds):
        end = time.monotonic() + seconds
        while not self.done() and time.monotonic() < end:
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))

    def failure(self, what, e):
        """Log a failed read or post, but the same one again only after QUIET_S. A 401 means the Matrix sign-in has
        stopped working (the device was signed out elsewhere): that ends the relay with exit 1."""
        if isinstance(e, CampusError) and e.status == 401:
            raise e
        kind = "%s: %s" % (what, e if isinstance(e, CampusError) else type(e).__name__)
        if kind != self.failed or time.monotonic() - self.failed_at > QUIET_S:
            log.warning("%s", kind)
            self.failed, self.failed_at = kind, time.monotonic()

    def post(self, text, kind):
        if self.notices.fresh(text):
            self.room.notice(text)
            self.counts[kind] = self.counts.get(kind, 0) + 1
            log.info("notice posted (%s)", kind)

    def sign_in(self):
        user, password = read_credentials(self.a.cred_file)
        self.me = self.campus.sign_in(user, password)
        del password
        self.room.start()

    def voice(self):
        senders = self.a.senders
        if self.me in senders:
            log.warning("%s is the relay's own account: its messages are never spoken (a bridge on the account route "
                        "posts each line as that account, so they would go round again)", self.me)
        log.info("voice: reading %s for %d agent(s): %s", self.a.room, len(senders), ", ".join(sorted(senders)))
        tails = {d: Tail(os.path.join(d, "participants.jsonl")) for d in set(senders.values())}
        while not self.done():
            try:
                for act in route_events(self.room.new_events(), senders, self.me, self.a.max_chars, self.room.sent):
                    if act[0] == "speak":
                        sender = act[1]
                        try:
                            line_id, _ = write_line(senders[sender], act[2])
                            self.counts["spoken"] = self.counts.get("spoken", 0) + 1
                            log.info("voice: a line from %s (%d chars) queued as %s", sender, len(act[2]), line_id)
                        except OSError as e:
                            log.error("voice: a line from %s couldn't be queued (%s)", sender, type(e).__name__)
                    elif act[1] in ("note", "too long", "edit", "own account", "bridge line"):
                        log.info("voice: %s from %s, not spoken", act[1], act[2])
            except (CampusError, OSError, ValueError) as e:
                self.failure("room read", e)
            for tail in tails.values():
                for line in tail.lines():
                    msg = parse_participant_line(line)
                    if msg:
                        try:
                            self.post(msg, msg[1:msg.index(":")])
                        except (CampusError, OSError, ValueError) as e:
                            self.failure("notice", e)
            self.nap(self.a.every)

    def captions(self):
        tail = Tail(self.a.heard)
        log.info("captions: following new lines of %s into %s", self.a.heard, self.a.room)
        while not self.done():
            for line in tail.lines():
                msg = caption_text(line, self.a.max_chars)
                if msg:
                    try:
                        self.room.notice(msg)
                        self.counts["captions"] = self.counts.get("captions", 0) + 1
                    except (CampusError, OSError, ValueError) as e:
                        self.failure("caption post", e)
            self.nap(self.a.every)

    def sign_out(self):
        """Log the Matrix device out, with at most SIGN_OUT_S for it in all (each socket operation has the campus's
        timeout, not the request), in a daemon thread left behind if the time runs out. Never raises."""
        if not self.campus.mx_token:
            return
        out = []

        def attempt():
            try:
                out.append(self.campus.sign_out())
            except Exception as e:
                out.append(e)
        t = threading.Thread(target=attempt, name="sign_out", daemon=True)
        t.start()
        t.join(SIGN_OUT_S)
        if not out:
            log.error("Matrix sign-out: no answer within %.0f s; the device may still be signed in", SIGN_OUT_S)
        elif isinstance(out[0], Exception):
            log.error("Matrix sign-out failed (%s)", type(out[0]).__name__)
        else:
            log.info("Matrix sign-out: %s", "done" if out[0] else "the homeserver didn't confirm it")


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    check_args(ap, a)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    try:
        campus = Campus(a.campus)
    except CampusError as e:
        ap.error("--campus: %s" % e)
    relay = Relay(a, campus)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, relay.stop)
    code = 0
    try:
        relay.sign_in()
        relay.voice() if a.mode == "voice" else relay.captions()
    except CampusError as e:
        log.error("%s", e)
        code = 1 if relay.room.token else 2      # refused before it could read the room: the sign-in or the join
    except Exception as e:
        log.error("stopped by an error: %s", type(e).__name__)
        code = 1
    finally:
        relay.sign_out()                         # a signal meanwhile only sets relay.stopping, so this finishes
        log.info("%s: stopped (%s); %s", a.mode, relay.stopping or "error",
                 ", ".join("%s %d" % kv for kv in sorted(relay.counts.items())) or "nothing posted or queued")
    return code


if __name__ == "__main__":
    sys.exit(main())
