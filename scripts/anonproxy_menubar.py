#!/usr/bin/env python3
"""Anonproxy menubar app (macOS, rumps).

Point-and-go control of the anonymizing proxy:

* pick an engagement profile (per-test context: scope, detectors, model, port)
* Start/Stop/Restart without touching a terminal
* ＋ New engagement… — asks for a name + scope terms, fills the rest with sane
  defaults, saves it, and starts it
* Copy client env / Open audit dashboard / Verify coverage /
  Export & archive vault (close-out ritual)

Run:  python3 scripts/anonproxy_menubar.py
Needs: pip install rumps   (or pip install 'anonproxy[menu]')

UI updates are marshalled through a small queue drained by the status timer,
so background threads never touch Cocoa directly.
"""
from __future__ import annotations

import atexit
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    import rumps
except ImportError:  # pragma: no cover
    print("the menubar app needs rumps:\n"
          "  pip install rumps          (or: pip install 'anonproxy[menu]')")
    raise SystemExit(1)

from anonproxy.config import Settings                    # noqa: E402
from anonproxy.profiles import (Profile, ProfileStore,    # noqa: E402
                                close_engagement, client_env_lines,
                                copy_to_clipboard, open_in_editor)

LOG_DIR = Path.home() / ".anonproxy" / "logs"

# menubar glyphs: ● ours running, ◐ an external proxy owns the port, ○ stopped
ICON_ON, ICON_EXT, ICON_OFF = "🛡️●", "🛡️◐", "🛡️○"


