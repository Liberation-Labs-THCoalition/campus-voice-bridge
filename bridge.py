#!/usr/bin/env python3
"""An outside agent's voice in a campus class or jam: each line it writes is posted to the chat, then spoken.

    bridge.py --campus https://CAMPUS --host-id HOST --cred-file run/campus.json      (the agent's own campus account)
    bridge.py --url wss://LIVEKIT --guest-link-file run/guest_link --guest-name NAME  (a class's or a jam's guest link)
    bridge.py --url wss://LIVEKIT --token-file run/token                              (a LiveKit token in hand)
    [--voice af_heart] [--tts-socket run/tts/tts.sock] [--models DIR] [--fast-cpu] [--run-dir DIR]
    [--control-dir DIR] [--rejoin]

Text first, voice second: a line is posted to a chat, and only then read aloud. A line that couldn't be posted isn't
spoken, so nothing is heard in the call that can't also be read in the chat it went to. Which chat that is, and who
reads it, depends on the way in (below): on the guest route it is LiveKit's own chat, which campus members don't see
in a jam, nor on a class page that has a Matrix room.

Two ways in (campus.py has what production does):
- The agent's own campus account (--campus, --host-id, --cred-file): sign in with the account's Matrix password, join
  the running class, and post each line to the class's Matrix room, which is the chat the class page shows. A class
  with no Matrix room shows LiveKit's own chat (topic lk.chat) instead, so the line goes there.
- A guest link (--url with --guest-link-file): the file holds the link itself (https://CAMPUS/call/TOKEN, owner-only),
  and the bridge joins by it at start the way the guest page does. The LiveKit address is --url, since the campus's
  answer doesn't carry one. --token-file instead holds a LiveKit token you already have. Either way the line's text
  goes to LiveKit's chat, lk.chat, which the guest page shows. Students on a class page with a Matrix room don't see
  it, and in a jam people read jam chat, so there only other guests see the text, while everyone in the call hears it.
  The campus hands a guest a signed credential at its first join, and the guest page presents it at the next one, so
  that a host's removal (the campus's guest_bans) keeps that guest out. The bridge does the same: it keeps the
  credential in RUN/guest_token (owner-only, for that campus only, never logged) and presents it at every join by a link.
With both given, the account route goes first. If any of its steps fails, the bridge signs out and uses the guest
link; the log names the failed step, and every status row says which chat the line went to.

Lines arrive as files in RUN/say/ (RUN is --run-dir, default run/ beside this file): JSON {"id": ..., "text": ...}
written by say.py, or plain UTF-8 text in a .txt file (one file per line, e.g. written over ssh). Files are taken in
name order; names starting with "." are skipped (write there, then rename), and a .txt is taken only once it is
SETTLE_S old, so a half-written one isn't read. Taken files move to RUN/said/.

Speech: Kokoro (kokoro-onnx), one sentence at a time. With --tts-socket, a tts_server.py makes each sentence, so the
first plays while the next is made. That is a Unix socket in a folder only this account can enter (0700), never a
network port: on another machine, the server's own socket is forwarded here with SSH (README.md). When the server can't
be reached, or its answer is no use, the bridge uses Kokoro on this machine's CPU for the next REMOTE_RETRY_S seconds
(model files in --models), same voice. A CPU slower than real time would stall mid-line, so on that path the whole
line is made before any of it plays, unless --fast-cpu says this CPU keeps up. If no speech can be made at all (no
model files, a voice Kokoro doesn't have), the line is "not_spoken": its text stands, a short note goes to the chat,
and the bridge goes on to the next line.

Did it reach the room? Each line counts the RTP packets this track sent while it played, against the line's voiced
20 ms frames (10 ms frames whose RMS is at least VOICED_RMS, halved). DTX is on, so a silent track sends almost
nothing; with DTX off a track sends about 50 packets a second whether or not any audio was captured, and the count
can't tell a dropped line from a spoken one. Under 80% of the voiced frames, and the line is "not_spoken": the status
log says so and a short note goes to the chat. The SFU's receiver reports (fraction lost) go in the log too. When the
call's stats can't be read, the verdict is "unknown". A line cut from outside gets a row too: "dropped" (a drop, or a
removal by the host), "call_ended" (the call ended under it) or "stopped" (Ctrl-C, SIGTERM or SIGHUP). A cut row's
t_chat is null when the cut came before the chat post returned; the post may still have landed.

How long did a line take to sound? Every status row of a line that reached the chat carries wall-clock stamps (epoch
seconds): t_landed (the line file's mtime), t_seen (the bridge took the file), t_chat (the chat post returned; speech
starts here), t_synth_done (the audio that plays first is in hand) and t_first_frame (its first 10 ms frame went into
LiveKit's audio source). The first voiced frame starts lead_s later. Before t_landed, and after the frame is pushed
(the call's network, the listener's browser), the bridge sees nothing.

Control files (control.py): a file named "stop" in RUN/control/ cuts the line being spoken, and the next line still
plays. A small regular file named "drop" there, or in a --control-dir folder, makes the bridge leave the call within a
couple of seconds: the line being spoken is cut, even while it is still being made; the track is unpublished; the call
is left; and the account route's Matrix device is signed out. Then the bridge exits with code 4 and writes
RUN/dropped, and while that file exists it won't join again (it exits 4) unless it is started with --rejoin, so nothing
that restarts it can put it back in the call. --rejoin also moves every line still waiting in RUN/say/ to
RUN/say_held/<time>/, so nothing queued before or during the drop is spoken in a later call (move a file back into
RUN/say/ to have it spoken).

Removed from the call: if the host removes the bridge (LiveKit's PARTICIPANT_REMOVED), or another join takes its
identity (DUPLICATE_IDENTITY: the same account opened somewhere else, or a second bridge), that counts as a drop: the
bridge writes RUN/dropped and exits 4, so only --rejoin brings it back. Any other end of the call exits 3, with
LiveKit's reason in the log and in RUN/status.jsonl.

Exit codes: 0 Ctrl-C, SIGTERM or SIGHUP (it leaves the call and signs out first); 1 an error; 2 bad arguments, a file it
won't use, or the campus refused; 3 the call ended for another reason; 4 dropped or removed, or not joining after that.
Once it is leaving (after a drop, the end of the call, or a first signal), another Ctrl-C, SIGTERM or SIGHUP is ignored,
so the sign-out and the status rows finish and the exit code stands; SIGKILL still ends it. The sign-out gets at most
SIGN_OUT_S (20 s) in all, so a homeserver that answers slowly can't hold the exit that way. A sign-in still under way
when it was stopped can't be cancelled, and can open a Matrix device after the shutdown looked for one; that device is
signed out once the event loop has closed, with at most SIGN_OUT_LATE_S (20 s) for that. Any request to the campus still
under way when it was stopped (a sign-in, a class join, a chat post) can't be cancelled either, and the exit waits for
it to end.

The log and RUN/status.jsonl keep ids, sizes, timings and verdicts, never a line's text, a token, a link or a guest
credential. A private call's room is named for the start of its link, so that name is masked in every log line,
LiveKit's own included.

Who is in the call: RUN/participants.jsonl (owner-only) gets a row {t, event, name} when someone joins ("joined") or
leaves ("left"), and one "present" row for each person already there when the bridge joins, so relay.py can post
"(joined: NAME)" notices in the chat. It keeps the name a person shows in the call (their LiveKit identity only when
they show none), and the log never does. A row that can't be written is logged and skipped; it never breaks the call.
"""
import argparse, asyncio, http.client, io, json, logging, os, re, signal, socket, stat, sys, threading, time

