"""
SafeDesk Remote Assist 2.0 - shared protocol and utilities.

DESIGN PRINCIPLE (the "legit" part):
    Nothing on a client PC is ever shared or controllable until a human
    on that PC explicitly accepts that specific session request - and
    FULL CONTROL sessions require accepting the bigger "ACCEPT FULL
    CONTROL" button, not just view.

WIRE FORMAT
    Every message on the TCP socket is framed as:
        [4-byte big-endian payload length][1-byte message type][payload]
    The length field counts ONLY the payload bytes.

    Message types:
        0x01 = JSON control message   (requests, grants, denials, end,
                                       clipboard text, power commands)
        0x02 = JPEG screen frame      (client -> helper only)
        0x03 = input event            (helper -> client, control mode
                                       only: mouse / keyboard, JSON)

LAN DISCOVERY
    Client answers UDP broadcasts on port 5556 so the helper can list
    nearby "device IDs" instead of typing IP addresses (AnyDesk-style,
    but LAN-scoped).

SESSION LOGS
    Every consent decision, grant, denial, timeout, command and
    disconnect is appended to logs/<role>_<date>.log next to the
    scripts. Input events are COUNTED, never recorded with content.
"""

import json
import os
import random
import socket
import struct
import threading
import time

APP_NAME = "SafeDesk Remote Assist"
APP_VERSION = "2.0"
PROTO_VERSION = 2

TYPE_JSON = 0x01
TYPE_FRAME = 0x02
TYPE_INPUT = 0x03

DEFAULT_TCP_PORT = 5555
PORT_RANGE = 12                    # client tries 5555..5566 if 5555 is taken
DISCOVERY_PORT = 5556
DISCOVERY_PROBE = b"SAFEDESK-DISCOVER-V1"

CONSENT_TIMEOUT = 30               # seconds the approval popup stays open
MAX_PAYLOAD = 25 * 1024 * 1024     # sanity cap on a single frame (25 MB)
CLIP_MAX = 2 * 1024 * 1024         # sanity cap on clipboard text (2 MB)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def send_msg(sock, mtype, payload):
    """Send one framed message (type + payload)."""
    sock.sendall(struct.pack("!IB", len(payload), mtype) + payload)


def send_json(sock, obj):
    send_msg(sock, TYPE_JSON, json.dumps(obj).encode("utf-8"))


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None            # peer closed cleanly
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    """Return (mtype, payload) or None on clean EOF. Raises OSError on resets."""
    header = _recv_exact(sock, 5)
    if header is None:
        return None
    length, mtype = struct.unpack("!IB", header)
    if length > MAX_PAYLOAD:
        raise OSError("payload too large: %d bytes" % length)
    if length == 0:
        return mtype, b""
    payload = _recv_exact(sock, length)
    if payload is None:
        return None
    return mtype, payload


def recv_json(sock):
    msg = recv_msg(sock)
    if msg is None:
        return None
    mtype, payload = msg
    if mtype != TYPE_JSON:
        raise OSError("expected JSON control message, got type 0x%02x" % mtype)
    return json.loads(payload.decode("utf-8"))


# ---------------------------------------------------------------------------
# Audit logging (both sides record every decision)
# ---------------------------------------------------------------------------

class AuditLogger:
    """Thread-safe, append-only audit trail.

    Writes to logs/<role>_<YYYYMMDD>.log and optionally mirrors each line
    into the GUI via a callback. Records consent decisions, grants,
    denials, timeouts and session lifecycle events - useful evidence if
    anyone ever asks "who connected to this machine and when".
    """

    def __init__(self, role, base_dir=None, gui_cb=None):
        self.role = role
        self.gui_cb = gui_cb
        self._lock = threading.Lock()
        base = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.log_dir = os.path.join(base, "logs")
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except OSError:
            self.log_dir = base
        self.path = os.path.join(
            self.log_dir, "%s_%s.log" % (role, time.strftime("%Y%m%d")))
        self.log("APP_START", app=APP_NAME, version=APP_VERSION, pid=os.getpid())

    def log(self, event, **fields):
        line = "[%s] [%s] %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), self.role.upper(), event)
        if fields:
            line += " " + " ".join("%s=%s" % (k, v) for k, v in fields.items())
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        if self.gui_cb:
            try:
                self.gui_cb(line)
            except Exception:
                pass
        return line


