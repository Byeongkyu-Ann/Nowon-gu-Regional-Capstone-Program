"""
config.py - 시스템 전역 설정

가장 중요한 스위치는 아래의 SIMULATION_MODE 입니다.
    True  : 하드웨어 없이 가상 로봇 + 가상 환자로 전체 기능을 테스트
    False : 아두이노(시리얼)에 연결하여 실제 로봇 구동

명령행 옵션으로도 덮어쓸 수 있습니다.  (python main.py --sim / --real)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

# =============================================================================
# [SWITCH] 테스트 모드 스위치  ->  True 로 바꾸면 하드웨어 없이 가상으로 동작
# =============================================================================
SIMULATION_MODE: bool = False


@dataclass
class SerialConfig:
    port: str = "COM3"            # 포트를 못 찾으면 auto_detect 로 아두이노를 탐색
    baud: int = 115200
    timeout_s: float = 0.05
    auto_detect: bool = True
    boot_wait_s: float = 2.0      # 아두이노 리셋 대기
    heartbeat_period_s: float = 0.5   # PC -> 아두이노 생존 신호 주기
    comm_timeout_s: float = 2.0       # 이 시간 동안 수신이 없으면 통신 두절로 판단


@dataclass
class MotionConfig:
    m1_init: int = 0              # 1번 모터(굴곡/주먹 쥠) 초기 각도
    m2_init: int = 180            # 2번 모터(신전/손 폄) 초기 각도
    control_dt: float = 0.05       # 제어 주기 [s]
    cal_speed: float = 20.0        # 캘리브레이션 구동 속도 [deg/s]
    return_speed: float = 60.0     # 일반 복귀 속도 [deg/s]
    speed_min: int = 15
    speed_max: int = 100
    arm_offsets: Tuple[int, ...] = (0, 10, 20, 30)   # 기존 코드와 동일한 속도 후보 구성
    # 센서/모터 유효성 검사 (캘리브레이션 결과가 비정상이면 중단)
    min_flex_span: int = 60        # ADC 카운트
    min_motor_travel: float = 10.0  # [deg]


@dataclass
class TherapyConfig:
    zones: int = 4                      # 한 구동(phase)을 나누는 구간 수 (구간마다 속도 결정)
    session_duration_s: float = 30 * 60  # 세션 길이
    max_cycles: Optional[int] = None     # None 이면 시간 기준으로만 종료
    rest_between_cycles_s: float = 3.0
    warmup_cycles: int = 2               # 빠른 시작 직후 속도 상한을 낮추는 사이클 수
    warmup_max_arm: int = 1
    # 가동범위(ROM): 캘리브레이션 범위의 rom_start 비율에서 시작하여 사이클마다 확장
    rom_start: float = 0.80
    rom_max: float = 1.00
    rom_step: float = 0.02               # 성공 사이클마다 +2%
    # 베이지안 안전 점수
    safety_init: float = 0.8
    safety_floor: float = 0.1
    safety_pain2_factor: float = 0.4
    safety_pain1_factor: float = 0.8
    safety_recover: float = 0.05
    # 통증 반응
    rollback_speed_ratio: float = 0.3    # 고통(2) 시 현재 속도의 30% 로 복귀
    over_flex_margin_pct: float = 10.0   # 측정 범위를 이만큼 넘으면 구간 종료
    # 기본값(저장된 기록이 없을 때) - 반드시 현장에서 한 번 확인할 것
    default_m1_travel: float = 45.0      # 1번 모터 최대 이동량 [deg]
    default_m2_travel: float = 45.0      # 2번 모터 최대 이동량 [deg]
    default_rom: float = 0.6
    default_initial_k: int = 15


@dataclass
class AgentConfig:
    """안전 제약 톰슨 샘플링(SafeLadderTS) 하이퍼파라미터"""
    pain_tolerance: float = 0.20      # 한 구간 통과 중 통증이 날 허용 확률 상한 (ε)
    prior_safe: float = 1.5           # Beta 사전분포 (안전 횟수)
    prior_pain: float = 0.5           # Beta 사전분포 (통증 횟수)
    certify_min_safe: float = 3.0     # 이 횟수 이상 무통증 통과해야 '검증된 속도'
    certify_max_mean_pain: float = 0.15
    propagate: float = 0.5            # 단조성 가정: 안전→더 느린 속도, 통증→더 빠른 속도에 전파
    cooldown_cycles: int = 3          # 고통(2) 후 해당 구간 속도 상한 해제까지의 사이클 수
    discount: float = 0.99            # 사이클마다 과거 증거 할인 (환자 상태 변화 반영)
    # 보상 (분석/리그렛 계산용)
    reward_pain1: float = -0.5
    reward_pain2: float = -2.0


@dataclass
class HardwareProtocol:
    """아두이노 펌웨어 프로토콜 (firmware/rehab_controller.ino 와 동일해야 함)"""
    buzzer_estop_ms: int = 2500


@dataclass
class SimConfig:
    time_scale: float = 8.0           # 가상 시간 배속 (UI/헤드리스 공통)
    auto_confirm: bool = False        # True 면 캘리브레이션 확인 버튼을 자동으로 누름
    auto_patient: bool = True         # 가상 환자가 스스로 통증 버튼을 누름
    seed: Optional[int] = 7


@dataclass
class UIConfig:
    use_global_hotkeys: bool = True   # pynput 전역 단축키 (창 포커스가 없어도 Space 정지)
    window_title: str = "상지 재활 로봇 제어 시스템"
    font_family: str = "Malgun Gothic"
    live_window_s: float = 20.0


@dataclass
class Config:
    simulation: bool = SIMULATION_MODE
    serial: SerialConfig = field(default_factory=SerialConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    therapy: TherapyConfig = field(default_factory=TherapyConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    hw: HardwareProtocol = field(default_factory=HardwareProtocol)
    sim: SimConfig = field(default_factory=SimConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    data_dir: Path = Path(__file__).resolve().parent / "data"
    profile_max_age_days: int = 14    # 이보다 오래된 캘리브레이션은 빠른 시작 시 경고


def make_config(simulation: Optional[bool] = None) -> Config:
    cfg = Config()
    if simulation is not None:
        cfg.simulation = simulation
    return cfg
