"""
simulation.py - 가상 환자 모델

실제 환자 없이 알고리즘/UI/안전 기능을 검증하기 위한 모델입니다.
  * 환자는 손가락 위치(x)에 따라 서로 다른 '속도 허용치'를 가집니다 (양 끝 범위일수록 낮음).
  * 허용 속도를 넘어서면 통증 발생 위험률(hazard)이 시그모이드로 증가합니다.
  * 안전하게 완료한 사이클이 쌓이면 허용치가 서서히 상승합니다 (재활 효과).

이 모델은 '알고리즘 비교용 가정'일 뿐이며 임상 데이터가 아닙니다.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


class SimulatedPatient:
    HAZARD_GAIN = 3.0       # [1/s] 허용치를 충분히 넘었을 때의 통증 발생률
    SHARPNESS = 4.0         # [deg/s] 시그모이드 기울기
    ADAPT_PER_SAFE_CYCLE = 0.0015

    def __init__(self, tolerance: float = 55.0, rng: Optional[np.random.Generator] = None):
        self.base_tolerance = float(tolerance)
        self.rng = rng or np.random.default_rng()

    # ---- 모델 ----------------------------------------------------------
    def tolerance(self, x: float) -> float:
        """손가락 위치 x(0=완전 폄, 1=완전 굽힘)에서의 속도 허용치 [deg/s]"""
        edge = max(0.0, abs(x - 0.5) - 0.1) / 0.4          # 0~1, 양 끝에서 1
        return self.base_tolerance * (1.0 - 0.40 * min(1.0, edge))

    def hazard(self, speed: float, x: float) -> float:
        return self.HAZARD_GAIN * _sigmoid((speed - self.tolerance(x)) / self.SHARPNESS)

    def expected_pain(self, speed: float, x: float, duration_s: float) -> float:
        """속도 speed 로 duration_s 동안 구동 시 통증이 한 번이라도 발생할 확률"""
        return 1.0 - math.exp(-self.hazard(speed, x) * duration_s)

    def sample_pain(self, speed: float, x: float, dt: float) -> int:
        """한 제어 tick 동안의 통증 입력 (0:없음, 1:뻐근함, 2:고통)"""
        if speed < 1.0:
            return 0
        if self.rng.random() >= 1.0 - math.exp(-self.hazard(speed, x) * dt):
            return 0
        excess = max(0.0, speed - self.tolerance(x))
        p_severe = min(1.0, excess / 25.0) * 0.7
        return 2 if self.rng.random() < p_severe else 1

    def adapt(self, safe_cycle: bool) -> None:
        if safe_cycle:
            self.base_tolerance *= 1.0 + self.ADAPT_PER_SAFE_CYCLE

    # ---- 분석용 오라클 ---------------------------------------------------
    @staticmethod
    def context_x(phase: int, zone: int, zones: int) -> float:
        """구간(phase, zone)의 대표 손가락 위치 (오라클 계산용 근사)"""
        frac = (zone + 0.5) / zones
        return 0.4 + 0.6 * frac if phase == 0 else 0.4 - 0.4 * frac

    def oracle_arm(self, phase: int, zone: int, zones: int, arms, zone_travel_deg: float,
                   eps: float) -> int:
        """허용 통증확률 eps 이하에서 가장 빠른 속도 후보의 인덱스 (정답 기준)"""
        x = self.context_x(phase, zone, zones)
        best = 0
        for k, v in enumerate(arms):
            if self.expected_pain(v, x, zone_travel_deg / v) <= eps:
                best = k
        return best
