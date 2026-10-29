"""rigwatch server: samples every couple of seconds, keeps history, serves the dashboard.

Whole-PC accounting (every CPU figure is percent of the whole machine):

  your apps      cgroup / process counters inside WSL, plus their Windows programs
  rest of WSL    WSL busy time nobody in apps.toml claims (docker daemon, shells, ...)
  rest of PC     Windows busy time outside the WSL VM and outside your apps

Memory splits the same way, plus "WSL cache": what the VM holds on the Windows
side (vmmemWSL working set) beyond what Linux reports as in use.
"""
from __future__ import annotations

import base64
import fnmatch
import html
import json
import os
import sqlite3
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .collector import Config, WslSampler

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
TICK = 2.0
RING = 900                      # 30 min of 2 s samples
RETAIN_DAYS = 30

SEVERITY = {"good": 0, "info": 0, "warning": 1, "serious": 2, "critical": 3}


# ---------------------------------------------------------------- Windows sampler

class WindowsFeed(threading.Thread):
    """Keeps one PowerShell sampler alive and remembers its latest line."""

    def __init__(self, watch: list[str]):
        super().__init__(daemon=True, name="windows-feed")
        names = ",".join("'" + w.replace("'", "") + "'" for w in watch) or "''"
        src = (HERE / "winsampler.ps1").read_text().replace("__WATCH__", names)
        self.encoded = base64.b64encode(src.encode("utf-16-le")).decode()
        self.latest: dict | None = None
        self.latest_at = 0.0
        self.error = ""
        self.proc: subprocess.Popen | None = None

    def run(self) -> None:
        backoff = 2.0
        while True:
            started = time.monotonic()
            try:
                self.proc = subprocess.Popen(
                    [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", self.encoded],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
                for line in self.proc.stdout:
                    try:
                        self.latest = json.loads(line)
                        self.latest_at = time.monotonic()
                        self.error = ""
                    except ValueError:
                        continue
                self.proc.wait()
                self.error = f"sampler exited ({self.proc.returncode})"
            except OSError as e:
                self.error = f"cannot start PowerShell: {e}"
            backoff = 2.0 if time.monotonic() - started > 60 else min(backoff * 2, 60)
            time.sleep(backoff)

    def fresh(self) -> dict | None:
        return self.latest if self.latest and time.monotonic() - self.latest_at < 10 else None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


# ---------------------------------------------------------------- health

def _worst(issues: list[dict]) -> str:
    return max((i["level"] for i in issues), key=lambda l: SEVERITY[l], default="good")


def unit_part_state(u: dict | None) -> tuple[str, str]:
    """(level, text) for a systemd unit's show properties."""
    if not u:
        return "info", ""
    active, sub = u.get("ActiveState", ""), u.get("SubState", "")
    restarts = int(u.get("NRestarts", "0") or 0)
    if active == "failed":
        return "critical", "failed"
    if active in ("activating", "deactivating") or sub == "auto-restart":
        return "warning", sub or active
    if active == "inactive":
        return ("info", "idle until its timer") if u.get("Type") == "oneshot" else ("serious", "stopped")
    return ("warning" if restarts >= 3 else "good"), (f"running · restarted {restarts}×" if restarts else "running")


def build_snapshot(cfg: Config, wsl: dict, win: dict | None, sampler: WslSampler, win_error: str = "") -> dict:
    scale = wsl["ncpu"] / win["ncpu"] if win else 1.0
    win_procs = {p["name"]: p for p in (win or {}).get("procs", [])}
    nics = {n["name"]: n for n in (win or {}).get("net", [])}
    by_name = {c["name"]: c for c in sampler.containers}
    live_ctr = {p["label"] for g in wsl["groups"].values() for p in g["parts"] if p["kind"] == "ctr"}

    groups = []
    apps_wsl_cpu = apps_wsl_mem = apps_win_cpu = apps_win_mem = 0.0
    for slot, g in enumerate(cfg.groups, start=1):
        w = wsl["groups"][g.id]
        parts = []
        issues = []
        for p in w["parts"]:
            part = dict(p, cpu=p["cpu"] * scale, state="", level="good")
            if p["kind"] == "unit":
                part["level"], part["state"] = unit_part_state(sampler.units.get(p["label"]))
            elif p["kind"] == "sys":
                part["level"], part["state"] = unit_part_state(sampler.units.get("sys:" + p["label"]))
            elif p["kind"] == "ctr":
                c = by_name.get(p["label"], {})
                part["state"] = c.get("status", "running")
                part["level"] = {"unhealthy": "critical", "starting": "warning"}.get(c.get("health", ""), "good")
            parts.append(part)

        # configured units that have no cgroup right now (stopped, failed, timer-idle)
        present = {p["label"] for p in w["parts"]}
        for name, u in sampler.units.items():
            if name.startswith("sys:") or name in present:
                continue
            if any(fnmatch.fnmatchcase(name, pat) for pat in g.units):
                level, state = unit_part_state(u)
                parts.append({"key": f"unit:{name}", "label": name, "kind": "unit", "cpu": 0.0, "mem": 0,
                              "io": 0.0, "pids": 0, "state": state, "level": level})
        # containers docker lists for this group without a live cgroup
        for c in sampler.containers:
            if c["name"] in live_ctr:
                continue
            if not (any(fnmatch.fnmatchcase(c["name"], pat) for pat in g.containers)
                    or (c["project"] and c["project"] in g.projects)):
                continue
            if c["state"] == "running":
                level, state = "warning", "ghost: Docker says running, but it has no processes"
            elif c["state"] in ("exited", "dead"):
                level, state = "serious", c["status"]
            else:
                level, state = "info", c["status"]
            parts.append({"key": f"ctr:{c['name']}", "label": c["name"], "kind": "ctr", "cpu": 0.0, "mem": 0,
                          "io": 0.0, "pids": 0, "state": state, "level": level})

        win_cpu = win_mem = 0.0
        for name in g.windows:
            wp = win_procs.get(name)
            if wp:
                win_cpu += wp["cpu"]
                win_mem += wp["ws"]
                parts.append({"key": f"win:{name}", "label": f"{name}.exe", "kind": "win", "cpu": wp["cpu"],
                              "mem": wp["ws"], "io": 0.0, "pids": wp["n"], "state": "Windows", "level": "good"})
            elif win:
                parts.append({"key": f"win:{name}", "label": f"{name}.exe", "kind": "win", "cpu": 0.0, "mem": 0,
                              "io": 0.0, "pids": 0, "state": "not running on Windows", "level": "serious"})
        if g.id == "rigwatch" and win and win.get("self"):
            win_cpu += win["self"]["cpu"]
            win_mem += win["self"]["ws"]
            parts.append({"key": "win:sampler", "label": "powershell.exe (Windows sampler)", "kind": "win",
                          "cpu": win["self"]["cpu"], "mem": win["self"]["ws"], "io": 0.0, "pids": 1,
                          "state": "Windows", "level": "good"})

        nic = None
        for n in g.nics:
            if n in nics:
                nic = {"name": n, "rx": nics[n]["rx"], "tx": nics[n]["tx"]}

        for p in parts:
            if SEVERITY[p["level"]] > 0:
                issues.append({"level": p["level"], "text": f"{p['label']}: {p['state']}"})

        cpu = w["cpu"] * scale + win_cpu
        mem = w["mem"] + win_mem
        apps_wsl_cpu += w["cpu"] * scale
        apps_wsl_mem += w["mem"]
        apps_win_cpu += win_cpu
        apps_win_mem += win_mem
        timers = [t for t in sampler.timers if any(fnmatch.fnmatchcase(t["name"], p) for p in g.timers)]
        parts.sort(key=lambda x: (-x["cpu"], -x["mem"], x["label"]))
        groups.append({"id": g.id, "name": g.name, "blurb": g.blurb, "slot": slot, "cpu": cpu, "mem": mem,
                       "io": w["io"], "pids": w["pids"] + sum(p["pids"] for p in parts if p["kind"] == "win"),
                       "parts": parts, "issues": issues, "status": _worst(issues), "timers": timers, "nic": nic})

    wsl_cpu = wsl["cpu"] * scale
    cpu = {g["id"]: g["cpu"] for g in groups}
    cpu["rest_wsl"] = max(0.0, wsl_cpu - apps_wsl_cpu)
    mem = {g["id"]: g["mem"] for g in groups}
    mem["rest_wsl"] = max(0.0, wsl["mem_used"] - apps_wsl_mem)

    winout = None
    if win:
        vm = win_procs.get("vmmemWSL") or win_procs.get("vmmem") or {"cpu": 0.0, "ws": 0.0}
        cpu["rest_win"] = max(0.0, win["cpu"] - wsl_cpu - apps_win_cpu)
        mem["wsl_cache"] = max(0.0, vm["ws"] - wsl["mem_used"])
        mem["rest_win"] = max(0.0, win["mem_used"] - max(vm["ws"], wsl["mem_used"]) - apps_win_mem)
        mine = set(n for g in cfg.groups for n in g.windows) | {"vmmemWSL", "vmmem"}
        top = sorted((p for p in win["procs"] if p["name"] not in mine), key=lambda p: (-p["cpu"], -p["ws"]))
        winout = {
            "cpu": win["cpu"], "mem_used": win["mem_used"], "mem_total": win["mem_total"],
            "vmmem_ws": vm["ws"], "vmmem_cpu": vm["cpu"], "nproc": win["nproc"],
            "procs": top[:24], "gpu": win.get("gpu"), "vram_total": win.get("vram_total", 0),
            "net": [dict(n, app=next((g.name for g in cfg.groups if n["name"] in g.nics), "")) for n in win["net"]],
        }
        total_cpu, total_mem = win["cpu"], win["mem_total"]
    else:
        total_cpu, total_mem = wsl_cpu, wsl["mem_total"]

    return {
        "t": int(time.time() * 1000),
        "host": {"cpu_name": (win or {}).get("cpu_name", ""), "gpu_name": (win or {}).get("gpu_name", ""),
                 "ncpu": (win or wsl)["ncpu"], "mem_total": total_mem},
        "windows_ok": bool(win), "windows_error": "" if win else (win_error or "waiting for the Windows sampler"),
        "pc": {"cpu": cpu, "cpu_total": total_cpu, "mem": mem, "mem_total": total_mem,
               "apps_cpu": apps_wsl_cpu + apps_win_cpu, "apps_mem": apps_wsl_mem + apps_win_mem},
        "wsl": {"cpu": wsl_cpu, "mem_used": wsl["mem_used"], "mem_total": wsl["mem_total"],
                "swap_used": wsl["swap_used"], "net": wsl["net"]},
        "win": winout,
        "groups": groups,
        "machine": {"name": cfg.machine, "peers": [{"name": p.name, "url": p.url} for p in cfg.peers]},
    }


# ---------------------------------------------------------------- history

class History:
    """2 s samples in memory for the last 30 min; one row per series per minute in SQLite."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS minute (
            ts INTEGER NOT NULL, key TEXT NOT NULL, cpu REAL, cpu_max REAL, mem REAL,
            PRIMARY KEY (ts, key))""")
        self.lock = threading.Lock()
        self.ring: deque[dict] = deque(maxlen=RING)
        self.acc: dict[str, list[float]] = {}
        self.acc_min: int | None = None
        self.pruned = 0.0

    def add(self, snap: dict) -> None:
        t = snap["t"] // 1000
        point = {"t": snap["t"], "c": snap["pc"]["cpu"], "m": snap["pc"]["mem"]}
        gpu = (snap.get("win") or {}).get("gpu") or {}
        point["c"] = dict(point["c"], gpu=gpu.get("util", 0.0), total=snap["pc"]["cpu_total"])
        with self.lock:
            self.ring.append(point)
            minute = t // 60 * 60
            if self.acc_min is not None and minute != self.acc_min:
                self._flush()
            self.acc_min = minute
            for k, v in point["c"].items():
                a = self.acc.setdefault(k, [0.0, 0.0, 0.0, 0])      # cpu sum, cpu max, mem sum, n
                a[0] += v
                a[1] = max(a[1], v)
                a[2] += point["m"].get(k, 0.0)
                a[3] += 1
            for k, v in point["m"].items():
                if k not in point["c"]:
                    a = self.acc.setdefault(k, [0.0, 0.0, 0.0, 0])
                    a[2] += v
                    a[3] += 1

    def _flush(self) -> None:
        rows = [(self.acc_min, k, a[0] / a[3], a[1], a[2] / a[3]) for k, a in self.acc.items() if a[3]]
        self.db.executemany("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?)", rows)
        if time.time() - self.pruned > 3600:
            self.db.execute("DELETE FROM minute WHERE ts < ?", (int(time.time()) - RETAIN_DAYS * 86400,))
            self.pruned = time.time()
        self.db.commit()
        self.acc = {}

    RANGES = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

    def query(self, rng: str) -> dict:
        span = self.RANGES.get(rng, 900)
        with self.lock:
            if span <= RING * TICK:
                pts = list(self.ring)
                cutoff = (time.time() - span) * 1000
                pts = [p for p in pts if p["t"] >= cutoff]
                keys = sorted({k for p in pts for k in (*p["c"], *p["m"])})
                return {"range": rng, "step": TICK, "t": [p["t"] for p in pts],
                        "cpu": {k: [round(p["c"].get(k, 0.0), 3) for p in pts] for k in keys},
                        "mem": {k: [round(p["m"].get(k, 0.0)) for p in pts] for k in keys},
                        **self._stats(pts)}
            step = max(60, span // 360 // 60 * 60)
            since = int(time.time()) - span
            rows = self.db.execute(
                "SELECT ts / ? * ? AS b, key, AVG(cpu), MAX(cpu_max), AVG(mem) FROM minute "
                "WHERE ts >= ? GROUP BY b, key ORDER BY b", (step, step, since)).fetchall()
            peaks = self.db.execute(
                "SELECT key, AVG(cpu), MAX(cpu_max), AVG(mem), MAX(mem) FROM minute WHERE ts >= ? GROUP BY key",
                (since,)).fetchall()
        buckets = sorted({r[0] for r in rows})
        idx = {b: i for i, b in enumerate(buckets)}
        keys = sorted({r[1] for r in rows})
        cpu = {k: [0.0] * len(buckets) for k in keys}
        mem = {k: [0.0] * len(buckets) for k in keys}
        for b, k, c, _cmax, m in rows:
            cpu[k][idx[b]] = round(c or 0.0, 3)
            mem[k][idx[b]] = round(m or 0.0)
        return {"range": rng, "step": step, "t": [b * 1000 for b in buckets], "cpu": cpu, "mem": mem,
                "avg": {k: a for k, a, _, _, _ in peaks}, "peak": {k: p for k, _, p, _, _ in peaks},
                "mem_avg": {k: m for k, _, _, m, _ in peaks}, "mem_peak": {k: m for k, _, _, _, m in peaks}}

    @staticmethod
    def _stats(pts: list[dict]) -> dict:
        avg, peak, mavg, mpeak = {}, {}, {}, {}
        for p in pts:
            for k, v in p["c"].items():
                avg[k] = avg.get(k, 0.0) + v
                peak[k] = max(peak.get(k, 0.0), v)
            for k, v in p["m"].items():
                mavg[k] = mavg.get(k, 0.0) + v
                mpeak[k] = max(mpeak.get(k, 0.0), v)
        n = max(1, len(pts))
        return {"avg": {k: v / n for k, v in avg.items()}, "peak": peak,
                "mem_avg": {k: v / n for k, v in mavg.items()}, "mem_peak": mpeak}


# ---------------------------------------------------------------- the loop + HTTP

class Monitor:
    def __init__(self, cfg: Config, db: Path, windows: bool = True):
        self.cfg = cfg
        self.sampler = WslSampler(cfg)
        self.history = History(db)
        self.feed = None
        if windows and os.path.exists(POWERSHELL):
            self.feed = WindowsFeed(sorted({n for g in cfg.groups for n in g.windows}))
        self.latest: dict | None = None
        self.cond = threading.Condition()
        self.started = time.time()

    def start(self) -> None:
        if self.feed:
            self.feed.start()
        threading.Thread(target=self._loop, daemon=True, name="sampler").start()

    def _loop(self) -> None:
        nxt = time.monotonic()
        while True:
            try:
                wsl = self.sampler.sample()
                if wsl["ready"]:
                    win = self.feed.fresh() if self.feed else None
                    err = (self.feed.error if self.feed else "Windows sampling is off (--no-windows)")
                    snap = build_snapshot(self.cfg, wsl, win, self.sampler, err)
                    snap["uptime"] = time.time() - self.started
                    self.history.add(snap)
                    with self.cond:
                        self.latest = snap
                        self.cond.notify_all()
            except Exception as e:          # keep sampling through one bad tick
                print(f"rigwatch: sample failed: {e!r}", flush=True)
            nxt += TICK
            time.sleep(max(0.05, nxt - time.monotonic()))
            if time.monotonic() - nxt > 10:   # the machine slept; do not try to catch up
                nxt = time.monotonic()


CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def home_link(env=os.environ) -> str:
    """The "back to the hub" pill from DASH_HOME_URL / DASH_HOME_LABEL; empty when unset or not http(s)."""
    url = (env.get("DASH_HOME_URL") or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        print("rigwatch: ignoring DASH_HOME_URL, it must start with http:// or https://", flush=True)
        return ""
    label = (env.get("DASH_HOME_LABEL") or "").strip() or "Home"
    return f'<a class="home" href="{html.escape(url)}">&#8962; {html.escape(label)}</a>'


def make_handler(mon: Monitor):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rigwatch"

        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._json({"ok": mon.latest is not None, "windows": bool(mon.latest and mon.latest["windows_ok"])})
            if url.path == "/api/now":
                return self._json(mon.latest or {"warming_up": True})
            if url.path == "/api/history":
                rng = parse_qs(url.query).get("range", ["15m"])[0]
                return self._json(mon.history.query(rng))
            if url.path == "/api/stream":
                return self._stream()
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            f = (STATIC / name).resolve()
            if STATIC in f.parents and f.is_file():
                body = f.read_bytes()
                if f.suffix == ".html":
                    body = body.replace(b"<!--home-->", home_link().encode())
                return self._send(200, body, CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
            self._send(404, b"not found", "text/plain")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last = None
            try:
                while True:
                    with mon.cond:
                        mon.cond.wait_for(lambda: mon.latest is not last, timeout=15)
                        snap = mon.latest
                    if snap is last:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        last = snap
                        self.wfile.write(b"data: " + json.dumps(snap, separators=(",", ":")).encode() + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    return Handler


def serve(cfg: Config, host: str, port: int, db: Path, windows: bool = True, mon: Monitor | None = None) -> None:
    mon = mon or Monitor(cfg, db, windows)
    mon.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(mon))
    httpd.daemon_threads = True
    print(f"rigwatch on http://{host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if mon.feed:
            mon.feed.stop()
