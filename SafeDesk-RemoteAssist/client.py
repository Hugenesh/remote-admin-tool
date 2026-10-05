"""
SafeDesk Remote Assist 2.0 - CLIENT app (runs on the PC that gets assisted).

SAFETY MODEL
    * The client listens passively. It never initiates anything.
    * Every incoming request triggers a modal popup showing WHO wants in
      and WHAT they asked for: VIEW ONLY or FULL CONTROL.
    * The popup offers three answers: ACCEPT FULL CONTROL / VIEW ONLY /
      REJECT. Doing nothing auto-REJECTS after 30 seconds.
    * FULL CONTROL = helper mouse + keyboard + text clipboard + remote
      power commands. LOCK applies immediately (reversible locally);
      LOG OFF / RESTART require a second on-screen confirmation here.
    * The app auto-elevates via the normal UAC prompt at start so helper
      input also reaches admin windows (Windows UIPI blocks injected
      input from a non-elevated process into elevated windows).
    * "END SESSION" banner button (or closing the app) kills everything
      instantly; keys/buttons stuck mid-press are released on session end.
    * Everything is audit-logged to logs/client_YYYYMMDD.log. Input
      events are COUNTED, never recorded with their content.
"""

import io
import json
import os
import socket
import subprocess
import threading
import time
import tkinter.messagebox

import common as C

try:
    import customtkinter as ctk
    GUI_OK = True
except Exception:                       # headless / missing tk
    GUI_OK = False


ACCENT_RED = "#E23B3B"
ACCENT_GREEN = "#2FA84F"
ACCENT_BLUE = "#2C6BED"
BG_DARK = "#16161A"
BG_PANEL = "#1E1E24"
TXT_DIM = "#9A9AA5"


# ---------------------------------------------------------------------------
# Screen streamer
# ---------------------------------------------------------------------------

class StreamWorker(threading.Thread):
    """Captures this PC's primary screen and pushes JPEG frames out.

    All socket writes go through the provided send_fn so that stream
    frames, clipboard messages and replies never interleave mid-frame.
    """

    def __init__(self, send_fn, stop_event, logger, fps=12.0, quality=60, max_width=1600):
        super().__init__(daemon=True, name="screen-streamer")
        self.send_fn = send_fn
        self.stop_event = stop_event
        self.log = logger
        self.fps = fps
        self.quality = quality
        self.max_width = max_width
        self.frames_sent = 0
        self.error = None

    def run(self):
        try:
            import mss
            from PIL import Image
        except ImportError as exc:
            self.error = "missing dependency: %s" % exc
            self.log.log("STREAM_ERROR", detail=self.error)
            return

        resample = getattr(Image, "Resampling", Image).BILINEAR
        interval = 1.0 / self.fps
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                self.log.log("STREAM_CAPTURE_START",
                             size="%dx%d" % (monitor["width"], monitor["height"]),
                             fps=self.fps, quality=self.quality)
                while not self.stop_event.is_set():
                    t0 = time.time()
                    raw = sct.grab(monitor)
                    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    if img.width > self.max_width:
                        h = max(1, int(img.height * self.max_width / img.width))
                        img = img.resize((self.max_width, h), resample)
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=self.quality)
                    self.send_fn(buf.getvalue())
                    self.frames_sent += 1
                    delay = interval - (time.time() - t0)
                    if delay > 0:
                        self.stop_event.wait(delay)
        except (OSError, RuntimeError) as exc:
            self.error = str(exc)
            self.log.log("STREAM_ERROR", detail=self.error[:200])
        finally:
            self.log.log("STREAM_FRAMES_SENT", count=self.frames_sent)


# ---------------------------------------------------------------------------
# Input applier (control mode): applies helper events via pynput
# ---------------------------------------------------------------------------

