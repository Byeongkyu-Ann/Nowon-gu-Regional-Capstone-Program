"""
hardware.py - 하드웨어 추상화 계층

    HardwareInterface   : 세션 로직이 사용하는 공통 인터페이스
    SerialHardware      : 아두이노 시리얼 통신 (실제 로봇)
    SimulatedHardware   : 가상 로봇 + 가상 환자 (SIMULATION_MODE = True)

시리얼 프로토콜 (firmware/rehab_controller.ino 참고)
  PC -> 아두이노
      M<번호>,<각도>,<속도>   모터 구동 (기존 코드와 동일)
      STOP                    모든 모터 즉시 정지
      BUZ,<ms>                부저 경보 (ms 동안 비프 패턴)
      HB                      생존 신호 (통신 두절 감시용)
  아두이노 -> PC
      DATA,<flex>,<fsr>       센서값 (기존 코드와 동일)
      EVT,ESTOP               하드웨어 비상정지 버튼이 눌림
"""
from __future__ import annotations

import abc
import math
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except Exception:  # pyserial 미설치 환경(시뮬레이션만 사용)에서도 import 가능하도록
    serial = None  # type: ignore

from config import Config
from simulation import SimulatedPatient


class HardwareInterface(abc.ABC):
    label = "unknown"

    def __init__(self) -> None:
        self.connected = False
        self.on_estop: Optional[Callable[[str], None]] = None   # 하드웨어 비상정지 버튼
        self.on_pain: Optional[Callable[[int], None]] = None    # (시뮬레이션) 가상 환자의 통증 입력

    @abc.abstractmethod
    def open(self) -> bool: ...
    @abc.abstractmethod
    def close(self) -> None: ...
    @abc.abstractmethod
    def read_sensors(self) -> Tuple[int, int]: ...
    @abc.abstractmethod
    def send_motor(self, motor: int, angle: float, speed: float) -> None: ...
    @abc.abstractmethod
    def stop_motors(self) -> None: ...
    @abc.abstractmethod
    def buzzer(self, ms: int) -> None: ...

    # --- 선택 기능 (기본 구현은 no-op) ---
    def advance(self, dt: float) -> None:
        """가상 시간을 dt 만큼 진행 (시뮬레이션에서만 의미 있음)"""

    def rx_age(self) -> float:
        """마지막 수신 후 경과 시간 [s]. 실제 하드웨어만 의미 있음"""
        return 0.0

    def calibration_hint(self, motor: int, angle: float) -> bool:
        """시뮬레이션이 '이제 손가락이 한계에 도달했다'고 알려주는 훅 (실제 하드웨어는 항상 False)"""
        return False

    def hint(self, name: str, value: bool = True) -> None:
        """시뮬레이션 진행 보조 신호 (grip, therapy 등). 실제 하드웨어는 무시"""


