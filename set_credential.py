#!/usr/bin/env python3
"""Put the Matrix username and password of the agent's campus account where the bridge reads them.

Run it yourself, in your own terminal, and only for an account whose password you hold. An account the campus made by
itself (for a guest who signed up, say) has a random Matrix password nobody knows: use a guest link for that one.

    python -I set_credential.py [--check]            write run/campus.json (refuses if it exists)
    python -I set_credential.py --replace [--check]  write it again over the old one
    python -I set_credential.py --check-only [FILE]  ask nothing, write nothing: sign in once with FILE (default
                                                     run/campus.json) and sign out
    [--run-dir DIR] puts the file in DIR instead of run/ (give bridge.py the same --run-dir)

Asking. Only at the keyboard (/dev/tty), and with echo off for all three answers: the username, then the password
twice. The username is shown back ("Username: ada") only once it fits Matrix's own grammar for a username (lowercase
a-z, digits and . _ = - / +), so a password typed or auto-typed at the username question by mistake is refused unseen,
unless it is made of those characters only; then "Username:" shows it, and Ctrl-C at the next question stops with
nothing written or sent. There is no way to hand it the password by argument, environment variable, file or pipe:
without a terminal it refuses to start, and if it can't turn echo off it stops before reading anything. Nothing it
prints holds the password or its length, or quotes a refused username; argparse's own messages, which can quote a
value from the command line, are replaced by a fixed line, and no refusal echoes a --run-dir value.

Writing. The file is the JSON campus.read_credentials expects, {"username": ..., "password": ...}, at mode 600, for
bridge.py --cred-file run/campus.json. Before asking anything it refuses unless the run folder is a real folder (not a
link), owned by the user running this, with mode 700, and from then on it works only through the folder it checked (an
fd), so a folder swapped in under the same name changes nothing. It first removes any .campus.json.<hex>.tmp an earlier
run left there when it was killed, and says so. It writes a new .campus.json.<hex>.tmp (O_CREAT|O_EXCL|O_NOFOLLOW at
0600, then fchmod 600 whatever the umask), fsyncs it, and puts it in place in one step: a hard link for a new file
(refused if campus.json appeared meanwhile), a rename for --replace; then it fsyncs the folder. So campus.json is never
half-written, never has a wider mode, and no link is followed. Ctrl-C, SIGQUIT (Ctrl-backslash), SIGTERM or SIGHUP
(an ssh drop) while it writes or checks removes the temporary file; only SIGKILL or a power cut can leave one, and the
next run removes it. It writes nothing else.

Checking. --check signs in once through campus.py exactly as the bridge does (reading the file back with the bridge's
own reader), signs that new Matrix device out again, and prints "signed in: yes" or "signed in: no (step S, HTTP N)",
plus one line when a device may have stayed signed in. With --replace --check, the new file is checked BEFORE it
replaces the old one, and replaces it only if the Matrix server took the password ("yes", or the campus's 403, which
comes only after a Matrix sign-in); otherwise the old file stays exactly as it was. A new file (--check alone) stays as
written whatever the check says. Each sign-in opens a Matrix device on the account and counts toward the campus's
limit of 30 in 15 minutes.

Exit codes: 0 done; 1 refused or stopped (nothing written, unless the message says the file is written); 2 usage; 3 the
sign-in check said no. --campus URL picks another campus (https, or http on loopback for tests/campus_stub.py).
"""
import argparse, contextlib, errno, getpass, hmac, io, json, os, pwd, re, secrets, signal, stat, sys, urllib.parse, \
    warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                # -I leaves the script's own folder off the path
from campus import Campus, CampusError, read_credentials  # noqa: E402