class InputApplier:
    """Applies incoming helper input events on this PC.

    Coordinates arrive NORMALIZED (0..1 relative to the streamed primary
    monitor) and are mapped onto the local primary screen size, so
    different resolutions between helper and client work fine.
    """

    def __init__(self, logger):
        self.log = logger
        self.ok = False
        self.error = None
        self.count = 0
        self.sw = 1920
        self.sh = 1080
        self._held_keys = set()
        try:
            from pynput.keyboard import Controller as KCtrl, Key, KeyCode
            from pynput.mouse import Button, Controller as MCtrl
            self._Key = Key
            self._KeyCode = KeyCode
            self._kb = KCtrl()
            self._mouse = MCtrl()
            self._btn = {1: Button.left, 2: Button.middle, 3: Button.right}
            self.ok = True
        except Exception as exc:
            self.error = str(exc)
            return
        if C.is_windows():
            try:
                import ctypes
                user32 = ctypes.windll.user32
                self.sw = user32.GetSystemMetrics(0) or self.sw   # SM_CXSCREEN
                self.sh = user32.GetSystemMetrics(1) or self.sh   # SM_CYSCREEN
            except Exception:
                pass
        self.log.log("INPUT_APPLIER_READY", screen="%dx%d" % (self.sw, self.sh))

    def apply(self, obj):
        """Apply one input event dict. Never raises."""
        if not self.ok or not isinstance(obj, dict):
            return
        kind = obj.get("t")
        try:
            if kind == "mm":
                self._move(obj)
            elif kind in ("md", "mu"):
                self._move(obj)
                btn = self._btn.get(int(obj.get("b", 0)))
                if btn is not None:
                    if kind == "md":
                        self._mouse.press(btn)
                    else:
                        self._mouse.release(btn)
            elif kind == "mw":
                dx = int(obj.get("dx", 0))
                dy = int(obj.get("dy", 0))
                if dx or dy:
                    self._mouse.scroll(dx, dy)
            elif kind in ("kd", "ku"):
                key = self._key(obj.get("k"))
                if key is not None:
                    if kind == "kd":
                        self._kb.press(key)
                        self._held_keys.add(key)
                    else:
                        self._kb.release(key)
                        self._held_keys.discard(key)
            else:
                return
            self.count += 1
        except Exception:
            pass

    # -- internals ----------------------------------------------------------

    def _move(self, obj):
        x = obj.get("x")
        y = obj.get("y")
        if x is None or y is None:
            return
        self._mouse.position = (
            int(float(x) * self.sw), int(float(y) * self.sh))

    def _key(self, wire):
        if not wire:
            return None
        try:
            if wire.startswith("Key."):
                return getattr(self._Key, wire[4:])
            if wire.startswith("c:"):
                return self._KeyCode.from_char(wire[2:])
        except Exception:
            return None
        return None

    def reset(self):
        """Release anything stuck mid-press when a session dies."""
        if not self.ok:
            return
        try:
            for btn in self._btn.values():
                self._mouse.release(btn)
        except Exception:
            pass
        for key in list(self._held_keys):
            try:
                self._kb.release(key)
            except Exception:
                pass
        self._held_keys.clear()
        for name in ("ctrl_l", "ctrl_r", "shift_l", "shift_r",
                     "alt_l", "alt_r"):
            try:
                self._kb.release(getattr(self._Key, name))
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Consent-gated TCP service
# ---------------------------------------------------------------------------

