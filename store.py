"""
store.py - 환자별 치료값(캘리브레이션 + 학습 상태) 저장/불러오기

저장 위치: <data_dir>/profiles/<patient_id>.json
저장 내용 : 모터 한계각, 센서 기준값, 악력, 속도 후보, 안전 점수, 가동범위 비율,
            강화학습 사후분포, 최근 세션 이력
기록이 없으면 config.TherapyConfig 의 보수적 기본값(저속·소범위)으로 시작합니다.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from config import Config


def _safe_id(patient_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_\-]", "_", patient_id.strip()) or "default"


@dataclass
class Profile:
    patient_id: str
    is_default: bool = True            # 저장 기록이 없어 기본값으로 만든 프로필
    calibrated: bool = False           # 센서 캘리브레이션(굽힘/펴짐 기준) 완료 여부
    calibration_time: str = ""
    m1_limit: float = 0.0
    m2_limit: float = 180.0
    flex_open: float = 0.0
    flex_close: float = 1023.0
    avg_fsr: float = 0.0
    initial_k: int = 15
    safety_score: float = 0.8
    rom_frac: float = 0.6
    agent: Dict[str, Any] = field(default_factory=dict)
    sessions: List[Dict[str, Any]] = field(default_factory=list)
    last_end_reason: str = ""
    updated: str = ""

    def age_days(self) -> float:
        if not self.calibration_time:
            return float("inf")
        return (datetime.now() - datetime.fromisoformat(self.calibration_time)).total_seconds() / 86400.0


class ProfileStore:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.dir = cfg.data_dir / "profiles"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, patient_id: str) -> Path:
        return self.dir / f"{_safe_id(patient_id)}.json"

    def exists(self, patient_id: str) -> bool:
        return self.path(patient_id).exists()

    def default_profile(self, patient_id: str) -> Profile:
        t, m = self.cfg.therapy, self.cfg.motion
        return Profile(
            patient_id=patient_id, is_default=True, calibrated=False,
            m1_limit=m.m1_init + t.default_m1_travel,
            m2_limit=m.m2_init - t.default_m2_travel,
            initial_k=t.default_initial_k, safety_score=t.safety_init, rom_frac=t.default_rom)

    def load(self, patient_id: str) -> Profile:
        p = self.path(patient_id)
        if not p.exists():
            return self.default_profile(patient_id)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in Profile.__dataclass_fields__}
            prof = Profile(**known)
            prof.is_default = False
            return prof
        except Exception:
            # 손상된 파일은 보존하고 기본값으로 시작
            p.rename(p.with_suffix(f".corrupt-{datetime.now():%Y%m%d%H%M%S}.json"))
            return self.default_profile(patient_id)

    def save(self, prof: Profile) -> Path:
        prof.updated = datetime.now().isoformat(timespec="seconds")
        prof.is_default = False
        prof.sessions = prof.sessions[-50:]
        path = self.path(prof.patient_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(prof), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)     # 저장 도중 전원이 꺼져도 기존 파일이 깨지지 않도록 원자적 교체
        return path

    def list_patients(self) -> List[str]:
        return sorted(p.stem for p in self.dir.glob("*.json") if ".corrupt" not in p.name)
