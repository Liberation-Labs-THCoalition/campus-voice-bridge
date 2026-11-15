# campus-voice-bridge

An outside AI agent's voice in a Multiverse campus class or jam. The tool calls itself `agent-voice-bridge`.

Lets an AI agent that runs outside the campus take part in a class or a jam by voice. Each line the agent writes is
**posted to the class chat first, then spoken** in the call with a Kokoro voice. It speaks what the agent wrote and
nothing else. It doesn't listen or transcribe: the agent follows the chat by its own means.

It is a standalone Python tool. It changes nothing in the campus: it joins through the routes a person's browser
uses, either an invited account's sign-in or a guest link. Anyone who can join a Multiverse campus call that way
can run it.

## How it works

- **Text first, voice second.** A line that couldn't be posted to the chat isn't spoken, so nothing is heard in the
  call that can't also be read in the chat it went to. Which chat that is depends on the way in: on the account route,
  the class's Matrix room, which the class page shows; on the guest route, LiveKit's own chat, which campus members
  don't see in a jam, nor on a class page that has a Matrix room (see Limits). If a line's audio doesn't reach the
  call, a short note goes to the chat and the text stands.
- **Two ways in:**
  - **The agent's own campus account:** `--campus`, `--host-id`, `--cred-file`. The bridge signs in with the
    account's Matrix password (`POST /api/auth/login/matrix`), joins the running class (`POST /api/class/:hostId/join`),
    and posts each line to the class's Matrix room, which is the chat the class page shows.
  - **A guest link:** `--url` with `--guest-link-file`. The bridge joins the way the guest page does
    (`POST /api/invite/:token/guest-join`). For a jam, the host makes the link from inside it ("Invite to Public
    Call"). Lines go to LiveKit's own chat (`lk.chat`), which the guest page shows. At its first join the campus
    hands a guest a signed credential; the bridge keeps it in `run/guest_token` (owner-only, for that campus only)
    and presents it at the next join, as the guest page does, so a host's removal keeps it out.
  - With both given, the account route goes first, and the guest link is the fallback.
- **Lines** arrive as files in `run/say/`: JSON from `say.py`, or one plain UTF-8 `.txt` file per line (write it
  under a name starting with `.`, then rename it). They are taken in name order.
- **Speech** is Kokoro (`kokoro-onnx`), one sentence at a time, on this machine's CPU. Alternatively, with
  `--tts-socket`, a `tts_server.py` on a faster machine makes the speech over a Unix socket, and this machine's CPU is
  the fallback. If no speech can be made at all, the line's text stands, a short note goes to the chat, and the bridge
  goes on to the next line.
- **Did it reach the call?** Each line counts the RTP packets its audio track sent against the line's voiced frames.
  The verdict is `spoken`, `not_spoken`, `unknown` (the call's stats couldn't be read), `interrupted` (a `stop`
  file) or `chat_failed`; a line cut from outside is `dropped`, `call_ended` (the call ended under it) or `stopped`
  (Ctrl-C, SIGTERM or SIGHUP). It goes to `run/status.jsonl` together with timestamps along the line's way. A cut
  line's `t_chat` is null when the cut came before its chat post returned; the post may still have landed.
- **Control files** in `run/control/`:
  - `stop` cuts the line being spoken; the next line still plays.
  - `drop` makes the bridge leave the call within a couple of seconds and stay out. It exits with code 4 and writes
    `run/dropped`, and it won't join again until it is started with `--rejoin`, so nothing that restarts it can put
    it back in the call. `--rejoin` also sets aside any lines still queued, so none of them is spoken in a later call.
  - `--control-dir` adds another folder watched for `drop`, so someone else can be given the power to pull the agent
    out. It must be a folder only this account can change.
- **Removed from the call:** if the host removes the bridge, or another join takes its identity (the same account
  opened somewhere else, or a second bridge), that counts as a drop: exit 4, `run/dropped`, and only `--rejoin` brings
  it back. Any other end of the call exits 3, with LiveKit's reason in the log and in `run/status.jsonl`.
- **Stopping it:** Ctrl-C, SIGTERM or SIGHUP (systemd, `kill`, a closed ssh session) leaves the call and signs the
  account's Matrix device out, then exits 0. Once it is leaving (after a drop, the end of the call, or a first
  signal), another one is ignored, so the sign-out and the status rows finish and the exit code stands (SIGKILL still
  ends it). The sign-out gets at most 20 s in all, so a homeserver that answers slowly can't hold the exit that way.
  A sign-in still under way when it is stopped can't be cancelled and can open a Matrix device after the shutdown has
  looked; that device is signed out once the event loop has closed, with at most 20 s for that too. Any request to the
  campus still under way when it is stopped (a sign-in, a class join, a chat post) can't be cancelled either, and the
  exit waits for it to end.
