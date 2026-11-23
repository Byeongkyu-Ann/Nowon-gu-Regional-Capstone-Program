"""
session.py - 재활 세션 제어 (상태 머신 + 안전 로직)

진행 모드
  FULL  : 캘리브레이션(모터 한계, 센서 기준, 악력) -> 치료
  QUICK : 저장된 치료값으로 캘리브레이션 없이 바로 치료
          (기록이 없으면 보수적 기본값: 저속 + 소범위 + 기존 센서 기준 없음)

긴급정지(E-STOP) 경로 - 어떤 경로든 동일하게 처리
  1) 키보드 Space (치료 중)      2) UI 긴급정지 버튼
  3) 로봇 본체의 하드웨어 버튼    4) 통신 두절 감시(실제 하드웨어)
  -> 모터 즉시 정지 + 부저 경보 + 안전 위치 복귀 + 리포트 저장
"""
from __future__ import annotations

import enum
import queue
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

import analytics
from agent import SafeLadderTS
from config import Config
from hardware import HardwareInterface
from records import PHASE_NAMES, SessionLog
from store import Profile, ProfileStore


class Stage(enum.Enum):
    IDLE = "대기"
    CALIBRATION = "캘리브레이션"
    READY = "치료 준비"
    THERAPY = "치료 중"
    FINISHED = "종료"
    ABORTED = "중단"


class RunMode(enum.Enum):
    FULL = "full"      # 캘리브레이션 + 치료
    QUICK = "quick"    # 저장값으로 바로 치료


class EmergencyStop(Exception):
    def __init__(self, source: str) -> None:
        super().__init__(source)
        self.source = source


class SessionObserver:
    """UI/콘솔이 구현하는 콜백 (세션 스레드에서 호출되므로 UI 는 시그널로 전달할 것)"""
    def log(self, text: str, level: str = "info") -> None: ...
    def stage(self, stage: Stage) -> None: ...
    def prompt(self, title: str, body: str, button: str, needs_confirm: bool) -> None: ...
    def snapshot(self, snap: Dict[str, Any]) -> None: ...
    def finished(self, info: Dict[str, Any]) -> None: ...