# =============================================================================
# 실제 하드웨어
# =============================================================================
class SerialHardware(HardwareInterface):
    label = "아두이노 (시리얼)"

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self._ser = None
        self._lock = threading.Lock()
        self._flex, self._fsr = 512, 0
        self._last_rx = 0.0
        self._running = False
        self._threads: List[threading.Thread] = []
        self.error: str = ""

    # --- 연결 ---
    def _resolve_port(self) -> Optional[str]:
        sc = self.cfg.serial
        if serial is None:
            return None
        ports = list(serial.tools.list_ports.comports())
        names = [p.device for p in ports]
        if sc.port in names:
            return sc.port
        if sc.auto_detect:
            for p in ports:
                desc = f"{p.description} {p.manufacturer}".lower()
                if any(k in desc for k in ("arduino", "ch340", "usb serial", "usb-serial", "cp210")):
                    return p.device
            if len(ports) == 1:
                return ports[0].device
        return sc.port  # 마지막 시도: 설정값 그대로

    def open(self) -> bool:
        if serial is None:
            self.error = "pyserial 이 설치되어 있지 않습니다 (pip install pyserial)"
            return False
        port = self._resolve_port()
        try:
            self._ser = serial.Serial(port, self.cfg.serial.baud, timeout=self.cfg.serial.timeout_s)
            time.sleep(self.cfg.serial.boot_wait_s)
            self._ser.reset_input_buffer()
        except Exception as exc:
            self.error = f"{port} 연결 실패: {exc}"
            self.connected = False
            return False
        self.connected = True
        self._running = True
        self._last_rx = time.monotonic()
        for target in (self._reader, self._heartbeat):
            th = threading.Thread(target=target, daemon=True)
            th.start()
            self._threads.append(th)
        return True

    def close(self) -> None:
        self._running = False
        try:
            if self._ser and self._ser.is_open:
                self.stop_motors()
                self._ser.close()
        except Exception:
            pass
        self.connected = False

    # --- 수신/송신 ---
    def _write(self, text: str) -> None:
        if not self.connected or not self._ser:
            return
        try:
            with self._lock:
                self._ser.write(text.encode("utf-8"))
        except Exception as exc:
            self.error = f"송신 오류: {exc}"
            self.connected = False

    def _reader(self) -> None:
        while self._running:
            try:
                line = self._ser.readline().decode("utf-8", errors="ignore").strip()
            except Exception as exc:
                self.error = f"수신 오류: {exc}"
                self.connected = False
                return
            if not line:
                continue
            self._last_rx = time.monotonic()
            if line.startswith("DATA,"):
                parts = line.split(",")
                try:
                    self._flex, self._fsr = int(parts[1]), int(parts[2])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("EVT,ESTOP") or line.startswith("BTN,1"):
                if self.on_estop:
                    self.on_estop("하드웨어 비상정지 버튼")

    def _heartbeat(self) -> None:
        while self._running:
            self._write("HB\n")
            time.sleep(self.cfg.serial.heartbeat_period_s)

    def read_sensors(self) -> Tuple[int, int]:
        return self._flex, self._fsr

    def send_motor(self, motor: int, angle: float, speed: float) -> None:
        self._write(f"M{int(motor)},{int(round(angle))},{int(round(speed))}\n")

    def stop_motors(self) -> None:
        self._write("STOP\n")

    def buzzer(self, ms: int) -> None:
        self._write(f"BUZ,{int(ms)}\n")

    def rx_age(self) -> float:
        return time.monotonic() - self._last_rx if self.connected else 1e9


