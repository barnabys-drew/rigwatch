"""Sample WSL resource use per app from cgroup v2 and /proc.

Every systemd unit and docker container already has its own cgroup, so CPU and
memory are attributed exactly from the kernel's own counters. The one exception
is a "split" unit (the Herdr terminal server) whose cgroup hosts every shell and
agent session; there each process is matched by command line and its CPU comes
from /proc/<pid>/stat deltas.

Units of the numbers this module returns:
  cpu   percent of the whole WSL VM (100 = every vCPU busy)
  mem   bytes, anonymous + kernel (cgroup memory.current minus page cache)
  io    bytes per second read + written
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


# ---------------------------------------------------------------- config

@dataclass
class Group:
    id: str
    name: str
    blurb: str = ""
    units: list[str] = field(default_factory=list)
    system_units: list[str] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    procs: list[re.Pattern] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    timers: list[str] = field(default_factory=list)
    nics: list[str] = field(default_factory=list)
    catch_all: bool = False


@dataclass
class Peer:
    """Another machine's rigwatch, linked from the header so one page can switch between them."""
    name: str
    url: str


@dataclass
class Config:
    groups: list[Group]
    split_units: list[str]
    ignore_units: list[str]
    machine: str = ""                                   # this machine's name in the switcher (default: hostname)
    peers: list[Peer] = field(default_factory=list)

    def group_ids(self) -> list[str]:
        return [g.id for g in self.groups]


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    groups = []
    for g in raw.get("group", []):
        g = dict(g)
        g["procs"] = [re.compile(p) for p in g.get("procs", [])]
        groups.append(Group(**g))
    if len(groups) > 8:
        raise ValueError("apps.toml: at most 8 groups (fold the rest into the catch-all)")
    peers = []
    for p in raw.get("peer", []):
        name, url = str(p.get("name", "")).strip(), str(p.get("url", "")).strip()
        if not name or not url.startswith(("https://", "http://")):
            raise ValueError(f"apps.toml: each [[peer]] needs a name and an http(s) url, got {p!r}")
        peers.append(Peer(name, url))
    return Config(groups, raw.get("split_units", []), raw.get("ignore_units", []),
                  machine=str(raw.get("machine", "")).strip() or socket.gethostname(), peers=peers)