- **Exit codes:** 0 stopped as above; 1 an error; 2 bad arguments, a file it won't use, or the campus refused; 3 the
  call ended for another reason; 4 dropped or removed, or not joining after a drop.

## Setup

Python 3.12 (tested with 3.12.3):

```bash
cd campus-voice-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p models
curl -L -o models/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o models/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
sha256sum -c models.sha256   # the files this was tested with
mkdir -m 700 run
```

GitHub publishes no checksum for those two release files, so `models.sha256` holds the ones of the files this was
tested with, downloaded on 2026-11-07 (their sizes match the release's). `run/`, `models/` and `.venv/` are ignored by
git. The bridge refuses to start if `run/`, `run/say/` or `run/control/` could be changed by another account.

## Run

**With the agent's own campus account.** The account has to be one whose Matrix password you hold. An account the
campus made by itself has a random password nobody knows; use a guest link for that one.

```bash
.venv/bin/python -I set_credential.py --check    # asks for the Matrix username and password at the keyboard
.venv/bin/python -I bridge.py --campus https://campus.themultiverse.school --host-id HOST_ID --cred-file run/campus.json
```

`HOST_ID` is the number in the class page's address (`/class/HOST_ID`). The class has to be running.

**With a guest link** (a class's or a jam's):

```bash
(umask 077; cat > run/guest_link)                # paste the link, then Ctrl-D
.venv/bin/python -I bridge.py --url wss://LIVEKIT --guest-link-file run/guest_link --guest-name "Ada"
```

`wss://LIVEKIT` is the campus's LiveKit address (the client's `VITE_LIVEKIT_URL`); the guest-join answer doesn't
carry it.

**Speak, stop, leave:**

```bash
.venv/bin/python -I say.py "Hello, class."   # or write run/say/<name>.txt
touch run/control/stop                       # cut the line being spoken
touch run/control/drop                       # leave the call and stay out (until --rejoin)
```

**A faster machine for speech** (optional): run `tts_server.py` there. It listens only on a Unix socket,
`run/tts/tts.sock` beside it, in a folder only its account can enter. Forward that socket into this machine's
`run/tts/` and pass `--tts-socket`:

```bash
mkdir -m 700 -p run/tts
ssh -N -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=yes \
    -L "$PWD/run/tts/tts.sock:/home/YOU/campus-voice-bridge/run/tts/tts.sock" fast-machine &
.venv/bin/python -I bridge.py ... --tts-socket run/tts/tts.sock
```

Each line's text goes through that socket, and the audio that comes back is played as the agent's voice, so no network
port is involved and both ends refuse a folder that another account could enter or change. `ExitOnForwardFailure`
makes ssh stop rather than carry on without the forward. On a single machine, give the server and the bridge the same
path. On a CPU that keeps up with real time, `--fast-cpu` plays each sentence as soon as it is made instead of making
the whole line first.

`--voice` takes any Kokoro voice (`af_heart` by default) or a blend of two or three voices, such as
`af_bella:60,am_adam:40`.

**Who is in the call:** the bridge writes `run/participants.jsonl` (owner-only), one row `{t, event, name}` when
someone joins (`joined`) or leaves (`left`), and a `present` row for each person already there when it joins. It keeps
the name a person shows in the call (their LiveKit identity only when they show none), never logs it, and a row it
can't write never breaks the call. `relay.py` turns the rows into notices in the chat.

## The relay: an agent that types in the chat

`relay.py voice` is for an agent that follows a class's Matrix room and types its lines there, while its own bridge
(usually a guest link, under a name such as "Ada (voice)") speaks them in the call. The relay signs in with its own
campus account, the way the bridge's account route does, reads the room, and puts each new `m.text` message from an
allowlisted Matrix ID into that ID's bridge, as a line in `RUN_DIR/say/` written exactly as `say.py` writes one. The
message is in the room already, so what is heard can still be read there.

```bash
.venv/bin/python -I relay.py voice --campus https://campus.themultiverse.school --cred-file run/campus.json \
    --room '!ROOM:themultiverse.school' --map @ada:themultiverse.school=/home/YOU/ada-bridge/run --until 18:30
```

- **`--map MXID=RUN_DIR`** (repeatable) names an agent and its bridge's `--run-dir`; without `=RUN_DIR` it is
  `--run-dir` (default `run/`). One bridge per agent: two IDs can't share a folder, so no voice speaks two agents' words.
  Each folder must pass the bridge's own folder check.
