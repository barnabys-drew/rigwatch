"""python3 -m rigwatch serve | demo | sample"""
import argparse
import json
import time
from pathlib import Path

from .collector import WslSampler, load_config
from .server import build_snapshot, serve

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "apps.example.toml"


def default_config() -> Path:
    """Your apps.toml when there is one, otherwise the shipped example."""
    own = ROOT / "apps.toml"
    return own if own.exists() else EXAMPLE


def main() -> None:
    ap = argparse.ArgumentParser(prog="rigwatch")
    ap.add_argument("--config", type=Path, default=None, help="default: apps.toml, else apps.example.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="sample continuously and serve the dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8775)
    s.add_argument("--db", type=Path, default=Path.home() / ".local/share/rigwatch/history.db")
    s.add_argument("--no-windows", action="store_true", help="skip the PowerShell sampler")
    d = sub.add_parser("demo", help="serve the dashboard with made-up apps (runs anywhere)")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8775)
    sub.add_parser("sample", help="print one WSL-only snapshot as JSON and exit")
    args = ap.parse_args()

    if args.cmd == "demo":
        from .demo import DemoMonitor, demo_db
        cfg = load_config(EXAMPLE)          # the demo's apps are written against the example groups
        db = demo_db()
        serve(cfg, args.host, args.port, db, mon=DemoMonitor(cfg, db))
        return
    cfg = load_config(args.config or default_config())
    if args.cmd == "serve":
        serve(cfg, args.host, args.port, args.db, windows=not args.no_windows)
    else:
        sampler = WslSampler(cfg)
        sampler.sample()
        time.sleep(2)
        print(json.dumps(build_snapshot(cfg, sampler.sample(), None, sampler), indent=1))


if __name__ == "__main__":
    main()
