"""Unit tests for attribution and accounting, against a fake cgroup tree (no root, no docker)."""
import re
import tempfile
import time
import unittest
from pathlib import Path

from rigwatch.collector import (Config, Group, Leaf, Proc, assign_procs, classify_leaf, cgroup_usage,
                                discover_leaves, load_config, _short_cmd)
from rigwatch.server import History, build_snapshot, home_link, unit_part_state

ROOT = Path(__file__).resolve().parent.parent


def cfg():
    return Config(
        groups=[
            Group(id="tw", name="tapwatch", units=["tapwatch-*.service"], system_units=["osqueryd.service"],
                  containers=["tapwatch-*"], windows=["dumpcap"], nics=["Ethernet"]),
            Group(id="cc", name="Claude", procs=[re.compile(r"^(\S*/)?claude(\s|$)")]),
            Group(id="chat", name="Chat", projects=["chat"]),
            Group(id="other", name="Other", catch_all=True),
        ],
        split_units=["herdr.service"], ignore_units=["herdr.service", "dbus.service"])


def write(path: Path, **files):
    path.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (path / name.replace("_", ".", 1)).write_text(body)


class Classify(unittest.TestCase):
    def test_units_containers_projects_and_catch_all(self):
        c = cfg()
        self.assertEqual(classify_leaf(Leaf("", "unit", "tapwatch-ui.service", Path()), c), "tw")
        self.assertEqual(classify_leaf(Leaf("", "sys", "osqueryd.service", Path()), c), "tw")
        self.assertEqual(classify_leaf(Leaf("", "ctr", "tapwatch-mirror", Path()), c), "tw")
        self.assertEqual(classify_leaf(Leaf("", "ctr", "chat-homeserver-1", Path(), "chat"), c), "chat")
        self.assertEqual(classify_leaf(Leaf("", "unit", "newthing.service", Path()), c), "other")
        self.assertEqual(classify_leaf(Leaf("", "ctr", "zealous_franklin", Path()), c), "other")
        # infrastructure stays in "rest of WSL"
        self.assertIsNone(classify_leaf(Leaf("", "unit", "dbus.service", Path()), c))
        self.assertIsNone(classify_leaf(Leaf("", "unit", "herdr.service", Path()), c))
        self.assertIsNone(classify_leaf(Leaf("", "sys", "docker.service", Path()), c))

    def test_process_tree_claims_children(self):
        procs = {
            10: Proc(10, 1, 0, 0, 0, "/home/u/.local/bin/herdr server"),
            11: Proc(11, 10, 0, 0, 0, "/bin/bash"),
            12: Proc(12, 11, 0, 0, 0, "claude --resume abc"),
            13: Proc(13, 12, 0, 0, 0, "node /x/node_modules/@playwright/mcp/cli.js"),
            14: Proc(14, 13, 0, 0, 0, "/bin/bash -c ls"),
        }
        owner = assign_procs(procs, cfg().groups)
        self.assertEqual(owner, {12: "cc", 13: "cc", 14: "cc"})

    def test_short_labels(self):
        self.assertEqual(_short_cmd("claude --resume 57354baf"), "claude session")
        self.assertEqual(_short_cmd("/home/u/.local/share/claude/versions/2.1.303 --resume x"), "claude session")
        self.assertEqual(_short_cmd("claude bg-spare --bg-spare /tmp/x"), "claude bg-spare")
        self.assertEqual(_short_cmd("/n/bin/node /n/lib/node_modules/@playwright/mcp/cli.js"), "node @playwright/mcp")

    def test_shipped_configs_load(self):
        for path in [ROOT / "apps.example.toml", ROOT / "apps.toml"]:
            if not path.exists():
                continue
            c = load_config(path)
            self.assertLessEqual(len(c.groups), 8)
            self.assertEqual(len({g.id for g in c.groups}), len(c.groups))
            self.assertEqual(sum(g.catch_all for g in c.groups), 1)