import numpy as np
from livekit import rtc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                # -I leaves the script's own folder off the path
from campus import BRIDGE_LINE_KEY, GUEST_CREDENTIAL, Campus, CampusError, parse_guest_link, read_credentials  # noqa: E402
from control import (make_private_dirs, models_problem, printable, private_dir_problem, take_drop_file,  # noqa: E402
                     unsafe_dir)

DROP_EXIT = 4
CONNECT_WAIT_S = 20                     # a drop during the join: let a LiveKit connect under way finish, then leave
UNPUBLISH_WAIT_S = 1.0                  # when leaving after a drop
DISCONNECT_WAIT_S = 2.0
SIGN_OUT_S = 20.0                       # the whole of close()'s sign-out, however slowly the homeserver answers
SIGN_OUT_LATE_S = 20.0                  # the whole of sign_out_late, however slowly the homeserver answers
REMOTE_RETRY_S = 30                     # after the TTS server fails, this machine's CPU speaks for this long
TTS_TIMEOUT_S = 15
MAX_WAV = 16 << 20                      # bytes; a sentence's audio is far smaller (24 kHz 16-bit mono: 48 kB a second)
SR = 24000
FRAME = SR // 100                       # 10 ms of audio per captured frame
VOICED_RMS = 300                        # int16; a frame at least this loud counts as voiced
SPOKEN_FRACTION = 0.8
MAX_LINE = 1500
MAX_FILE = 16384    # bytes; a line is at most MAX_LINE characters anyway
SETTLE_S = 0.5
PARTICIPANT_NAME = 80                   # characters of a name kept in RUN/participants.jsonl
# The ends of a call that count as a drop (bridge.py's docstring), with the "via" a drop row and RUN/dropped give them.
KICKS = {rtc.DisconnectReason.PARTICIPANT_REMOVED: "removed by the host",
         rtc.DisconnectReason.DUPLICATE_IDENTITY: "another join under this identity"}
log = logging.getLogger("bridge")


class FileRefused(Exception):
    """A file the bridge was pointed at and won't use: missing, a link, not a regular file, too big, or readable by
    others. The message names the file and why, never what is in it."""


def reason_name(reason):
    """LiveKit's name for a disconnect reason (an int from its protocol), or the value as given if it has none."""
    try:
        return rtc.DisconnectReason.Name(reason)
    except (ValueError, TypeError):
        return str(reason)


def shown_room(name):
    """A room's name as the log may show it: a private call's room is named for the start of its link
    (as the campus names it), so that part is left out."""
    return "private-..." if str(name).startswith("private-") else name


# A private call's room, as the campus names it: "private-" and the first 12 characters of the link's token, which is 8 to 128
# of these characters (campus.py GUEST_PATH). Fewer than 8 after "private-" is a word, such as "private-call".
PRIVATE_ROOM = re.compile(r"\bprivate-[A-Za-z0-9_-]{8,12}(?![A-Za-z0-9_-])")


class MaskingFormatter(logging.Formatter):
    """Log lines with a private call's room name masked, whoever logged them. LiveKit's Rust core logs 'Disconnected
    from room "<name>"' through the "livekit" logger, where shown_room() can't reach; masking the finished line covers
    its arguments and any traceback too. Once a route knows the room, mask_room() adds the part of its name that
    comes from the link, which is then masked in whatever shape it shows (upper case around it, %-encoded, a word
    character beside it): the pattern alone goes by the name's shape."""

    secrets = ()                        # a tuple, replaced whole, so another thread's log line never sees it change

    def format(self, record):
        line = super().format(record)
        for s in self.secrets:
            line = line.replace(s, "...")
        return PRIVATE_ROOM.sub("private-...", line)


def mask_room(name):
    """Mask, in every log line from now on, the part of a private call's room name that comes from its link (8 to 12
    characters after "private-"). Any other room's name is left as it is."""
    part = str(name)[len("private-"):] if str(name).startswith("private-") else ""
    if len(part) >= 8 and part not in MaskingFormatter.secrets:
        MaskingFormatter.secrets = MaskingFormatter.secrets + (part,)


def log_handler(stream=None):
    """The handler main() logs through: stderr (or stream), every line masked."""
    h = logging.StreamHandler(stream)
    h.setFormatter(MaskingFormatter("%(asctime)s %(levelname)s %(message)s"))
    return h


BLEND_PART = re.compile(r"([a-z]{2}_[a-z]+):(\d{1,3})")


def blend_parts(spec):
    """[(voice, percent), ...] for a blend such as "af_bella:60,am_adam:40" (two or three voices, percents summing
    to 100), None for a plain voice name; ValueError for anything else. tts_server.py has the same rule."""
    if ":" not in spec and "," not in spec:
        return None
    parts = [BLEND_PART.fullmatch(p.strip()) for p in spec.split(",")]
    if not (2 <= len(parts) <= 3 and all(parts)):
        raise ValueError("a blend is two or three voice:percent parts, e.g. af_bella:60,am_adam:40")
    out = [(m.group(1), int(m.group(2))) for m in parts]
    if sum(w for _, w in out) != 100 or min(w for _, w in out) <= 0:
        raise ValueError("a blend's percents must be positive and sum to 100")
    return out


def kokoro_voice(k, spec):
    """What Kokoro.create takes: the name, or a blend's weighted sum of style vectors (wa * style(a) + (1 - wa) *
    style(b), then float32), the same mix tts_server.py makes."""
    parts = blend_parts(spec)
    if parts is None:
        return spec
    return np.asarray(sum(w / 100 * k.get_voice_style(n) for n, w in parts), np.float32)


def sentences(text):
    parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+", text.strip()) if p.strip()]
    out = []
    for p in parts:                     # fold very short pieces into the one before, so the voice doesn't clip
        if out and len(p) < 24:
            out[-1] += " " + p
        else:
            out.append(p)
    return out or [text.strip()]


def lead_silence(samples):
    """Seconds before the first voiced 10 ms frame (RMS >= VOICED_RMS, as play() counts them); None if none is."""
    n = len(samples) // FRAME
    if n == 0:
        return None
    rms = np.sqrt(np.mean(samples[:n * FRAME].astype(np.float64).reshape(n, FRAME) ** 2, axis=1))
    v = np.flatnonzero(rms >= VOICED_RMS)
    return round(v[0] * FRAME / SR, 2) if v.size else None