class RehabSession(threading.Thread):
    def __init__(self, cfg: Config, hw: HardwareInterface, store: ProfileStore, patient_id: str,
                 mode: RunMode, observer: Optional[SessionObserver] = None,
                 rng: Optional[np.random.Generator] = None) -> None:
        super().__init__(daemon=True, name="RehabSession")
        self.cfg, self.hw, self.store = cfg, hw, store
        self.patient_id, self.mode = patient_id, mode
        self.obs = observer or SessionObserver()
        self.rng = rng or np.random.default_rng(cfg.sim.seed)

        self.stage = Stage.IDLE
        self.profile: Profile = store.default_profile(patient_id)
        self.agent: Optional[SafeLadderTS] = None
        self.arms: List[float] = []
        self.log = SessionLog(patient_id, mode.value, cfg.simulation, zones=cfg.therapy.zones)

        self._confirm_evt = threading.Event()
        self._estop_evt = threading.Event()
        self._finish_evt = threading.Event()
        self._estop_src = ""
        self._buzzed = False
        self._buzz_end = -1.0
        self._rom_floor = cfg.therapy.rom_start
        self._pain_q: "queue.SimpleQueue[int]" = queue.SimpleQueue()
        self._lock = threading.Lock()

        self.t = 0.0                       # 세션 시간 (시뮬레이션은 가상 시간)
        self._t0 = time.monotonic()
        self._last_real = time.monotonic()
        self._last_pub = 0.0
        self._cmd = {1: float(cfg.motion.m1_init), 2: float(cfg.motion.m2_init)}

        # 실시간 상태
        self.cycle = 0
        self.rom_frac = cfg.therapy.default_rom
        self.safety = cfg.therapy.safety_init
        self.pain1 = self.pain2 = 0
        self.phase_name, self.zone, self.speed, self.arm_idx, self.pct = "IDLE", -1, 0.0, -1, 0.0
        self.angle = 0.0

        hw.on_estop = self.request_estop
        hw.on_pain = self.report_pain

    # ======================================================================
    # 외부(UI/키보드/하드웨어)에서 호출하는 입력 API - 모두 스레드 안전
    # ======================================================================
    def confirm(self) -> None:
        self._confirm_evt.set()

    def report_pain(self, level: int) -> None:
        if self.stage == Stage.THERAPY and level in (1, 2):
            self._pain_q.put(level)

    def request_finish(self) -> None:
        """정상 종료 요청 (현재 사이클 종료 후 결과 저장, 부저 없음)"""
        self._finish_evt.set()

    def request_estop(self, source: str = "UI") -> None:
        """긴급정지. 호출 스레드에서 즉시 모터 정지 + 부저를 보내 지연을 최소화한다."""
        if self.stage not in (Stage.CALIBRATION, Stage.READY, Stage.THERAPY):
            return
        with self._lock:
            if self._estop_evt.is_set():
                return
            self._estop_src = source
            self._estop_evt.set()
        self.hw.stop_motors()
        self._sound_alarm()
        self._confirm_evt.set()      # 대기 중인 루프를 깨운다

    def _sound_alarm(self) -> None:
        if not self._buzzed:
            self._buzzed = True
            self._buzz_end = self.t + self.cfg.hw.buzzer_estop_ms / 1000.0
            self.hw.buzzer(self.cfg.hw.buzzer_estop_ms)

    # ======================================================================
    # 시간/루프 유틸
    # ======================================================================
    def _tick(self, dt: Optional[float] = None, check: bool = True) -> float:
        """한 제어 주기를 진행하고 실제 경과 dt 를 반환. 긴급정지/통신두절이면 예외."""
        cfg = self.cfg
        nominal = dt if dt is not None else cfg.motion.control_dt
        if cfg.simulation:
            self.hw.advance(nominal)
            self.t += nominal
            time.sleep(nominal / cfg.sim.time_scale)
            eff = nominal
        else:
            time.sleep(nominal)
            now = time.monotonic()
            eff = min(0.2, now - self._last_real)
            self._last_real = now
            self.t = now - self._t0
        if check:
            if self._estop_evt.is_set():
                raise EmergencyStop(self._estop_src)
            if (not cfg.simulation and self.stage in (Stage.CALIBRATION, Stage.READY, Stage.THERAPY)
                    and self.hw.rx_age() > cfg.serial.comm_timeout_s):
                self._estop_src = "통신 두절"
                self._estop_evt.set()
                self.hw.stop_motors()
                raise EmergencyStop(self._estop_src)
        self._publish()
        return eff

    def _wait(self, seconds: float, check: bool = True) -> None:
        end = self.t + seconds
        while self.t < end:
            self._tick(check=check)

    def _send(self, motor: int, angle: float, speed: float) -> None:
        if self._estop_evt.is_set():
            return
        self._cmd[motor] = angle
        self.hw.send_motor(motor, angle, speed)

    def _move_and_wait(self, motor: int, target: float, speed: float, check: bool = True) -> None:
        dist = abs(self._cmd[motor] - target)
        self._send(motor, target, speed)
        self._wait(dist / max(speed, 1.0) + 0.4, check=check)

    def _return_home(self, speed: float, check: bool = True) -> None:
        mo = self.cfg.motion
        d = max(abs(self._cmd[1] - mo.m1_init), abs(self._cmd[2] - mo.m2_init))
        self._send(1, mo.m1_init, speed)
        self._send(2, mo.m2_init, speed)
        self._wait(d / max(speed, 1.0) + 0.5, check=check)

    def _say(self, text: str, level: str = "info") -> None:
        self.obs.log(text, level)

    def _set_stage(self, st: Stage) -> None:
        self.stage = st
        self.obs.stage(st)

    def _wait_confirm(self, title: str, body: str, button: str = "다음") -> None:
        self.obs.prompt(title, body, button, True)
        self._confirm_evt.clear()
        auto = self.cfg.simulation and self.cfg.sim.auto_confirm
        n = 0
        while not self._confirm_evt.is_set():
            self._tick(0.02)
            n += 1
            if auto and n > 5:
                break
        if self._estop_evt.is_set():
            raise EmergencyStop(self._estop_src)
        self._confirm_evt.clear()

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_pub < 0.06:
            return
        self._last_pub = now
        flex, fsr = self.hw.read_sensors()
        self.obs.snapshot(dict(
            stage=self.stage, t=self.t, phase=self.phase_name, zone=self.zone, speed=self.speed,
            arm=self.arm_idx, n_arms=len(self.arms), pct=self.pct, flex=flex, fsr=fsr,
            cycle=self.cycle, rom=self.rom_frac, safety=self.safety, pain1=self.pain1, pain2=self.pain2,
            zones=self.cfg.therapy.zones, buzzer=self.t < self._buzz_end,
            remaining=max(0.0, self.cfg.therapy.session_duration_s - (self.t - getattr(self, "_t_therapy0", self.t)))
            if self.stage == Stage.THERAPY else 0.0))

    # ======================================================================
    # 메인 흐름
    # ======================================================================
    def run(self) -> None:
        info: Dict[str, Any] = {}
        try:
            if not self.hw.connected:
                raise RuntimeError("하드웨어가 연결되어 있지 않습니다.")
            self.profile = self.store.load(self.patient_id)
            self._set_stage(Stage.CALIBRATION if self.mode == RunMode.FULL else Stage.READY)
            if self.mode == RunMode.FULL:
                self._calibrate()
            else:
                self._quick_start_checks()
            self._prepare_agent()
            self._set_stage(Stage.READY)
            self._wait_confirm("치료 시작 준비",
                               "손가락 장착 상태와 와이어 장력을 확인한 뒤 [치료 시작]을 누르세요.\n"
                               "치료 중에는 Space, 화면의 긴급정지, 로봇의 비상정지 버튼으로 언제든 정지할 수 있습니다.",
                               "치료 시작")
            self._therapy()
            self.log.end_reason = self.log.end_reason or "COMPLETED"
        except EmergencyStop as e:
            self._handle_estop(e.source)
        except Exception as exc:                      # 예기치 못한 오류도 안전하게 종료
            self._say(f"예기치 못한 오류: {exc}", "error")
            self._say(traceback.format_exc(), "error")
            self._estop_src = f"소프트웨어 오류: {exc}"
            self._handle_estop(self._estop_src, alarm=True)
        finally:
            info = self._finalize()
            self.obs.finished(info)

    # ----------------------------------------------------------------------
    # 캘리브레이션 (기존 3단계 로직 유지)
    # ----------------------------------------------------------------------
    def _pull_until(self, motor: int, direction: int, limit_angle: float) -> float:
        mo = self.cfg.motion
        angle = mo.m1_init if motor == 1 else mo.m2_init
        self._confirm_evt.clear()
        while True:
            if self._confirm_evt.is_set():
                break
            if (direction > 0 and angle >= limit_angle) or (direction < 0 and angle <= limit_angle):
                break
            if self.hw.calibration_hint(motor, angle):
                break
            self._send(motor, angle, mo.cal_speed)
            dt = self._tick()
            angle += direction * mo.cal_speed * dt
        if self._estop_evt.is_set():
            raise EmergencyStop(self._estop_src)
        self._confirm_evt.clear()
        return angle

    def _mean_sensors(self, seconds: float):
        n, fl, fs = 0, 0.0, 0.0
        end = self.t + seconds
        while self.t < end:
            self._tick()
            f, s = self.hw.read_sensors()
            fl, fs, n = fl + f, fs + s, n + 1
        return fl / max(n, 1), fs / max(n, 1)

    def _calibrate(self) -> None:
        mo, prof = self.cfg.motion, self.profile
        self._say("=== 캘리브레이션 시작 ===")
        self._return_home(mo.return_speed)

        self._wait_confirm("1단계 - 모터 가동범위 (손 펴기)",
                           "환자의 손이 완전히 펴지도록 2번 모터가 와이어를 당깁니다.\n"
                           "손이 충분히 펴졌다고 판단되면 [손 다 펴짐]을 누르세요.", "구동 시작")
        self.obs.prompt("손 펴는 중...", "손이 다 펴졌으면 버튼을 누르세요. (Space 가능)", "손 다 펴짐", True)
        m2_limit = self._pull_until(2, -1, 0.0)
        self._return_home(mo.return_speed)

        self._wait_confirm("1단계 - 모터 가동범위 (주먹 쥐기)",
                           "이번에는 1번 모터가 와이어를 당겨 주먹을 쥐게 합니다.", "구동 시작")
        self.obs.prompt("주먹 쥐는 중...", "주먹이 다 쥐어졌으면 버튼을 누르세요. (Space 가능)", "주먹 다 쥐어짐", True)
        m1_limit = self._pull_until(1, +1, 180.0)
        self._return_home(mo.return_speed)

        travel1, travel2 = m1_limit - mo.m1_init, mo.m2_init - m2_limit
        if travel1 < mo.min_motor_travel or travel2 < mo.min_motor_travel:
            raise RuntimeError(f"모터 가동범위가 너무 작습니다 (굴곡 {travel1:.0f}°, 신전 {travel2:.0f}°). 와이어 장력을 확인하고 다시 시도하세요.")
        self._say(f"모터 한계: 굴곡 {travel1:.0f}°, 신전 {travel2:.0f}°")

        self._wait_confirm("2단계 - 센서 기준값", "모터가 한계 위치까지 움직이며 플렉스 센서 기준값을 측정합니다.", "측정 시작")
        self._move_and_wait(2, m2_limit, mo.cal_speed)
        self._wait(1.5)
        flex_open, _ = self._mean_sensors(1.0)
        self._return_home(mo.return_speed)
        self._move_and_wait(1, m1_limit, mo.cal_speed)
        self._wait(1.5)
        flex_close, _ = self._mean_sensors(1.0)
        self._return_home(mo.return_speed)
        if abs(flex_close - flex_open) < mo.min_flex_span:
            raise RuntimeError(f"플렉스 센서 변화량이 너무 작습니다 ({abs(flex_close-flex_open):.0f}). 센서 부착 상태를 확인하세요.")
        self._say(f"플렉스 기준: 폄 {flex_open:.0f} / 굽힘 {flex_close:.0f}")

        self._wait_confirm("3단계 - 근력(악력) 측정", "근력 측정 모듈을 5초 동안 있는 힘껏 쥐게 하세요.", "측정 시작")
        self.hw.hint("grip", True)
        self._say("5초간 힘껏 쥐세요!")
        self._wait(1.0)
        _, avg_fsr = self._mean_sensors(5.0)
        self.hw.hint("grip", False)
        initial_k = max(15, min(80, int((avg_fsr / 1023.0) * 45) + 15))   # 기존 공식 유지
        self._say(f"평균 근력 {avg_fsr:.0f}, 기본 속도 {initial_k}")

        arms_changed = prof.initial_k != initial_k or prof.is_default
        prof.m1_limit, prof.m2_limit = m1_limit, m2_limit
        prof.flex_open, prof.flex_close, prof.avg_fsr, prof.initial_k = flex_open, flex_close, avg_fsr, initial_k
        prof.calibrated = True
        prof.calibration_time = datetime.now().isoformat(timespec="seconds")
        if arms_changed:
            prof.agent = {}
        prof.rom_frac = self.cfg.therapy.rom_start
        self.store.save(prof)
        self._say("캘리브레이션 완료, 치료값을 저장했습니다.")

    # ----------------------------------------------------------------------
    # 빠른 시작
    # ----------------------------------------------------------------------
    def _rom_floor_for(self, prof: Profile) -> float:
        th = self.cfg.therapy
        return th.rom_start if prof.calibrated else th.default_rom

    def _quick_start_checks(self) -> None:
        prof, th = self.profile, self.cfg.therapy
        self._return_home(self.cfg.motion.return_speed)
        if prof.is_default:
            self._wait_confirm(
                "저장된 치료 기록이 없습니다",
                f"보수적 기본값으로 시작합니다.\n- 모터 이동량 굴곡 {th.default_m1_travel:.0f}° / 신전 {th.default_m2_travel:.0f}°\n"
                f"- 최저 속도 {th.default_initial_k}부터 단계적 상승\n- 센서 기준 없음(위치는 모터 각도로 추정)\n"
                "와이어 장력과 손가락 상태를 확인한 뒤 계속하세요.", "기본값으로 진행")
            self._say("기본값 모드: 저속·소범위로 시작합니다.", "warn")
        else:
            age = prof.age_days()
            msg = f"저장된 치료값을 불러왔습니다. (사이클 이력 {len(prof.sessions)}회)"
            if prof.calibrated and age > self.cfg.profile_max_age_days:
                msg += f"\n⚠ 캘리브레이션이 {age:.0f}일 전 기록입니다. 센서/와이어 상태가 달라졌다면 전체 캘리브레이션을 권장합니다."
                self._say(f"캘리브레이션 {age:.0f}일 경과", "warn")
            if prof.last_end_reason.startswith("ESTOP"):
                msg += "\n⚠ 직전 세션이 긴급정지로 종료되어 가동범위를 초기값으로 낮춰 시작합니다."
                prof.rom_frac = self._rom_floor_for(prof)
            self._say(msg.replace("\n", " "))

    # ----------------------------------------------------------------------
    # 학습기 준비
    # ----------------------------------------------------------------------
    def _prepare_agent(self) -> None:
        mo, th, prof = self.cfg.motion, self.cfg.therapy, self.profile
        k = prof.initial_k
        arms = sorted({float(min(mo.speed_max, k + o)) for o in mo.arm_offsets})
        self.arms = arms
        self.agent = SafeLadderTS(2 * th.zones, arms, self.cfg.agent, self.rng)
        restored = bool(prof.agent) and self.agent.load_dict(prof.agent)
        self._say(f"속도 후보 {arms} deg/s / 학습 상태 {'복원' if restored else '초기화'}")
        self._rom_floor = th.rom_start if prof.calibrated else th.default_rom
        if self.mode == RunMode.QUICK and not prof.is_default:
            self.rom_frac = min(th.rom_max, max(self._rom_floor, prof.rom_frac))
        else:
            self.rom_frac = self._rom_floor
        self.safety = prof.safety_score
        self.log.arms = arms
        self.log.zones = th.zones
        if hasattr(self.hw, "oracle_arms"):
            full1 = abs(prof.m1_limit - mo.m1_init)
            full2 = abs(mo.m2_init - prof.m2_limit)
            self.log.oracle = self.hw.oracle_arms(arms, th.zones, (full1, full2), self.cfg.agent.pain_tolerance)

    # ----------------------------------------------------------------------
    # 치료
    # ----------------------------------------------------------------------
    def _pct_from_flex(self, flex: float, fallback: float) -> float:
        prof = self.profile
        if prof.calibrated and abs(prof.flex_close - prof.flex_open) > 1:
            return 100.0 * (flex - prof.flex_open) / (prof.flex_close - prof.flex_open)
        return fallback

    def _therapy(self) -> None:
        th = self.cfg.therapy
        self._set_stage(Stage.THERAPY)
        self.hw.hint("therapy", True)
        self._t_therapy0 = self.t
        self._say("=== 치료 시작 ===")
        while True:
            if self._finish_evt.is_set():
                self.log.end_reason = "OPERATOR_FINISH"
                break
            if self.t - self._t_therapy0 >= th.session_duration_s:
                self.log.end_reason = "TIME_UP"
                break
            if th.max_cycles is not None and self.cycle >= th.max_cycles:
                self.log.end_reason = "MAX_CYCLES"
                break
            self._run_cycle()
            self._wait(th.rest_between_cycles_s)
        self.hw.hint("therapy", False)
        self._return_home(self.cfg.motion.return_speed)

    def _run_cycle(self) -> None:
        th, mo = self.cfg.therapy, self.cfg.motion
        self.cycle += 1
        rec = dict(cycle=self.cycle, t_start=round(self.t, 2), rom_frac=self.rom_frac,
                   safety_start=self.safety, pain1=0, pain2=0, outcome="OK")
        c_p1, c_p2 = self.pain1, self.pain2
        t0 = self.t
        self._say(f"--- Cycle {self.cycle} (가동범위 {self.rom_frac*100:.0f}%, 안전점수 {self.safety:.2f}) ---")
        res = self._run_phase(0)
        if res == "OK":
            self._return_home(mo.return_speed)
            res = self._run_phase(1)
        if res == "OK":
            self._return_home(mo.return_speed)
            self.safety = min(1.0, self.safety + th.safety_recover)
            self.rom_frac = min(th.rom_max, self.rom_frac + th.rom_step)
            self.hw.hint("cycle_ok", True)
            self._say("사이클 완료 (가동범위 +2%)")
        else:
            rec["outcome"] = "PAIN2"
            self.rom_frac = self._rom_floor
            self._say("고통 감지: 안전 위치 복귀, 가동범위를 초기값으로 되돌립니다.", "warn")
        self.agent.end_cycle()
        rec.update(pain1=self.pain1 - c_p1, pain2=self.pain2 - c_p2, safety_end=self.safety,
                   rom_next=self.rom_frac, duration=round(self.t - t0, 2))
        ds = [d["speed"] for d in self.log.decisions if d["cycle"] == self.cycle]
        rec["mean_speed"] = float(np.mean(ds)) if ds else 0.0
        self.log.cycles.append(rec)
        self.log.snapshots.append(dict(cycle=self.cycle, pain_mean=self.agent.pain_mean().tolist(),
                                       pain_std=self.agent.pain_std().tolist(),
                                       certified=[self.agent.certified_arm(c) for c in range(self.agent.C)]))
        self.phase_name, self.zone, self.speed, self.pct = "IDLE", -1, 0.0, 0.0
        self._publish(force=True)

    def _close_zone(self, dec: Optional[Dict[str, Any]], outcome: Optional[int]) -> None:
        if dec is None:
            return
        if dec.get("outcome") is None:        # 통증 없이 구간 통과
            dec["outcome"] = 0
            self.agent.update(dec["ctx"], dec["arm"], 0)
        o = dec["outcome"]
        dec["reward"] = dec["speed"] / self.cfg.motion.speed_max if o == 0 else \
            (self.cfg.agent.reward_pain1 if o == 1 else self.cfg.agent.reward_pain2)

    def _drain_offphase_pain(self) -> None:
        """구동 구간 밖(복귀/휴식 중)에 들어온 통증 입력: 안전 점수에만 반영하고 학습에는 쓰지 않는다."""
        while not self._pain_q.empty():
            lv = self._pain_q.get()
            self.log.pain_events.append(dict(t=round(self.t, 2), cycle=self.cycle, phase=-1, zone=-1,
                                             level=lv, pct=self.pct, speed=0.0))
            self._update_safety(lv)
            if lv == 2:
                self.pain2 += 1
            else:
                self.pain1 += 1
            self._say(f"[구간 외 통증 입력 {lv}] 안전 점수에만 반영", "warn")

    def _run_phase(self, phase: int) -> str:
        try:
            return self._run_phase_inner(phase)
        finally:
            self.hw.hint("phase", False)

    def _run_phase_inner(self, phase: int) -> str:
        cfg, th, mo, prof = self.cfg, self.cfg.therapy, self.cfg.motion, self.profile
        motor = 1 if phase == 0 else 2
        init = mo.m1_init if phase == 0 else mo.m2_init
        full = prof.m1_limit if phase == 0 else prof.m2_limit
        limit = init + (full - init) * self.rom_frac
        direction = 1.0 if limit > init else -1.0
        travel = abs(limit - init)
        angle = init
        zone, dec = -1, None
        self.phase_name = PHASE_NAMES[phase]
        self._drain_offphase_pain()
        self.hw.hint("phase", True)
        max_arm = th.warmup_max_arm if (self.mode == RunMode.QUICK and self.cycle <= th.warmup_cycles) else None

        while True:
            progress = min(1.0, abs(angle - init) / travel)
            new_zone = min(th.zones - 1, int(progress * th.zones))
            if new_zone != zone:
                self._close_zone(dec, None)
                zone = new_zone
                ctx = phase * th.zones + zone
                arm, info = self.agent.select(ctx, self.safety, max_arm)
                self.arm_idx, self.speed, self.zone = arm, self.arms[arm], zone
                dec = dict(cycle=self.cycle, t=round(self.t, 2), phase=phase, zone=zone, ctx=ctx, arm=arm,
                           speed=self.speed, outcome=None, reward=None, **info)
                self.log.decisions.append(dec)

            # --- 통증 입력 처리 ---
            level = 0
            while not self._pain_q.empty():
                level = max(level, self._pain_q.get())
            if level:
                flex_now, _ = self.hw.read_sensors()
                self.log.pain_events.append(dict(t=round(self.t, 2), cycle=self.cycle, phase=phase, zone=zone,
                                                 level=level, pct=self.pct, speed=self.speed))
                if level == 2:
                    self.pain2 += 1
                    self._update_safety(2)
                    self.agent.update(dec["ctx"], dec["arm"], 2)
                    dec["outcome"] = 2
                    self._close_zone(dec, 2)
                    self._say(f"[고통] {PHASE_NAMES[phase]} 구간 {zone+1}: 즉시 복귀", "error")
                    self._rollback(self.speed)
                    return "PAIN2"
                self.pain1 += 1
                self._update_safety(1)
                if dec["outcome"] is None:
                    self.agent.update(dec["ctx"], dec["arm"], 1)
                    dec["outcome"] = 1
                self._say(f"[뻐근함] {PHASE_NAMES[phase]} 구간 {zone+1}: 속도 유지, 학습에 반영", "warn")

            # --- 구동 ---
            self._send(motor, angle, self.speed)
            dt = self._tick()
            flex, fsr = self.hw.read_sensors()
            fallback = 100.0 * progress if phase == 0 else 100.0 * (1.0 - progress)
            self.pct = self._pct_from_flex(flex, fallback)
            self.angle = angle
            self.log.steps.append((round(self.t, 2), self.cycle, phase, zone, round(angle, 1),
                                   self.speed, round(self.pct, 1), fsr))
            angle += direction * self.speed * dt
            if (angle - init) * direction >= travel:
                self._send(motor, limit, self.speed)
                self._close_zone(dec, None)
                break
            if prof.calibrated:                  # 측정 범위를 벗어난 과신전/과굴곡 방지
                if (phase == 0 and self.pct > 100 + th.over_flex_margin_pct) or \
                   (phase == 1 and self.pct < -th.over_flex_margin_pct):
                    self._say("센서 측정 범위를 벗어나 구간을 조기 종료합니다.", "warn")
                    self._close_zone(dec, None)
                    break
        return "OK"

    def _update_safety(self, level: int) -> None:
        th = self.cfg.therapy
        f = th.safety_pain2_factor if level == 2 else th.safety_pain1_factor
        self.safety = max(th.safety_floor, self.safety * f)

    def _rollback(self, current_speed: float) -> None:
        """고통(2): 현재 속도의 30% 로 안전 위치까지 복귀"""
        th, mo = self.cfg.therapy, self.cfg.motion
        sp = max(10.0, current_speed * th.rollback_speed_ratio)
        self._return_home(sp)

    # ----------------------------------------------------------------------
    # 종료 처리
    # ----------------------------------------------------------------------
    def _handle_estop(self, source: str, alarm: bool = False) -> None:
        was_therapy = self.stage == Stage.THERAPY
        self._say(f"■ 긴급정지 ({source})", "error")
        self.hw.stop_motors()
        self._sound_alarm()
        self.log.end_reason = f"ESTOP:{source}"
        self.log.estop_source = source
        try:   # 장력 해제를 위해 안전 위치로 복귀 (복귀 중에는 추가 예외를 발생시키지 않음)
            self._estop_evt.clear()
            self._return_home(40.0, check=False)
        except Exception:
            pass
        self._estop_evt.set()
        self._set_stage(Stage.FINISHED if was_therapy else Stage.ABORTED)

    def _finalize(self) -> Dict[str, Any]:
        self.hw.hint("therapy", False)
        self.hw.hint("grip", False)
        th = self.cfg.therapy
        if self.stage not in (Stage.FINISHED, Stage.ABORTED):
            self._set_stage(Stage.FINISHED if self.log.cycles else Stage.ABORTED)
        self.log.duration_s = self.t - getattr(self, "_t_therapy0", self.t) if self.log.cycles else 0.0

        info: Dict[str, Any] = dict(stage=self.stage, log=self.log, files={}, summary=self.log.summary())
        if self.log.cycles and self.agent is not None:
            prof = self.profile
            prof.agent = self.agent.to_dict()
            prof.safety_score = self.safety
            prof.rom_frac = self._rom_floor if self.log.end_reason.startswith("ESTOP") else self.rom_frac
            prof.last_end_reason = self.log.end_reason
            s = self.log.summary()
            prof.sessions.append(dict(time=self.log.started, cycles=s["cycles_total"], pain1=s["pain1"],
                                      pain2=s["pain2"], end=s["end_reason"], rom=s["final_rom_pct"],
                                      mean_speed=s["mean_speed"]))
            try:
                self.store.save(prof)
                self._say("치료값을 저장했습니다. 다음 세션은 [빠른 시작]으로 이어서 진행할 수 있습니다.")
            except Exception as exc:
                self._say(f"치료값 저장 실패: {exc}", "error")
            try:
                out = self.cfg.data_dir / "reports" / self.patient_id / datetime.now().strftime("%Y%m%d_%H%M%S")
                info["files"] = analytics.save_report(self.log, out)
                info["report_dir"] = out
                self._say(f"리포트 저장: {out}")
            except Exception as exc:
                self._say(f"리포트 저장 실패: {exc}", "error")
        self._publish(force=True)
        return info
