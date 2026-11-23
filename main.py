"""
main.py - 실행 진입점

    python main.py                     # config.py 의 SIMULATION_MODE 에 따라 UI 실행
    python main.py --sim               # 가상 모드(하드웨어 불필요)로 UI 실행
    python main.py --real              # 실제 하드웨어 모드 강제
    python main.py --sim --headless --mode full --cycles 20     # UI 없이 전체 세션 자동 실행(테스트)
    python main.py --sim --headless --mode quick --patient P001 # 저장된 치료값으로 빠른 시작
"""
from __future__ import annotations

import argparse
import sys
import time

import config as config_module
from config import make_config
from hardware import create_hardware
from session import RehabSession, RunMode, SessionObserver, Stage
from store import ProfileStore


class ConsoleObserver(SessionObserver):
    def __init__(self) -> None:
        self.done = False
        self.info = None

    def log(self, text, level="info"):
        print(f"[{level.upper():5s}] {text}")

    def stage(self, stage):
        print(f"--- 단계: {stage.value} ---")

    def prompt(self, title, body, button, needs_confirm):
        print(f"[PROMPT] {title}: {body.splitlines()[0] if body else ''}")

    def finished(self, info):
        self.info = info
        self.done = True


def run_headless(args, cfg) -> int:
    cfg.sim.auto_confirm = True
    cfg.sim.time_scale = args.speedup
    if args.cycles:
        cfg.therapy.max_cycles = args.cycles
    if args.minutes:
        cfg.therapy.session_duration_s = args.minutes * 60
    hw = create_hardware(cfg)
    if not hw.open():
        print("하드웨어 연결 실패:", getattr(hw, "error", ""))
        return 1
    obs = ConsoleObserver()
    store = ProfileStore(cfg)
    sess = RehabSession(cfg, hw, store, args.patient, RunMode(args.mode), obs)
    sess.start()
    try:
        while not obs.done:
            time.sleep(0.2)
    except KeyboardInterrupt:
        sess.request_estop("키보드 인터럽트")
        sess.join(5)
    hw.close()
    print("요약:", obs.info["summary"] if obs.info else None)
    print("파일:", obs.info.get("files") if obs.info else None)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="상지 재활 로봇 제어 시스템")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--sim", action="store_true", help="가상 모드 (하드웨어 불필요)")
    g.add_argument("--real", action="store_true", help="실제 하드웨어 모드")
    ap.add_argument("--headless", action="store_true", help="UI 없이 콘솔로 실행(자동 테스트용)")
    ap.add_argument("--mode", choices=["full", "quick"], default="full")
    ap.add_argument("--patient", default="default")
    ap.add_argument("--cycles", type=int, default=0)
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--speedup", type=float, default=40.0, help="헤드리스 가상 시간 배속")
    args = ap.parse_args()

    sim = True if args.sim else False if args.real else config_module.SIMULATION_MODE
    cfg = make_config(sim)
    if args.headless:
        if not cfg.simulation:
            print("헤드리스 모드는 --sim 에서만 지원합니다.")
            return 2
        return run_headless(args, cfg)

    from ui import run_app
    return run_app(cfg)


if __name__ == "__main__":
    sys.exit(main())