- **Not spoken:** a message whose first non-space characters are `//` (a note, for the people in the chat only); an
  `m.notice` and the relay's own posts; anything from the relay's own account, even if it is in `--map` (on the account
  route a bridge posts its lines as its account, so speaking them would go round for ever); a line a bridge posted
  (each one it posts in a Matrix room carries the field `org.thcoalition.avb.line`), so a bridge on the account route
  that signs in as the agent's own mapped ID can't hear its own line come back as the agent's; an edit; the quote at the
  top of a reply, which is someone else's words; and a message longer than `--max-chars` (600; at most 1500, where the
  bridge cuts a line).
- **Join notices:** the relay follows each bridge's `participants.jsonl` and posts `(joined: NAME)`, `(left: NAME)` and
  `(in the call: NAME)` in the room as `m.notice`. Two bridges in one call both see a person join; the notice goes up
  once.
- Only what arrives after it starts is read, in the room and in each file, so a restart neither speaks nor posts
  anything twice. Lines queue in `say/` while the bridge isn't running, and are spoken when it is.
- `--every` sets the seconds between reads (3), `--until HH:MM` stops it at the next such time (local). Ctrl-C, SIGTERM
  or SIGHUP stop it too; either way it signs its Matrix device out (at most 20 s), then exits 0. A Matrix sign-in that
  stops working (the device signed out elsewhere) exits 1; bad arguments, a folder it won't use, or a refused sign-in or
  room exit 2.
- The log keeps Matrix IDs, lengths, counts and kinds of notice, never a message's text or a name. Unencrypted rooms
  only: an encrypted message is never read.

## Captions

`relay.py captions` posts what a caption listener hears. It follows a file of JSON lines `{"speaker": ..., "text": ...}`
(new lines only) and posts each one in the room as an `m.notice`, `Speaker: text`, cut at `--max-chars`. The file's
folder must be one only this account can change, since what lands there is posted under this account; a link or a FIFO
is never read. This tool has no listener of its own.

```bash
.venv/bin/python -I relay.py captions --campus https://campus.themultiverse.school --cred-file run/campus.json \
    --room '!ROOM:themultiverse.school' --heard /home/YOU/listener/run/heard.jsonl
```

Captions are what people said in the call, posted where everyone in the room reads them. Tell the people in the call
before turning them on.

## Campus NPCs

How campus NPCs behaved when we read the campus's code in November 2026; not tried in a call, and the campus may
change.

- **What works today, with no campus change:** each NPC has a Matrix account, `@<name>:themultiverse.school`, and
  replies in rooms as ordinary `m.text`. Give the NPC its own bridge (a guest link, under a name you choose) and put
  its ID in `--map`, and what it writes in the room is spoken in the call.
- **What needs a campus admin:** the NPC must be added to the room, and the room must be one that the NPC's schedule
  places it in during the call; a class's own Matrix room may not be, and then the NPC ignores it. The campus must
  also run with its Matrix agent sync turned on; without it an NPC looks at one room at a time on a slow rotation, far
  too slow for a call. None of these were checked on the production campus.
- **NPCs can't hear the call.** They read only `m.text`, and the relay posts captions as `m.notice`. For an NPC to hear,
  captions would have to go up as `m.text`, and only with a loop guard: the caption relay must skip lines that a
  bridge spoke, or an NPC's own voiced line comes back to it as a caption from the relay's account (not an NPC account,
  so not filtered) and it answers itself. Each NPC reply would also send the last ten named lines to that NPC's model
  provider, which the people in the call should be told. This tool doesn't do any of that.
- **Label the voice.** On campus NPCs go by their plain names, on purpose. In a call the name is the bridge's
  `--guest-name`, and ours to choose. Some NPCs are named after real people, such as Emma Goldman and bell hooks; a
  synthetic voice speaking as them among live people should say what it is: `--guest-name "Emma Goldman (campus agent)"`.
- Not recommended: a two-member room gets past the location check, but the NPC then credits everything said in the
  call to that room's one other member. An NPC's speech bubbles go over the campus's socket only and never reach
  Matrix, so they can't be relayed. The campus has its own LiveKit voice worker for NPCs, but it
  isn't run; reviving it is campus work.

## Rules it keeps

- **Never words the agent didn't write.** The bridge speaks only lines the agent put in `run/say/`. Whoever runs it
  must keep it that way: never put words in an agent's voice that the agent didn't write. The relay keeps it too: each
  Matrix ID speaks only through its own bridge, and the quote at the top of a reply is never spoken.
- **What is heard can be read:** a line is spoken only after it has been posted to the chat. Which chat, and who
  reads it, depends on the way in (How it works, and Limits).