# =============================================================================
# 가상 하드웨어
# =============================================================================
class SimulatedHardware(HardwareInterface):
    """서보 슬루레이트 + 환자 손가락 + 센서 노이즈를 모사하는 가상 로봇"""
    label = "가상 로봇 (시뮬레이션)"

    # 가상 손가락 기구 파라미터
    BASE_TONE = 0.40        # 모터가 풀린 상태에서의 손가락 위치 (경직으로 약간 굽혀진 상태)
    R1 = 100.0              # 1번 모터 각도 -> 굽힘 변환 [deg per full range]
    R2 = 90.0               # 2번 모터 각도 -> 신전 변환
    FLEX_OPEN, FLEX_CLOSE = 210.0, 760.0
    FSR_GRIP = 520.0

    def __init__(self, cfg: Config, patient: Optional[SimulatedPatient] = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.sim.seed)
        self.patient = patient or SimulatedPatient(rng=self.rng)
        self.target = {1: float(cfg.motion.m1_init), 2: float(cfg.motion.m2_init)}
        self.angle = dict(self.target)
        self.slew = {1: 20.0, 2: 20.0}
        self.x = self.BASE_TONE
        self._flex = self.FLEX_OPEN
        self._fsr = 0.0
        self.grip_active = False
        self.therapy_active = False
        self.phase_active = False
        self._refractory = 0.0
        self._vel = 0.0
        self.buzzer_log: List[Tuple[float, int]] = []
        self.buzzer_until = 0.0
        self.sim_time = 0.0
        self.motors_stopped = False

    def open(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    # --- 명령 ---
    def send_motor(self, motor: int, angle: float, speed: float) -> None:
        self.motors_stopped = False
        self.target[motor] = float(angle)
        self.slew[motor] = max(1.0, float(speed))

    def stop_motors(self) -> None:
        self.motors_stopped = True
        self.target = dict(self.angle)

    def buzzer(self, ms: int) -> None:
        self.buzzer_log.append((self.sim_time, int(ms)))
        self.buzzer_until = self.sim_time + ms / 1000.0

    @property
    def buzzer_on(self) -> bool:
        return self.sim_time < self.buzzer_until

    def press_physical_estop(self) -> None:
        """UI 의 '하드웨어 버튼 시뮬레이션' 에서 호출 - 실제 버튼과 같은 경로로 처리"""
        if self.on_estop:
            self.on_estop("하드웨어 비상정지 버튼")

    # --- 물리 ---
    def advance(self, dt: float) -> None:
        self.sim_time += dt
        moved = 0.0
        for m in (1, 2):
            diff = self.target[m] - self.angle[m]
            step = max(-self.slew[m] * dt, min(self.slew[m] * dt, diff))
            self.angle[m] += step
            moved = max(moved, abs(step) / dt)
        self._vel = moved

        x_t = self.BASE_TONE + (self.angle[1] - self.cfg.motion.m1_init) / self.R1 \
            - (self.cfg.motion.m2_init - self.angle[2]) / self.R2
        x_t = min(1.0, max(0.0, x_t))
        self.x += (x_t - self.x) * (1.0 - math.exp(-dt / 0.25))

        span = self.FLEX_CLOSE - self.FLEX_OPEN
        self._flex = self.FLEX_OPEN + self.x * span + self.rng.normal(0, 2.0)
        fsr_t = self.FSR_GRIP if self.grip_active else 0.0
        self._fsr += (fsr_t - self._fsr) * (1.0 - math.exp(-dt / 0.4))

        # 가상 환자의 통증 입력
        self._refractory = max(0.0, self._refractory - dt)
        if self.therapy_active and self.phase_active and self.cfg.sim.auto_patient and self._refractory <= 0.0 and not self.motors_stopped:
            level = self.patient.sample_pain(self._vel, self.x, dt)
            if level and self.on_pain:
                self._refractory = 1.0
                self.on_pain(level)

    def read_sensors(self) -> Tuple[int, int]:
        return int(self._flex), int(max(0.0, self._fsr + self.rng.normal(0, 3.0)))

    def calibration_hint(self, motor: int, angle: float) -> bool:
        if motor == 1:
            return angle - self.cfg.motion.m1_init >= (1.0 - self.BASE_TONE) * self.R1
        return self.cfg.motion.m2_init - angle >= self.BASE_TONE * self.R2

    def hint(self, name: str, value: bool = True) -> None:
        if name == "grip":
            self.grip_active = value
        elif name == "therapy":
            self.therapy_active = value
        elif name == "phase":
            self.phase_active = value
        elif name == "cycle_ok":
            self.patient.adapt(True)       # 안전하게 완료한 사이클 -> 환자의 허용 속도가 서서히 상승

    def oracle_arms(self, arms, zones: int, travels, eps: float) -> List[int]:
        """분석용: 가상 환자의 실제 허용치 기준 최적 속도 인덱스 (phase 0 의 구간들, phase 1 의 구간들 순)"""
        return [self.patient.oracle_arm(ph, z, zones, arms, travels[ph] / zones, eps)
                for ph in (0, 1) for z in range(zones)]


def create_hardware(cfg: Config) -> HardwareInterface:
    return SimulatedHardware(cfg) if cfg.simulation else SerialHardware(cfg)
