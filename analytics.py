"""
analytics.py - 치료/강화학습 분석 리포트 (그래프 9종 + 엑셀 + JSON)

대시보드 구성
  행1  A 가동범위·안전점수 추이 | B 구간별 선택 속도(사이클별) | C 통증확률 사후분포(구간×속도)
  행2  D 누적 통증/안전 통과   | E 학습 곡선(속도·불확실성)  | F 탐색 vs 활용 비율
  행3  G 통증 발생 위치 분포   | H 구간 결과 비율            | I 핵심 지표 요약
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from matplotlib import rcParams
from matplotlib.figure import Figure

from records import PHASE_NAMES, SessionLog

from matplotlib import font_manager as _fm

_avail = {f.name for f in _fm.fontManager.ttflist}
rcParams["font.family"] = [n for n in ("Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR") if n in _avail] + ["DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

C_BLUE, C_TEAL, C_ORANGE, C_RED, C_GREY, C_GREEN = "#1f6feb", "#12a594", "#f59e0b", "#dc2626", "#64748b", "#16a34a"
BG, PANEL = "#f5f7fa", "#ffffff"


def ctx_labels(zones: int) -> List[str]:
    return [f"굽힘 {z+1}" for z in range(zones)] + [f"신전 {z+1}" for z in range(zones)]


def decisions_df(log: SessionLog) -> pd.DataFrame:
    df = pd.DataFrame(log.decisions)
    if df.empty:
        return df
    df["phase_name"] = df["phase"].map(PHASE_NAMES)
    return df


def _style(ax, title: str) -> None:
    ax.set_title(title, fontsize=11, fontweight="bold", color="#0f172a", loc="left")
    ax.set_facecolor(PANEL)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.grid(True, alpha=0.25, linestyle="--")


def build_dashboard(log: SessionLog, fig: Optional[Figure] = None, compact: bool = False) -> Figure:
    fig = fig or Figure(figsize=(17, 11.5))
    fig.clear()
    fig.patch.set_facecolor(BG)
    cyc = pd.DataFrame(log.cycles)
    dec = decisions_df(log)
    Z = log.zones
    labels = ctx_labels(Z)
    gs = fig.add_gridspec(3, 3, hspace=0.42, wspace=0.28, left=0.055, right=0.985, top=0.94, bottom=0.05)

    if cyc.empty:
        ax = fig.add_subplot(gs[:, :])
        ax.axis("off")
        ax.text(0.5, 0.5, "분석할 치료 데이터가 없습니다", ha="center", va="center", fontsize=16, color=C_GREY)
        return fig

    n = len(cyc)
    x = cyc["cycle"].values

    # A. 가동범위 / 안전점수
    ax = fig.add_subplot(gs[0, 0]); _style(ax, "A. 가동범위 확장과 안전 점수")
    ax.plot(x, cyc["rom_frac"] * 100, color=C_BLUE, lw=2.2, label="목표 가동범위(%)")
    ax.fill_between(x, cyc["rom_frac"] * 100, color=C_BLUE, alpha=0.12)
    ax.set_xlabel("사이클"); ax.set_ylabel("가동범위 (%)", color=C_BLUE)
    ax2 = ax.twinx(); ax2.plot(x, cyc["safety_end"], color=C_ORANGE, lw=2, ls="--", label="안전 점수")
    ax2.set_ylim(0, 1.05); ax2.set_ylabel("안전 점수", color=C_ORANGE)
    for sp in ("top",): ax2.spines[sp].set_visible(False)
    bad = cyc[cyc["outcome"] != "OK"]
    if not bad.empty:
        ax.scatter(bad["cycle"], bad["rom_frac"] * 100, color=C_RED, zorder=5, s=36, label="고통 발생")
    ax.legend(loc="lower right", fontsize=8, frameon=False)

    # B. 선택 속도 히트맵 (구간 x 사이클)
    ax = fig.add_subplot(gs[0, 1]); _style(ax, "B. 구간별 선택 속도 (deg/s)"); ax.grid(False)
    if not dec.empty:
        piv = dec.groupby(["ctx", "cycle"])["speed"].mean().unstack("cycle").reindex(range(2 * Z))
        im = ax.imshow(piv.values, aspect="auto", cmap="viridis", origin="upper",
                       extent=[piv.columns.min() - 0.5, piv.columns.max() + 0.5, 2 * Z - 0.5, -0.5])
        ax.set_yticks(range(2 * Z)); ax.set_yticklabels(labels, fontsize=8)
        ax.set_xlabel("사이클")
        fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
        ax.axhline(Z - 0.5, color="white", lw=1.5)

    # C. 통증확률 사후분포
    ax = fig.add_subplot(gs[0, 2]); _style(ax, "C. 통증 확률 추정 (최종 사후평균)"); ax.grid(False)
    snap = log.snapshots[-1]
    pm = np.array(snap["pain_mean"])
    im = ax.imshow(pm, aspect="auto", cmap="RdYlGn_r", vmin=0, vmax=0.6)
    ax.set_xticks(range(len(log.arms))); ax.set_xticklabels([f"{a:.0f}" for a in log.arms])
    ax.set_yticks(range(2 * Z)); ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("속도 후보 (deg/s)")
    for i in range(pm.shape[0]):
        for j in range(pm.shape[1]):
            ax.text(j, i, f"{pm[i, j]:.2f}", ha="center", va="center", fontsize=8, color="black")
        cert = snap["certified"][i]
        if cert >= 0:
            ax.scatter(cert, i, marker="s", s=170, facecolors="none", edgecolors="black", linewidths=1.6)
    if log.oracle is not None:
        for i, o in enumerate(log.oracle):
            ax.scatter(o - 0.36, i - 0.28, marker="*", s=70, color="#1d4ed8", zorder=6)
    ax.set_title("C. 통증 확률 추정 (□ 검증된 속도" + (" / ★ 정답)" if log.oracle is not None else ")"),
                 fontsize=11, fontweight="bold", loc="left", color="#0f172a")
    fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)

    # D. 누적 통증/안전
    ax = fig.add_subplot(gs[1, 0]); _style(ax, "D. 누적 통증 · 안전 통과")
    if not dec.empty:
        g = dec.groupby("cycle")["outcome"]
        p1 = g.apply(lambda s: (s == 1).sum()).reindex(x, fill_value=0).cumsum()
        p2 = g.apply(lambda s: (s == 2).sum()).reindex(x, fill_value=0).cumsum()
        ok = g.apply(lambda s: (s == 0).sum()).reindex(x, fill_value=0).cumsum()
        ax.plot(x, p1, color=C_ORANGE, lw=2, label="뻐근함(1)"); ax.plot(x, p2, color=C_RED, lw=2, label="고통(2)")
        ax.set_ylabel("누적 통증 횟수"); ax.set_xlabel("사이클")
        axr = ax.twinx(); axr.plot(x, ok, color=C_GREEN, lw=2, alpha=0.8, label="무통증 구간 통과")
        axr.set_ylabel("누적 안전 통과", color=C_GREEN); axr.spines["top"].set_visible(False)
        h1, l1 = ax.get_legend_handles_labels(); h2, l2 = axr.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=8, frameon=False, loc="upper left")

    # E. 학습 곡선
    ax = fig.add_subplot(gs[1, 1]); _style(ax, "E. 학습 곡선 (평균 속도 · 불확실성)")
    ax.plot(x, cyc["mean_speed"], color=C_BLUE, lw=2.2, label="선택 속도 평균")
    if log.oracle is not None:
        orc = float(np.mean([log.arms[i] for i in log.oracle]))
        ax.axhline(orc, color="#1d4ed8", ls=":", lw=1.8, label=f"정답 평균 {orc:.0f}")
    ax.set_xlabel("사이클"); ax.set_ylabel("속도 (deg/s)", color=C_BLUE)
    unc = [float(np.mean(s["pain_std"])) for s in log.snapshots]
    axr = ax.twinx(); axr.plot(x, unc, color=C_TEAL, lw=2, ls="--", label="추정 불확실성(σ)")
    axr.set_ylabel("사후분포 표준편차 평균", color=C_TEAL); axr.spines["top"].set_visible(False)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = axr.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=8, frameon=False, loc="upper right")

    # F. 탐색 vs 활용
    ax = fig.add_subplot(gs[1, 2]); _style(ax, "F. 탐색 비율 · 검증 속도 도달")
    if not dec.empty:
        expl = dec.groupby("cycle")["explore"].mean().reindex(x, fill_value=0) * 100
        ax.bar(x, expl, color=C_TEAL, alpha=0.7, label="탐색 선택 비율(%)")
        ax.set_ylabel("탐색 비율 (%)"); ax.set_xlabel("사이클"); ax.set_ylim(0, 135)
        certs = [np.mean([log.arms[c] if c >= 0 else 0 for c in s["certified"]]) for s in log.snapshots]
        axr = ax.twinx(); axr.plot(x, certs, color=C_BLUE, lw=2.2, label="검증된 속도 평균")
        axr.set_ylabel("검증된 속도 (deg/s)", color=C_BLUE); axr.spines["top"].set_visible(False)
        h1, l1 = ax.get_legend_handles_labels(); h2, l2 = axr.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=8, frameon=False, loc="upper left", ncol=2)

    # G. 통증 위치
    ax = fig.add_subplot(gs[2, 0]); _style(ax, "G. 통증 발생 위치 (손가락 위치 %)")
    pe = pd.DataFrame(log.pain_events)
    bins = np.linspace(-10, 110, 13)
    if not pe.empty:
        ax.hist([pe[pe.level == 1]["pct"], pe[pe.level == 2]["pct"]], bins=bins, stacked=True,
                color=[C_ORANGE, C_RED], label=["뻐근함(1)", "고통(2)"])
        ax.legend(fontsize=8, frameon=False)
    else:
        ax.text(0.5, 0.5, "통증 입력 없음", ha="center", va="center", transform=ax.transAxes, color=C_GREY)
    ax.set_xlabel("0% 완전 폄  ↔  100% 완전 굽힘"); ax.set_ylabel("횟수")

    # H. 구간 결과
    ax = fig.add_subplot(gs[2, 1]); _style(ax, "H. 구간 통과 결과"); ax.grid(False)
    if not dec.empty:
        cnt = [(dec.outcome == 0).sum(), (dec.outcome == 1).sum(), (dec.outcome == 2).sum()]
        cols = [C_GREEN, C_ORANGE, C_RED]
        labs = ["안전", "뻐근함", "고통"]
        keep = [i for i, c in enumerate(cnt) if c > 0]
        total = sum(cnt)
        wedges, _t = ax.pie([cnt[i] for i in keep], colors=[cols[i] for i in keep], startangle=90,
                            wedgeprops=dict(width=0.42, edgecolor="white"))
        ax.legend(wedges, [f"{labs[i]}  {cnt[i]}회 ({100*cnt[i]/total:.1f}%)" for i in keep], loc="upper center",
                  bbox_to_anchor=(0.5, 0.02), fontsize=9, frameon=False)

    # I. 요약
    ax = fig.add_subplot(gs[2, 2]); ax.axis("off")
    s = log.summary()
    rows = [("환자 ID", s["patient_id"]), ("모드", "가상(시뮬레이션)" if s["simulated"] else "실제 로봇"),
            ("시작 모드", "전체 캘리브레이션" if s["mode"] == "full" else "빠른 시작"),
            ("치료 시간", f"{s['duration_s']/60:.1f} 분"), ("사이클(성공/전체)", f"{s['cycles_ok']} / {s['cycles_total']}"),
            ("통증 뻐근(1) / 고통(2)", f"{s['pain1']} / {s['pain2']}"), ("평균 속도", f"{s['mean_speed']:.1f} deg/s"),
            ("최종 가동범위", f"{s['final_rom_pct']:.0f} %"), ("최종 안전점수", f"{s['final_safety']:.2f}"),
            ("종료 사유", s["end_reason"] or "-")]
    ax.set_title("I. 핵심 지표", fontsize=11, fontweight="bold", loc="left", color="#0f172a")
    for i, (k, v) in enumerate(rows):
        y = 0.92 - i * 0.092
        ax.text(0.02, y, k, fontsize=10, color=C_GREY, transform=ax.transAxes)
        ax.text(0.98, y, str(v), fontsize=10.5, fontweight="bold", ha="right", transform=ax.transAxes, color="#0f172a")
        ax.plot([0.02, 0.98], [y - 0.025] * 2, color="#e2e8f0", lw=0.8, transform=ax.transAxes)

    fig.suptitle(f"치료 결과 및 강화학습 분석 리포트 - {log.patient_id}  ({log.started})", fontsize=14,
                 fontweight="bold", color="#0f172a", x=0.055, ha="left", y=0.985)
    return fig


def save_report(log: SessionLog, out_dir: Path) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: Dict[str, Path] = {}

    fig = build_dashboard(log)
    png = out_dir / "dashboard.png"
    fig.savefig(png, dpi=130, facecolor=fig.get_facecolor())
    files["png"] = png

    labels = ctx_labels(log.zones)
    summ = log.summary()
    xlsx = out_dir / "rehab_report.xlsx"
    steps = pd.DataFrame(log.steps, columns=["t", "cycle", "phase", "zone", "angle", "speed", "flex_pct", "fsr"])
    dec = decisions_df(log)
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        pd.DataFrame(list(summ.items()), columns=["항목", "값"]).to_excel(w, "Summary", index=False)
        pd.DataFrame(log.cycles).to_excel(w, "Cycles", index=False)
        dec.to_excel(w, "Decisions", index=False)
        pd.DataFrame(log.pain_events).to_excel(w, "PainEvents", index=False)
        if log.snapshots:
            last = log.snapshots[-1]
            pd.DataFrame(last["pain_mean"], index=labels, columns=[f"{a:.0f}" for a in log.arms]).to_excel(w, "PainProb_Final")
            hist = [dict(cycle=s["cycle"], mean_sigma=float(np.mean(s["pain_std"])),
                         mean_pain_prob=float(np.mean(s["pain_mean"])),
                         certified_speed_mean=float(np.mean([log.arms[c] if c >= 0 else 0 for c in s["certified"]])))
                    for s in log.snapshots]
            pd.DataFrame(hist).to_excel(w, "AgentHistory", index=False)
        steps.to_excel(w, "Steps", index=False)
    files["xlsx"] = xlsx

    js = out_dir / "summary.json"
    payload = dict(summary=summ, arms=log.arms, oracle=log.oracle,
                   final_pain_mean=log.snapshots[-1]["pain_mean"] if log.snapshots else None,
                   final_certified=log.snapshots[-1]["certified"] if log.snapshots else None)
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    files["json"] = js
    return files
