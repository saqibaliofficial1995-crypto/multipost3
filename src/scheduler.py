"""Daily auto-upload scheduler. Background me chalta rehta hai.

  python src/scheduler.py --config config.yaml

Config ke schedule.daily_times par apne aap scan + upload karega.
Alternative (simpler): cron / Windows Task Scheduler — README dekho.
"""
import argparse
import os
import sys

from apscheduler.schedulers.blocking import BlockingScheduler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
from main import cmd_run, cmd_scan, load_cfg  # noqa: E402


def job(cfg):
    print("\n########## scheduled run ##########")
    try:
        cmd_scan(cfg)
        cmd_run(cfg, run_all=True)
    except Exception as e:
        print(f"[scheduler] error: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    for p in ("work", "processed", "tokens", "inbox"):
        os.makedirs(cfg["paths"][p], exist_ok=True)
    db.init(cfg["paths"]["db"])

    sched = BlockingScheduler()
    for t in cfg["schedule"]["daily_times"]:
        hh, mm = t.split(":")
        sched.add_job(job, "cron", hour=int(hh), minute=int(mm), args=[cfg],
                      misfire_grace_time=3600, id=f"daily_{t}")
        print(f"[scheduler] daily @ {t}")

    print("[scheduler] running. Ctrl+C to stop.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n[scheduler] stopped")


if __name__ == "__main__":
    main()
