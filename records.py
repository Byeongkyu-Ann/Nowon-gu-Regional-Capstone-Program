"""records.py - 세션 중 수집되는 모든 로그 (분석/리포트의 원천 데이터)"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

PHASE_NAMES = {0: "BEND", 1: "EXTEND"}


@dataclass
class SessionLog:
    patient_id: str
    mode: str
    simulated: bool
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    arms: List[float] = field(default_factory=list)
    zones: int = 4
    # (t, cycle, phase, zone, angle, speed, flex_pct, fsr)
    steps: List[tuple] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    cycles: List[Dict[str, Any]] = field(default_factory=list)
    pain_events: List[Dict[str, Any]] = field(default_factory=list)
    snapshots: List[Dict[str, Any]] = field(default_factory=list)   # 사이클별 사후분포
    oracle: Optional[List[int]] = None                              # (시뮬레이션) 정답 속도 인덱스
    end_reason: str = ""
    estop_source: str = ""
    duration_s: float = 0.0

    @property
    def n_cycles_ok(self) -> int:
        return sum(1 for c in self.cycles if c["outcome"] == "OK")

    def summary(self) -> Dict[str, Any]:
        p1 = sum(1 for e in self.pain_events if e["level"] == 1)
        p2 = sum(1 for e in self.pain_events if e["level"] == 2)
        speeds = [d["speed"] for d in self.decisions]
        return dict(
            patient_id=self.patient_id, mode=self.mode, simulated=self.simulated,
            started=self.started, duration_s=round(self.duration_s, 1),
            cycles_total=len(self.cycles), cycles_ok=self.n_cycles_ok,
            pain1=p1, pain2=p2,
            mean_speed=round(sum(speeds) / len(speeds), 2) if speeds else 0.0,
            final_rom_pct=round(self.cycles[-1]["rom_frac"] * 100, 1) if self.cycles else 0.0,
            final_safety=round(self.cycles[-1]["safety_end"], 3) if self.cycles else 0.0,
            end_reason=self.end_reason, estop_source=self.estop_source)
