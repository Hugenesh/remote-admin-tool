"""
SafeDesk Remote Assist 2.0 - HELPER app (runs on the support/admin PC).

    * Pick the session mode: VIEW ONLY or FULL CONTROL (default).
    * Type the client's IP, or hit "Scan LAN" to list nearby SafeDesk IDs.
    * Sends a connection request; the CLIENT decides. You wait.
    * If the client accepts, the live screen view starts. In FULL CONTROL
      mode every mouse move / click / keystroke over the remote view is
      forwarded and applied on the client - always-live after ACCEPT.
    * FULL CONTROL also syncs the text clipboard both ways and enables
      the remote power buttons (lock / log off / restart).
    * Either side can end the session at any moment.
    * Every request/grant/denial is audit-logged to logs/helper_YYYYMMDD.log.

NOTE: this tool can only ever see (or control) a PC whose human owner
explicitly pressed ACCEPT. That consent gate is the whole point.
"""

import json
import queue
import socket
import threading
import time
import tkinter.messagebox as mb

import common as C

try:
    import customtkinter as ctk
    GUI_OK = True
except Exception:
    GUI_OK = False

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except Exception:
    PIL_OK = False

ACCENT_RED = "#E23B3B"
ACCENT_GREEN = "#2FA84F"
ACCENT_BLUE = "#2C6BED"
BG_DARK = "#16161A"
BG_PANEL = "#1E1E24"
TXT_DIM = "#9A9AA5"
VIEW_W = 780
VIEW_H = 500

BTN_MAP = {1: 1, 2: 2, 3: 3}          # tk button number -> wire button id


# ---------------------------------------------------------------------------
# Outgoing session (network side, runs in threads)
# ---------------------------------------------------------------------------

