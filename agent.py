"""
agent.py - 속도 선택 학습 알고리즘

SafeLadderTS (신규, 기본값)
    '통증 확률 ε 이하에서 가장 빠른 속도'를 찾는 안전 제약 밴딧.
      1) 구간(phase x zone)별, 속도 후보별로 '통증 확률'을 Beta 분포로 추정
      2) 톰슨 샘플링으로 확률을 뽑아 ε 이하인 후보 중 가장 빠른 것을 선택 (탐색 포함)
      3) 사다리(ladder): 검증된(충분히 무통증 통과한) 속도의 바로 위 단계까지만 탐색
      4) 단조성 전파: 속도 k 가 안전하면 더 느린 속도도 안전, 통증이면 더 빠른 속도도 통증
      5) 고통(2) 이후 해당 구간 속도 상한을 낮추고 사이클이 지나며 단계적으로 해제

LinUCBAgent (기존 방식)
    기존 코드의 알고리즘을 그대로 옮긴 것. 벤치마크 비교용으로만 사용합니다.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from config import AgentConfig


class SafeLadderTS:
    def __init__(self, n_contexts: int, arms: Sequence[float], cfg: AgentConfig,
                 rng: Optional[np.random.Generator] = None) -> None:
        self.cfg = cfg
        self.arms = [float(a) for a in arms]
        self.C, self.K = int(n_contexts), len(self.arms)
        self.rng = rng or np.random.default_rng()
        self.s = np.full((self.C, self.K), cfg.prior_safe)     # 안전 통과 횟수(+사전)
        self.f = np.full((self.C, self.K), cfg.prior_pain)     # 통증 횟수(+사전)
        self.cap = np.full(self.C, self.K - 1, dtype=int)      # 구간별 속도 상한
        self.cooldown = np.zeros(self.C, dtype=int)
        self.n_pulls = np.zeros((self.C, self.K), dtype=int)

    # ---- 사후분포 통계 ----------------------------------------------------
    def pain_mean(self) -> np.ndarray:
        return self.f / (self.f + self.s)

    def pain_std(self) -> np.ndarray:
        n = self.f + self.s
        return np.sqrt(self.f * self.s / (n ** 2 * (n + 1)))

    def certified_arm(self, ctx: int) -> int:
        """충분한 증거로 안전이 검증된 가장 빠른 속도 인덱스 (없으면 -1)"""
        c = self.cfg
        best = -1
        for k in range(self.K):
            safe_n = self.s[ctx, k] - c.prior_safe
            mean = self.f[ctx, k] / (self.f[ctx, k] + self.s[ctx, k])
            if safe_n >= c.certify_min_safe and mean <= c.certify_max_mean_pain:
                best = k
        return best

    # ---- 선택/갱신 --------------------------------------------------------
    def select(self, ctx: int, safety_score: float, max_arm: Optional[int] = None) -> Tuple[int, Dict]:
        c = self.cfg
        eps = c.pain_tolerance * max(safety_score, 0.3)
        certified = self.certified_arm(ctx)
        ladder = min(self.K - 1, certified + 1)
        cap = min(ladder, int(self.cap[ctx]))
        if max_arm is not None:
            cap = min(cap, max_arm)
        samples = self.rng.beta(self.f[ctx], self.s[ctx])
        feasible = [k for k in range(cap + 1) if samples[k] <= eps]
        arm = max(feasible) if feasible else 0
        self.n_pulls[ctx, arm] += 1
        info = dict(eps=eps, certified=certified, ladder=ladder, cap=cap,
                    p_sample=float(samples[arm]), p_mean=float(self.pain_mean()[ctx, arm]),
                    explore=bool(arm > certified))
        return arm, info

    def update(self, ctx: int, arm: int, outcome: int) -> None:
        """outcome: 0=무통증 통과, 1=뻐근함, 2=고통"""
        c = self.cfg
        if outcome == 0:
            self.s[ctx, arm] += 1.0
            self.s[ctx, :arm] += c.propagate
        else:
            w = 1.0 if outcome == 1 else 2.0
            self.f[ctx, arm] += w
            self.f[ctx, arm + 1:] += c.propagate * w
            if outcome == 2:
                self.cap[ctx] = max(0, arm - 1)
                self.cooldown[ctx] = c.cooldown_cycles

    def end_cycle(self) -> None:
        """사이클 종료 시: 상한 단계적 해제 + 과거 증거 할인"""
        c = self.cfg
        for ctx in range(self.C):
            if self.cap[ctx] < self.K - 1:
                self.cooldown[ctx] -= 1
                if self.cooldown[ctx] <= 0:
                    self.cap[ctx] += 1
                    self.cooldown[ctx] = c.cooldown_cycles
        self.s = c.prior_safe + c.discount * (self.s - c.prior_safe)
        self.f = c.prior_pain + c.discount * (self.f - c.prior_pain)

    # ---- 저장/복원 --------------------------------------------------------
    def to_dict(self) -> Dict:
        return dict(type="SafeLadderTS", arms=self.arms, s=self.s.tolist(), f=self.f.tolist(),
                    cap=self.cap.tolist(), cooldown=self.cooldown.tolist())

    def load_dict(self, d: Dict) -> bool:
        try:
            if d.get("type") != "SafeLadderTS" or [float(a) for a in d["arms"]] != self.arms:
                return False
            s, f = np.array(d["s"], dtype=float), np.array(d["f"], dtype=float)
            if s.shape != self.s.shape:
                return False
            self.s, self.f = s, f
            self.cap = np.array(d["cap"], dtype=int)
            self.cooldown = np.array(d["cooldown"], dtype=int)
            return True
        except Exception:
            return False


class LinUCBAgent:
    """기존 코드의 LinUCB (비교 벤치마크 전용). 틱마다 행동을 선택하고 사후에 보상을 부여."""

    def __init__(self, n_arms: int, n_features: int = 4, alpha: float = 0.2) -> None:
        self.n_arms, self.alpha = n_arms, alpha
        self.A = [np.identity(n_features) for _ in range(n_arms)]
        self.b = [np.zeros((n_features, 1)) for _ in range(n_arms)]

    def select_action(self, context: Sequence[float]) -> int:
        x = np.array(context, dtype=float).reshape(-1, 1)
        p = np.zeros(self.n_arms)
        for a in range(self.n_arms):
            A_inv = np.linalg.inv(self.A[a])
            theta = A_inv @ self.b[a]
            p[a] = (theta.T @ x)[0, 0] + self.alpha * np.sqrt((x.T @ A_inv @ x)[0, 0])
        return int(np.argmax(p))

    def update(self, action: int, context: Sequence[float], reward: float) -> None:
        x = np.array(context, dtype=float).reshape(-1, 1)
        self.A[action] += x @ x.T
        self.b[action] += reward * x
