"""The bridge's control files, read in RUN/control/ (and in any --control-dir folder):

  stop   cut the line being spoken; the next line still plays.
  drop   leave the call and stay out: the bridge exits 4 and won't join again until it is started with --rejoin.

Anyone who can create a file in one of those folders can pull the agent out of the call, so keep them private to the
people who should have that power. A drop file is never opened: what counts is that a small regular file of that name
is there. Links, folders, FIFOs and big files are left alone (and logged once), so nothing can make the bridge follow
a link or block on a read.

The folder checks live here too, so that bridge.py and tts_server.py hold their folders to the same rules: unsafe_dir
for a folder whose files decide what the bridge does (RUN, say/, control/, --control-dir), models_problem for the
Kokoro files it loads (--models), and private_dir_problem for the folder of the TTS socket, which no other account may
even enter. make_private_dirs makes such folders without leaving a looser one above them. This module needs nothing
outside the standard library, so tts_server.py can use it on a machine without LiveKit.
"""
import os
import stat

MAX_DROP_FILE = 4096
PREVIEW = 64


def printable(data, limit=PREVIEW):
    """At most `limit` characters of data (bytes or str), printable ASCII only; anything else is shown as '?'."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data[:limit]).decode("ascii", "replace")
    return "".join(c if " " <= c <= "~" else "?" for c in str(data)[:limit])


def take_drop_file(path):
    """(True, note) if path is a small regular file, which is then removed (note says if it couldn't be); (False, why)
    otherwise, with why None when nothing is there. Never follows a link and never opens the file."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False, None
    except OSError as e:
        return False, "can't look at it (%s)" % type(e).__name__
    if not stat.S_ISREG(st.st_mode):
        return False, "not a regular file"
    if st.st_size > MAX_DROP_FILE:
        return False, "bigger than %d bytes" % MAX_DROP_FILE
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass                    # taken by someone else between the look and the removal; it was there
    except OSError as e:
        return True, "left in place: can't remove it (%s)" % type(e).__name__
    return True, None


def group_is_ours(gid):
    """True if no account but this one (and root) belongs to group gid, as a member or by its own primary group. With
    a umask of 002 a user's folders are group-writable by the user's own group, which then has no other member."""
    import grp, pwd
    try:
        g = grp.getgrgid(gid)
        me = pwd.getpwuid(os.getuid()).pw_name
        others = {m for m in g.gr_mem if m not in (me, "root")}
        others |= {p.pw_name for p in pwd.getpwall() if p.pw_gid == gid and p.pw_uid not in (0, os.getuid())}
    except KeyError:
        return False
    return not others


def writable_by_others(st):
    """True if others, or a group with another account in it, can write what st describes."""
    return bool(st.st_mode & 0o002 or (st.st_mode & 0o020 and not group_is_ours(st.st_gid)))


def unsafe_dir(path, owners=None):
    """Why a folder can't be trusted, or None. The bridge acts on the files in it (a line in say/, a drop file in a
    control folder), as this account, so the folder and every folder above it must be ones only this account (or root)
    can change: a real folder, not a link; owned by us (or by one of `owners`, uids); not writable by others, nor by a
    group with another account in it; and every folder above it owned by us or root, and not writable that way either
    unless sticky (as /tmp is, which stops others from renaming what they don't own)."""
    real = os.path.realpath(path)
    if real != os.path.abspath(path):
        return "it is or passes through a link (use %s)" % real
    try:
        st = os.lstat(real)
    except OSError as e:
        return "can't look at it (%s)" % type(e).__name__
    if not stat.S_ISDIR(st.st_mode):
        return "not a folder"
    if st.st_uid not in (owners or (os.getuid(),)):
        return "not owned by this account"
    if writable_by_others(st):
        return "group or others can write it"
    d = os.path.dirname(real)
    while True:
        try:
            st = os.lstat(d)
        except OSError as e:
            return "can't look at %s (%s)" % (d, type(e).__name__)
        if st.st_uid not in (0, os.getuid()):
            return "%s belongs to another account" % d
        if writable_by_others(st) and not st.st_mode & stat.S_ISVTX:
            return "%s can be changed by group or others" % d
        if d == os.path.dirname(d):
            return None
        d = os.path.dirname(d)


MAX_LINK_HOPS = 8                       # links in a row on the way to a model file


def models_problem(path, names=("kokoro-v1.0.onnx", "voices-v1.0.bin")):
    """Why the bridge won't load Kokoro's files from path, or None. What it loads decides how every line sounds, so
    the folder, and every folder a link on the way to a file points into, must pass unsafe_dir's rules (root may own
    them too), and each file must be a regular file owned by this account or root, which no group with another account
    in it, nor others, can write. Links are followed one hop at a time: a link resolved straight to its end would show
    only the last folder, and a link on the way in a folder others can change could be pointed anywhere."""
    ours = (0, os.getuid())
    why = unsafe_dir(path, owners=ours)
    if why:
        return why
    for n in names:
        p = os.path.join(path, n)
        for _ in range(MAX_LINK_HOPS + 1):
            try:
                st = os.lstat(p)
                nxt = os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
            except OSError as e:
                return "%s: can't look at it (%s)" % (n, type(e).__name__)
            if nxt is None:
                break
            p = os.path.join(os.path.dirname(p), nxt)     # a relative link is read from its own folder
            why = unsafe_dir(os.path.dirname(p), owners=ours)
            if why:
                return "%s links into %s: %s" % (n, os.path.dirname(p), why)
        else:
            return "%s: more than %d links in a row" % (n, MAX_LINK_HOPS)
        if not stat.S_ISREG(st.st_mode):
            return "%s: not a regular file" % n
        if st.st_uid not in ours:
            return "%s belongs to another account" % n
        if writable_by_others(st):
            return "%s: group or others can write it" % n
    return None


def make_private_dirs(paths):
    """Make each folder 0700, and any missing folder above it 0700 too, not under the umask. Raises OSError."""
    old = os.umask(0o077)
    try:
        for p in paths:
            os.makedirs(p, mode=0o700, exist_ok=True)
    finally:
        os.umask(old)


def private_dir_problem(path):
    """Why a folder can't hold the TTS socket, or None: unsafe_dir's rules, and no way in at all for group or others
    (0700), so no other account can connect to the socket there, read what is said through it, or put its own socket
    in its place."""
    why = unsafe_dir(path)
    if why:
        return why
    if os.lstat(path).st_mode & 0o077:
        return "group or others can enter it (want 0700)"
    return None