class Cgroups(unittest.TestCase):
    def test_discover_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            unit = root / "user.slice/user-1000.slice/user@1000.service/app.slice/web-dashboard.service"
            write(unit, cpu_stat="usage_usec 5000000\nuser_usec 1\n", memory_current="3000000\n",
                  memory_stat="anon 2000000\nfile 1000000\n", io_stat="8:0 rbytes=100 wbytes=50 rios=1\n",
                  pids_current="2\n")
            write(root / "system.slice/docker-abc123.scope", cpu_stat="usage_usec 1\n")
            write(root / "system.slice/osqueryd.service", cpu_stat="usage_usec 1\n")
            leaves = discover_leaves(root, 1000, {"abc123": {"name": "tts-server", "project": ""}})
            self.assertEqual(sorted(l.key for l in leaves),
                             ["ctr:tts-server", "sys:osqueryd.service", "unit:web-dashboard.service"])
            u = cgroup_usage(unit)
            self.assertEqual(u, {"cpu_usec": 5000000, "mem": 2000000, "io": 150, "pids": 2})


class FakeSampler:
    def __init__(self, units=None, containers=None):
        self.units = units or {}
        self.containers = containers or []
        self.timers = []


def wsl_sample(c):
    groups = {g.id: {"cpu": 0.0, "mem": 0, "io": 0.0, "pids": 0, "parts": []} for g in c.groups}
    groups["tw"].update(cpu=2.0, mem=500, parts=[{"key": "unit:tapwatch-ui.service", "label": "tapwatch-ui.service",
                                                  "kind": "unit", "cpu": 2.0, "mem": 500, "io": 0.0, "pids": 1}])
    groups["cc"].update(cpu=3.0, mem=1000)
    return {"ready": True, "ncpu": 16, "cpu": 10.0, "mem_total": 32000, "mem_used": 4000, "mem_cache": 0,
            "swap_used": 0, "net": 0.0, "groups": groups}