def stamp(t):
    return None if t is None else round(t, 3)


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP to a tts_server.py over its Unix socket: nothing on the network, so no proxy and no redirect can reach it."""

    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(self.timeout)
            s.connect(self.socket_path)
        except BaseException:
            s.close()
            raise
        self.sock = s


class Speech:
    """The TTS server first (with --tts-socket, unless it failed in the last REMOTE_RETRY_S), Kokoro on this machine's
    CPU second. Returns int16 samples at 24 kHz."""

    def __init__(self, voice, tts_socket=None, models=None, local_threads=4):
        self.voice, self.socket, self.models, self.local_threads = voice, tts_socket, models, local_threads
        self.remote_down_until = 0.0
        self._local = None

    def _remote(self, text):
        st = os.lstat(self.socket)
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError("the TTS socket isn't a socket of this account's")
        body = json.dumps({"text": text, "voice": self.voice, "speed": 1.0}).encode()
        conn = UnixHTTPConnection(self.socket, TTS_TIMEOUT_S)
        try:
            conn.request("POST", "/speak", body=body, headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            n = r.getheader("Content-Length") or ""
            if r.status != 200 or not n.isdigit() or int(n) > MAX_WAV:
                raise ValueError("the TTS server answered %d with %s bytes" % (r.status, n or "no length of"))
            wav = r.read(int(n))
        finally:
            conn.close()
        if len(wav) != int(n):
            raise ValueError("the TTS server's answer was cut off")
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(wav), dtype="int16")
        if sr != SR or data.ndim != 1:
            raise ValueError("the TTS server's audio isn't %d Hz mono" % SR)
        return data

    def _local_engine(self):
        if self._local is None:
            if self.models is None:     # main() found no model files at the start, so none were checked: none load
                raise FileNotFoundError("no Kokoro model files were checked at the start")
            import onnxruntime as ort
            from kokoro_onnx import Kokoro
            so = ort.SessionOptions()
            so.intra_op_num_threads = self.local_threads
            so.inter_op_num_threads = 1
            sess = ort.InferenceSession(os.path.join(self.models, "kokoro-v1.0.onnx"), so,
                                        providers=["CPUExecutionProvider"])
            self._local = Kokoro.from_session(sess, os.path.join(self.models, "voices-v1.0.bin"))
        return self._local

    def _local_say(self, text):
        k = self._local_engine()
        samples, sr = k.create(text, voice=kokoro_voice(k, self.voice), speed=1.0, lang="en-us")
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16)

    def remote_up(self):
        return bool(self.socket) and time.monotonic() >= self.remote_down_until

    async def say(self, text):
        """(samples, backend), backend "remote" or "local". Any failure of the server (unreachable, refused, cut off,
        not HTTP, not WAV) falls back to this machine's CPU; a failure there is the caller's to handle."""
        if self.remote_up():
            try:
                return await asyncio.to_thread(self._remote, text), "remote"
            except Exception as e:
                log.warning("the TTS server failed (%s); this machine's CPU speaks for %d s", type(e).__name__,
                            REMOTE_RETRY_S)
                self.remote_down_until = time.monotonic() + REMOTE_RETRY_S
        return await asyncio.to_thread(self._local_say, text), "local"


def audio_packets_sent(stats):
    """Packets sent so far on our audio track, from the publisher's outbound-rtp stats; and the latest loss fraction
    the SFU reported back (remote-inbound-rtp), or None."""
    sent, lost = 0, None
    for s in stats.publisher_stats:
        if s.HasField("outbound_rtp") and s.outbound_rtp.stream.kind == "audio":
            sent += s.outbound_rtp.sent.packets_sent
        if s.HasField("remote_inbound_rtp") and s.remote_inbound_rtp.stream.kind == "audio":
            lost = s.remote_inbound_rtp.remote_inbound.fraction_lost
    return sent, lost


async def in_daemon_thread(fn, limit_s, name):
    """fn() in a daemon thread, waited on for at most limit_s: [its result], [the exception it raised], or [] if the
    time ran out, the thread then left behind. A wait_for around asyncio.to_thread would return on time, but its
    executor thread runs on, and the exit waits for it (asyncio.run's executor shutdown, then the interpreter's join
    of executor threads); nothing waits for a daemon thread at the exit."""
    loop = asyncio.get_running_loop()
    done, out = loop.create_future(), []

    def finish():
        if not done.done():
            done.set_result(None)

    def attempt():
        try:
            out.append(fn())
        except Exception as e:
            out.append(e)
        try:
            loop.call_soon_threadsafe(finish)
        except RuntimeError:
            pass                        # the loop has closed: nobody is waiting any more
    threading.Thread(target=attempt, name=name, daemon=True).start()
    await asyncio.wait({done}, timeout=limit_s)     # neither cancels nor raises when the time runs out
    return out[:1]


class Bridge:
    def __init__(self, a):
        self.a = a
        self.run_dir = os.path.abspath(a.run_dir)
        self.marker = os.path.join(self.run_dir, "dropped")    # written by a drop; while it exists, no join
        self.room = rtc.Room()
        self.source = rtc.AudioSource(SR, 1, queue_size_ms=500)
        self.speech = Speech(a.voice, a.tts_socket, a.models, a.local_threads)
        self.stop_now = asyncio.Event()
        self.t_first_frame = None       # wall time the line's first frame went into the source; play() sets it
        self.t_chat = None              # wall time the line's chat post returned; _speak_line sets it
        self.leaving = False            # set by leave() and shutdown(); stopped_by also counts a drop or a call end
        self.stop_signal = None         # the signal that stopped the bridge, for a cut line's row
        self.skipped = set()            # line files that couldn't be moved to said/: left in say/, never taken again
        self.line_no = 0
        self.disconnected = asyncio.Event()
        self.disconnect_reason = None   # LiveKit's reason, when the call ended for us
        self.campus = None
        self.chat_room = None           # the class's Matrix room, when the account route is in use
        self.chat = "lk.chat"
        self.dropped = asyncio.Event()  # sticky: once set, the bridge leaves and stays out
        self.drop_t = self.drop_via = None
        self.in_call = self.left = False
        self.current_line = self.line_task = self.joining = None
        self.audio_pub = None
        self.tasks = set()              # background tasks, kept so they aren't collected mid-run
        self.control_dirs = [os.path.join(self.run_dir, "control")] + [os.path.abspath(d) for d in a.control_dir]
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        for d in ("say", "said", "control"):
            os.makedirs(os.path.join(self.run_dir, d), mode=0o700, exist_ok=True)

    def status(self, **row):
        row["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(os.path.join(self.run_dir, "status.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
        log.info("%s %s: %s", row.get("event", "line"), row.get("id"),
                 {k: v for k, v in row.items() if k not in ("id", "at", "event")})

    def note_participant(self, event, p):
        """One row in RUN/participants.jsonl, {t, event, name}, for relay.py's join and leave notices in the chat:
        event is "joined", "left", or "present" (already in the call when this bridge joined). Only the name the person
        shows in the call is kept, or their LiveKit identity when they show none; never logged. The file is owner-only,
        appended to, and never a link or a FIFO. A note must never break the call, so a failure is logged (its kind
        only) and dropped."""
        try:
            name = str(getattr(p, "name", "") or getattr(p, "identity", "") or "")
            name = "".join(c for c in name if c.isprintable()).strip()[:PARTICIPANT_NAME] or "someone"
            fd = os.open(os.path.join(self.run_dir, "participants.jsonl"),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise FileRefused("participants.jsonl: not a regular file")
                if st.st_mode & 0o077:
                    os.fchmod(f.fileno(), 0o600)
                f.write(json.dumps({"t": round(time.time(), 3), "event": event, "name": name}) + "\n")
        except Exception as e:
            log.warning("participants note (%s) not written: %s", event, type(e).__name__)

    def spawn(self, coro, name):
        t = asyncio.create_task(coro, name=name)
        self.tasks.add(t)
        t.add_done_callback(self._task_ended)
        return t

    def _task_ended(self, t):
        self.tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("background task %s ended: %s", t.get_name(), type(t.exception()).__name__)

    async def account_route(self):
        """(LiveKit url, token) through the agent's campus account, and the class's Matrix room if it has one."""
        self.campus = Campus(self.a.campus)
        user, password = read_credentials(self.a.cred_file)
        await asyncio.to_thread(self.campus.sign_in, user, password)
        del password
        if self.dropped.is_set():
            return None, None           # a drop came during the sign-in: no class join either; connect() stops here
        info = await asyncio.to_thread(self.campus.join_class, self.a.host_id)
        mask_room(info.get("roomName"))     # a class's room is class-<host>-<time>; masked anyway, as guest_route does
        if info["matrixRoomId"]:
            if info["matrixStatus"] != "joined":
                await asyncio.to_thread(self.campus.join_room, info["matrixRoomId"])
            self.chat_room, self.chat = info["matrixRoomId"], "matrix"
        log.info("account route: in class %s; chat: %s (the join said %s)", shown_room(info.get("roomName")),
                 self.chat, info.get("matrixStatus"))
        return info["livekitUrl"], info["token"]

    async def connect(self):
        url = token = None
        if self.a.campus:
            try:
                url, token = await self.account_route()
            except CampusError as e:
                log.warning("account route: %s", e)
                await self.close()
                self.chat_room, self.chat = None, "lk.chat"
                if not self.a.url:
                    raise
                log.warning("using the guest link instead; its chat is lk.chat")
        if self.dropped.is_set():
            return                      # a drop came during the sign-in: don't join at all
        if url is None:
            url, token = await asyncio.to_thread(guest_route, self.a, self.run_dir)
            if self.dropped.is_set():
                return                  # a drop came during the guest join: don't join the call
        self.room.on("disconnected", self.on_disconnected)
        self.room.on("participant_connected", lambda p, *_: self.note_participant("joined", p))
        self.room.on("participant_disconnected", lambda p, *_: self.note_participant("left", p))
        await self.room.connect(url, token)
        self.in_call = not self.disconnected.is_set()
        if self.dropped.is_set():
            return                      # a drop came during the join: leave() takes it from here
        if self.disconnected.is_set():
            return                      # the call ended during the join: run() ends with exit 3 and its reason
        try:
            self.audio_pub = await self.publish_audio()
        except Exception:
            if self.dropped.is_set() or self.disconnected.is_set():
                return                  # a drop, or the call's end, while the track was being published
            raise
        log.info("joined room %s as %s", shown_room(self.room.name), self.room.local_participant.identity)
        try:
            present = list(self.room.remote_participants.values())
        except Exception as e:                      # a note never breaks the call
            log.warning("participants note (present) not written: %s", type(e).__name__)
            present = []
        for p in present:
            self.note_participant("present", p)     # who was in the call already, for relay.py's notices

    async def publish_audio(self):
        track = rtc.LocalAudioTrack.create_audio_track("agent-voice", self.source)
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE, dtx=True)
        return await self.room.local_participant.publish_track(track, opts)

    def on_disconnected(self, reason=None, *_):
        """The call ended for us. A removal by the host, or another join taking this identity, is a drop: the bridge
        stays out until --rejoin, rather than coming back on a restart. Anything else ends the run with exit 3."""
        self.disconnect_reason = reason
        self.in_call = False
        if reason in KICKS:
            self.request_drop(KICKS[reason])
        self.disconnected.set()

    async def packets_sent(self):
        """audio_packets_sent of the call's stats now, or (None, None) if there are none to read: livekit raises once
        the room is gone, and a line that was playing then gets the verdict "unknown"."""
        try:
            return audio_packets_sent(await self.room.get_rtc_stats())
        except Exception as e:
            log.warning("no call stats (%s)", type(e).__name__)
            return None, None

    async def post_chat(self, text, line_id=None):
        """Post text in the call's chat. On the account route that is the class's Matrix room, and the post carries
        {BRIDGE_LINE_KEY: line_id}: when this bridge signs in as an ID that relay.py maps to it, the relay reads the
        post as that ID's message, and without the mark it would queue the line again, over and over."""
        if self.chat_room:
            await asyncio.to_thread(self.campus.post_line, self.chat_room, text,
                                    extra={BRIDGE_LINE_KEY: line_id or ""})
        else:
            await self.room.local_participant.send_text(text, topic="lk.chat")

    async def close(self):
        """Sign the account route's Matrix device out (idempotent). True if there was a device to sign out, None if the
        homeserver didn't finish within SIGN_OUT_S. The campus's timeout bounds each socket operation, not the request,
        and once the bridge is leaving a signal is ignored, so a homeserver answering a byte at a time could otherwise
        hold the shutdown for as long as it liked. sign_out drops the token before it asks, so sign_out_late doesn't
        try that device again."""
        if not self.campus:
            return False
        had = bool(self.campus.mx_token)
        out = await in_daemon_thread(self.campus.sign_out, SIGN_OUT_S, "sign_out")
        if not out:
            log.error("Matrix sign-out: no answer within %.0f s, so given up; the device may still be signed in",
                      SIGN_OUT_S)
            return None
        if isinstance(out[0], Exception):
            raise out[0]
        return had

    async def play(self, samples):
        """Push samples 10 ms at a time. Returns (False if a stop or a drop cut it off, voiced 10 ms frames played)."""
        if len(samples) % FRAME:
            samples = np.concatenate([samples, np.zeros(FRAME - len(samples) % FRAME, np.int16)])
        voiced = 0
        for i in range(0, len(samples), FRAME):
            if self.stop_now.is_set() or self.dropped.is_set():
                return False, voiced
            chunk = samples[i:i + FRAME]
            voiced += int(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)) >= VOICED_RMS)
            await self.source.capture_frame(rtc.AudioFrame(chunk.tobytes(), SR, 1, FRAME))
            if self.t_first_frame is None:
                self.t_first_frame = time.time()
        return True, voiced

    async def speak_line(self, line_id, text, t_landed=None, t_seen=None):
        """_speak_line, with a status row when the line is cut from outside (also while it is still being made):
        "dropped" by a drop, "call_ended" by the end of the call, "stopped" when the bridge is stopped. The row's
        t_chat is null if the cut came before the chat post returned."""
        self.current_line = line_id
        self.t_chat = None
        t0_wall = time.time()
        try:
            await self._speak_line(line_id, text, t_landed, t_seen)
        except asyncio.CancelledError:
            if self.dropped.is_set():
                verdict, why = "dropped", None
            elif self.disconnected.is_set():
                verdict, why = "call_ended", ("the call ended while the line played" if self.t_chat is not None
                                              else "the call ended before the chat post returned")
            else:
                verdict, why = "stopped", "the bridge was stopped" + (" (%s)" % self.stop_signal
                                                                      if self.stop_signal else "")
            try:
                self.status(id=line_id, t0=round(t0_wall, 3), t1=round(time.time(), 3), chars=len(text),
                            chat=self.chat, verdict=verdict, error=why, t_landed=stamp(t_landed),
                            t_seen=stamp(t_seen), t_chat=stamp(self.t_chat))
            except OSError as e:
                log.error("couldn't write the cut line's status row (%s)", type(e).__name__)
            raise
        finally:
            self.current_line = None

    async def _speak_line(self, line_id, text, t_landed=None, t_seen=None):
        self.line_no += 1
        t0 = time.monotonic()
        t0_wall = time.time()
        try:
            await self.post_chat(text, line_id)
        except Exception as e:
            return self.status(id=line_id, chars=len(text), chat=self.chat, verdict="chat_failed",
                               error=str(e) if isinstance(e, CampusError) else type(e).__name__)
        t_chat = self.t_chat = time.time()
        parts = sentences(text)
        # Counted from the first audio, not from the line's start: with DTX on, a silent track still sends a packet
        # every few hundred ms, and a long wait for speech would otherwise add them to a failed line.
        before = nxt = synth_error = None
        audio_s, backends, first_audio, cut, voiced = 0.0, [], None, False, 0
        t_synth_done = first_pcm = first_chars = None
        self.t_first_frame = None
        try:
            try:
                if self.speech.remote_up() or self.a.fast_cpu:     # a sentence plays while the next one is made
                    nxt = asyncio.create_task(self.speech.say(parts[0]))
                    for k in range(len(parts)):
                        try:
                            samples, backend = await nxt
                        except Exception as e:      # no speech for this sentence from either engine
                            synth_error = type(e).__name__
                            break
                        backends.append(backend)
                        if k + 1 < len(parts):
                            nxt = asyncio.create_task(self.speech.say(parts[k + 1]))
                        if first_audio is None:
                            first_audio = time.monotonic() - t0
                            t_synth_done, first_pcm, first_chars = time.time(), samples, len(parts[0])
                            before, _ = await self.packets_sent()
                        audio_s += len(samples) / SR
                        done, v = await self.play(samples)
                        voiced += v
                        if not done:
                            cut = True
                            break
                else:                       # a CPU slower than real time: make the whole line, then play it
                    whole = []
                    try:
                        for p in parts:
                            samples, backend = await self.speech.say(p)
                            whole.append(samples)
                            backends.append(backend)
                    except Exception as e:
                        synth_error = type(e).__name__
                    if synth_error is None:
                        samples = np.concatenate(whole)
                        first_audio = time.monotonic() - t0
                        t_synth_done, first_pcm, first_chars = time.time(), samples, sum(map(len, parts))
                        before, _ = await self.packets_sent()
                        audio_s = len(samples) / SR
                        done, voiced = await self.play(samples)
                        cut = not done
                if cut:
                    self.source.clear_queue()
                else:
                    await self.source.wait_for_playout()
            finally:
                if nxt is not None and not nxt.done():
                    nxt.cancel()            # a stop, a drop or a failure while the next sentence was being made
            await asyncio.sleep(0.5)        # let the last packets and an RTCP report land before counting
            after, lost = await self.packets_sent()
        except Exception as e:              # anything else still ends the bridge, but the line gets its row first
            self.status(id=line_id, t0=round(t0_wall, 3), t1=round(time.time(), 3), chars=len(text), chat=self.chat,
                        verdict="error", error=type(e).__name__)
            raise
        sent = max(0, after - before) if None not in (before, after) else None
        expected = voiced // 2              # 20 ms packets
        if cut:
            verdict = "interrupted"
        elif synth_error or expected == 0:  # no speech, or a line made of silence: not spoken either
            verdict = "not_spoken"
        elif sent is None:
            verdict = "unknown"             # no stats to count: the room was gone when they were read
        elif sent >= SPOKEN_FRACTION * expected:
            verdict = "spoken"
        else:
            verdict = "not_spoken"
        self.status(id=line_id, t0=round(t0_wall, 3), t1=round(time.time(), 3), chars=len(text), sentences=len(parts),
                    backend="+".join(sorted(set(backends))), first_audio_s=round(first_audio or 0, 2),
                    audio_s=round(audio_s, 2), total_s=round(time.monotonic() - t0, 2), pkts_expected=expected,
                    pkts_sent=sent, fraction_lost=lost, chat=self.chat, verdict=verdict,
                    error=None if synth_error is None else "synthesis failed (%s)" % synth_error,
                    t_landed=stamp(t_landed), t_seen=stamp(t_seen), t_chat=stamp(t_chat),
                    t_synth_done=stamp(t_synth_done), t_first_frame=stamp(self.t_first_frame),
                    lead_s=None if first_pcm is None else lead_silence(first_pcm),   # counted now: not before the sound
                    first_chars=first_chars, voice=self.a.voice)
        if verdict == "not_spoken":
            try:
                await self.post_chat("(The voice for that line didn't reach the call. The text above stands.)", line_id)
            except Exception:
                pass

    async def watch_control(self):
        """RUN/control/stop cuts the line being spoken; a file named drop in any control folder is a drop."""
        stop = os.path.join(self.run_dir, "control", "stop")
        drops = [os.path.join(d, "drop") for d in self.control_dirs]
        noted = set()
        while True:
            try:
                if os.path.exists(stop):
                    os.unlink(stop)
                    self.stop_now.set()
                    log.info("stop requested")
            except OSError as e:
                log.warning("stop file: %s", type(e).__name__)
            for p in drops:
                found, note = take_drop_file(p)
                if found:
                    self.request_drop("file " + p)
                if note and (p, note) not in noted:     # say it once, not every 50 ms
                    noted.add((p, note))
                    log.warning("%s %s: %s", "drop file" if found else "ignoring", p, note)
            await asyncio.sleep(0.05)

    def request_drop(self, via):
        """Leave the call and stay out. Called by the control watcher; idempotent, never raises."""
        if self.dropped.is_set():
            log.info("drop again via %s (already leaving)", via)
            return
        self.drop_t, self.drop_via = time.monotonic(), via
        self.dropped.set()
        self.stop_now.set()
        log.warning("drop via %s: leaving the call", via)
        try:
            fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "via": via}, f)
        except OSError as e:
            log.error("drop: couldn't write %s (%s), so a restart wouldn't know about this drop", self.marker,
                      type(e).__name__)
        try:
            self.status(id="drop", event="drop", via=via, in_call=self.in_call, line=self.current_line)
        except OSError as e:
            log.error("drop: couldn't write its status row (%s)", type(e).__name__)

    def marker_summary(self):
        try:
            fd = os.open(self.marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                d = json.loads(f.read(1024).decode("utf-8"))
            return "dropped %s via %s" % (printable(d["at"], 40), printable(d["via"], 200))
        except (OSError, ValueError, KeyError, TypeError):
            return "%s exists" % self.marker

    def gate_at_start(self):
        """True if the bridge must not join: an earlier drop (RUN/dropped), or a drop file already waiting.
        With --rejoin both are cleared instead; a waiting drop file that can't be removed still keeps it out."""
        files = [os.path.join(d, "drop") for d in self.control_dirs]
        if self.a.rejoin:
            if os.path.lexists(self.marker):
                summary = self.marker_summary()
                try:
                    os.unlink(self.marker)
                except OSError as e:
                    log.error("--rejoin: couldn't clear %s (%s); not joining", self.marker, type(e).__name__)
                    return True
                log.warning("--rejoin: cleared the earlier drop (%s)", summary)
                self.hold_queued_lines()
            for p in files:
                found, note = take_drop_file(p)
                if found and note:
                    log.error("--rejoin: a drop file is waiting in %s and %s; not joining", os.path.dirname(p), note)
                    return True
                if found:
                    log.warning("--rejoin: removed a waiting drop file from %s", os.path.dirname(p))
            return False
        if os.path.lexists(self.marker):
            log.error("not joining: %s. The bridge stays out until it is started with --rejoin.", self.marker_summary())
            return True
        for p in files:
            found, _ = take_drop_file(p)
            if found:
                self.request_drop("file " + p)
                log.error("not joining: a drop file was waiting in %s. Start with --rejoin to join.", os.path.dirname(p))
                return True
        return False

    def hold_queued_lines(self):
        """After a drop, move what is waiting in RUN/say/ to RUN/say_held/<time>/: lines queued before the drop, or
        while the bridge was out, are not spoken when it rejoins. Names and counts only are logged."""
        say_dir = os.path.join(self.run_dir, "say")
        try:
            names = sorted(os.listdir(say_dir))
        except OSError as e:
            log.warning("--rejoin: couldn't list say/ (%s)", type(e).__name__)
            return
        if not names:
            return
        held = os.path.join(self.run_dir, "say_held", time.strftime("%Y%m%d-%H%M%S"))
        try:
            make_private_dirs([held])   # say_held/ too, if missing: 0700, not under the umask
        except OSError as e:
            log.error("--rejoin: couldn't make %s (%s); %d queued line(s) left in say/", held, type(e).__name__,
                      len(names))
            return
        moved = 0
        for n in names:
            try:
                os.rename(os.path.join(say_dir, n), os.path.join(held, n))
                moved += 1
            except OSError as e:
                log.warning("--rejoin: couldn't hold %s (%s)", printable(n, 80), type(e).__name__)
        log.warning("--rejoin: %d line(s) queued before or during the drop moved to %s, not spoken", moved, held)
        try:
            self.status(id="rejoin", event="held", lines=moved, folder=held)
        except OSError:
            pass

    async def until_done_or_drop(self, task, call_end=False):
        """Wait for task or a drop, and with call_end for the end of the call too, whichever comes first."""
        waiters = [asyncio.ensure_future(self.dropped.wait())]
        if call_end:
            waiters.append(asyncio.ensure_future(self.disconnected.wait()))
        try:
            await asyncio.wait({task, *waiters}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.dropped.wait(), seconds)
        except TimeoutError:
            pass

    async def disconnect_quietly(self):
        try:
            await asyncio.wait_for(self.room.disconnect(), DISCONNECT_WAIT_S)
            out = "ok"
        except TimeoutError:
            out = "timeout"
        except Exception as e:
            out = "error: " + type(e).__name__
        self.in_call, self.left = False, True
        return out

    async def leave(self):
        """After a drop: cut the line, unpublish, leave the call, sign out. Returns DROP_EXIT."""
        self.leaving = True
        t_drop = self.drop_t or time.monotonic()
        self.stop_now.set()
        cut_line = self.current_line
        if self.line_task and not self.line_task.done():
            self.line_task.cancel()
            await asyncio.wait({self.line_task}, timeout=1.0)
        unpublished, disconnect = 0, "not in a call"
        if self.in_call:
            try:
                self.source.clear_queue()
            except Exception as e:
                log.warning("drop: clearing the audio queue failed (%s)", type(e).__name__)
            if self.audio_pub is not None:
                try:
                    await asyncio.wait_for(self.room.local_participant.unpublish_track(self.audio_pub.sid),
                                           UNPUBLISH_WAIT_S)
                    unpublished = 1
                except Exception as e:
                    log.warning("drop: unpublishing the track failed (%s)", type(e).__name__)
            disconnect = await self.disconnect_quietly()
        left_s = round(time.monotonic() - t_drop, 2)
        log.warning("drop: out of the call %.2f s after the drop (disconnect: %s)", left_s, disconnect)
        signed_out = await self.close()
        try:
            self.status(id="drop", event="left", via=self.drop_via, left_s=left_s, disconnect=disconnect,
                        unpublished=unpublished, cut_line=cut_line, matrix_signed_out=signed_out)
        except OSError as e:
            log.error("drop: couldn't write its status row (%s)", type(e).__name__)
        return DROP_EXIT

    async def shutdown(self):
        """On the way out, for any reason: stop the watchers, leave the call if still in it, sign out."""
        self.leaving = True
        for t in [*self.tasks, self.line_task, self.joining]:
            if t is not None and not t.done():
                t.cancel()
        if self.in_call:
            await self.disconnect_quietly()
        await self.close()

    async def run(self):
        if self.gate_at_start():
            return DROP_EXIT
        self.spawn(self.watch_control(), "control")
        joining = self.joining = asyncio.create_task(self.connect())
        await self.until_done_or_drop(joining)
        if self.dropped.is_set():
            # connect() skips the join after a drop; a LiveKit connect already under way is let finish (at most
            # CONNECT_WAIT_S), so that leave() can disconnect it rather than leave a half-made participant behind.
            done, _ = await asyncio.wait({joining}, timeout=CONNECT_WAIT_S)
            if not done:
                joining.cancel()
            elif not joining.cancelled() and joining.exception() is not None:
                log.warning("the join failed during the drop (%s)", type(joining.exception()).__name__)
            return await self.leave()
        joining.result()                # a failed join ends the bridge
        say_dir = os.path.join(self.run_dir, "say")
        while not (self.disconnected.is_set() or self.dropped.is_set()):
            names = [n for n in ready_lines(say_dir) if n not in self.skipped]
            if not names:
                await self.pause(0.1)
                continue
            path = os.path.join(say_dir, names[0])
            t_seen = time.time()
            self.stop_now.clear()           # a stop counts from here, so one sent while the line is posted cuts it
            try:
                t_landed = os.stat(path, follow_symlinks=False).st_mtime     # the file's last write
            except OSError:
                t_landed = None
            line_id, text = read_line_file(path)
            try:
                os.replace(path, os.path.join(self.run_dir, "said", names[0]))
            except OSError as e:
                # Left in say/, it would be taken and spoken again on every pass: it is skipped from now on, unspoken.
                self.skipped.add(names[0])
                log.error("couldn't move line file %s to said/ (%s); left in say/ and not spoken",
                          printable(names[0], 80), type(e).__name__)
                try:
                    self.status(id=line_id, event="not_taken", error="couldn't move it to said/ (%s)"
                                % type(e).__name__)
                except OSError:
                    pass
                await self.pause(0.1)       # never a pass without an await, whatever is left in say/
                continue
            if text:
                self.line_task = asyncio.create_task(self.speak_line(line_id, text, t_landed, t_seen))
                await self.until_done_or_drop(self.line_task, call_end=True)
                if self.dropped.is_set():
                    break                       # leave() cuts the line
                if not self.line_task.done():   # the call ended under the line: stop making and playing it
                    self.line_task.cancel()
                    await asyncio.wait({self.line_task}, timeout=1.0)
                elif not self.line_task.cancelled():
                    self.line_task.result()     # an error in a line ends the bridge
        if self.dropped.is_set():
            return await self.leave()
        why = reason_name(self.disconnect_reason)
        log.warning("disconnected from the call (%s)", why)
        try:
            self.status(id="disconnect", event="disconnected", reason=why)
        except OSError as e:
            log.error("couldn't write the disconnect's status row (%s)", type(e).__name__)
        return 3


def ready_lines(say_dir, now=None):
    """Line files ready to take, in name order: *.json (say.py renames them in whole) and *.txt at least SETTLE_S old.
    A name that isn't a regular file (a link, a FIFO, a folder) is taken at once, so read_line_file refuses it and it
    moves to said/, instead of sitting in the queue."""
    now = time.time() if now is None else now
    out = []
    for n in sorted(os.listdir(say_dir)):
        if n.startswith(".") or not n.endswith((".json", ".txt")):
            continue
        try:
            st = os.stat(os.path.join(say_dir, n), follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or n.endswith(".json") or now - st.st_mtime >= SETTLE_S:
            out.append(n)
    return out


def read_line_file(path):
    """(id, text) from one line file; text is None if it can't be used. Never follows a link, never waits on a FIFO
    (it is opened non-blocking, then refused as not a regular file); logs no text."""
    name = os.path.basename(path)
    stem = name.rsplit(".", 1)[0]
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise ValueError("not a regular file")
            raw = f.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise ValueError("too large")
        if name.endswith(".json"):
            req = json.loads(raw.decode("utf-8"))
            return str(req.get("id", stem))[:64], str(req["text"]).strip()[:MAX_LINE] or None
        return stem[:64], raw.decode("utf-8").lstrip("﻿").strip()[:MAX_LINE] or None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("unreadable line file %s: %s", name, type(e).__name__)
        return stem[:64], None


def run_dir_problem(run_dir):
    """Why RUN can't be used, or None. What lands in say/ is spoken and a file in control/ can cut a line or pull the
    bridge out of the call, so RUN, say/ and control/ must be folders only this account (or root) can change."""
    for d in (run_dir, os.path.join(run_dir, "say"), os.path.join(run_dir, "control")):
        why = unsafe_dir(d)
        if why:
            return "%s: %s" % (d, why)
    return None


def read_private(path):
    """The text in an owner-only file (a guest link or a LiveKit token). Never follows a link or waits on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise FileRefused("%s: can't open it (%s)" % (path, e.strerror or type(e).__name__)) from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise FileRefused("%s: must be a regular file readable only by its owner" % path)
        raw = f.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise FileRefused("%s: bigger than %d bytes" % (path, MAX_FILE))
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise FileRefused("%s: not UTF-8 text" % path) from None


def load_guest_credential(path, base):
    """The guest credential kept in path for the campus at base, or None: none kept, one kept for another campus, or a
    file the bridge won't trust. Never follows a link; never logs the credential."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("guest credential: can't open %s (%s); joining as a new guest", path, type(e).__name__)
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                log.warning("guest credential: %s isn't a regular file only its owner can read; joining as a new guest",
                            path)
                return None
            d = json.loads(f.read(4096).decode("utf-8"))
        cred = d.get("guestToken")
        if not (isinstance(cred, str) and GUEST_CREDENTIAL.fullmatch(cred)):
            raise ValueError("no credential in it")
    except (OSError, ValueError, AttributeError):
        log.warning("guest credential: %s can't be read; joining as a new guest", path)
        return None
    if d.get("campus") != base:
        log.info("guest credential: the one kept is for another campus; joining here as a new guest")
        return None
    return cred


def save_guest_credential(path, base, credential):
    """Keep the campus's guest credential, owner-only, in place of any older one (a new file renamed over it)."""
    tmp = path + ".new"
    try:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"campus": base, "guestToken": credential}, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("guest credential: couldn't keep it in %s (%s), so after a restart this bridge is a new guest, and "
                    "a host's removal of it wouldn't hold", path, type(e).__name__)


def guest_route(a, run_dir=None):
    """(LiveKit url, token) for the guest route: the token file as given, or the guest link resolved as its page does.
    With run_dir, the guest credential kept in RUN/guest_token for that campus is presented, and the one the campus
    answers with is kept. Neither the link, the token nor the credential is logged."""
    if not a.guest_link_file:
        return a.url, read_private(a.token_file)
    base, invite = parse_guest_link(read_private(a.guest_link_file))
    keep = os.path.join(run_dir, "guest_token") if run_dir else None
    known = load_guest_credential(keep, base) if keep else None
    token, room, kind, credential = Campus(base).guest_join(invite, a.guest_name, known)
    mask_room(room)                     # a private call's room is named for its link
    if keep and credential and credential != known:
        save_guest_credential(keep, base, credential)
    log.info("guest link: a %s link, into room %s, as %s guest", kind, shown_room(room),
             "a returning" if known and credential == known else "a new")
    if kind == "private":       # a room made from the link alone, in no class or jam
        log.warning("guest link: a private-call link; only people holding this link are in its room, nobody in a class "
                    "or jam. For a jam, ask for 'Invite to Public Call' from inside it.")
    return a.url, token


def stopped_by(sig, task, b=None):
    """A signal's handler: cancel the run, so that amain's shutdown leaves the call and signs out. Ignored once the
    bridge is leaving: a drop or the end of the call has been seen, or leave() or shutdown() has begun. So a signal
    can't cut the sign-out short or turn the exit code into 0, even in the wait before leave() begins (up to
    CONNECT_WAIT_S after a drop during the join, about 1 s after the call ends under a line)."""
    name = signal.Signals(sig).name
    if b is not None and (b.leaving or b.dropped.is_set() or b.disconnected.is_set()):
        log.warning("%s: already leaving; the sign-out and the status rows finish first", name)
        return
    log.warning("%s: leaving the call", name)
    if b is not None:
        b.stop_signal = name
    task.cancel()


async def amain(a, box=None):
    # Room and AudioSource bind to the running loop when built; built before asyncio.run, they bind to another one
    # and the SDK's callbacks never arrive (publish_track then waits forever).
    b = Bridge(a)
    if box is not None:
        box.append(b)                   # for main()'s sign-out once the loop has closed
    me, loop = asyncio.current_task(), asyncio.get_running_loop()
    # Ctrl-C, systemd, docker, kill, a closed ssh session: leave, sign out, end
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, stopped_by, sig, me, b)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    try:
        return await b.run()
    finally:
        await b.shutdown()