def _match(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


# ---------------------------------------------------------------- cgroup leaves

@dataclass
class Leaf:
    """One cgroup that maps to one thing: a unit or a container."""
    key: str            # "unit:web-dashboard.service", "sys:osqueryd.service", "ctr:tts-server"
    kind: str           # unit | sys | ctr
    name: str           # unit or container name
    path: Path
    project: str = ""   # compose project, containers only


def read_kv(path: Path) -> dict[str, int]:
    out = {}
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition(" ")
            if v.isdigit():
                out[k] = int(v)
    except OSError:
        pass
    return out


def read_int(path: Path) -> int:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def read_io(path: Path) -> int:
    """Total bytes read + written across devices from io.stat."""
    total = 0
    try:
        for line in path.read_text().splitlines():
            for tok in line.split()[1:]:
                k, _, v = tok.partition("=")
                if k in ("rbytes", "wbytes") and v.isdigit():
                    total += int(v)
    except OSError:
        pass
    return total


def cgroup_usage(path: Path) -> dict:
    stat = read_kv(path / "memory.stat")
    current = read_int(path / "memory.current")
    return {
        "cpu_usec": read_kv(path / "cpu.stat").get("usage_usec", 0),
        "mem": max(0, current - stat.get("file", 0)),
        "io": read_io(path / "io.stat"),
        "pids": read_int(path / "pids.current"),
    }


def discover_leaves(root: Path, uid: int, containers: dict[str, dict]) -> list[Leaf]:
    """Every unit / container cgroup that currently exists.

    `containers` maps full container id -> {"name", "project"} (from docker ps).
    """
    leaves = []
    user = root / f"user.slice/user-{uid}.slice/user@{uid}.service"
    for slice_ in ("app.slice", "session.slice"):
        d = user / slice_
        if d.is_dir():
            for p in d.iterdir():
                if p.name.endswith(".service") and p.is_dir():
                    leaves.append(Leaf(f"unit:{p.name}", "unit", p.name, p))
    sysd = root / "system.slice"
    if sysd.is_dir():
        for p in sysd.iterdir():
            if not p.is_dir():
                continue
            if p.name.startswith("docker-") and p.name.endswith(".scope"):
                cid = p.name[len("docker-"):-len(".scope")]
                info = containers.get(cid, {"name": cid[:12], "project": ""})
                leaves.append(Leaf(f"ctr:{info['name']}", "ctr", info["name"], p, info["project"]))
            elif p.name.endswith(".service"):
                leaves.append(Leaf(f"sys:{p.name}", "sys", p.name, p))
    return leaves


def classify_leaf(leaf: Leaf, cfg: Config) -> str | None:
    """Group id that owns this leaf, or None (it is "rest of WSL")."""
    for g in cfg.groups:
        if leaf.kind == "unit" and _match(leaf.name, g.units):
            return g.id
        if leaf.kind == "sys" and _match(leaf.name, g.system_units):
            return g.id
        if leaf.kind == "ctr" and (_match(leaf.name, g.containers)
                                   or (leaf.project and leaf.project in g.projects)):
            return g.id
    if leaf.kind in ("unit", "ctr") and leaf.name not in cfg.ignore_units \
            and leaf.name not in cfg.split_units:
        for g in cfg.groups:
            if g.catch_all:
                return g.id
    return None


# ---------------------------------------------------------------- processes

@dataclass
class Proc:
    pid: int
    ppid: int
    start: int          # starttime in ticks, with pid makes a stable identity
    ticks: int          # utime + stime
    rss_anon: int
    cmd: str


def read_proc(pid: int) -> Proc | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm may contain spaces and parens; fields resume after the last ")"
        rest = stat[stat.rindex(")") + 2:].split()
        ppid, utime, stime, start = int(rest[1]), int(rest[11]), int(rest[12]), int(rest[19])
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        rss_anon = 0
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("RssAnon:"):
                rss_anon = int(line.split()[1]) * 1024
                break
        return Proc(pid, ppid, start, utime + stime, rss_anon, cmd)
    except (OSError, ValueError, IndexError):
        return None


def cgroup_pids(path: Path) -> list[int]:
    pids = []
    for f in [path / "cgroup.procs", *path.rglob("*/cgroup.procs")]:
        try:
            pids += [int(x) for x in f.read_text().split()]
        except (OSError, ValueError):
            pass
    return pids


def assign_procs(procs: dict[int, Proc], groups: list[Group]) -> dict[int, str]:
    """pid -> group id for processes whose command line, or an ancestor's, matches."""
    direct = {}
    for p in procs.values():
        for g in groups:
            if any(r.search(p.cmd) for r in g.procs):
                direct[p.pid] = g.id
                break
    out = {}
    for pid in procs:
        cur, seen = pid, 0
        while cur in procs and seen < 64:
            if cur in direct:
                out[pid] = direct[cur]
                break
            cur, seen = procs[cur].ppid, seen + 1
    return out


# ---------------------------------------------------------------- docker / systemd

def docker_containers() -> list[dict]:
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        labels = dict(kv.split("=", 1) for kv in r.get("Labels", "").split(",") if "=" in kv)
        status = r.get("Status", "")
        health = "unhealthy" if "(unhealthy)" in status else "healthy" if "(healthy)" in status \
            else "starting" if "(health: starting)" in status else ""
        rows.append({"id": r["ID"], "name": r["Names"], "project": labels.get("com.docker.compose.project", ""),
                     "state": r.get("State", ""), "status": status, "health": health,
                     "image": r.get("Image", "")})
    return rows


SHOW_PROPS = "Id,ActiveState,SubState,Type,NRestarts,ActiveEnterTimestamp,Description"


def systemd_units(user: bool, names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    cmd = ["systemctl"] + (["--user"] if user else []) + ["show", "-p", SHOW_PROPS, "--", *names]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    units = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if kv.get("Id"):
            units[kv["Id"]] = kv
    return units


def user_unit_names() -> list[str]:
    try:
        out = subprocess.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
                              "--type=service,timer"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split()[0] for line in out.splitlines() if line.split()]


def user_timers() -> list[dict]:
    try:
        out = subprocess.run(["systemctl", "--user", "list-timers", "--all", "--output=json"],
                             capture_output=True, text=True, timeout=10).stdout
        rows = json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [{"name": r.get("unit", ""), "activates": r.get("activates", ""),
             "next": (r.get("next") or 0) // 1_000_000 or None,
             "last": (r.get("last") or 0) // 1_000_000 or None} for r in rows]


# ---------------------------------------------------------------- the sampler

def read_wsl_totals() -> dict:
    with open("/proc/stat") as f:
        cpu = [int(x) for x in f.readline().split()[1:]]
    idle = cpu[3] + cpu[4]                       # idle + iowait
    total = sum(cpu[:8])                         # guest time is already inside user
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    net = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() in ("lo",) or name.strip().startswith(("veth", "docker", "br-")):
                continue
            cols = data.split()
            net += int(cols[0]) + int(cols[8])
    return {"busy": total - idle, "total": total, "mem_total": mem["MemTotal"],
            "mem_used": mem["MemTotal"] - mem["MemAvailable"], "mem_cache": mem.get("Cached", 0),
            "swap_used": mem["SwapTotal"] - mem["SwapFree"], "net": net}


class WslSampler:
    """Holds the previous counters so each call returns rates since the last one."""

    STATE_EVERY = 15.0      # seconds between docker / systemctl refreshes

    def __init__(self, cfg: Config, root: Path = Path("/sys/fs/cgroup"), uid: int | None = None):
        self.cfg = cfg
        self.root = root
        self.uid = os.getuid() if uid is None else uid
        self.ncpu = os.cpu_count() or 1
        self.prev_t: float | None = None
        self.prev_tot: dict | None = None
        self.prev_leaf: dict[str, dict] = {}
        self.prev_proc: dict[tuple[int, int], int] = {}
        self.containers: list[dict] = []
        self.units: dict[str, dict] = {}
        self.timers: list[dict] = []
        self.state_at = 0.0

    def refresh_state(self, now: float) -> None:
        if now - self.state_at < self.STATE_EVERY:
            return
        self.state_at = now
        self.containers = docker_containers()
        names = [n for n in user_unit_names() if n.endswith(".service")]
        self.units = systemd_units(True, names)
        sys_names = sorted({p for g in self.cfg.groups for p in g.system_units if "*" not in p})
        self.units.update({f"sys:{k}": v for k, v in systemd_units(False, sys_names).items()})
        self.timers = user_timers()

    def sample(self) -> dict:
        now = time.monotonic()
        self.refresh_state(now)
        tot = read_wsl_totals()
        dt = (now - self.prev_t) if self.prev_t else 0.0
        by_id = {c["id"]: c for c in self.containers}
        leaves = discover_leaves(self.root, self.uid, by_id)

        groups = {g.id: {"cpu": 0.0, "mem": 0, "io": 0.0, "pids": 0, "parts": {}} for g in self.cfg.groups}
        seen_leaf: dict[str, dict] = {}
        split_paths = []

        def add(gid, key, label, kind, cpu, mem, io, pids):
            g = groups[gid]
            g["cpu"] += cpu
            g["mem"] += mem
            g["io"] += io
            g["pids"] += pids
            part = g["parts"].setdefault(key, {"key": key, "label": label, "kind": kind,
                                               "cpu": 0.0, "mem": 0, "io": 0.0, "pids": 0})
            part["cpu"] += cpu
            part["mem"] += mem
            part["io"] += io
            part["pids"] += pids

        for leaf in leaves:
            if leaf.kind == "unit" and leaf.name in self.cfg.split_units:
                split_paths.append(leaf.path)
                continue
            gid = classify_leaf(leaf, self.cfg)
            if gid is None:
                continue
            u = cgroup_usage(leaf.path)
            seen_leaf[str(leaf.path)] = u      # by path: a container's name resolves late
            p = self.prev_leaf.get(str(leaf.path))
            cpu = io = 0.0
            if dt > 0:
                # a cgroup born since the last sample has used all of its counters since then
                base = p or ({"cpu_usec": 0, "io": 0} if self.prev_t else u)
                cpu = max(0, u["cpu_usec"] - base["cpu_usec"]) / (dt * 1e6 * self.ncpu) * 100
                io = max(0, u["io"] - base["io"]) / dt
            add(gid, leaf.key, leaf.name, leaf.kind, cpu, u["mem"], io, u["pids"])

        # processes inside the split units (Claude Code sessions inside Herdr, ...)
        procs = {}
        for path in split_paths:
            for pid in cgroup_pids(path):
                pr = read_proc(pid)
                if pr:
                    procs[pid] = pr
        owner = assign_procs(procs, [g for g in self.cfg.groups if g.procs])
        seen_proc = {}
        for pid, gid in owner.items():
            pr = procs[pid]
            ident = (pr.pid, pr.start)
            seen_proc[ident] = pr.ticks
            prev = self.prev_proc.get(ident)
            cpu = 0.0
            if dt > 0 and prev is not None:
                cpu = max(0, pr.ticks - prev) / CLK_TCK / (dt * self.ncpu) * 100
            label = _short_cmd(pr.cmd)
            add(gid, f"proc:{label}", label, "proc", cpu, pr.rss_anon, 0.0, 1)

        wsl_cpu = 0.0
        net = 0.0
        if self.prev_tot and tot["total"] > self.prev_tot["total"]:
            wsl_cpu = (tot["busy"] - self.prev_tot["busy"]) / (tot["total"] - self.prev_tot["total"]) * 100
            net = max(0, tot["net"] - self.prev_tot["net"]) / dt if dt > 0 else 0.0

        self.prev_t, self.prev_tot = now, tot
        self.prev_leaf, self.prev_proc = seen_leaf, seen_proc

        for g in groups.values():
            g["parts"] = sorted(g["parts"].values(), key=lambda x: (-x["cpu"], -x["mem"]))
        return {
            "ready": dt > 0,
            "ncpu": self.ncpu,
            "cpu": wsl_cpu,
            "mem_total": tot["mem_total"],
            "mem_used": tot["mem_used"],
            "mem_cache": tot["mem_cache"],
            "swap_used": tot["swap_used"],
            "net": net,
            "groups": groups,
        }


def _short_cmd(cmd: str) -> str:
    """A readable, stable label for a process: the program name plus its first verb."""
    parts = cmd.split()
    if not parts:
        return "?"
    prog = parts[0].rsplit("/", 1)[-1]
    if prog in ("node", "python", "python3", "bun") and len(parts) > 1:
        script = parts[1].rsplit("/", 1)[-1]
        # npx-installed MCP servers live under .../node_modules/<pkg>/...
        m = re.search(r"node_modules/(@[^/]+/[^/]+|[^/]+)", parts[1])
        return f"{prog} {m.group(1) if m else script}"
    if re.fullmatch(r"\d+\.\d+\.\d+", prog):          # ~/.local/share/claude/versions/2.1.303
        prog = "claude"
    sub = next((p for p in parts[1:] if not p.startswith("-")), "")
    if prog == "claude":
        return "claude " + (sub if sub in ("daemon", "bg-pty-host", "bg-spare") else "session")
    return prog