RUN = os.path.join(HERE, "run")
NAME = "campus.json"
LEFTOVER = re.compile(r"\.campus\.json\.[0-9a-f]{12}\.tmp")    # this script's temporary files, and nothing else
CAMPUS = "https://campus.themultiverse.school"     # the Multiverse campus
SERVER = "themultiverse.school"                     # the campus homeserver's user ids: @ada:themultiverse.school
LOCALPART = re.compile(r"[a-z0-9._=/+-]+")          # the Matrix spec's grammar for the localpart of a user id
NOTHING = "Nothing was written."
YES, PASSWORD_OK, NO = "yes", "the password works", "no"
STRAY_403 = ("The campus signed in to Matrix before it refused, so one device named MultiverseCampus stays signed in "
             "on this account: remove it from the account's sessions list.")
MAYBE_STRAY = ("The campus may have signed in to Matrix before this went wrong, so a device named MultiverseCampus may "
               "be signed in on this account: look at the account's sessions list.")


class Refused(Exception):
    """A reason to stop. The message never holds the username or the password."""


class Signalled(BaseException):
    """SIGTERM, SIGHUP or SIGQUIT while the file is written or checked: raised, so the temporary file is removed."""


def run_dir_problem(st, euid):
    """None if a folder's lstat/fstat result is fit to hold the credential, else what is wrong with it."""
    if not stat.S_ISDIR(st.st_mode):
        return "isn't a folder"
    if st.st_uid != euid:
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
        return "is owned by %s, not by %s, who is running this" % (owner, user_name(euid))
    if stat.S_IMODE(st.st_mode) != 0o700:
        return "has mode %o, not 700" % stat.S_IMODE(st.st_mode)
    return None