def sign_out_late(b):
    """After the loop has closed: a sign-in still running when the bridge was stopped can't be cancelled (its thread
    runs on), and may have opened a Matrix device after shutdown() looked for one. Sign that device out too. Signals
    are ignored meanwhile, since the loop's handlers are gone and SIGTERM's default would end the process first. The
    whole sign-out gets SIGN_OUT_LATE_S: the campus's timeout bounds each socket operation, not the request, so a
    homeserver answering a byte at a time could otherwise hold it, and the signals, for as long as it liked. It runs
    in a daemon thread, left behind at the exit if the time runs out. Never raises."""
    campus = getattr(b, "campus", None)
    if not (campus and campus.mx_token):
        return
    log.warning("signing out a Matrix device the sign-in opened after the bridge was stopped")
    old, out = {}, []

    def attempt():
        try:
            out.append(campus.sign_out())
        except Exception as e:
            out.append(e)
    try:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old[s] = signal.signal(s, signal.SIG_IGN)
        t = threading.Thread(target=attempt, name="sign_out_late", daemon=True)
        t.start()
        t.join(SIGN_OUT_LATE_S)
        if not out:
            log.error("that device: no answer within %.0f s, so given up; it may still be signed in", SIGN_OUT_LATE_S)
        elif isinstance(out[0], Exception):
            log.error("signing that device out failed (%s)", type(out[0]).__name__)
        else:
            log.warning("that device: %s", "signed out" if out[0] else "the homeserver didn't confirm the sign-out")
    except Exception as e:
        log.error("signing that device out failed (%s)", type(e).__name__)
    finally:
        for s, h in old.items():
            try:
                signal.signal(s, h)
            except (ValueError, TypeError, OSError):
                pass


