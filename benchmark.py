"""강화학습 알고리즘 비교 벤치마크 (하드웨어 불필요, 구간 단위 추상화 시뮬레이션)

비교 대상: 기존 LinUCB / 개선 SafeLadderTS / 고정 속도(최저·중간·최고)
지표: 누적 통증(뻐근함·고통), 오라클 대비 평균 속도, 누적 리그렛
리그렛 = Σ [ max(0, 오라클 속도 - 선택 속도)/오라클 속도 + 통증 페널티(뻐근 0.5, 고통 2.0) ]
실행:  python benchmark.py [--cycles 40] [--seeds 30]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from agent import LinUCBAgent, SafeLadderTS
from dataclasses import replace
from typing import Optional

from config import AgentConfig
from simulation import SimulatedPatient

ARMS = [37.0, 47.0, 57.0, 67.0]
ZONES = 4
ZONE_DEG = 45.0 / ZONES
PEN1, PEN2 = 0.5, 2.0


def _outcome(pat: SimulatedPatient, speed: float, x: float) -> int:
    dur = ZONE_DEG / speed
    if pat.rng.random() >= pat.expected_pain(speed, x, dur):
        return 0
    excess = max(0.0, speed - pat.tolerance(x))
    return 2 if pat.rng.random() < min(1.0, excess / 25.0) * 0.7 else 1


class NoLadderTS(SafeLadderTS):
    """절제(ablation): 검증된 속도 +1 단계 제한을 제거 (상한은 고통 후 cap 만 유지)"""

    def select(self, ctx, safety_score, max_arm=None):
        c = self.cfg
        eps = c.pain_tolerance * max(safety_score, 0.3)
        cap = int(self.cap[ctx])
        samples = self.rng.beta(self.f[ctx], self.s[ctx])
        feasible = [k for k in range(cap + 1) if samples[k] <= eps]
        arm = max(feasible) if feasible else 0
        self.n_pulls[ctx, arm] += 1
        return arm, {}


def run(policy: str, tol: float, cycles: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    pat = SimulatedPatient(tol, rng)
    cfg = AgentConfig()
    ts: Optional[SafeLadderTS] = None
    if policy == "SafeLadderTS":
        ts = SafeLadderTS(2 * ZONES, ARMS, cfg, rng)
    elif policy == "TS(래더 제한 없음)":
        ts = NoLadderTS(2 * ZONES, ARMS, cfg, rng)
    elif policy == "TS(단조 전파 없음)":
        ts = SafeLadderTS(2 * ZONES, ARMS, replace(cfg, propagate=0.0), rng)
    ucb = LinUCBAgent(len(ARMS)) if policy == "LinUCB(기존)" else None
    safety = 1.0
    rec = dict(p1=0, p2=0, regret=0.0, ratio=[], cum_pain=[], p2_cycle=[])
    for _ in range(cycles):
        cyc_pain = 0
        p2_before = rec["p2"]
        for ph in (0, 1):
            for z in range(ZONES):
                ctx = ph * ZONES + z
                x = pat.context_x(ph, z, ZONES)
                o = pat.oracle_arm(ph, z, ZONES, ARMS, ZONE_DEG, cfg.pain_tolerance)
                if ts:
                    arm, _ = ts.select(ctx, safety)
                elif ucb:
                    feat = [1.0, ph, (z + 0.5) / ZONES, safety]
                    arm = ucb.select_action(feat)
                else:
                    arm = {"고정-최저": 0, "고정-중간": 1, "고정-최고": 3}[policy]
                out = _outcome(pat, ARMS[arm], x)
                if ts:
                    ts.update(ctx, arm, out)
                if ucb:
                    r = ARMS[arm] / ARMS[-1] - (PEN1 if out == 1 else PEN2 if out == 2 else 0.0)
                    ucb.update(arm, feat, r)
                rec["p1"] += out == 1
                rec["p2"] += out == 2
                cyc_pain += out > 0
                rec["regret"] += max(0.0, ARMS[o] - ARMS[arm]) / ARMS[o] + (PEN1 if out == 1 else PEN2 if out == 2 else 0.0)
                rec["ratio"].append(ARMS[arm] / ARMS[o])
                if out == 2:
                    safety = max(0.1, safety * 0.4)
                elif out == 1:
                    safety = max(0.1, safety * 0.8)
        rec["p2_cycle"].append(rec["p2"] - p2_before)
        if cyc_pain == 0:
            safety = min(1.0, safety + 0.05)
        pat.adapt(cyc_pain == 0)
        if ts:
            ts.end_cycle()
        rec["cum_pain"].append(rec["p1"] + rec["p2"])
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=40)
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--out", default="data/benchmark")
    a = ap.parse_args()
    pols = ["SafeLadderTS", "LinUCB(기존)", "TS(래더 제한 없음)", "TS(단조 전파 없음)", "고정-최저", "고정-중간", "고정-최고"]
    tols = [40.0, 55.0, 70.0]
    res = {}
    for p in pols:
        rs = [run(p, t, a.cycles, s) for t in tols for s in range(a.seeds)]
        by_tol = {}
        for t in tols:
            sub = [r for r, (tt, _) in zip(rs, [(t2, s2) for t2 in tols for s2 in range(a.seeds)]) if tt == t]
            by_tol[str(int(t))] = dict(pain1=float(np.mean([r["p1"] for r in sub])), pain2=float(np.mean([r["p2"] for r in sub])),
                                       speed_ratio=float(np.mean([np.mean(r["ratio"]) for r in sub])))
        p2 = np.array([r["p2"] for r in rs], dtype=float)
        res[p] = dict(
            pain1=float(np.mean([r["p1"] for r in rs])), pain2=float(p2.mean()), pain2_sd=float(p2.std(ddof=1)),
            pain2_runs=p2.tolist(),
            speed_ratio=float(np.mean([np.mean(r["ratio"]) for r in rs])),
            regret=float(np.mean([r["regret"] for r in rs])),
            pain2_first10=float(np.mean([sum(r["p2_cycle"][:10]) for r in rs])),
            pain2_last10=float(np.mean([sum(r["p2_cycle"][-10:]) for r in rs])),
            by_tolerance=by_tol,
            cum_pain=np.mean([r["cum_pain"] for r in rs], axis=0).tolist())
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    # 고통(2) 횟수 차이의 95% 부트스트랩 신뢰구간 (SafeLadderTS - LinUCB)
    rng = np.random.default_rng(0)
    x, y = np.array(res["SafeLadderTS"]["pain2_runs"]), np.array(res["LinUCB(기존)"]["pain2_runs"])
    diffs = [rng.choice(x, len(x)).mean() - rng.choice(y, len(y)).mean() for _ in range(5000)]
    res["_bootstrap_pain2_diff_TS_minus_LinUCB"] = dict(mean=float(np.mean(diffs)), ci95=[float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))])
    (out / "benchmark.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"조건: {a.cycles}사이클 x {len(tols)}개 환자 허용치(40/55/70 deg/s) x {a.seeds}시드, 4구간x2방향\n")
    print(f"{'정책':<20}{'뻐근함':>8}{'고통':>8}{'고통SD':>8}{'속도/오라클':>12}{'리그렛':>9}{'고통 1~10':>10}{'고통 31~40':>11}")
    for p in pols:
        r = res[p]
        print(f"{p:<20}{r['pain1']:>8.1f}{r['pain2']:>8.1f}{r['pain2_sd']:>8.1f}{r['speed_ratio']:>12.2f}{r['regret']:>9.1f}{r['pain2_first10']:>10.2f}{r['pain2_last10']:>11.2f}")
    b = res["_bootstrap_pain2_diff_TS_minus_LinUCB"]
    print(f"\n고통 횟수 차이(SafeLadderTS - LinUCB) 평균 {b['mean']:.2f}, 95% CI [{b['ci95'][0]:.2f}, {b['ci95'][1]:.2f}]")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager as fm
        for n in ("Noto Sans CJK KR", "Malgun Gothic", "NanumGothic"):
            if any(f.name == n for f in fm.fontManager.ttflist):
                plt.rcParams["font.family"] = n
                break
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax = plt.subplots(1, 2, figsize=(11, 4))
        for p in pols[:2] + pols[4:]:
            ax[0].plot(res[p]["cum_pain"], label=p)
        ax[0].set_xlabel("사이클"); ax[0].set_ylabel("누적 통증 횟수(평균)"); ax[0].legend(fontsize=8); ax[0].grid(alpha=.3)
        ax[1].scatter([res[p]["pain1"] + res[p]["pain2"] for p in pols], [res[p]["speed_ratio"] for p in pols], s=80)
        for p in pols:
            ax[1].annotate(p, (res[p]["pain1"] + res[p]["pain2"], res[p]["speed_ratio"]), fontsize=9)
        ax[1].set_xlabel("총 통증 횟수"); ax[1].set_ylabel("오라클 대비 평균 속도"); ax[1].grid(alpha=.3)
        fig.tight_layout(); fig.savefig(out / "benchmark.png", dpi=130)
    except Exception as e:
        print("그래프 생략:", e)


if __name__ == "__main__":
    main()
