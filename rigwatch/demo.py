"""Synthetic data for screenshots and for trying rigwatch away from WSL.

`python3 -m rigwatch demo` serves the real dashboard fed by made-up apps that
follow apps.example.toml. Every number still goes through build_snapshot, so
the accounting on screen is the same code the live mode runs.
"""
from __future__ import annotations

import math
import random
import tempfile
import threading
import time
from pathlib import Path

from .collector import Config
from .server import TICK, History, Monitor, build_snapshot

GB = 2 ** 30
MB = 2 ** 20

# group id -> parts: (label, kind, cpu %, memory bytes, pids)
PARTS = {
    "sensor": [("sensor-capture.service", "unit", 0.35, 14 * MB, 3), ("zeek-mirror", "ctr", 0.55, 120 * MB, 18),
               ("osqueryd.service", "sys", 0.25, 80 * MB, 2), ("sensor-alerter.service", "unit", 0.08, 410 * MB, 1),
               ("sensor-ui.service", "unit", 0.01, 60 * MB, 1)],
    "agents": [("claude session", "proc", 0.6, 1300 * MB, 6), ("claude bg-pty-host", "proc", 0.05, 130 * MB, 3),
               ("node @playwright/mcp", "proc", 0.02, 380 * MB, 4), ("claude daemon", "proc", 0.04, 62 * MB, 1)],
    "chat": [("chat-homeserver-1", "ctr", 0.06, 110 * MB, 40), ("chat-livekit-1", "ctr", 0.04, 42 * MB, 24),
             ("chat-client-1", "ctr", 0.0, 14 * MB, 9), ("chat-tailscale-1", "ctr", 0.01, 31 * MB, 12)],
    "web": [("finance-dashboard.service", "unit", 0.02, 34 * MB, 1), ("grafana", "ctr", 0.05, 95 * MB, 14),
            ("tokens-dashboard.service", "unit", 0.01, 22 * MB, 1)],
    "media": [("music-bot.service", "unit", 0.03, 75 * MB, 3), ("tts-server", "ctr", 0.02, 1050 * MB, 30)],
    "data": [("postgres", "ctr", 0.07, 260 * MB, 11), ("redis", "ctr", 0.03, 18 * MB, 5)],
    "other": [("job-worker.service", "unit", 0.02, 44 * MB, 1), ("video-lab-jvb-1", "ctr", 0.05, 420 * MB, 60),
              ("scraper.service", "unit", 0.01, 36 * MB, 2)],
    "rigwatch": [("rigwatch.service", "unit", 0.06, 13 * MB, 1)],
}
WIN_APPS = {"dumpcap": (0.5, 60 * MB), "osqueryd": (0.6, 45 * MB)}
WIN_PROCS = [("chrome", 1.6, 3.9 * GB, 31), ("Code", 0.8, 1.4 * GB, 14), ("Discord", 0.5, 1.2 * GB, 6),
             ("Spotify", 0.3, 420 * MB, 7), ("explorer", 0.2, 230 * MB, 1), ("dwm", 0.4, 130 * MB, 1),
             ("svchost", 0.3, 1.5 * GB, 92), ("MsMpEng", 0.2, 300 * MB, 1), ("steamwebhelper", 0.05, 600 * MB, 9),
             ("WindowsTerminal", 0.4, 210 * MB, 1), ("OneDrive", 0.05, 160 * MB, 1), ("System", 0.3, 4 * MB, 1)]


class DemoState:
    """Stands in for WslSampler's systemd / docker view."""

    def __init__(self):
        now = time.time()
        self.units = {"job-worker.service": {"ActiveState": "active", "SubState": "running", "NRestarts": "3"},
                      "transcribe-watch.service": {"ActiveState": "inactive", "Type": "oneshot"}}
        self.containers = []
        self.timers = [{"name": "sensor-report.timer", "next": now + 14 * 3600, "last": now - 10 * 3600},
                       {"name": "transcribe-watch.timer", "next": now + 7 * 60, "last": now - 23 * 60},
                       {"name": "music-bot-update.timer", "next": now + 11 * 3600, "last": now - 13 * 3600}]


def _wave(t: float, period: float, phase: float = 0.0) -> float:
    return 0.5 + 0.5 * math.sin(2 * math.pi * (t / period) + phase)