def build_parser():
    ap = argparse.ArgumentParser(description="An outside agent's voice in a campus class or jam: each line is posted "
                                             "to the chat, then spoken. See the module docstring and README.md.")
    ap.add_argument("--campus", help="the campus's address, for the agent's own campus account")
    ap.add_argument("--host-id", help="the class host's id (the class page's /class/<host-id>)")
    ap.add_argument("--cred-file", help="owner-only JSON with the account's Matrix username and password "
                                        "(set_credential.py writes it)")
    ap.add_argument("--url", help="the campus's LiveKit address (wss://...), for a guest link or a token file")
    ap.add_argument("--guest-link-file", help="owner-only file holding a class's or a jam's guest link, "
                                              "https://CAMPUS/call/TOKEN; joined by at start")
    ap.add_argument("--guest-name", help="the name the guest link joins under (1 to 40 characters)")
    ap.add_argument("--token-file", help="owner-only file holding a LiveKit token, in place of --guest-link-file")
    ap.add_argument("--voice", default="af_heart", help="a Kokoro voice, or a blend such as af_bella:60,am_adam:40")
    ap.add_argument("--tts-socket", help="a tts_server.py's Unix socket, in a folder only this account can enter "
                                         "(0700); a server on another machine is forwarded here with ssh -L "
                                         "(README.md). Default: speech on this machine's CPU only")
    ap.add_argument("--models", default=os.path.join(HERE, "models"),
                    help="folder with kokoro-v1.0.onnx and voices-v1.0.bin, for speech on this machine's CPU")
    ap.add_argument("--local-threads", type=int, default=4, help="CPU threads for speech on this machine")
    ap.add_argument("--fast-cpu", action="store_true",
                    help="this machine makes speech faster than real time: play each sentence as soon as it is made")
    ap.add_argument("--run-dir", default=os.path.join(HERE, "run"),
                    help="the folder for say/, said/, control/, status.jsonl, the drop marker and the guest "
                         "credential (default: run/); only this account may be able to change it")
    ap.add_argument("--control-dir", action="append", default=[],
                    help="another folder watched for a file named drop (repeatable); RUN/control is always watched")
    ap.add_argument("--rejoin", action="store_true",
                    help="join even after a drop: clears RUN/dropped and any drop file waiting in the control folders")
    return ap