class ViewerSession:
    """Requests consent from a client, then receives the screen stream.

    In control mode this object ALSO sends input events, clipboard text
    and power commands - but the client gates all of that behind its own
    explicit ACCEPT, and may downgrade control -> view at any time.
    """

    def __init__(self, ip, port, helper_name, helper_id, mode, callbacks,
                 logger):
        self.ip = ip
        self.port = port
        self.helper_name = helper_name
        self.helper_id = helper_id
        self.requested = "control" if mode == "control" else "view"
        self.mode = self.requested        # may be downgraded by the client
        self.cb = callbacks               # dict of callables, see HelperGUI
        self.log = logger
        self.sock = None
        self.state = "idle"               # idle -> requesting -> viewing -> ended
        self.stop_flag = threading.Event()
        self.frame_q = queue.Queue(maxsize=2)
        self.client_name = None
        self.session_id = None
        self.inputs_sent = 0

    # -- control ------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="viewer-session").start()

    def stop(self):
        if self.sock:
            try:
                C.send_json(self.sock, {"type": "end"})
            except OSError:
                pass
        self.stop_flag.set()
        self._kill_sock()

    # -- outgoing control-mode messages (GUI thread is the only writer) ------

    def send_input(self, obj):
        """Send one mouse/keyboard event (control mode only)."""
        if self.mode != "control" or self.stop_flag.is_set() or not self.sock:
            return
        try:
            C.send_msg(self.sock, C.TYPE_INPUT,
                       json.dumps(obj).encode("utf-8"))
            self.inputs_sent += 1
        except OSError:
            pass

    def send_clip(self, text):
        if self.mode != "control" or not self.sock:
            return
        try:
            C.send_json(self.sock, {"type": "clip",
                                    "text": str(text)[: C.CLIP_MAX]})
        except OSError:
            pass

    def send_cmd(self, action):
        if self.mode != "control" or not self.sock:
            return
        try:
            C.send_json(self.sock, {"type": "cmd", "action": action})
            self.log.log("CMD_SENT", action=action, ip=self.ip)
        except OSError:
            pass

    # -- internals -----------------------------------------------------------

    def _kill_sock(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    def _run(self):
        started = time.time()
        try:
            self.sock = socket.create_connection((self.ip, self.port), timeout=6)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            self._post("on_denied", "connect_failed",
                       "Could not reach %s:%s (%s)" % (self.ip, self.port, exc))
            return

        self.session_id = C.new_session_id()
        try:
            self.sock.settimeout(C.CONSENT_TIMEOUT + 15)
            C.send_json(self.sock, {
                "type": "connect_request",
                "proto": C.PROTO_VERSION,
                "helper_name": self.helper_name,
                "helper_id": self.helper_id,
                "session_id": self.session_id,
                "requested": self.requested,
            })
            self._post("on_state", "requesting",
                       "Waiting for consent on %s (%s)..." % (
                           self.ip, self.requested))
            self.log.log("REQUEST_SENT", ip=self.ip, port=self.port,
                         session_id=self.session_id,
                         requested=self.requested)

            reply = C.recv_json(self.sock)
            if reply is None:
                self._post("on_denied", "no_reply",
                           "Client closed the connection without answering.")
                return
            if not reply.get("granted"):
                self.log.log("REQUEST_DENIED", ip=self.ip,
                             reason=reply.get("reason"))
                self._post("on_denied", reply.get("reason", "denied"),
                           "Client DENIED the request (%s)." % reply.get("reason", "?"))
                return

            self.mode = reply.get("mode") \
                if reply.get("mode") in ("view", "control") else "view"
            self.log.log("REQUEST_GRANTED", ip=self.ip, mode=self.mode)
            self.state = "viewing"

            if self.mode == "control":
                self._post("on_state", "controlling",
                           "CONTROLLING %s - input is LIVE" % self.ip)
            elif self.requested == "control":
                self._post("on_state", "viewing",
                           "Client downgraded to VIEW (pynput missing there).")
            else:
                self._post("on_state", "viewing", "Viewing %s" % self.ip)

            self.sock.settimeout(None)
            frames = 0
            while not self.stop_flag.is_set():
                msg = C.recv_msg(self.sock)
                if msg is None:
                    self._post("on_state", "ended", "Client disconnected.")
                    break
                mtype, payload = msg
                if mtype == C.TYPE_FRAME and PIL_OK:
                    frames += 1
                    try:
                        from io import BytesIO
                        img = Image.open(BytesIO(payload))
                        img.load()
                        self._push_frame(img)
                    except Exception:
                        self.log.log("FRAME_DECODE_ERROR")
                elif mtype == C.TYPE_JSON:
                    obj = json.loads(payload.decode("utf-8"))
                    if obj.get("type") in ("end", "bye"):
                        self._post("on_state", "ended",
                                   "Session ended by the client.")
                        break
                    if obj.get("type") == "clip":
                        self._post("on_clip",
                                   str(obj.get("text", ""))[: C.CLIP_MAX])
            self.log.log("VIEW_ENDED", ip=self.ip,
                         frames=frames, inputs=self.inputs_sent,
                         duration_sec=round(time.time() - started, 1))
        except socket.timeout:
            self._post("on_denied", "timeout",
                       "Client did not answer within %d s." % C.CONSENT_TIMEOUT)
        except OSError as exc:
            if not self.stop_flag.is_set():
                self._post("on_state", "ended", "Connection lost (%s)." % exc)
            else:
                self._post("on_state", "ended", "You stopped viewing.")
        finally:
            self.state = "ended"
            self.stop_flag.set()
            self._kill_sock()

    def _push_frame(self, img):
        try:
            self.frame_q.put_nowait(img)
        except queue.Full:
            try:
                self.frame_q.get_nowait()
                self.frame_q.put_nowait(img)
            except (queue.Empty, queue.Full):
                pass

    def _post(self, key, *args):
        fn = self.cb.get(key)
        if fn:
            try:
                self.cb[key](*args)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helper GUI
# ---------------------------------------------------------------------------

if GUI_OK:

    class HelperGUI(ctk.CTk):
        def __init__(self):
            super().__init__()
            ctk.set_appearance_mode("dark")
            self.title("SafeDesk Remote Assist - Helper")
            self.geometry("1120x740")
            self.minsize(980, 660)
            self.configure(fg_color=BG_DARK)

            self.device_id = C.make_device_id("helper")
            self.log = C.AuditLogger("helper", gui_cb=self._gui_log)
            self.session = None
            self.scan_map = {}            # ip -> announce info
            self._photo = None            # keep PhotoImage refs alive
            self._view_t0 = None
            self._fps_count = 0
            self._fps_val = 0.0
            self.clip_sync = None
            self._btn_held = set()
            self._last_move = 0.0
            self._disp_w = None           # displayed (scaled) frame size
            self._disp_h = None

            self._build_sidebar()
            self._build_main()
            self._tick()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.log.log("HELPER_READY", id=self.device_id)

        # -- layout -----------------------------------------------------------

        def _build_sidebar(self):
            sb = ctk.CTkFrame(self, fg_color=BG_PANEL, width=310, corner_radius=0)
            sb.pack(side="left", fill="y")
            sb.pack_propagate(False)

            ctk.CTkLabel(sb, text="SafeDesk", font=("Segoe UI", 24, "bold")
                         ).pack(anchor="w", padx=20, pady=(18, 0))
            ctk.CTkLabel(sb, text="Consent-based remote assist - LAN",
                         font=("Segoe UI", 11), text_color=TXT_DIM
                         ).pack(anchor="w", padx=20, pady=(0, 14))

            ctk.CTkLabel(sb, text="YOUR ID", font=("Segoe UI", 10),
                         text_color=TXT_DIM).pack(anchor="w", padx=20)
            ctk.CTkLabel(sb, text=C.format_id(self.device_id),
                         font=("Consolas", 17, "bold")).pack(anchor="w", padx=20)

            ctk.CTkLabel(sb, text="Your display name", font=("Segoe UI", 11),
                         text_color=TXT_DIM).pack(anchor="w", padx=20, pady=(14, 0))
            self.name_entry = ctk.CTkEntry(sb, height=30,
                                           font=("Segoe UI", 12))
            self.name_entry.insert(0, C.host_name())
            self.name_entry.pack(fill="x", padx=20, pady=(3, 10))

            ctk.CTkLabel(sb, text="Client IP address", font=("Segoe UI", 11),
                         text_color=TXT_DIM).pack(anchor="w", padx=20)
            self.ip_entry = ctk.CTkEntry(sb, height=34,
                                         placeholder_text="e.g. 192.168.1.50",
                                         font=("Consolas", 13))
            self.ip_entry.pack(fill="x", padx=20, pady=(3, 8))

            ctk.CTkLabel(sb, text="Session mode", font=("Segoe UI", 11),
                         text_color=TXT_DIM).pack(anchor="w", padx=20)
            self.mode_seg = ctk.CTkSegmentedButton(
                sb, values=["VIEW ONLY", "FULL CONTROL"],
                command=self._mode_changed)
            self.mode_seg.set("FULL CONTROL")
            self.mode_seg.pack(fill="x", padx=20, pady=(3, 8))

            self.connect_btn = ctk.CTkButton(
                sb, text="CONNECT + REQUEST CONTROL", height=42,
                fg_color=ACCENT_RED, hover_color="#B22D2D",
                font=("Segoe UI", 13, "bold"), command=self._connect_clicked)
            self.connect_btn.pack(fill="x", padx=20, pady=(2, 8))

            self.stop_btn = ctk.CTkButton(
                sb, text="Stop session", height=34,
                fg_color="#3A3A44", hover_color="#4A4A55",
                font=("Segoe UI", 12), command=self._stop_clicked,
                state="disabled")
            self.stop_btn.pack(fill="x", padx=20, pady=(0, 10))

            # ---- remote power (control mode only) ----
            ctk.CTkLabel(sb, text="REMOTE POWER (control sessions)",
                         font=("Segoe UI", 10), text_color=TXT_DIM
                         ).pack(anchor="w", padx=20)
            self.lock_btn = ctk.CTkButton(
                sb, text="Lock remote screen", height=30,
                fg_color="#3A3A44", hover_color="#4A4A55",
                font=("Segoe UI", 12), command=self._cmd_lock,
                state="disabled")
            self.lock_btn.pack(fill="x", padx=20, pady=(4, 3))
            self.logoff_btn = ctk.CTkButton(
                sb, text="Log off remote user", height=30,
                fg_color="#8A5A19", hover_color="#6E4713",
                font=("Segoe UI", 12), command=self._cmd_logoff,
                state="disabled")
            self.logoff_btn.pack(fill="x", padx=20, pady=(0, 3))
            self.restart_btn = ctk.CTkButton(
                sb, text="Restart remote PC", height=30,
                fg_color="#8A1F1F", hover_color="#6E1919",
                font=("Segoe UI", 12), command=self._cmd_restart,
                state="disabled")
            self.restart_btn.pack(fill="x", padx=20, pady=(0, 10))

            ctk.CTkLabel(sb, text="NEARBY CLIENTS (LAN scan)",
                         font=("Segoe UI", 10), text_color=TXT_DIM
                         ).pack(anchor="w", padx=20)
            self.scan_btn = ctk.CTkButton(
                sb, text="Scan LAN", height=30, fg_color="#2C6BED",
                hover_color="#2356BE", font=("Segoe UI", 12),
                command=self._scan_clicked)
            self.scan_btn.pack(fill="x", padx=20, pady=(4, 6))
            self.scan_list = ctk.CTkScrollableFrame(sb, height=150,
                                                    fg_color=BG_DARK)
            self.scan_list.pack(fill="x", padx=20, pady=(0, 10))
            ctk.CTkLabel(self.scan_list, text="No scan yet.",
                         font=("Segoe UI", 11), text_color=TXT_DIM).pack()

            ctk.CTkLabel(sb, text="ACTIVITY LOG", font=("Segoe UI", 10),
                         text_color=TXT_DIM).pack(anchor="w", padx=20)
            self.logbox = ctk.CTkTextbox(sb, fg_color=BG_DARK,
                                         text_color="#C9C9D1",
                                         font=("Consolas", 10))
            self.logbox.pack(fill="both", expand=True, padx=20, pady=(4, 16))
            self.logbox.configure(state="disabled")

        def _build_main(self):
            main = ctk.CTkFrame(self, fg_color=BG_DARK, corner_radius=0)
            main.pack(side="right", fill="both", expand=True)

            self.info_lbl = ctk.CTkLabel(
                main, text="No active session",
                font=("Segoe UI", 13), text_color=TXT_DIM)
            self.info_lbl.pack(pady=(12, 6))

            # Plain tk.Label on purpose: reliable mouse/keyboard event
            # bindings for the control-mode capture layer.
            import tkinter as tk
            self.view = tk.Label(
                main, text="REMOTE SCREEN\n\n(request consent from the client first)",
                font=("Segoe UI", 16), fg="#52525C", bg="#0E0E12", bd=0)
            self.view.pack(expand=True, fill="both", padx=16, pady=(0, 12))

            bar = ctk.CTkFrame(main, fg_color="transparent")
            bar.pack(fill="x", padx=16, pady=(0, 14))
            self.stat_lbl = ctk.CTkLabel(bar, text="idle",
                                         font=("Segoe UI", 12),
                                         text_color=TXT_DIM)
            self.stat_lbl.pack(side="left")
            self.dot = ctk.CTkLabel(bar, text="\u25cf", font=("Segoe UI", 16),
                                    text_color="#6A6A75")
            self.dot.pack(side="right")

        # -- actions ------------------------------------------------------------

        def _mode_changed(self, value):
            self.connect_btn.configure(
                text="CONNECT + REQUEST CONTROL"
                if value == "FULL CONTROL" else "CONNECT + REQUEST VIEW")

        def _current_mode(self):
            return "control" if self.mode_seg.get() == "FULL CONTROL" else "view"

        def _connect_clicked(self):
            ip = self.ip_entry.get().strip()
            if not ip:
                self._flash_info("Enter the client's IP first.", "#F5C542")
                return
            try:
                ip = socket.gethostbyname(ip)   # allow hostnames too
            except OSError:
                self._flash_info("Cannot resolve that address.", ACCENT_RED)
                return
            name = self.name_entry.get().strip() or C.host_name()
            self._begin_session(ip, self.scan_map.get(ip, {}).get("port",
                                                                C.DEFAULT_TCP_PORT),
                                name)

        def _begin_session(self, ip, port, name):
            self._stop_current("new request started")
            mode = self._current_mode()
            self.log.log("CONNECTING", ip=ip, port=port, mode=mode)
            self.session = ViewerSession(
                ip, port, name, self.device_id, mode,
                callbacks={
                    "on_state": self._on_state,
                    "on_denied": self._on_denied,
                    "on_clip": self._on_clip,
                },
                logger=self.log)
            self.clip_sync = C.ClipboardSync(
                self, lambda text: self.session.send_clip(text), self.log)
            self.session.start()
            self.connect_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.mode_seg.configure(state="disabled")
            self._set_dot("#F5C542")

        def _stop_clicked(self):
            self._stop_current("helper pressed stop")

        def _stop_current(self, why):
            self._teardown_input()
            if self.clip_sync:
                self.clip_sync.stop()
                self.clip_sync = None
            if self.session:
                self.session.stop()
                self.log.log("SESSION_STOPPED", reason=why)
            self.session = None
            self.connect_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.mode_seg.configure(state="normal")
            self._set_power_enabled(False)
            self._set_dot("#6A6A75")
            self._view_t0 = None
            self._disp_w = None
            self._disp_h = None
            self._btn_held.clear()
            self.info_lbl.configure(text="No active session",
                                    text_color=TXT_DIM)
            self.view.configure(
                image=None,
                text="REMOTE SCREEN\n\n(request consent from the client first)")
            self.stat_lbl.configure(text="idle")

        def _scan_clicked(self):
            self.scan_btn.configure(state="disabled", text="Scanning...")
            for w in self.scan_list.winfo_children():
                w.destroy()
            ctk.CTkLabel(self.scan_list, text="Broadcasting probe...",
                         font=("Segoe UI", 11), text_color=TXT_DIM).pack()

            def work():
                found = C.scan_lan()
                self.after(0, lambda: self._scan_done(found))
            threading.Thread(target=work, daemon=True).start()

        def _scan_done(self, found):
            self.scan_map = found
            for w in self.scan_list.winfo_children():
                w.destroy()
            if not found:
                ctk.CTkLabel(self.scan_list,
                             text="No SafeDesk clients found.\n"
                                  "Check both PCs are on the same network.",
                             font=("Segoe UI", 11),
                             text_color=TXT_DIM).pack()
            for ip, info in sorted(found.items()):
                row = ctk.CTkFrame(self.scan_list, fg_color=BG_PANEL,
                                   corner_radius=6)
                row.pack(fill="x", padx=4, pady=3)
                ctk.CTkLabel(row, text="%s  %s" % (
                    C.format_id(info.get("id", "?")),
                    str(info.get("name", "?"))[:18]),
                    font=("Consolas", 12), justify="left").pack(
                        side="left", padx=8, pady=4)
                ctk.CTkButton(row, text="Connect", width=70, height=24,
                              fg_color=ACCENT_RED, hover_color="#B22D2D",
                              font=("Segoe UI", 11, "bold"),
                              command=lambda i=ip: self._connect_scanned(i)
                              ).pack(side="right", padx=6, pady=4)
            self.log.log("SCAN_DONE", clients=len(found))
            self.scan_btn.configure(state="normal", text="Scan LAN")

        def _connect_scanned(self, ip):
            info = self.scan_map.get(ip, {})
            name = self.name_entry.get().strip() or C.host_name()
            self.ip_entry.delete(0, "end")
            self.ip_entry.insert(0, ip)
            self._begin_session(ip, info.get("port", C.DEFAULT_TCP_PORT), name)

        # -- remote power buttons ------------------------------------------------

        def _set_power_enabled(self, on):
            for btn in (self.lock_btn, self.logoff_btn, self.restart_btn):
                btn.configure(state="normal" if on else "disabled")

        def _cmd_lock(self):
            if self.session:
                self.session.send_cmd("lock")

        def _cmd_logoff(self):
            if not self.session:
                return
            if mb.askyesno("SafeDesk", "Send LOG OFF to %s?\nThe remote user's "
                           "open programs will close." % self.session.ip):
                self.session.send_cmd("logoff")

        def _cmd_restart(self):
            if not self.session:
                return
            if mb.askyesno("SafeDesk", "Send RESTART to %s?\nThe client user "
                           "still gets a confirmation popup and can abort with "
                           "'shutdown /a'." % self.session.ip):
                self.session.send_cmd("restart")

        # -- input capture (control mode) -----------------------------------------

        def _input_active(self):
            return (self.session is not None
                    and self.session.mode == "control"
                    and self.session.state == "viewing"
                    and not self.session.stop_flag.is_set())

        def _setup_input(self):
            self.view.bind("<Motion>", self._on_mouse_move)
            for num in (1, 2, 3):
                self.view.bind("<ButtonPress-%d>" % num, self._on_button_press)
                self.view.bind("<ButtonRelease-%d>" % num, self._on_button_release)
            self.view.bind("<Button-4>", self._on_wheel_linux)
            self.view.bind("<Button-5>", self._on_wheel_linux)
            self.view.bind("<MouseWheel>", self._on_wheel)
            self.bind("<Key>", self._on_key_down)
            self.bind("<KeyRelease>", self._on_key_up)
            self._btn_held = set()
            self._last_move = 0.0
            self.log.log("INPUT_CAPTURE_ON")

        def _teardown_input(self):
            try:
                for num in (1, 2, 3):
                    self.view.unbind("<ButtonPress-%d>" % num)
                    self.view.unbind("<ButtonRelease-%d>" % num)
                self.view.unbind("<Motion>")
                self.view.unbind("<Button-4>")
                self.view.unbind("<Button-5>")
                self.view.unbind("<MouseWheel>")
                self.unbind("<Key>")
                self.unbind("<KeyRelease>")
            except Exception:
                pass
            self._btn_held = set()

        def _norm_xy(self, ev):
            """Map a view-widget event to normalized 0..1 stream coords."""
            vw = self.view.winfo_width() or VIEW_W
            vh = self.view.winfo_height() or VIEW_H
            dw = self._disp_w or vw
            dh = self._disp_h or vh
            ox = (vw - dw) / 2.0
            oy = (vh - dh) / 2.0
            nx = (ev.x - ox) / float(dw)
            ny = (ev.y - oy) / float(dh)
            return min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny))

        def _on_mouse_move(self, ev):
            if not self._input_active():
                return
            now = time.time()
            # throttle free motion to ~60/s; never throttle drags
            if not self._btn_held and (now - self._last_move) < 0.015:
                return
            self._last_move = now
            x, y = self._norm_xy(ev)
            self.session.send_input({"t": "mm", "x": x, "y": y})

        def _on_button_press(self, ev):
            if not self._input_active():
                return
            b = BTN_MAP.get(ev.num)
            if not b:
                return
            self._btn_held.add(b)
            x, y = self._norm_xy(ev)
            self.session.send_input({"t": "md", "b": b, "x": x, "y": y})

        def _on_button_release(self, ev):
            if not self._input_active():
                return
            b = BTN_MAP.get(ev.num)
            if not b:
                return
            self._btn_held.discard(b)
            x, y = self._norm_xy(ev)
            self.session.send_input({"t": "mu", "b": b, "x": x, "y": y})

        def _on_wheel(self, ev):
            if not self._input_active():
                return
            dy = int(round(ev.delta / 120.0))    # Windows: 120 per notch
            if dy:
                self.session.send_input({"t": "mw", "dy": dy})

        def _on_wheel_linux(self, ev):
            if not self._input_active():
                return
            self.session.send_input(
                {"t": "mw", "dy": 1 if ev.num == 4 else -1})

        def _on_key_down(self, ev):
            if not self._input_active():
                return
            wire = C.serialize_key(ev.keysym, ev.char)
            if wire:
                self.session.send_input({"t": "kd", "k": wire})

        def _on_key_up(self, ev):
            if not self._input_active():
                return
            wire = C.serialize_key(ev.keysym, ev.char)
            if wire:
                self.session.send_input({"t": "ku", "k": wire})

        # -- clipboard ------------------------------------------------------------

        def _on_clip(self, text):
            def ui():
                if self.clip_sync:
                    self.clip_sync.apply_incoming(text)
            self.after(0, ui)

        # -- session callbacks (arrive on session thread) -----------------------

        def _on_state(self, state, message):
            def ui():
                self.stat_lbl.configure(text=message)
                if state == "requesting":
                    self._set_dot("#F5C542")
                elif state in ("viewing", "controlling"):
                    control = state == "controlling"
                    self._set_dot(ACCENT_RED if control else ACCENT_GREEN)
                    self._view_t0 = time.time()
                    self.info_lbl.configure(
                        text="%s - %s (session %s)" % (
                            "LIVE CONTROL" if control else "LIVE VIEW",
                            self.session.ip if self.session else "?",
                            C.format_id(self.session.session_id)
                            if self.session else "?"),
                        text_color=ACCENT_RED if control else ACCENT_GREEN)
                    if control:
                        self._setup_input()
                        self._set_power_enabled(True)
                        if self.clip_sync:
                            self.clip_sync.start()
                        self.log.log("CONTROL_ACTIVE", ip=self.session.ip)
                    else:
                        self._set_power_enabled(False)
                elif state == "ended":
                    self._stop_current("session ended")
            self.after(0, ui)

        def _on_denied(self, reason, message):
            sess = self.session
            def ui():
                self._flash_info(message, ACCENT_RED)
                self._set_dot(ACCENT_RED)
                def reset_if_still_same():
                    if self.session is sess:
                        self._stop_current("denied/reset")
                self.after(2500, reset_if_still_same)
            self.after(0, ui)

        # -- frame pump + stats (runs on the GUI thread) --------------------------

        def _tick(self):
            # drain queue, keep newest frame
            img = None
            if self.session is not None:
                try:
                    while True:
                        img = self.session.frame_q.get_nowait()
                except queue.Empty:
                    pass
            if img is not None and PIL_OK:
                try:
                    disp = self._fit(img, VIEW_W, VIEW_H)
                    self._disp_w, self._disp_h = disp.width, disp.height
                    self._photo = ImageTk.PhotoImage(disp)
                    self.view.configure(image=self._photo, text="")
                    self._fps_count += 1
                except Exception:
                    pass
            now = time.time()
            if self._view_t0 and now >= self._view_t0 + 1.0:
                dur = int(now - self._view_t0)
                verb = "controlling" if (self.session and
                                         self.session.mode == "control") \
                    else "viewing"
                self.stat_lbl.configure(
                    text="%s  %02d:%02d  -  %d fps" % (
                        verb, dur // 60, dur % 60, self._fps_val))
            self.after(50, self._tick)

        def _fit(self, img, w, h):
            scale = min(w / img.width, h / img.height, 1.0)
            if scale < 1.0:
                img = img.resize(
                    (max(1, int(img.width * scale)),
                     max(1, int(img.height * scale))),
                    getattr(Image, "Resampling", Image).BILINEAR)
            return img

        # -- misc ---------------------------------------------------------------

        def _flash_info(self, text, color):
            self.info_lbl.configure(text=text, text_color=color)

        def _set_dot(self, color):
            self.dot.configure(text_color=color)

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

        def _fps_loop(self):
            self._fps_val = self._fps_count
            self._fps_count = 0
            self.after(1000, self._fps_loop)

        def _on_close(self):
            if self.clip_sync:
                self.clip_sync.stop()
            if self.session:
                self.session.stop()
            self.destroy()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not GUI_OK:
        print("[SafeDesk] GUI libraries are missing.")
        print("[SafeDesk] Run install_deps.bat, or:")
        print("           python -m pip install customtkinter pillow")
        return 1
    app = HelperGUI()
    app._fps_loop()
    print("[SafeDesk] Helper running. Enter the client's IP or scan the LAN.")
    print("[SafeDesk] FULL CONTROL only works after the client ACCEPTS it.")
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