def demo_inputs(cfg: Config, t: float, rng: random.Random) -> tuple[dict, dict]:
    """(wsl sample, windows sample) for wall-clock time t."""
    hour = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
    day = 0.35 + 0.65 * max(0.0, math.sin(math.pi * (hour - 7) / 16)) if 7 <= hour <= 23 else 0.3
    burst = 1.0 + 6.0 * max(0.0, math.sin(2 * math.pi * t / 1500)) ** 8      # an agent working in bursts
    groups = {}
    for g in cfg.groups:
        parts = []
        for label, kind, cpu, mem, pids in PARTS.get(g.id, []):
            k = cpu * (0.6 + 0.8 * rng.random()) * (burst if g.id == "agents" else 1.0)
            if g.id == "sensor":
                k *= 0.7 + 0.6 * day
            m = mem * (0.97 + 0.06 * _wave(t, 7200, hash(label) % 7))
            parts.append({"key": f"{kind}:{label}", "label": label, "kind": kind, "cpu": k, "mem": int(m),
                          "io": rng.random() * 40_000 if kind != "proc" else 0.0, "pids": pids})
        groups[g.id] = {"cpu": sum(p["cpu"] for p in parts), "mem": sum(p["mem"] for p in parts),
                        "io": sum(p["io"] for p in parts), "pids": sum(p["pids"] for p in parts),
                        "parts": sorted(parts, key=lambda p: -p["cpu"])}
    apps_cpu = sum(g["cpu"] for g in groups.values())
    apps_mem = sum(g["mem"] for g in groups.values())
    wsl_cpu = apps_cpu + 1.2 + rng.random() * 0.8
    wsl_used = apps_mem + 1.6 * GB
    wsl = {"ready": True, "ncpu": 16, "cpu": wsl_cpu, "mem_total": 24 * GB, "mem_used": wsl_used,
           "mem_cache": 3 * GB, "swap_used": 0, "net": 120_000 + rng.random() * 60_000, "groups": groups}

    # an evening gaming session, 7 pm to 11 pm
    game = max(0.0, math.sin(math.pi * (hour - 19) / 4)) ** 0.5 if 19 <= hour <= 23 else 0.0
    game *= 0.85 + 0.15 * rng.random()
    procs = []
    for name, cpu, ws, n in WIN_PROCS:
        c = cpu * (0.5 + rng.random()) * (0.6 + 0.8 * day)
        procs.append({"name": name, "cpu": c, "ws": ws * (0.95 + 0.1 * rng.random()), "n": n})
    if game > 0.05:
        procs.append({"name": "Game", "cpu": 14 * game, "ws": 6 * GB * min(1, game * 3), "n": 1})
    for name, (cpu, ws) in WIN_APPS.items():
        procs.append({"name": name, "cpu": cpu * (0.6 + 0.8 * rng.random()), "ws": ws, "n": 1})
    vmmem_ws = wsl_used + 3.4 * GB
    procs.append({"name": "vmmemWSL", "cpu": wsl_cpu, "ws": vmmem_ws, "n": 1})
    win_cpu = wsl_cpu + sum(p["cpu"] for p in procs if p["name"] != "vmmemWSL") + 1.5
    win_used = vmmem_ws + sum(p["ws"] for p in procs if p["name"] != "vmmemWSL") + 2.5 * GB
    gpu = 4 + 3 * rng.random() + 80 * game
    gprocs = [{"name": "Game", "util": 80 * game, "type": "3D"}] if game > 0.05 else []
    gprocs += [{"name": "chrome", "util": 2 + 2 * rng.random(), "type": "VideoDecode"},
               {"name": "dwm", "util": 1.0, "type": "3D"}]
    win = {"ncpu": 16, "cpu": min(100.0, win_cpu), "mem_total": 64 * GB, "mem_used": min(64 * GB, win_used),
           "cpu_name": "16-thread desktop CPU", "gpu_name": "Desktop GPU", "vram_total": 16 * GB,
           "procs": procs, "nproc": 140, "self": {"cpu": 0.08, "ws": 160 * MB},
           "gpu": {"util": min(100.0, gpu), "vram_used": 2.8 * GB + 7 * GB * game, "procs": gprocs},
           "net": [{"name": "Ethernet", "desc": "2.5GbE adapter", "rx": 300_000 + 900_000 * rng.random(),
                    "tx": 60_000 + 80_000 * rng.random()},
                   {"name": "Mirror", "desc": "USB gigabit adapter", "rx": 1_500_000 + 2_000_000 * day * rng.random(),
                    "tx": 0.0}]}
    return wsl, win


class DemoMonitor(Monitor):
    def __init__(self, cfg: Config, db: Path):
        super().__init__(cfg, db, windows=False)
        self.state = DemoState()
        self.rng = random.Random(7)
        self._backfill()

    def _snap(self, t: float) -> dict:
        wsl, win = demo_inputs(self.cfg, t, self.rng)
        snap = build_snapshot(self.cfg, wsl, win, self.state)
        snap["t"] = int(t * 1000)
        snap["uptime"] = time.time() - self.started + 3 * 86400
        return snap

    def _backfill(self) -> None:
        """Seven days of minute rows plus the last 30 minutes at full resolution."""
        now = time.time()
        rows = []
        for m in range(7 * 24 * 60, 30, -1):
            t = (now // 60 - m) * 60
            s = self._snap(t)
            c = dict(s["pc"]["cpu"], gpu=s["win"]["gpu"]["util"], total=s["pc"]["cpu_total"])
            for k in set(c) | set(s["pc"]["mem"]):
                v = c.get(k, 0.0)
                rows.append((int(t), k, v, v * (1.2 + self.rng.random()), s["pc"]["mem"].get(k, 0.0)))
        self.history.db.executemany("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?)", rows)
        self.history.db.commit()
        for i in range(900, 0, -1):
            self.history.add(self._snap(now - i * TICK))

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="demo").start()

    def _loop(self) -> None:
        while True:
            snap = self._snap(time.time())
            self.history.add(snap)
            with self.cond:
                self.latest = snap
                self.cond.notify_all()
            time.sleep(TICK)


def demo_db() -> Path:
    return Path(tempfile.mkdtemp(prefix="rigwatch-demo-")) / "history.db"