def main():
    ap = build_parser()
    a = ap.parse_args()
    if a.campus and not (a.host_id and a.cred_file):
        ap.error("--campus needs --host-id and --cred-file")
    if a.token_file and a.guest_link_file:
        ap.error("give --token-file or --guest-link-file, not both")
    if bool(a.url) != bool(a.token_file or a.guest_link_file):
        ap.error("--url goes with --token-file or --guest-link-file")
    if a.guest_link_file and not (a.guest_name and 1 <= len(a.guest_name.strip()) <= 40):
        ap.error("--guest-link-file needs --guest-name (1 to 40 characters)")
    if not (a.campus or a.url):
        ap.error("give --campus (the agent's own account), --url with --guest-link-file or --token-file, or both")
    try:
        blend_parts(a.voice)
    except ValueError as e:
        ap.error("--voice: %s" % e)
    if a.tts_socket:
        a.tts_socket = os.path.abspath(a.tts_socket)
        why = private_dir_problem(os.path.dirname(a.tts_socket))
        if why:
            ap.error("--tts-socket: its folder %s: %s (each line's text goes through it)"
                     % (os.path.dirname(a.tts_socket), why))
    have_models = all(os.path.isfile(os.path.join(a.models, f)) for f in ("kokoro-v1.0.onnx", "voices-v1.0.bin"))
    if not (have_models or a.tts_socket):
        ap.error("--models: kokoro-v1.0.onnx and voices-v1.0.bin aren't there (see README.md), and there is no "
                 "--tts-socket")
    if have_models:                     # what is loaded from there decides how every line sounds
        why = models_problem(a.models)
        if why:
            ap.error("--models %s: %s" % (a.models, why))
    if a.local_threads < 1:
        ap.error("--local-threads: at least 1")
    for d in a.control_dir:
        why = unsafe_dir(d)
        if why:
            ap.error("--control-dir %s: %s" % (d, why))
    for f in (a.guest_link_file, a.token_file):
        if f:
            try:
                read_private(f)
            except FileRefused as e:
                ap.error(str(e))
    a.run_dir = os.path.abspath(a.run_dir)
    try:
        make_private_dirs([os.path.join(a.run_dir, d) for d in ("", "say", "said", "control")])
    except OSError as e:
        ap.error("--run-dir %s: can't make it (%s)" % (a.run_dir, type(e).__name__))
    why = run_dir_problem(a.run_dir)
    if why:
        ap.error("--run-dir: %s" % why)
    logging.basicConfig(level=logging.INFO, handlers=[log_handler()], force=True)
    if not have_models:
        log.warning("no Kokoro model files in %s: whenever the TTS server can't be reached, lines go unspoken (their "
                    "text stands, with a note)", a.models)
        a.models = None                 # unchecked, so never loaded: files put there later are not used
    box = []
    try:
        return asyncio.run(amain(a, box))
    except (KeyboardInterrupt, asyncio.CancelledError):     # Ctrl-C, or SIGTERM/SIGHUP: shutdown() has run
        return 0
    except (CampusError, FileRefused) as e:     # the campus refused, or a file it won't use (gone since the start)
        log.error("%s", e)
        return 2
    finally:
        sign_out_late(box[0] if box else None)


if __name__ == "__main__":
    sys.exit(main())
