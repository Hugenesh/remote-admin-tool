# SafeDesk Remote Assist 2.0

A small, consent-based **remote support and full-control tool** for
Windows (LAN only), built in Python with a dark CustomTkinter GUI. It
behaves like a minimal AnyDesk: the helper PC connects, the client PC
gets a big popup, and **nothing is ever shared or controlled until a
human on the client presses ACCEPT** - and FULL CONTROL needs the big
red "ACCEPT FULL CONTROL" button, not just a plain accept.

## IMPORTANT - authorized use only

This tool is for legitimately supporting systems you own or administer
(your family PCs, your lab, your company endpoints **with the user's
knowledge**). FULL CONTROL mode is equivalent to handing someone your
keyboard and mouse - only grant it to people you trust, and only on
machines you have the right to touch. The consent popup and the audit
log are the core safety design: the person at the client PC always sees
who is asking and for what, can reject with one click, and every
decision (plus how many input events happened) is recorded. Do not
remove the consent gate, and do not use this on machines you have no
right to touch.

## What's new in 2.0

| Feature            | Details                                                              |
|--------------------|----------------------------------------------------------------------|
| FULL CONTROL       | Helper mouse + keyboard work on the client, always-live after ACCEPT |
| Clipboard sync     | Text clipboard syncs both directions during control sessions         |
| Remote power       | Lock / log off / restart buttons (client confirms log off & restart) |
| Admin elevation    | Client auto-asks UAC at start so control reaches admin windows       |
| Audit counters     | Input events are counted in the log, never recorded with content     |
| Safer downgrade    | No pynput on the client? Control request gracefully falls back to view |

## How the consent gate works (the "legit" part)

1. Client app sits in the "PROTECTED - waiting for requests" state.
   It shares nothing and accepts no input, ever, on its own.
2. Helper picks a session mode (VIEW ONLY / FULL CONTROL) and sends a
   connection request (display name + IP + fresh 9-digit session ID).
3. The client PC shows a topmost modal popup for 30 seconds with the
   helper's name + IP + session ID and up to three answers:
   - **ACCEPT FULL CONTROL** (only shown for control requests)
   - **VIEW ONLY** (downgrades the request, still fully logged)
   - **REJECT**
4. If the user does nothing, the request is AUTO-REJECTED at 0 s.
5. If accepted:
   - VIEW: live screen stream (~12 fps JPEG), helper is read-only.
   - FULL CONTROL: the same stream plus helper mouse/keyboard events,
     bidirectional text clipboard sync, and the remote power buttons.
6. Either side ends instantly: client "END SESSION" banner button,
   helper "Stop session", or closing either app. Stuck keys/buttons are
   released on the client when the session dies.
7. Every request, grant, denial, timeout, power command and disconnect
   goes to `logs/client_YYYYMMDD.log` / `logs/helper_YYYYMMDD.log`.

## Why the client wants to run as admin

Windows UIPI blocks simulated input from a non-elevated process into
elevated (admin) windows. Because FULL CONTROL is the point, the client
asks for the **standard Windows UAC prompt at every start** (you chose
"Auto-elevate at start"). Consequences:

- Accepting UAC -> control works everywhere, including Task Manager,
  elevated terminals, software installs.
- Declining UAC -> the app still runs; VIEW works, and control works
  for normal windows only. The GUI shows a yellow "NOT ELEVATED" badge
  and a "Relaunch as admin" button to retry.
- The UAC prompt is a normal Windows dialog - nothing is hidden and the
  person at the PC can always say no.

## Files

| File                | What it is                                             |
|---------------------|--------------------------------------------------------|
| `client.py`         | Run on the PC that gets viewed / controlled            |
| `helper.py`         | Run on the support/admin PC that views and controls    |
| `common.py`         | Wire protocol, discovery, audit logger, clipboard sync |
| `install_deps.bat`  | One-click dependency installer                         |
| `start_client.bat`  | One-click client launcher (auto-triggers UAC)          |
| `start_helper.bat`  | One-click helper launcher                              |
| `logs/`             | Audit logs are created here automatically              |

## Quick start (Windows)

1. Install Python 3.9+ on **both** PCs (python.org, tick
   "Add python.exe to PATH" during setup).
2. Copy this whole folder to both PCs.
3. On each PC, double-click `install_deps.bat` once.
   It installs: `customtkinter`, `mss`, `pillow`, `pynput`.