def user_name(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def errno_name(e):
    return errno.errorcode.get(e.errno, str(e.errno))


def open_run_dir(path, label):
    """An fd on the folder, after checking it. Everything after this goes through the fd, so a folder swapped in
    under the same name afterwards changes nothing."""
    must = "It must be a folder owned by %s with mode 700. %s" % (user_name(os.geteuid()), NOTHING)
    label = label[:1].upper() + label[1:]
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        why = ("is a link" if os.path.islink(path) else "isn't there" if isinstance(e, FileNotFoundError)
               else "can't be opened (%s)" % errno_name(e))
        raise Refused("%s %s. %s" % (label, why, must)) from None
    problem = run_dir_problem(os.fstat(fd), os.geteuid())
    if problem:
        os.close(fd)
        raise Refused("%s %s. %s" % (label, problem, must))
    return fd


def existing(dfd, name):
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def clear_leftovers(dfd, label):
    """Remove the temporary files a killed run left (they hold a password): regular files of the running user only."""
    gone = 0
    for n in sorted(os.listdir(dfd)):
        st = existing(dfd, n) if LEFTOVER.fullmatch(n) else None
        if st is not None and stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid():
            os.unlink(n, dir_fd=dfd)
            gone += 1
    if gone:
        print("Removed %d temporary file(s) that an earlier run, stopped while writing, left in %s."
              % (gone, label), file=sys.stderr)


def check_username(u):
    """The Matrix username as the campus signs in with it: a bare localpart (ada) or @ada:themultiverse.school, in
    Matrix's lowercase grammar. No message here quotes what was typed: it may be a password."""
    if not u:
        raise Refused("No username given. " + NOTHING)
    if u.startswith("@"):
        local, sep, server = u[1:].partition(":")
        if not sep:
            raise Refused("Leave out the @ (just the name, for example ada), or give the whole "
                          "@name:%s. %s" % (SERVER, NOTHING))
        if server.lower() != SERVER or not LOCALPART.fullmatch(local):
            raise Refused("That isn't a username on the campus's Matrix server (@name:%s, the name in lowercase). %s"
                          % (SERVER, NOTHING))
        return u
    if "@" in u:
        raise Refused("That looks like an email address. The campus signs in with the Matrix username (for example "
                      "ada), not the email. " + NOTHING)
    if not LOCALPART.fullmatch(u):
        raise Refused("A Matrix username has only lowercase letters, digits and . _ = - / + (what you typed isn't "
                      "shown). " + NOTHING)
    return u


class Terminal:
    """The keyboard and screen of the terminal this runs in (/dev/tty), and nothing else: stdin is never read."""

    def __init__(self):
        try:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        except OSError:
            raise Refused("This asks for a password, so it runs only in a terminal you are typing in (run it "
                          "yourself, not through a session or a pipe). " + NOTHING) from None
        self.tty = io.TextIOWrapper(io.FileIO(fd, "r+"), errors="strict")

    def say(self, line):
        self.tty.write(line + "\n")
        self.tty.flush()

    def ask_secret(self, prompt):
        """A line typed with echo off. getpass reads the same /dev/tty; if it can't turn echo off it warns and falls
        back to reading with echo, and that warning is an error here, raised before anything is read."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass(prompt, stream=self.tty)
            except getpass.GetPassWarning:
                raise Refused("This terminal won't turn echo off, so nothing was read. " + NOTHING) from None

    def close(self):
        try:
            self.tty.close()
        except OSError:
            pass


def ask_credentials(term):
    try:
        user = check_username(term.ask_secret("Matrix username (for example ada; not the email; "
                                              "it shows after Enter): ").strip())
        term.say("Username: " + user)
        pw = term.ask_secret("Matrix password (nothing shows as you type or paste): ")
        if not pw:
            raise Refused("No password given. " + NOTHING)
        again = term.ask_secret("Same password again: ")
    except UnicodeDecodeError:          # its message would quote a byte of what was typed: say nothing of it
        raise Refused("What was typed couldn't be read as text (is the terminal set to UTF-8?). " + NOTHING) from None
    try:
        same = hmac.compare_digest(pw.encode("utf-8"), again.encode("utf-8"))
    except UnicodeEncodeError:
        raise Refused("That password has characters this can't store. " + NOTHING) from None
    del again
    if not same:
        raise Refused("The two passwords weren't the same. " + NOTHING)
    return user, pw


def write_file(dfd, name, data):
    """Create name in the folder, O_EXCL and O_NOFOLLOW at 0600, write it whole and fsync it; returns its (st_dev,
    st_ino). The caller removes it if anything fails."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        os.fchmod(fd, 0o600)            # the umask can only take bits away; this makes it exactly 600
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        st = os.fstat(fd)
        return st.st_dev, st.st_ino
    finally:
        os.close(fd)


@contextlib.contextmanager
def signals_raise():
    """SIGTERM, SIGHUP and SIGQUIT raise Signalled while this holds (their default would end the process with no
    cleanup)."""
    def stop(signum, _frame):
        raise Signalled(signum)
    old = [(s, signal.signal(s, stop)) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)]
    try:
        yield
    finally:
        for s, h in old:
            signal.signal(s, h)


def campus_url(u):
    p = urllib.parse.urlsplit(u)
    loopback = p.hostname in ("127.0.0.1", "localhost", "::1")
    if not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ("", "/") \
            or not (p.scheme == "https" or (p.scheme == "http" and loopback)):
        raise Refused("--campus takes the campus's https address only, like %s. %s" % (CAMPUS, NOTHING))
    return u


def check_sign_in(path, url):
    """Sign in once with the file as the bridge reads it, and sign that device out again; print only the outcome.
    Returns YES, PASSWORD_OK (the campus refused with a 403, which it sends only after the Matrix server took the
    password) or NO."""
    c = None
    try:
        try:
            c = Campus(url)
            user, pw = read_credentials(path)
        except CampusError as e:
            print("signed in: no (step %s, HTTP none (no request sent))" % e.step)
            return NO
        try:
            c.sign_in(user, pw)
        except CampusError as e:
            print("signed in: no (step %s, HTTP %s)" % (e.step, e.status if e.status else "none"))
            if e.status == 403 and not e.edge:
                print(STRAY_403)
                return PASSWORD_OK
            if not c.mx_token and e.status and (200 <= e.status < 300 or e.status >= 500):
                print(MAYBE_STRAY)
            return NO
        except Exception as e:                      # a bug, not the campus: its type only, never its text
            print("signed in: no (step %s, HTTP none)" % type(e).__name__)
            return NO
        finally:
            del user, pw
        print("signed in: yes")
        return YES
    finally:
        # Only when a sign-in left this a Matrix token. An older campus.py's sign_out returns None: nothing is claimed.
        if c is not None and c.mx_token and c.sign_out() is False:
            print("signed out: no (one Matrix device named MultiverseCampus is still signed in on this account)")