class ConsentService:
    """Accepts helper connections. EVERY session requires consent_cb to
    return a grant from the local user. No grant -> nothing happens.
    """

    CMD_LABELS = {
        "lock": "LOCK this PC",
        "logoff": "LOG OFF the current Windows user",
        "restart": "RESTART this PC (10 s grace - 'shutdown /a' aborts)",
    }

    def __init__(self, consent_cb, logger, gui=None, port=C.DEFAULT_TCP_PORT):
        self.consent_cb = consent_cb      # cb(request:dict, peer_ip:str) -> dict
        self.log = logger
        self.gui = gui
        self.port = port
        self.active = False               # one session at a time
        self.session_mode = None          # "view" | "control" while live
        self.clip = None                  # ClipboardSync while live
        self.applier = None               # InputApplier in control mode
        self.stop_stream = threading.Event()
        self._worker = None
        self._conn = None
        self._wlock = threading.Lock()    # one writer at a time on the socket
        self._sock = None
        self._bind()

    def _bind(self):
        last_err = None
        for offset in range(C.PORT_RANGE):
            port = C.DEFAULT_TCP_PORT + offset
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("", port))
                s.listen(4)
                self._sock = s
                self.port = port
                self.log.log("LISTENING", port=port)
                return
            except OSError as exc:
                last_err = exc
                s.close()
        raise OSError("could not bind any port 5555-5566: %s" % last_err)

    # -- thread-safe socket writes -------------------------------------------

    def _send_frame(self, data):
        with self._wlock:
            C.send_msg(self._conn, C.TYPE_FRAME, data)

    def send_json_safe(self, obj):
        """Thread-safe control-JSON send (clipboard sync etc.)."""
        if self._conn is None:
            return
        with self._wlock:
            try:
                C.send_msg(self._conn, C.TYPE_JSON,
                           json.dumps(obj).encode("utf-8"))
            except OSError:
                pass

    def serve_forever(self):
        while True:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                return
            if self.active:
                try:
                    C.send_json(conn, {"type": "denied", "reason": "busy"})
                except OSError:
                    pass
                conn.close()
                self.log.log("REQUEST_REJECTED_BUSY", ip=addr[0])
                continue
            threading.Thread(target=self._handle, args=(conn, addr),
                             daemon=True, name="session-handler").start()

    # -- one incoming helper connection ------------------------------------

    def _handle(self, conn, addr):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.active = True
        self.stop_stream.clear()
        self.session_mode = None
        self.applier = None
        self._conn = conn
        started = time.time()
        helper = "?"
        granted = False
        mode = "view"
        try:
            conn.settimeout(C.CONSENT_TIMEOUT + 15)
            req = C.recv_json(conn)
            if not isinstance(req, dict) or req.get("type") != "connect_request":
                C.send_json(conn, {"type": "denied", "reason": "bad_request"})
                self.log.log("REQUEST_MALFORMED", ip=addr[0])
                return
            if req.get("proto") != C.PROTO_VERSION:
                C.send_json(conn, {"type": "denied", "reason": "protocol_mismatch"})
                self.log.log("REQUEST_PROTOCOL_MISMATCH",
                             ip=addr[0], proto=req.get("proto"))
                return

            helper = str(req.get("helper_name", "?"))[:40]
            wanted = "control" if req.get("requested") == "control" else "view"
            self.log.log("INCOMING_REQUEST",
                         helper=helper, ip=addr[0],
                         session_id=req.get("session_id"),
                         requested=wanted)

            # ---- THE GATE: block until the human decides (max 30 s) ----
            decision = self.consent_cb(req, addr[0]) or {}
            granted = bool(decision.get("granted"))
            if granted:
                mode = decision.get("mode") \
                    if decision.get("mode") in ("view", "control") else wanted
                if mode == "control":
                    self.applier = InputApplier(self.log)
                    if not self.applier.ok:
                        self.log.log("CONTROL_UNAVAILABLE",
                                     detail=str(self.applier.error)[:150])
                        mode = "view"                 # graceful downgrade
                        decision["note"] = "control_unavailable"
            decision["mode"] = mode
            C.send_json(conn, decision)
            if not granted:
                self.log.log("REQUEST_DENIED",
                             helper=helper, ip=addr[0],
                             reason=decision.get("reason", "unknown"))
                return

            self.session_mode = mode
            if mode == "control":
                self.log.log("CONTROL_GRANTED", helper=helper, ip=addr[0])
            else:
                self.log.log("VIEW_GRANTED", helper=helper, ip=addr[0])
            self.log.log("REQUEST_GRANTED", helper=helper, ip=addr[0])

            # ---- stream phase ----
            self._worker = StreamWorker(self._send_frame, self.stop_stream,
                                        self.log)
            self._worker.start()
            self.log.log("STREAM_START", helper=helper, mode=mode)

            conn.settimeout(None)
            while True:
                msg = C.recv_msg(conn)
                if msg is None:
                    self.log.log("HELPER_DISCONNECTED", helper=helper)
                    break
                mtype, payload = msg
                if mtype == C.TYPE_JSON:
                    obj = json.loads(payload.decode("utf-8"))
                    kind = obj.get("type")
                    if kind == "end":
                        self.log.log("SESSION_ENDED_BY_HELPER", helper=helper)
                        break
                    if kind == "clip" and mode == "control" and self.clip:
                        text = str(obj.get("text", ""))[: C.CLIP_MAX]
                        self._to_gui(
                            lambda t=text: self.clip.apply_incoming(t))
                    elif kind == "cmd" and mode == "control":
                        self._handle_cmd(obj)
                    # any other control JSON is ignored on this side
                elif mtype == C.TYPE_INPUT:
                    if mode == "control" and self.applier:
                        try:
                            self.applier.apply(
                                json.loads(payload.decode("utf-8")))
                        except ValueError:
                            pass
        except (OSError, ValueError) as exc:
            if granted:
                self.log.log("CONNECTION_ERROR", detail=str(exc)[:200])
        finally:
            self.stop_stream.set()
            if self._worker:
                self._worker.join(timeout=3)
            if self.applier:
                self.applier.reset()
            if self.clip:
                self.clip.stop()
                self.clip = None
            try:
                conn.close()
            except OSError:
                pass
            self.session_mode = None
            self.active = False
            self._conn = None
            self.log.log("SESSION_END",
                         helper=helper,
                         duration_sec=round(time.time() - started, 1),
                         inputs=self.applier.count if self.applier else 0)

    # -- remote power commands (control mode only, client-side confirm) -----

    def _handle_cmd(self, obj):
        action = str(obj.get("action", ""))[:16]
        if action not in ("lock", "logoff", "restart"):
            self.log.log("CMD_UNKNOWN", action=action)
            return
        self.log.log("CMD_REQUESTED", action=action)
        self._to_gui(lambda: self._confirm_cmd(action))

    def _confirm_cmd(self, action):
        """Runs on the GUI thread. LOCK applies immediately (the local
        user just logs back in); destructive actions ask first."""
        if action == "lock":
            ok = True
        else:
            ok = tkinter.messagebox.askyesno(
                "SafeDesk - remote command",
                "The helper requests to %s.\n\nAllow?" % self.CMD_LABELS[action])
        if not ok:
            self.log.log("CMD_DECLINED", action=action)
            return
        self.log.log("CMD_EXECUTED", action=action)
        threading.Thread(target=self._exec_cmd, args=(action,),
                         daemon=True, name="cmd-exec").start()

    @staticmethod
    def _exec_cmd(action):
        try:
            if action == "lock" and C.is_windows():
                import ctypes
                ctypes.windll.user32.LockWorkStation()
            elif action == "restart":
                subprocess.Popen(["shutdown", "/r", "/t", "10"])
            elif action == "logoff":
                subprocess.Popen(["shutdown", "/l"])
        except OSError:
            pass

    # -- helpers -------------------------------------------------------------

    def _to_gui(self, fn):
        if self.gui:
            try:
                self.gui.after(0, fn)
            except RuntimeError:
                pass

    # -- user pressed "End session" in the client GUI -----------------------

    def end_session(self):
        self.log.log("SESSION_ENDED_BY_CLIENT")
        self.stop_stream.set()
        if self._conn:
            try:
                self._conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Consent popup (modal, topmost, 30 s countdown, 3 answers)