4. On the client PC: double-click `start_client.bat`.
   - Windows shows the **UAC prompt** -> accept for full-control reach.
   - Windows Firewall may ask -> allow on **Private networks**.
   - The window shows the device ID (e.g. `123 456 789`), its IP, and
     whether the session is ELEVATED.
5. On the helper PC: double-click `start_helper.bat`.
   - Pick the session mode (default **FULL CONTROL**).
   - Type the client's IP and press **CONNECT + REQUEST CONTROL**, or
     press **Scan LAN** and pick a discovered device.
6. The client accepts (full control / view only) on the popup.
7. While controlling: click once inside the remote screen area, then
   just work - clicks, typing and scrolling go to the client PC.
   Typing into the helper's own IP/name fields stays local (tk focus).

Both PCs must be on the same LAN / subnet (office or home network).

## Remote power commands (FULL CONTROL)

Helper sidebar buttons, active only during control sessions:

- **Lock remote screen** - locks the workstation instantly; the client
  user just logs back in, so no extra popup.
- **Log off remote user** - helper confirms, then the client shows a
  second confirmation popup before running `shutdown /l`.
- **Restart remote PC** - helper confirms, client confirms, then
  `shutdown /r /t 10` runs: the client user has a 10-second grace
  window and can abort locally with `shutdown /a`.

## Network ports

| Port | Protocol | Used for                       |
|------|----------|--------------------------------|
| 5555 | TCP      | Screen stream + control + input |
| 5556 | UDP      | LAN discovery ("Scan LAN")     |

If 5555 is taken, the client walks up to 5566 automatically; Scan LAN
still finds it, manual-IP connects default to 5555 first.

## Audit log contents

Each line is one event, e.g.:

```
[2026-10-05 14:22:31] [CLIENT] INCOMING_REQUEST helper=IT-Bob ip=192.168.1.7 session_id=123456789 requested=control
[2026-10-05 14:22:36] [CLIENT] CONTROL_GRANTED helper=IT-Bob ip=192.168.1.7
[2026-10-05 14:23:10] [CLIENT] CMD_REQUESTED action=restart
[2026-10-05 14:23:15] [CLIENT] CMD_EXECUTED action=restart
[2026-10-05 14:25:02] [CLIENT] SESSION_ENDED_BY_CLIENT
[2026-10-05 14:25:02] [CLIENT] SESSION_END helper=IT-Bob duration_sec=151.1 inputs=1841
```

Logs record decisions, commands and **how many** input events happened.
They deliberately never record which keys were pressed - the log proves
accountability without becoming a keylogger. This is your evidence
trail: "who controlled this PC, when, for how long, and what commands
were approved".

## Troubleshooting

- **UAC prompt appears every time I start the client** -> that is the
  auto-elevation doing its job. Decline it if you only need view mode.
- **Helper mouse works on normal windows but not admin windows** ->
  the client was started without elevation (yellow badge). Restart the
  client and accept the UAC prompt.
- **Ctrl+Alt+Del / UAC secure desktop / login screen can't be
  controlled** -> by Windows design, the secure desktop and the secure
  attention sequence are off-limits to injected input. Reboot/logoff
  buttons cover the common "fix it from scratch" cases instead.
- **Scan LAN finds nothing** -> both PCs on the same subnet? UDP 5556
  blocked by a guest/enterprise WiFi? Just connect by IP instead.
- **Connect fails** -> Windows Firewall on the client: allow inbound
  TCP 5555 for Python on private networks.
- **Client downgraded my control request to VIEW** -> pynput is missing
  on the client; run `install_deps.bat` there.
- **Keyboard does nothing** -> click once inside the remote screen
  area first; while the helper's IP/name fields have focus, typing
  stays local on purpose.
- **Clipboard doesn't sync** -> text only (files/images are ignored),
  and only during FULL CONTROL sessions.
- **"GUI libraries are missing"** -> run `install_deps.bat`.
- **Slow stream** -> lower the fps/quality constants at the top of
  `client.py` (StreamWorker defaults: 12 fps, JPEG quality 60).
- **Multi-monitor** -> the stream and control target the PRIMARY
  monitor, same as v1.

## Optional: build standalone .exe files

With PyInstaller (`python -m pip install pyinstaller`), run in this
folder:

```
pyinstaller --onefile --noconsole --uac-admin --name SafeDesk-Client client.py common.py
pyinstaller --onefile --noconsole --name SafeDesk-Helper helper.py common.py
```

`--uac-admin` makes the client exe request elevation at start (same
effect as the script's auto-UAC). The exe files appear in `dist/`.
Same ports, same logs, same consent popup.