class Parser(argparse.ArgumentParser):
    """argparse's own error messages can quote a value from the command line (--check=VALUE): a fixed line instead."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print("That command line isn't right. This never takes the password on the command line: it asks for it. "
              + NOTHING, file=sys.stderr)
        sys.exit(2)


def main(argv=None):
    ap = Parser(description="Write the bridge's campus credential (run/campus.json), asking at the keyboard. Never "
                            "takes the password any other way.", allow_abbrev=False)
    ap.add_argument("--replace", action="store_true", help="write over an existing run/campus.json")
    ap.add_argument("--check", action="store_true",
                    help="then sign in once to verify it, and sign out again (with --replace: before replacing)")
    ap.add_argument("--check-only", nargs="?", const="", metavar="FILE",
                    help="ask and write nothing: sign in once with FILE (default run/campus.json), and sign out")
    ap.add_argument("--campus", default=CAMPUS, help="the campus's address (default %(default)s)")
    ap.add_argument("--run-dir", default=RUN,
                    help="the folder that holds campus.json (default: run/ beside this file; mode 700)")
    a, unknown = ap.parse_known_args(argv)
    if unknown:                 # not echoed: a password typed on the command line shouldn't land on the screen too
        print("%d argument(s) this doesn't take. It never takes the password on the command line: it asks for it. %s"
              % (len(unknown), NOTHING), file=sys.stderr)
        return 2
    try:
        url = campus_url(a.campus)
        if a.check_only is not None:
            if a.replace or a.check:
                print("--check-only asks and writes nothing; it doesn't go with --replace or --check.", file=sys.stderr)
                return 2
            path = a.check_only or os.path.join(a.run_dir, NAME)
            return 0 if check_sign_in(path, url) == YES else 3
        return write_mode(a, url)
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. " + NOTHING, file=sys.stderr)
        return 1
    except Exception as e:      # a bug: its type only (a message could quote what was typed); check run/ by hand
        print("Stopped by an unexpected %s." % type(e).__name__, file=sys.stderr)
        return 1


def write_mode(a, url):
    run_dir = os.path.abspath(a.run_dir)
    real = run_dir == os.path.abspath(RUN)
    label = run_dir if real else "the --run-dir folder"     # a --run-dir value is never echoed: it could be anything
    shown = os.path.join(run_dir, NAME) if real else "campus.json in the --run-dir folder"
    dfd = open_run_dir(run_dir, label)
    try:
        clear_leftovers(dfd, label)
        st = existing(dfd, NAME)
        if st is not None and not a.replace:
            raise Refused("%s is already there. %s To change it, run this again with --replace." % (shown, NOTHING))
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise Refused("%s is there but isn't a plain file. %s" % (shown, NOTHING))
        term = Terminal()
        try:
            user, pw = ask_credentials(term)
        finally:
            term.close()
        data = json.dumps({"username": user, "password": pw}).encode("ascii")
        del user, pw
        verdict, had = place(dfd, data, a, url, run_dir, shown)
        del data
        if verdict == NO and a.replace:     # --replace --check said no: the old file stays, nothing new was kept
            return 3
        return report(dfd, a, url, os.path.join(run_dir, NAME), verdict, had)
    finally:
        os.close(dfd)


def place(dfd, data, a, url, run_dir, shown):
    """Write a temporary file and put it in place as campus.json; returns (the --replace --check verdict or None,
    whether an old campus.json was replaced). With --replace --check the new file is checked first, and replaces the
    old one only if the Matrix server took the password."""
    tmp = ".%s.%s.tmp" % (NAME, secrets.token_hex(6))
    made = None                         # (st_dev, st_ino) of the temporary file once it is whole
    verdict = had = None
    tmp_gone = False                    # renamed into place, or linked into place and its own name removed
    try:
        with signals_raise():
            made = write_file(dfd, tmp, data)
            if a.replace and a.check:
                verdict = check_sign_in(os.path.join(run_dir, tmp), url)
                if verdict == NO:
                    print(("Kept the old campus.json exactly as it was; what you typed wasn't saved."
                           if existing(dfd, NAME) is not None else "Nothing was written: there was no campus.json, and "
                           "what you typed wasn't saved.") + " Run this again with --replace --check to try again.")
                    return verdict, False
            if a.replace:
                had = existing(dfd, NAME) is not None
                os.replace(tmp, NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
                tmp_gone = True
            else:
                try:
                    os.link(tmp, NAME, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                except FileExistsError:
                    raise Refused("%s appeared while you were typing. %s To change it, run this again with --replace."
                                  % (shown, NOTHING)) from None
                os.unlink(tmp, dir_fd=dfd)      # before the folder's fsync, so the removal is on disk with the link
                tmp_gone = True
            try:
                os.fsync(dfd)
            except OSError as e:
                raise Refused("The file is written: %s. But the fsync of its folder failed (%s), so it might not "
                              "survive a power cut. Check the disk, then run this with --check-only."
                              % (shown, errno_name(e))) from None
    except (KeyboardInterrupt, Signalled, OSError) as e:
        if isinstance(e, KeyboardInterrupt):
            what = "Stopped"
        elif isinstance(e, Signalled):
            what = "Stopped by %s" % signal.Signals(e.args[0]).name
        else:
            what = "Couldn't write %s (%s)" % (shown, errno_name(e))
        if landed(dfd, made):
            raise Refused("%s, but the file is written: %s. Run this with --check-only to check it." % (what, shown)) \
                from None
        raise Refused("%s. %s%s" % (what, NOTHING, " The old campus.json is as it was." if a.replace else "")) \
            from None
    finally:
        if not tmp_gone:                # a refusal, a failure or a stop: the temporary file goes
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            except OSError as e:
                print("Couldn't remove the temporary file %s (%s); it holds the password, so remove it by hand."
                      % (tmp, errno_name(e)), file=sys.stderr)
    return verdict, had


def landed(dfd, made):
    """True if campus.json is now the file this run made."""
    st = existing(dfd, NAME) if made else None
    return st is not None and (st.st_dev, st.st_ino) == made


def report(dfd, a, url, target, verdict, had):
    """The SET line, then --check for a new file (with --replace the check ran before the rename, giving verdict)."""
    try:
        st = os.stat(NAME, dir_fd=dfd, follow_symlinks=False)
        print("SET %s (mode %o, owner %s, %s)" % (target, stat.S_IMODE(st.st_mode), user_name(st.st_uid),
                                                   "replaced the old file" if had else "new file"))
        if not a.check:
            return 0
        if verdict is None:
            try:
                verdict = check_sign_in(target, url)
            except KeyboardInterrupt:
                print("\nStopped the check. The file is written; --check-only checks it again.", file=sys.stderr)
                return 3
        if verdict == PASSWORD_OK:
            print("The file stays: the Matrix server took the password. The Matrix account still has to be linked "
                  "to a campus student (campus Settings).")
        elif verdict == NO:
            print("The file stays as written; run this again with --replace --check to change it.")
        return 0 if verdict == YES else 3
    except KeyboardInterrupt:
        print("\nStopped, but the file is written: %s. Run this with --check-only to check it." % target,
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