# ---------------------------------------------------------------------------

if GUI_OK:

    class ConsentPopup(ctk.CTkToplevel):
        """The approval dialog. Nothing streams/controls until accepted."""

        def __init__(self, master, req, peer_ip, on_done):
            super().__init__(master)
            self.configure(fg_color=BG_PANEL)
            self.title("SafeDesk - Connection Request")
            self.resizable(False, False)
            self.attributes("-topmost", True)
            self.on_done = on_done
            self.remaining = C.CONSENT_TIMEOUT
            self._answered = False

            sid = C.format_id(req.get("session_id", "000000000"))
            name = str(req.get("helper_name", "Unknown"))[:40]
            want_control = str(req.get("requested", "view")) == "control"

            if want_control:
                self.geometry("470x520")
                sub = "wants FULL CONTROL of this PC\n" \
                      "(mouse + keyboard + clipboard + power commands)"
                head_color = ACCENT_RED
            else:
                self.geometry("470x460")
                sub = "wants to VIEW this screen"
                head_color = "#F5C542"

            head = ctk.CTkFrame(self, fg_color=BG_DARK, corner_radius=0)
            head.pack(fill="x")
            ctk.CTkLabel(head, text="CONNECTION REQUEST",
                         font=("Segoe UI", 15, "bold"),
                         text_color=head_color).pack(pady=(14, 2))
            ctk.CTkLabel(head, text="%s %s" % (name, sub),
                         font=("Segoe UI", 12),
                         text_color=TXT_DIM).pack(pady=(0, 12))

            body = ctk.CTkFrame(self, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=24, pady=10)

            ctk.CTkLabel(body, text="From IP", font=("Segoe UI", 11),
                         text_color=TXT_DIM).pack()
            ctk.CTkLabel(body, text=peer_ip,
                         font=("Consolas", 15, "bold")).pack(pady=(0, 6))

            ctk.CTkLabel(body, text="Session ID - quote this when verifying",
                         font=("Segoe UI", 11),
                         text_color=TXT_DIM).pack()
            ctk.CTkLabel(body, text=sid, font=("Consolas", 22, "bold"),
                         text_color="#F5C542").pack(pady=(2, 10))

            self.countdown_lbl = ctk.CTkLabel(
                body, text="Auto-REJECT in %d s if you do nothing" % self.remaining,
                font=("Segoe UI", 11), text_color=TXT_DIM)
            self.countdown_lbl.pack(pady=(0, 10))

            if want_control:
                ctk.CTkButton(body, text="ACCEPT FULL CONTROL",
                              width=220, height=46,
                              fg_color=ACCENT_RED, hover_color="#B22D2D",
                              font=("Segoe UI", 14, "bold"),
                              command=lambda: self._answer(
                                  True, "control", "user")).pack(pady=4)
                ctk.CTkButton(body, text="VIEW ONLY",
                              width=220, height=38,
                              fg_color=ACCENT_BLUE, hover_color="#2356BE",
                              font=("Segoe UI", 13, "bold"),
                              command=lambda: self._answer(
                                  True, "view", "user_view")).pack(pady=4)
            else:
                ctk.CTkButton(body, text="ACCEPT",
                              width=220, height=46,
                              fg_color=ACCENT_GREEN, hover_color="#25853D",
                              font=("Segoe UI", 14, "bold"),
                              command=lambda: self._answer(
                                  True, "view", "user")).pack(pady=4)
            ctk.CTkButton(body, text="REJECT",
                          width=220, height=38,
                          fg_color="#3A3A44", hover_color="#B22D2D",
                          font=("Segoe UI", 13, "bold"),
                          command=lambda: self._answer(
                              False, "view", "user")).pack(pady=4)

            self.protocol("WM_DELETE_WINDOW",
                          lambda: self._answer(False, "view", "popup_closed"))
            self._countdown()
            self.after(200, self._raise)

        def _raise(self):
            try:
                self.lift()
                self.focus_force()
                self.grab_set()
            except Exception:
                pass

        def _countdown(self):
            if self._answered:
                return
            if self.remaining <= 0:
                self._answer(False, "view", "timeout")
                return
            self.countdown_lbl.configure(
                text="Auto-REJECT in %d s if you do nothing" % self.remaining)
            self.remaining -= 1
            self.after(1000, self._countdown)

        def _answer(self, granted, mode, reason):
            if self._answered:
                return
            self._answered = True
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()
            self.on_done({"granted": granted, "mode": mode, "reason": reason})