def _port_open(port: int) -> bool:
    """Instant liveness probe: connect refused == nothing there."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


class AnonproxyBar(rumps.App):
    def __init__(self):
        super().__init__("🛡️", quit_button="Quit")
        self.store = ProfileStore()
        if not self.store.list():
            self.store.save(Profile(name="default"))
        recent = self.store.most_recent()
        self.selected = recent.name
        self.proc: subprocess.Popen | None = None
        self.running_profile: str | None = None
        self._ui: list[tuple] = []           # (kind, kwargs) drained on the main loop
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        atexit.register(self._shutdown_child)
        self._rebuild()
        self.timer = rumps.Timer(lambda _t: self._tick(), 3)
        self.timer.start()
        rumps.notification("Anonproxy menubar", "",
                           f"selected engagement: {self.selected}")

    # ------------------------------------------------------------------ state
    def _child_cmd(self, name: str) -> list[str]:
        return [sys.executable, "-m", "anonproxy", "up", name]

    def _is_running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _external_port(self) -> int | None:
        """A proxy on the selected profile's port that we didn't spawn."""
        prof = self.store.get(self.selected)
        port = prof.port if prof else 8099
        return port if _port_open(port) and not self._is_running() else None

    def _start(self, name: str) -> None:
        prof = self.store.get(name)
        if prof is None:
            self._ui.append(("alert", {"message": f"profile {name!r} vanished"}))
            return
        ext = self._external_port()
        if ext is not None:
            self._ui.append(("alert", {
                "title": "Port already in use",
                "message": f"127.0.0.1:{prof.port} is serving something else.\n"
                           f"Stop it or change the port in the profile."}))
            return
        logf = open(LOG_DIR / f"menubar-{prof.name}.log", "ab")  # noqa: SIM115
        self.proc = subprocess.Popen(
            self._child_cmd(prof.name), cwd=str(REPO),
            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.running_profile = prof.name
        self.store.touch(prof.name)
        self._rebuild()
        rumps.notification("Anonproxy started",
                           f"engagement {prof.name}", f"http://127.0.0.1:{prof.port}")

    def _stop(self, notify: bool = True) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.running_profile = None
        self._rebuild()
        if notify:
            rumps.notification("Anonproxy stopped", "", "")

    def _shutdown_child(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _tick(self) -> None:
        # child died underneath us (bad scope file, port clash, crash)
        if self.proc and not self._is_running():
            code = self.proc.returncode
            name = self.running_profile or "?"
            self.proc = None
            self.running_profile = None
            self._rebuild()
            self._ui.append(("notify", {
                "title": "Anonproxy exited",
                "subtitle": f"engagement {name}",
                "message": f"exit code {code} — log: "
                           f"{LOG_DIR / f'menubar-{name}.log'}"}))
        while self._ui:
            kind, kw = self._ui.pop(0)
            if kind == "alert":
                rumps.alert(kw.get("title", "Anonproxy"), kw.get("message", ""))
            elif kind == "notify":
                rumps.notification(kw.get("title", ""), kw.get("subtitle", ""),
                                   kw.get("message", ""))
        self.title = (ICON_ON if self._is_running()
                      else ICON_EXT if self._external_port() else ICON_OFF)
    # ------------------------------------------------------------------ menu
    def _rebuild(self) -> None:
        state = (f"● running: {self.running_profile}" if self._is_running()
                 else f"○ stopped — selected: {self.selected}")
        items: list = [rumps.MenuItem(state, callback=None), None,
                       rumps.MenuItem(f"{'Stop' if self._is_running() else 'Start'}"
                                      f"  ({self.selected})",
                                      callback="start_stop"),
                       rumps.MenuItem("Engagements")]
        engs = items[-1]
        for pr in self.store.list():
            item = rumps.MenuItem(pr.name, callback="select_profile")
            item.state = pr.name == self.selected
            engs.add(item)
        engs.add(None)
        engs.add(rumps.MenuItem("＋ New engagement…", callback="new_engagement"))
        engs.add(rumps.MenuItem("Edit profiles folder…", callback="edit_profiles"))
        items += [None,
                  rumps.MenuItem("Copy client env", callback="copy_env"),
                  rumps.MenuItem("Open audit dashboard", callback="open_audit"),
                  rumps.MenuItem("Verify coverage", callback="verify_coverage"),
                  rumps.MenuItem("Export & archive vault…",
                                 callback="export_archive")]
        self.menu.clear()
        for entry in items:
            self.menu.add(entry)

    # ------------------------------------------------------------------ actions
    def start_stop(self, sender) -> None:
        if self._is_running():
            self._stop()
        else:
            self._start(self.selected)

    def select_profile(self, sender) -> None:
        name = str(sender.title)
        if name == self.selected:
            return
        self.selected = name
        if self._is_running():
            # point-and-go between tests: swap engagement, restart cleanly
            old = self.running_profile
            self._stop(notify=False)
            self._start(name)
            rumps.notification("Switched engagement",
                               f"{old} → {name}", "vaults stay isolated")
        else:
            self._rebuild()

    def new_engagement(self, sender) -> None:
        w = rumps.Window(
            message="Profile/engagement id — becomes the isolated vault name.",
            title="New engagement (1/2)", default_text="", ok="Next", cancel="Cancel",
            dimensions=(340, 30))
        resp = w.run()
        if not resp.clicked or not resp.text.strip():
            return
        name = resp.text.strip()
        if self.store.exists(name):
            self._ui.append(("alert", {
                "title": "Exists",
                "message": f"profile '{name}' already exists — selected it"}))
            self.selected = self.store.get(name).name
            self._rebuild()
            return
        w2 = rumps.Window(
            message="Scope terms, comma-separated (client domains, hostnames,\n"
                    "org names). Leave blank — you can edit later.",
            title=f"New engagement: {name} (2/2)", default_text="",
            ok="Create & start", cancel="Cancel", dimensions=(340, 60))
        resp2 = w2.run()
        if not resp2.clicked:
            return
        prof = Profile(name=name, notes="created from menubar")
        prof.scope_terms = [t.strip() for t in resp2.text.split(",") if t.strip()]
        self.store.save(prof)
        self.selected = prof.name
        self._rebuild()
        self._start(prof.name)

    def edit_profiles(self, sender) -> None:
        open_in_editor(self.store.dir)

    def copy_env(self, sender) -> None:
        prof = self.store.get(self.selected)
        text = "\n".join(client_env_lines(prof))
        ok = copy_to_clipboard(text + "\n")
        rumps.notification("Client env",
                           "copied to clipboard" if ok else "clipboard unavailable",
                           f"ANTHROPIC_BASE_URL=http://127.0.0.1:{prof.port}")

    def open_audit(self, sender) -> None:
        prof = self.store.get(self.selected)
        s = Settings()
        url = f"http://127.0.0.1:{prof.port}/audit"
        if s.engine_api_token:
            from urllib.parse import quote
            url += "#token=" + quote(s.engine_api_token, safe="")
        webbrowser.open(url)

    def verify_coverage(self, sender) -> None:
        rumps.notification("Anonproxy verify", "running",
                           "checking coverage (regex floor + backends)…")

        def bg():
            try:
                p = subprocess.run(
                    [sys.executable, "-m", "anonproxy", "verify"],
                    cwd=str(REPO), capture_output=True, text=True, timeout=600)
                lines = [l.strip() for l in (p.stdout + p.stderr).splitlines()
                         if l.strip()]
                summary = " | ".join(lines[-2:])[:220] or f"exit {p.returncode}"
                self._ui.append(("notify", {
                    "title": f"Verify {'PASS ✓' if p.returncode == 0 else 'FAIL ✗'}",
                    "subtitle": "", "message": summary}))
            except Exception as e:  # timeout, interpreter issues…
                self._ui.append(("notify", {"title": "Verify error",
                                            "subtitle": "", "message": str(e)[:200]}))
        threading.Thread(target=bg, daemon=True).start()

    def export_archive(self, sender) -> None:
        prof = self.store.get(self.selected)
        s = Settings()
        prof.apply(s)
        try:
            res = close_engagement(s)
        except Exception as e:
            self._ui.append(("alert", {"title": "Close-out failed",
                                       "message": str(e)[:300]}))
            return
        msg = (f"{res['count']} mapping(s) exported\n"
               f"{res['csv']}\n"
               f"vault removed: {'yes' if res['vault_removed'] else 'no'}")
        if not res["count"]:
            msg += ("\n(no mappings found — ephemeral session? run close-out "
                    "while the proxy still holds them)")
        self._ui.append(("alert", {"title": f"Audited & closed: {prof.name}",
                                   "message": msg}))


if __name__ == "__main__":
    AnonproxyBar().run()