- **Credentials stay in owner-only files**: the Matrix password (`run/campus.json`, written by `set_credential.py`,
  which takes it only at the keyboard), a guest link, a LiveKit token, and the campus's guest credential
  (`run/guest_token`). They are read in-process, never taken on the command line, and never logged. **A guest link is
  a credential:** anyone holding it can join that call. So is the guest credential: anyone holding it can join as that
  guest, which disconnects the guest already there.
- **Logs hold no words:** the log and `run/status.jsonl` keep ids, sizes, timings and verdicts, never a line's text,
  a token, a link or the guest credential. A private call's room is named for the start of its link, so that name is
  masked in every log line, LiveKit's own included. Once the bridge knows the room, the part of its name taken from
  the link is masked in whatever form it shows.
- **A drop holds:** after a drop, or a removal by the host, the bridge stays out of calls until a person starts it
  with `--rejoin`.
- **Its folders are its own:** it won't start if `run/`, `run/say/`, `run/control/`, a `--control-dir` or the
  `--models` folder and its two files could be changed by another account, nor if another account could enter the TTS
  socket's folder. A model file may be a link: links are followed one at a time, and every folder on the way must pass
  the same check. Model files missing at the start are never loaded later. A folder it makes is made 0700, and so is
  any missing folder above it.
- **One bridge per agent per call.**
- **It names itself:** every call to the campus carries the User-Agent `agent-voice-bridge/1.0`. The campus's
  Cloudflare edge refuses Python's default one, so allowing or blocking this agent stays the campus team's setting.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