# ---------------------------------------------------------------------------
# Client GUI
# ---------------------------------------------------------------------------

if GUI_OK:

    class ClientApp(ctk.CTk):
        def __init__(self):
            super().__init__()
            ctk.set_appearance_mode("dark")
            self.title("SafeDesk Remote Assist - Client")
            self.geometry("470x700")
            self.resizable(False, False)
            self.configure(fg_color=BG_DARK)

            self.device_id = C.make_device_id("client")
            self.log = C.AuditLogger("client", gui_cb=self._gui_log)
            self.service = None
            self.clip = None

            # ---- header ----
            head = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
            head.pack(fill="x")
            ctk.CTkLabel(head, text="SafeDesk Remote Assist",
                         font=("Segoe UI", 19, "bold")).pack(pady=(14, 0))
            self.status_lbl = ctk.CTkLabel(head, text="PROTECTED - waiting for requests",
                                           font=("Segoe UI", 12, "bold"),
                                           text_color=ACCENT_GREEN)
            self.status_lbl.pack(pady=(2, 2))
            elevated = C.is_windows_admin()
            ctk.CTkLabel(
                head,
                text="ELEVATED - control reaches admin windows"
                if elevated else "NOT ELEVATED - control limited to normal windows",
                font=("Segoe UI", 11),
                text_color="#7CFF9B" if elevated else "#F5C542").pack()
            if not elevated and C.is_windows():
                ctk.CTkButton(head, text="Relaunch as admin (UAC)",
                              width=160, height=24,
                              fg_color="#3A3A44", hover_color="#4A4A55",
                              font=("Segoe UI", 11),
                              command=self._elevate).pack(pady=(4, 0))
            ctk.CTkLabel(head, text=" ", font=("Segoe UI", 4)).pack(pady=(0, 6))

            # ---- this device card ----
            card = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=10)
            card.pack(fill="x", padx=16, pady=(14, 8))
            ctk.CTkLabel(card, text="THIS DEVICE", font=("Segoe UI", 11),
                         text_color=TXT_DIM).pack(pady=(12, 4))
            ctk.CTkLabel(card, text=C.format_id(self.device_id),
                         font=("Consolas", 26, "bold")).pack()
            ctk.CTkButton(card, text="Copy my ID", width=110, height=26,
                          fg_color="#3A3A44", hover_color="#4A4A55",
                          font=("Segoe UI", 11),
                          command=self._copy_id).pack(pady=(4, 2))
            self.addr_lbl = ctk.CTkLabel(
                card, text="%s  -  %s" % (C.host_name(), C.get_lan_ip()),
                font=("Segoe UI", 12), text_color=TXT_DIM)
            self.addr_lbl.pack(pady=(0, 12))

            # ---- sharing banner (hidden until live) ----
            self.share_frame = ctk.CTkFrame(self, fg_color="#5A1E1E",
                                            corner_radius=10)
            self.share_lbl = ctk.CTkLabel(self.share_frame,
                                          text="", font=("Segoe UI", 12, "bold"),
                                          text_color="#FFD7D7")
            self.share_lbl.pack(side="left", padx=14, pady=10)
            self._end_btn = ctk.CTkButton(self.share_frame, text="END SESSION",
                                          width=120, height=30,
                                          fg_color=ACCENT_RED,
                                          hover_color="#B22D2D",
                                          font=("Segoe UI", 11, "bold"),
                                          command=self._end_clicked)
            self._end_btn.pack(side="right", padx=14, pady=8)

            # ---- event log ----
            ctk.CTkLabel(self, text="SECURITY LOG (also saved to logs/)",
                         font=("Segoe UI", 11), text_color=TXT_DIM
                         ).pack(anchor="w", padx=20, pady=(10, 2))
            self.logbox = ctk.CTkTextbox(self, fg_color=BG_PANEL,
                                         text_color="#C9C9D1",
                                         font=("Consolas", 11), height=220)
            self.logbox.pack(fill="both", expand=True, padx=16, pady=(0, 8))
            self.logbox.configure(state="disabled")

            ctk.CTkLabel(
                self, wraplength=430, justify="left",
                text="Nothing is shared until you press ACCEPT on a request. "
                     "FULL CONTROL additionally gives the helper your mouse, "
                     "keyboard, clipboard and power commands. Every decision "
                     "is recorded in the audit log.",
                font=("Segoe UI", 11), text_color=TXT_DIM).pack(pady=(0, 12))

            self._start_services()
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # -- services -------------------------------------------------------

        def _start_services(self):
            self.service = ConsentService(self._consent_flow, self.log,
                                          gui=self)
            C.start_discovery_responder(
                lambda: {"id": self.device_id, "name": C.host_name(),
                         "port": self.service.port},
                logger=self.log)
            self.log.log("CLIENT_READY", id=self.device_id,
                         ip=C.get_lan_ip(), port=self.service.port,
                         elevated=C.is_windows_admin())
            threading.Thread(target=self.service.serve_forever,
                             daemon=True, name="tcp-listener").start()

        # -- elevation -------------------------------------------------------

        def _elevate(self):
            try:
                if C.relaunch_as_admin():
                    self._on_close()
                else:
                    self._set_status("UAC elevation was declined",
                                     ACCENT_RED)
            except Exception as exc:
                self._set_status("Elevation failed: %s" % str(exc)[:60],
                                 ACCENT_RED)

        # -- consent wiring (runs on the session-handler thread) ------------

        def _consent_flow(self, req, peer_ip):
            result = {}
            done = threading.Event()

            def ui():
                if not GUI_OK:
                    result.update({"granted": False, "reason": "no_gui"})
                    done.set()
                    return
                self._show_popup(req, peer_ip, result, done)

            self.after(0, ui)
            done.wait(C.CONSENT_TIMEOUT + 10)
            return result

        def _show_popup(self, req, peer_ip, result, done):
            def on_done(decision):
                result.update(decision)
                done.set()

            popup = ConsentPopup(self, req, peer_ip, on_done)
            self._set_status("REQUEST - %s (%s)" % (
                str(req.get("helper_name", "?"))[:24], peer_ip), "#F5C542")

            def watcher():
                if done.is_set():
                    if result.get("granted"):
                        control = result.get("mode") == "control"
                        if control:
                            self._set_status("SHARING FULL CONTROL", ACCENT_RED)
                            self.share_lbl.configure(
                                text="LIVE CONTROL - %s (%s) has mouse + "
                                     "keyboard + clipboard" % (
                                         str(req.get("helper_name", "?"))[:20],
                                         peer_ip))
                        else:
                            self._set_status("SHARING SCREEN", ACCENT_RED)
                            self.share_lbl.configure(
                                text="LIVE - %s (%s) is viewing this screen" % (
                                    str(req.get("helper_name", "?"))[:24],
                                    peer_ip))
                        self.share_frame.pack(fill="x", padx=16, pady=8)
                        self._start_clipboard(control)
                        self._poll_session_end()
                    else:
                        self._set_status("PROTECTED - waiting for requests",
                                         ACCENT_GREEN)
                else:
                    self.after(400, watcher)
            self.after(400, watcher)

        # -- clipboard sync ---------------------------------------------------

        def _start_clipboard(self, enabled):
            if not enabled or self.clip:
                return
            self.clip = C.ClipboardSync(
                self, lambda text: self.service.send_json_safe(
                    {"type": "clip", "text": text}),
                self.log)
            self.service.clip = self.clip
            self.clip.start()
            self.log.log("CLIPBOARD_SYNC_ON")

        # -- session lifecycle -------------------------------------------------

        def _poll_session_end(self):
            """Flip the banner back when the session ends."""
            if self.service and not self.service.active:
                if self.clip:
                    self.clip.stop()
                    self.clip = None
                    self.service.clip = None
                    self.log.log("CLIPBOARD_SYNC_OFF")
                self.share_frame.pack_forget()
                self._set_status("PROTECTED - waiting for requests", ACCENT_GREEN)
                return
            self.after(700, self._poll_session_end)

        # -- small GUI helpers ----------------------------------------------

        def _set_status(self, text, color):
            self.status_lbl.configure(text=text, text_color=color)

        def _gui_log(self, line):
            def append():
                try:
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", line + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                except Exception:
                    pass
            try:
                self.after(0, append)
            except RuntimeError:
                pass

        def _copy_id(self):
            try:
                self.clipboard_clear()
                self.clipboard_append(self.device_id)
                self._set_status("ID copied to clipboard", "#7FB2FF")
            except Exception:
                pass

        def _end_clicked(self):
            if self.service:
                self.service.end_session()

        def _on_close(self):
            if self.service and self.service.active:
                self.log.log("CLIENT_QUIT_DURING_SESSION")
                self.service.end_session()
            if self.clip:
                self.clip.stop()
                self.clip = None
            self.destroy()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not GUI_OK:
        print("[SafeDesk] GUI libraries are missing.")
        print("[SafeDesk] Run install_deps.bat, or:")
        print("           python -m pip install customtkinter pynput")
        return 1

    # Auto-elevation: control must reach admin windows too (UIPI), so ask
    # for the normal UAC prompt at every start. Declining is allowed - the
    # app then continues with view + limited control and says so clearly.
    if C.is_windows() and not C.is_windows_admin():
        print("[SafeDesk] Requesting administrator rights (Windows UAC)...")
        try:
            if C.relaunch_as_admin():
                return 0                  # elevated copy takes over
        except Exception:
            pass
        print("[SafeDesk] Elevation declined/failed - continuing WITHOUT admin.")
        print("[SafeDesk] Control will not reach admin windows this session.")

    app = ClientApp()
    print("[SafeDesk] Client running. Your ID: %s   IP: %s" % (
        C.format_id(app.device_id), C.get_lan_ip()))
    print("[SafeDesk] Nothing is shared or controlled until you press ACCEPT.")
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