# ---------------------------------------------------------------------------
# Windows elevation (client auto-elevates so helper input also reaches
# admin windows - Windows UIPI blocks injected input from non-elevated
# processes into elevated windows)
# ---------------------------------------------------------------------------

def is_windows():
    return os.name == "nt"


def is_windows_admin():
    """True if this process runs with an elevated (admin) token on Windows."""
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(script_path=None):
    """Re-launch this app via the standard Windows UAC prompt.

    Returns True if the elevation attempt was STARTED (the caller should
    exit), False when it was declined / unavailable. The user always sees
    the normal UAC dialog - nothing is hidden here.
    """
    import ctypes
    import sys
    if not is_windows():
        return False
    if getattr(sys, "frozen", False):          # PyInstaller exe
        target, params = sys.executable, ""
    else:
        script = script_path or os.path.abspath(sys.argv[0])
        target, params = sys.executable, '"%s"' % script
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", target, params, None, 1)   # SW_SHOWNORMAL
    return rc > 32


# ---------------------------------------------------------------------------
# Small host/network helpers
# ---------------------------------------------------------------------------

def get_lan_ip():
    """Best-effort primary LAN address. No packet actually leaves the NIC."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def host_name():
    try:
        return socket.gethostname() or "unknown-pc"
    except OSError:
        return "unknown-pc"


def new_session_id():
    """Random 9-digit per-session number shown in the approval popup."""
    return str(random.randint(100000000, 999999999))


def make_device_id(role):
    """Stable 9-digit device id, persisted next to the scripts."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, ".%s_device_id" % role)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            val = fh.read().strip()
        if val.isdigit() and len(val) == 9:
            return val
    except OSError:
        pass
    val = new_session_id()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(val)
    except OSError:
        pass
    return val


def format_id(digits):
    """123456789 -> 123 456 789 (AnyDesk-style grouping)."""
    d = str(digits)
    if len(d) == 9:
        return "%s %s %s" % (d[0:3], d[3:6], d[6:9])
    return d


# ---------------------------------------------------------------------------
# Keyboard serialization: tk key event  <->  pynput-friendly wire string
#   "Key.enter"        -> client does getattr(pynput Key, name)
#   "c:<char>"         -> client does KeyCode.from_char(char)
#   None               -> key cannot be reproduced remotely, skip it
# ---------------------------------------------------------------------------

SPECIAL_KEYS = {
    "Return": "enter", "KP_Enter": "enter",
    "Escape": "esc",
    "BackSpace": "backspace",
    "Tab": "tab",
    "space": "space",
    "Prior": "page_up", "KP_Prior": "page_up",
    "Next": "page_down", "KP_Next": "page_down",
    "Home": "home", "End": "end",
    "Left": "left", "Right": "right", "Up": "up", "Down": "down",
    "KP_Left": "left", "KP_Right": "right", "KP_Up": "up", "KP_Down": "down",
    "Insert": "insert", "Delete": "delete",
    "Caps_Lock": "caps_lock", "Num_Lock": "num_lock",
    "Scroll_Lock": "scroll_lock",
    "Pause": "pause", "Print": "print_screen", "Menu": "menu",
    "Shift_L": "shift_l", "Shift_R": "shift_r",
    "Control_L": "ctrl_l", "Control_R": "ctrl_r",
    "Alt_L": "alt_l", "Alt_R": "alt_r",
    "Meta_L": "cmd", "Meta_R": "cmd",
    "Super_L": "cmd", "Super_R": "cmd",
    "KP_Multiply": "multiply", "KP_Add": "add",
    "KP_Subtract": "subtract", "KP_Divide": "divide",
    "KP_Decimal": "decimal",
}
for _i in range(1, 25):
    SPECIAL_KEYS["F%d" % _i] = "f%d" % _i