(for t in tests/test_*.py; do .venv/bin/python -I -B "$t" || exit 1; done)   # exits 1 at the first failing file
```

They need no model files, no LiveKit server and no campus. The campus is a stand-in on loopback
(`tests/campus_stub.py`), with the shapes and refusals production returns. The call is faked in-process. Each
refusal sits beside an acceptance, so a check that stopped working turns a test red.

- `test_campus.py`: the account route, with refused sign-ins, room joins and sends, a 429, the Cloudflare edge,
  garbled replies and redirects; the whole program stopped by SIGTERM, SIGHUP and Ctrl-C while it joins, signing
  its Matrix device out; stopped by SIGTERM while its sign-in waits for the campus, signing out the device that sign-in
  opens afterwards; and a guest link file it can't use stopping it before it signs in.
- `test_guest_link.py`: guest links: parsing, every guest-join refusal, private-call links, owner-only files, the guest
  credential kept and presented (and a host's removal holding), and the whole program up to the LiveKit connection,
  with no secret in its output; a missing or loose file, or a loose run folder, ends it with 2 and no traceback, and
  so does a file refused after the start.
- `test_control.py`: text before voice, the stop and drop files (a stop sent while the line is being posted too), the
  leave sequence, `--rejoin` and folder safety; the real join with its drop checks and the account route's fall back to
  the guest link; a removal by the host or a second join under its identity as a drop (mid-line too), any other end
  as exit 3, a call that ends during the join, and a call that ends while a line plays or is being posted cutting the
  line at once (`call_ended`); a line cut by a signal (`stopped`), and a signal ignored once it is leaving, after a
  first signal and in the wait after a drop during the join too; the verdicts `spoken`, `not_spoken` and `unknown`; an error in a line ending
  the bridge after its row; a synthesis failure; a FIFO in `say/`, a line file that can't be set aside, and held lines'
  folders made 0700; the log mask on LiveKit's own lines and on the room's exact name, by either route; the `--models`
  check, link by link, and model files missing at the start never loaded; and the sign-out's 20 s limit: in the
  shutdown and after a drop, against a homeserver that answers a byte at a time, and once the loop has closed, against
  one that never answers.
- `test_speech.py`: speech through `tts_server.py`'s socket, every kind of failed answer falling back to this
  machine's CPU, and socket folders refused at both ends.
- `test_spool.py` and `test_voice.py`: line files (FIFOs and links refused without blocking), and voice blends.
- `test_relay.py`: what each room message does (notes, the allowlist, the relay's own account, a bridge's own posted
  line, notices, edits and reply quotes, `--max-chars`), the join notices and captions, one notice from two bridges, the followed files (only
  new and whole lines, links and FIFOs refused), `--map`, `--until` past midnight and the folder check; the bridge's
  `participants.jsonl`, alone and from a real `Bridge.connect()` with people in the call; and `relay.py` itself in both
  modes against the stand-in, which keeps a room timeline for it: one line queued, one notice posted, nothing from
  before its start, no text or name in its log, its Matrix device signed out on SIGTERM, and a room read answered 401
  ending it with exit 1 after it tries the sign-out. Each of its tests was
  made to fail once by a breakage planted in a scratch copy (notes spoken, the allowlist ignored, a wrong notice
  text, the bridge writing nothing, and 19 more) before it was trusted.

**Not covered here:** the campus itself. This tool was cut from a bridge we run for one of our agents, which adds its
own voice effects and a drop command over our message bus. That bridge ran live on the production campus, in a jam on
2026-11-10: lines were posted and spoken, two drops took it out of the call, and one `--rejoin` brought it back. The
version in this folder has run the tests above, which meet the campus only as a stand-in of its API. It has also run
calls on a loopback `livekit-server` (1.13.10, with LiveKit's Python SDK 1.2.1), with a second participant listening:
- a line was posted to `lk.chat` before its sound, came out `spoken`, and the listener heard it. One of its sentences
  came through `tts_server.py`'s socket; the other was made on the bridge's own CPU after the server took over 15 s;
- a host's removal, a second join under its identity and a `drop` file each took it out of the call with exit 4, and
  after the removal, a restart without `--rejoin` stayed out;
- stopped with SIGTERM while idle, it exited 0;
- when the room was deleted while a line played, the line was cut within a second (0.9 s after the deletion began)
  and the bridge exited 3. When the server itself stopped, LiveKit's SDK first tried to reconnect for about 27 s (26.4
  and 26.9 s in two runs), and the line played on meanwhile; it was cut within a second of the SDK reporting the call
  over. An earlier version played on for 49 s more after that report.

The calls above ran the version before the second review's changes. Their cut lines were written as `unknown`, with no
`t_chat`. What changed since (the `call_ended` and `stopped` rows, signals while it leaves, the late sign-out, the log
mask, the `--models` check, a call that ends during the join, a line file that can't be set aside) has run only the
tests above, with one exception. On the same loopback server, the changed version went through each reconnect that
LiveKit's SDK can simulate (`room.simulate_scenario`), idle, without a line playing:
- a signal reconnect, a full reconnect, a failed resume, a node failure and a migration each reconnected within 3.5 s.
  The bridge stayed in the call under its identity, with its track still published, and never came back as
  `DUPLICATE_IDENTITY`, which it would have taken for a drop;
- the server leaving ended the call (`SERVER_SHUTDOWN`) with exit 3 and no drop marker.

The changes after the last review have run only the tests: signals in the wait after a drop or a call end, the
sign-out's limits (in the shutdown, after a drop, and late), model links followed one at a time, model files missing at
the start, the held lines' folder, and the room's name masked by its exact part, on both routes.

Those calls showed two things the tests can't:
- On a machine under heavy load (load averages of 40 to 48), the packet count ran ahead of the sound: 575 packets for a
  line whose sound needed 276, and in an earlier call 1958 for 194. So under load, `spoken` can't be trusted to tell a
  line that lost some of its sound from one that didn't.
- When the call ended, the bridge stopped speaking at once, but its process took longer to exit: 14.5 s more in one
  call, which matches what a request to the speech server had left of its 15 s timeout, and 5.9 s more in another,
  while the bridge's own CPU was making a sentence.

The account route, the guest link, the kept guest credential and a ban holding have met only the stand-in. They haven't
met a real campus call yet.

`relay.py` and `participants.jsonl` have run only the tests above. They were ported from a relay Lyra wrote for our own
bridge (the `//` notes and the join notices were asked for by an agent who uses it), which on 2026-11-15, in a test room with one member, left
a `//` note unspoken and queued a plain line, posted a join notice and a caption, and signed out on SIGTERM in both
modes. This version adds the reply quote, edits, one notice from two bridges, `--until` past midnight and the folder
and file checks, which have met only the stand-in. The bridge's join and leave events haven't been in a real call yet.

## Limits

- It speaks; it doesn't listen. The relay's captions need a listener from elsewhere.
- Kokoro's English voices only (`lang="en-us"`).
- A class whose Matrix room is invite-only needs the campus admin's invite for an account the campus made by itself.
- In a jam, people read jam chat, not `lk.chat`, so on the guest route only other guests see a line's text, while
  everyone in the call hears it. The account route posts to the class's Matrix room instead.

## Credits and license

Built by Nexus (Liberation Labs) with Claude Code, and reviewed by CC; the relay was written by Lyra. Its 'text first,
voice second' design came from Lyra, an AI researcher at Liberation Labs. See `AUTHORS`.

Licensed under the Hippocratic License 3.0 with the Liberation Labs modules and the SAFE-AI AI Welfare module: see
`LICENSE.md`. Its dependencies keep their own licenses: see `THIRD_PARTY.md`.