class Machines(unittest.TestCase):
    def test_machine_name_and_peers(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "apps.toml"
            path.write_text('machine = "workstation"\n[[peer]]\nname = "mini-pc"\nurl = "https://mini-pc.example:10002"\n')
            c = load_config(path)
            self.assertEqual((c.machine, [(p.name, p.url) for p in c.peers]),
                             ("workstation", [("mini-pc", "https://mini-pc.example:10002")]))
            path.write_text('[[peer]]\nname = "x"\nurl = "javascript:alert(1)"\n')
            with self.assertRaises(ValueError):
                load_config(path)
            path.write_text("")
            self.assertTrue(load_config(path).machine)      # the hostname when not set
            self.assertEqual(load_config(path).peers, [])


class Accounting(unittest.TestCase):
    def test_whole_pc_split_adds_up(self):
        c = cfg()
        win = {"ncpu": 16, "cpu": 30.0, "mem_total": 64000, "mem_used": 20000, "cpu_name": "x",
               "procs": [{"name": "vmmemWSL", "cpu": 9.0, "ws": 6000, "n": 1},
                         {"name": "dumpcap", "cpu": 1.0, "ws": 100, "n": 1},
                         {"name": "chrome", "cpu": 12.0, "ws": 5000, "n": 30}],
               "net": [{"name": "Ethernet", "rx": 1e6, "tx": 0, "desc": ""}], "nproc": 3, "self": None}
        snap = build_snapshot(c, wsl_sample(c), win, FakeSampler())
        cpu = snap["pc"]["cpu"]
        self.assertAlmostEqual(cpu["tw"], 3.0)            # 2 in WSL + 1 dumpcap.exe
        self.assertAlmostEqual(cpu["rest_wsl"], 5.0)      # 10 WSL busy - 2 - 3
        self.assertAlmostEqual(cpu["rest_win"], 19.0)     # 30 - 10 WSL - 1 dumpcap
        self.assertAlmostEqual(sum(cpu.values()), win["cpu"])
        mem = snap["pc"]["mem"]
        self.assertEqual(mem["wsl_cache"], 2000)          # vmmem 6000 - linux used 4000
        self.assertEqual(mem["rest_win"], 20000 - 6000 - 100)
        self.assertEqual(sum(mem.values()), win["mem_used"])
        tw = next(g for g in snap["groups"] if g["id"] == "tw")
        self.assertEqual(tw["nic"]["rx"], 1e6)
        self.assertNotIn("dumpcap", [p["name"] for p in snap["win"]["procs"]])

    def test_without_windows(self):
        c = cfg()
        snap = build_snapshot(c, wsl_sample(c), None, FakeSampler(), "boom")
        self.assertFalse(snap["windows_ok"])
        self.assertNotIn("rest_win", snap["pc"]["cpu"])
        self.assertEqual(snap["windows_error"], "boom")

    def test_health(self):
        c = cfg()
        sampler = FakeSampler(
            units={"tapwatch-ui.service": {"ActiveState": "failed"},
                   "tapwatch-report.service": {"ActiveState": "inactive", "Type": "oneshot"}},
            containers=[{"name": "tapwatch-ghost", "project": "", "state": "running", "status": "Up 9 hours",
                         "health": "", "id": "x"}])
        snap = build_snapshot(c, wsl_sample(c), None, sampler)
        tw = next(g for g in snap["groups"] if g["id"] == "tw")
        self.assertEqual(tw["status"], "critical")
        levels = {p["label"]: p["level"] for p in tw["parts"]}
        self.assertEqual(levels["tapwatch-report.service"], "info")
        self.assertEqual(levels["tapwatch-ghost"], "warning")
        self.assertEqual(unit_part_state({"ActiveState": "active", "NRestarts": "4"})[0], "warning")


class Demo(unittest.TestCase):
    def test_demo_snapshot_adds_up(self):
        import random
        from rigwatch.demo import DemoState, demo_inputs
        c = load_config(ROOT / "apps.example.toml")
        wsl, win = demo_inputs(c, time.time(), random.Random(1))
        snap = build_snapshot(c, wsl, win, DemoState())
        self.assertAlmostEqual(sum(snap["pc"]["cpu"].values()), win["cpu"], places=6)
        self.assertEqual(next(g for g in snap["groups"] if g["id"] == "other")["status"], "warning")
        self.assertTrue(next(g for g in snap["groups"] if g["id"] == "sensor")["nic"])


class HomeLink(unittest.TestCase):
    def test_present_when_set_and_escaped(self):
        html = home_link({"DASH_HOME_URL": "https://hub.example/?a=1&b=2", "DASH_HOME_LABEL": "Hub <1>"})
        self.assertEqual(html, '<a class="home" href="https://hub.example/?a=1&amp;b=2">&#8962; Hub &lt;1&gt;</a>')
        self.assertIn("&#8962; Home<", home_link({"DASH_HOME_URL": "HTTP://hub.example"}))

    def test_absent_when_unset_or_not_http(self):
        self.assertEqual(home_link({}), "")
        self.assertEqual(home_link({"DASH_HOME_URL": "  "}), "")
        self.assertEqual(home_link({"DASH_HOME_URL": "javascript:alert(1)"}), "")
        self.assertEqual(home_link({"DASH_HOME_URL": "//hub.example"}), "")

    def test_page_has_the_placeholder_in_the_header(self):
        page = (ROOT / "rigwatch" / "static" / "index.html").read_text()
        self.assertLess(page.index('<div class="top-right">'), page.index("<!--home-->"))


class HistoryTest(unittest.TestCase):
    def test_ring_and_minute_rollup(self):
        with tempfile.TemporaryDirectory() as d:
            h = History(Path(d) / "h.db")
            base = int(time.time()) // 60 * 60 - 180     # minute-aligned, so buckets are fixed
            for i, t in enumerate([base + 5, base + 10, base + 65, base + 125]):
                h.add({"t": t * 1000, "pc": {"cpu": {"a": float(i)}, "mem": {"a": 100.0}, "cpu_total": 5.0}, "win": None})
            live = h.query("15m")
            self.assertEqual(live["cpu"]["a"], [0.0, 1.0, 2.0, 3.0])
            self.assertEqual(live["peak"]["a"], 3.0)
            hour = h.query("1h")
            self.assertEqual(len(hour["t"]), 2)           # two finished minutes flushed, the current one pending
            self.assertEqual(hour["cpu"]["a"], [0.5, 2.0])
            self.assertEqual(hour["peak"]["a"], 2.0)


if __name__ == "__main__":
    unittest.main()