def serialize_key(keysym, char):
    """Map a tk key event (keysym + char) to the wire string."""
    if keysym in SPECIAL_KEYS:
        return "Key." + SPECIAL_KEYS[keysym]
    if char and len(char) == 1 and char.isprintable():
        return "c:" + char
    if len(keysym) == 1 and keysym.isprintable():
        return "c:" + keysym
    return None


# ---------------------------------------------------------------------------
# Clipboard sync (text only) - runs inside the tk main loop on both sides
# ---------------------------------------------------------------------------

class ClipboardSync:
    """Bidirectional TEXT clipboard sync for one live session.

    Polls the local clipboard via widget.clipboard_get(), pushes changes
    through send_fn(text) and applies incoming text on the GUI thread.
    Non-text clipboard content raises TclError in clipboard_get and is
    silently ignored. A small echo-suppression memory prevents the same
    text from bouncing back and forth forever.
    """

    def __init__(self, widget, send_fn, logger, interval=1.0):
        self.widget = widget            # any tk widget (root is fine)
        self.send_fn = send_fn          # callable(text)
        self.log = logger
        self.interval = interval
        self.active = False
        self._last_seen = None

    def start(self):
        if self.active:
            return
        self.active = True
        self._poll()

    def stop(self):
        self.active = False

    def _poll(self):
        if not self.active:
            return
        try:
            text = self.widget.clipboard_get()
        except Exception:
            text = None                 # non-text or empty clipboard
        if text and text != self._last_seen and len(text) <= CLIP_MAX:
            self._last_seen = text
            try:
                self.send_fn(text)
            except Exception:
                pass
        try:
            self.widget.after(int(self.interval * 1000), self._poll)
        except Exception:
            self.active = False

    def apply_incoming(self, text):
        """Set the local clipboard (call on the GUI thread)."""
        try:
            self._last_seen = text      # don't echo it back
            self.widget.clipboard_clear()
            self.widget.clipboard_append(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# LAN discovery: client responder + helper scanner
# ---------------------------------------------------------------------------

def start_discovery_responder(info_cb, logger=None):
    """Client side. Answers UDP probes with this device's announce info.

    info_cb: callable returning a dict with at least
             {"id": ..., "name": ..., "port": <tcp port>}
    """
    def loop():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError as exc:
            if logger:
                logger.log("DISCOVERY_BIND_FAIL", port=DISCOVERY_PORT, error=str(exc))
            return
        if logger:
            logger.log("DISCOVERY_LISTENING", port=DISCOVERY_PORT)
        while True:
            try:
                data, addr = s.recvfrom(1024)
                if data.strip() != DISCOVERY_PROBE:
                    continue
                reply = dict(info_cb())
                reply["type"] = "announce"
                # Reply straight back to the probe's source address.
                s.sendto(json.dumps(reply).encode("utf-8"), addr)
                if logger:
                    logger.log("DISCOVERY_ANNOUNCED", to=addr[0])
            except OSError:
                continue

    t = threading.Thread(target=loop, daemon=True, name="discovery-responder")
    t.start()
    return t


def scan_lan(timeout=2.5):
    """Helper side. Broadcast a probe and collect announce replies.

    Returns {ip: info_dict} of every SafeDesk client that answered.
    """
    results = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.25)
    targets = ["255.255.255.255"]
    ip = get_lan_ip()
    if ip and ip != "127.0.0.1":
        targets.append(".".join(ip.split(".")[:3]) + ".255")
    for target in targets:
        try:
            s.sendto(DISCOVERY_PROBE, (target, DISCOVERY_PORT))
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(4096)
            info = json.loads(data.decode("utf-8"))
            if info.get("type") == "announce":
                results[addr[0]] = info
        except (OSError, ValueError):
            continue
    s.close()
    return results
