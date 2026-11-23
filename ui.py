"""
ui.py - 임상용 제어 UI (PyQt5)

화면 구성
  [상단바]  시스템명 / 연결·모드 상태 / 시계
  [좌]      환자·진행 모드 선택, 진행 단계 표시
  [중]      안내 카드(현재 해야 할 일) / 손가락 위치 게이지 / 실시간 추이 그래프
  [우]      핵심 지표(사이클·남은 시간·가동범위·안전점수·속도) / 통증 입력 / 긴급정지
  [하]      이벤트 로그
  [결과]    치료 종료 후 분석 리포트(9종 그래프) + 파일 열기

안전 UI 원칙: 긴급정지는 항상 같은 위치에 가장 크게, 모든 버튼은 포커스를 받지 않도록 하여
Space 키가 임의의 버튼을 눌러 버리는 일이 없게 한다.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                             QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSizePolicy,
                             QShortcut, QStackedWidget, QVBoxLayout, QWidget)

import analytics
from config import Config
from hardware import SimulatedHardware, create_hardware
from session import RehabSession, RunMode, SessionObserver, Stage
from store import ProfileStore

# ----------------------------------------------------------------------------
# 디자인 토큰
# ----------------------------------------------------------------------------
BG, CARD, LINE = "#f2f5f9", "#ffffff", "#e3e8ef"
INK, MUTED = "#0f1b2d", "#667085"
PRIMARY, PRIMARY_D = "#0b6bcb", "#0a58a8"
TEAL, GREEN, AMBER, RED = "#0e9f8e", "#12a150", "#f79009", "#d92d20"
FONT_STACK = '"Malgun Gothic","Apple SD Gothic Neo","Noto Sans CJK KR","Noto Sans KR","Segoe UI",sans-serif'

QSS = f"""
* {{ font-family: {FONT_STACK}; color: {INK}; }}
QMainWindow, #root {{ background: {BG}; }}
#topbar {{ background: {INK}; }}
#topbar QLabel {{ color: white; }}
.Card, QFrame#card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 14px; }}
QLabel#h {{ font-size: 13px; font-weight: 700; color: {MUTED}; letter-spacing: 0.5px; }}
QLabel#kpiv {{ font-size: 26px; font-weight: 800; }}
QLabel#kpit {{ font-size: 12px; font-weight: 700; color: {MUTED}; }}
QLabel#kpis {{ font-size: 12px; color: {MUTED}; }}
QPushButton {{ border: none; border-radius: 12px; padding: 10px 16px; font-size: 15px; font-weight: 700; }}
QPushButton#primary {{ background: {PRIMARY}; color: white; }}
QPushButton#primary:hover {{ background: {PRIMARY_D}; }}
QPushButton#primary:disabled {{ background: #b9c4d3; }}
QPushButton#ghost {{ background: #eaf0f7; color: {INK}; }}
QPushButton#ghost:hover {{ background: #dde7f3; }}
QPushButton#ghost:disabled {{ color: #a0a9b8; background: #f1f4f8; }}
QPushButton#amber {{ background: #fff4d6; color: #8a5a00; border: 2px solid #f7c948; font-size: 18px; }}
QPushButton#amber:hover {{ background: #ffe9a8; }}
QPushButton#amber:disabled {{ background: #f4f4f4; color: #b0b0b0; border-color: #e4e4e4; }}
QPushButton#pain2 {{ background: #fee4e2; color: #912018; border: 2px solid #f97066; font-size: 18px; }}
QPushButton#pain2:hover {{ background: #fecdca; }}
QPushButton#pain2:disabled {{ background: #f4f4f4; color: #b0b0b0; border-color: #e4e4e4; }}
QPushButton#estop {{ background: {RED}; color: white; font-size: 22px; border: 4px solid #fda29b; border-radius: 16px; }}
QPushButton#estop:hover {{ background: #b42318; }}
QPushButton#estop:disabled {{ background: #e8c9c6; color: #fff; border-color: #f2dede; }}
QComboBox {{ border: 1px solid #cdd5df; border-radius: 10px; padding: 8px 10px; font-size: 15px; background: white; min-height: 24px; }}
QRadioButton {{ font-size: 14px; padding: 6px 2px; }}
QPlainTextEdit {{ background: #0f1b2d; color: #cfe3ff; border-radius: 10px; border: none; font-size: 12px; padding: 6px; font-family: Consolas, "D2Coding", monospace; }}
QProgressBar {{ border: none; background: #e6ebf2; border-radius: 5px; height: 10px; text-align: center; }}
QProgressBar::chunk {{ border-radius: 5px; background: {TEAL}; }}
"""


def _card(parent: Optional[QWidget] = None) -> QFrame:
    f = QFrame(parent)
    f.setObjectName("card")
    return f


def _noFocus(*widgets) -> None:
    for w in widgets:
        w.setFocusPolicy(Qt.NoFocus)


# ----------------------------------------------------------------------------
# 세션 -> UI 브리지 (세션 스레드에서 시그널로 안전하게 전달)
# ----------------------------------------------------------------------------
class Bridge(QObject, SessionObserver):
    sig_log = pyqtSignal(str, str)
    sig_stage = pyqtSignal(object)
    sig_prompt = pyqtSignal(str, str, str, bool)
    sig_snap = pyqtSignal(dict)
    sig_done = pyqtSignal(dict)
    sig_hotkey = pyqtSignal(str)

    def __init__(self) -> None:
        QObject.__init__(self)

    def log(self, text, level="info"): self.sig_log.emit(text, level)
    def stage(self, stage): self.sig_stage.emit(stage)
    def prompt(self, title, body, button, needs_confirm): self.sig_prompt.emit(title, body, button, needs_confirm)
    def snapshot(self, snap): self.sig_snap.emit(snap)
    def finished(self, info): self.sig_done.emit(info)


# ----------------------------------------------------------------------------
# 커스텀 위젯
# ----------------------------------------------------------------------------
class ArcGauge(QWidget):
    """손가락 위치(0~100%) 반원 게이지. 구간(zone) 구분, 현재 구간 강조, 목표 가동범위 표시."""

    def __init__(self) -> None:
        super().__init__()
        self.pct, self.zones, self.zone, self.phase, self.speed, self.rom = 0.0, 4, -1, "IDLE", 0.0, 0.8
        self.setMinimumSize(360, 250)

    def set_state(self, pct, zones, zone, phase, speed, rom) -> None:
        self.pct, self.zones, self.zone, self.phase, self.speed, self.rom = pct, zones, zone, phase, speed, rom
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w - 40, (h - 46) * 2)
        r = QRectF((w - side) / 2, 12, side, side)
        thick = side * 0.085
        r_in = r.adjusted(thick / 2, thick / 2, -thick / 2, -thick / 2)
        # 배경 구간
        zs = 180.0 / self.zones
        for z in range(self.zones):
            col = QColor("#dbe5f1") if z % 2 == 0 else QColor("#cdd9e8")
            if z == self.zone:
                col = QColor("#a9c8ee")
            p.setPen(QPen(col, thick, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(r_in, int((180 - z * zs) * 16), int(-zs * 16))
        # 진행 호
        pct = max(0.0, min(100.0, self.pct))
        col = QColor(AMBER) if self.phase == "BEND" else QColor(PRIMARY) if self.phase == "EXTEND" else QColor("#98a2b3")
        p.setPen(QPen(col, thick * 0.5, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r_in, 180 * 16, int(-pct * 1.8 * 16))
        # 현재 위치 표시 점
        ang = math.radians(180 - pct * 1.8)
        c = r.center()
        rad = side / 2 - thick / 2
        p.setPen(QPen(QColor("white"), 3)); p.setBrush(col)
        p.drawEllipse(QPointF(c.x() + rad * math.cos(ang), c.y() - rad * math.sin(ang)), thick * 0.42, thick * 0.42)
        # 구간 눈금
        p.setPen(QPen(QColor("white"), 2))
        for z in range(1, self.zones):
            a = math.radians(180 - z * zs)
            ro, ri = side / 2, side / 2 - thick
            p.drawLine(QPointF(c.x() + ri * math.cos(a), c.y() - ri * math.sin(a)),
                       QPointF(c.x() + ro * math.cos(a), c.y() - ro * math.sin(a)))
        # 텍스트
        p.setPen(QColor(INK))
        f = QFont(); f.setPixelSize(int(side * 0.16)); f.setBold(True); p.setFont(f)
        p.drawText(QRectF(c.x() - side * 0.3, c.y() - side * 0.30, side * 0.6, side * 0.17), Qt.AlignCenter, f"{pct:.0f}%")
        f.setPixelSize(int(side * 0.055)); f.setBold(False); p.setFont(f)
        p.setPen(QColor(MUTED))
        label = {"BEND": "주먹 쥐는 중", "EXTEND": "손 펴는 중"}.get(self.phase, "대기")
        p.drawText(QRectF(c.x() - side * 0.3, c.y() - side * 0.13, side * 0.6, side * 0.08), Qt.AlignCenter, label)
        f.setPixelSize(12); p.setFont(f)
        p.drawText(QRectF(r.left(), c.y() + 6, 90, 20), Qt.AlignLeft | Qt.AlignVCenter, "0% 폄")
        p.drawText(QRectF(r.right() - 110, c.y() + 6, 110, 20), Qt.AlignRight | Qt.AlignVCenter, "굽힘 100%")


class ArmBar(QWidget):
    """속도 후보 막대 - 현재 선택된 속도를 강조"""

    def __init__(self) -> None:
        super().__init__()
        self.arms, self.idx = [], -1
        self.setMinimumHeight(54)

    def set_state(self, arms, idx) -> None:
        self.arms, self.idx = arms, idx
        self.update()

    def paintEvent(self, _e) -> None:
        if not self.arms:
            return
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        n, gap = len(self.arms), 8
        bw = (self.width() - gap * (n - 1)) / n
        for i, a in enumerate(self.arms):
            rect = QRectF(i * (bw + gap), 4, bw, self.height() - 8)
            on = i == self.idx
            p.setPen(Qt.NoPen); p.setBrush(QColor(PRIMARY if on else "#e6ecf4")); p.drawRoundedRect(rect, 9, 9)
            p.setPen(QColor("white" if on else MUTED))
            f = QFont(); f.setPixelSize(15); f.setBold(on); p.setFont(f)
            p.drawText(rect, Qt.AlignCenter, f"{a:.0f}")


class Kpi(QFrame):
    def __init__(self, title: str, unit: str = "") -> None:
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self); lay.setContentsMargins(14, 8, 14, 8); lay.setSpacing(2)
        self.setMinimumHeight(86)
        self.t = QLabel(title); self.t.setObjectName("kpit")
        self.v = QLabel("-"); self.v.setObjectName("kpiv")
        self.s = QLabel(unit); self.s.setObjectName("kpis")
        for w in (self.t, self.v, self.s):
            lay.addWidget(w)

    def set(self, value: str, sub: str = "", color: str = INK) -> None:
        self.v.setText(value)
        self.v.setStyleSheet(f"color:{color};")
        self.s.setText(sub)


class Stepper(QWidget):
    STEPS = [("캘리브레이션", Stage.CALIBRATION), ("치료 준비", Stage.READY), ("치료 진행", Stage.THERAPY), ("결과 리포트", Stage.FINISHED)]

    def __init__(self) -> None:
        super().__init__()
        self.current: Optional[Stage] = None
        self.skip_cal = False
        self.setMinimumHeight(190)

    def set_stage(self, st: Optional[Stage], skip_cal: bool = False) -> None:
        self.current, self.skip_cal = st, skip_cal
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        order = [s for _, s in self.STEPS]
        cur = order.index(self.current) if self.current in order else (3 if self.current == Stage.ABORTED else -1)
        y0, dy = 22, 46
        for i, (name, _s) in enumerate(self.STEPS):
            y = y0 + i * dy
            skipped = self.skip_cal and i == 0
            done, active = i < cur, i == cur
            if i < len(self.STEPS) - 1:
                p.setPen(QPen(QColor(TEAL if done else "#d0d7e2"), 3)); p.drawLine(16, y + 12, 16, y + dy - 12)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(PRIMARY if active else TEAL if done else "#d0d7e2")); p.drawEllipse(QPointF(16, y), 11, 11)
            p.setPen(QColor("white")); f = QFont(); f.setPixelSize(12); f.setBold(True); p.setFont(f)
            p.drawText(QRectF(5, y - 11, 22, 22), Qt.AlignCenter, "✓" if done else str(i + 1))
            p.setPen(QColor(INK if active else MUTED))
            f.setPixelSize(15); f.setBold(active); p.setFont(f)
            p.drawText(QRectF(38, y - 12, 220, 24), Qt.AlignVCenter | Qt.AlignLeft, name + ("  (생략)" if skipped else ""))


class TraceWidget(QWidget):
    """실시간 추이: 손가락 위치(%) 선 + 구동 속도 계단 (직접 그려 DPI/폰트 문제를 피함)"""

    def __init__(self, window_s: float) -> None:
        super().__init__()
        self.window_s, self.t, self.pct, self.spd = window_s, [], [], []
        self.setMinimumHeight(150)

    def set_data(self, t, pct, spd) -> None:
        self.t, self.pct, self.spd = t, pct, spd
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        L, R, T, B = 44, 44, 8, 24
        w, h = self.width() - L - R, self.height() - T - B
        f = QFont(); f.setPixelSize(11); p.setFont(f)
        smax = max(60.0, (max(self.spd) * 1.2) if self.spd else 60.0)
        for v in (0, 50, 100):
            y = T + h * (1 - (v + 10) / 125.0)
            p.setPen(QPen(QColor("#e3e8ef"), 1, Qt.DashLine)); p.drawLine(L, int(y), L + w, int(y))
            p.setPen(QColor(PRIMARY)); p.drawText(QRectF(0, y - 9, L - 6, 18), Qt.AlignRight | Qt.AlignVCenter, f"{v}%")
        p.setPen(QColor(AMBER))
        for v in (0, smax / 2, smax):
            y = T + h * (1 - v / smax)
            p.drawText(QRectF(L + w + 6, y - 9, R - 6, 18), Qt.AlignLeft | Qt.AlignVCenter, f"{v:.0f}")
        if len(self.t) < 2:
            return
        t1 = max(self.t[-1], self.window_s); t0 = t1 - self.window_s
        X = lambda t: L + w * (t - t0) / self.window_s
        p.setPen(QColor(MUTED))
        for k in range(0, int(self.window_s) + 1, 5):
            tt = t0 + k
            p.drawText(QRectF(X(tt) - 24, T + h + 4, 48, 16), Qt.AlignCenter, f"{tt:.0f}s")
        p.setClipRect(QRectF(L, T, w, h))
        pts_s = QPen(QColor(AMBER), 1.8); p.setPen(pts_s)
        for i in range(1, len(self.t)):
            ya = T + h * (1 - self.spd[i - 1] / smax)
            p.drawLine(QPointF(X(self.t[i - 1]), ya), QPointF(X(self.t[i]), ya))
            yb = T + h * (1 - self.spd[i] / smax)
            p.drawLine(QPointF(X(self.t[i]), ya), QPointF(X(self.t[i]), yb))
        p.setPen(QPen(QColor(PRIMARY), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        prev = None
        for t, v in zip(self.t, self.pct):
            pt = QPointF(X(t), T + h * (1 - (max(-10, min(115, v)) + 10) / 125.0))
            if prev is not None:
                p.drawLine(prev, pt)
            prev = pt


# ----------------------------------------------------------------------------
# 전역 단축키 (창 포커스가 없어도 Space 로 정지)
# ----------------------------------------------------------------------------
class GlobalHotkeys:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge, self.listener, self.active = bridge, None, False

    def start(self) -> bool:
        try:
            from pynput import keyboard
        except Exception:
            return False

        def on_press(key):
            try:
                if key == keyboard.Key.space:
                    self.bridge.sig_hotkey.emit("space")
                elif getattr(key, "char", None) in ("1", "2"):
                    self.bridge.sig_hotkey.emit(key.char)
            except Exception:
                pass
        try:
            self.listener = keyboard.Listener(on_press=on_press)
            self.listener.start()
            self.active = True
        except Exception:
            self.active = False
        return self.active

    def stop(self) -> None:
        if self.listener:
            self.listener.stop()


# ----------------------------------------------------------------------------
# 메인 윈도우
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.store = ProfileStore(cfg)
        self.hw = create_hardware(cfg)
        self.hw_ok = self.hw.open()
        self.session: Optional[RehabSession] = None
        self.stage: Optional[Stage] = None
        self.last_info: Optional[Dict[str, Any]] = None
        self.trace_t, self.trace_pct, self.trace_spd = deque(maxlen=900), deque(maxlen=900), deque(maxlen=900)
        self._last_buzz = False

        self.bridge = Bridge()
        for sig, slot in ((self.bridge.sig_log, self.on_log), (self.bridge.sig_stage, self.on_stage),
                          (self.bridge.sig_prompt, self.on_prompt), (self.bridge.sig_snap, self.on_snap),
                          (self.bridge.sig_done, self.on_done), (self.bridge.sig_hotkey, self.on_hotkey)):
            sig.connect(slot)

        self.setWindowTitle(cfg.ui.window_title)
        self.resize(1500, 960)
        self.setMinimumSize(1280, 800)
        self.setStyleSheet(QSS)
        self._build()

        self.hotkeys = GlobalHotkeys(self.bridge)
        if not (cfg.ui.use_global_hotkeys and self.hotkeys.start()):
            for key in ("Space", "1", "2"):
                QShortcut(key, self, activated=lambda k=key.lower(): self.on_hotkey(k))
        self.clock = QTimer(self, interval=1000, timeout=self._tick_clock); self.clock.start()
        self.plot_timer = QTimer(self, interval=120, timeout=self._update_trace); self.plot_timer.start()
        self._refresh_profile_info()
        if not self.hw_ok:
            self.on_log(f"하드웨어 연결 실패: {getattr(self.hw, 'error', '')}", "error")
        self._set_idle_ui()

    # ------------------------------------------------------------------ UI 구성
    def _build(self) -> None:
        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        # 상단바
        top = QFrame(); top.setObjectName("topbar"); top.setFixedHeight(62)
        tl = QHBoxLayout(top); tl.setContentsMargins(24, 0, 24, 0)
        title = QLabel("⬡  상지 재활 로봇 제어 시스템"); title.setStyleSheet("font-size:20px;font-weight:800;color:white;")
        self.lbl_conn = QLabel(); self.lbl_mode = QLabel(); self.lbl_clock = QLabel()
        self.lbl_clock.setStyleSheet("font-size:15px;color:#cbd5e1;")
        tl.addWidget(title); tl.addStretch(1); tl.addWidget(self.lbl_mode); tl.addSpacing(14); tl.addWidget(self.lbl_conn)
        tl.addSpacing(18); tl.addWidget(self.lbl_clock)
        outer.addWidget(top)
        self.banner = QLabel(""); self.banner.setAlignment(Qt.AlignCenter); self.banner.setFixedHeight(0)
        outer.addWidget(self.banner)

        self.pages = QStackedWidget(); outer.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_control_page())
        self.pages.addWidget(self._build_report_page())
        self._refresh_header()

    def _build_control_page(self) -> QWidget:
        page = QWidget(); grid = QGridLayout(page); grid.setContentsMargins(20, 18, 20, 16); grid.setSpacing(16)

        # ---- 좌: 환자/모드 + 단계
        left = QVBoxLayout(); left.setSpacing(14)
        c1 = _card(); l1 = QVBoxLayout(c1); l1.setContentsMargins(18, 16, 18, 16); l1.setSpacing(8)
        h = QLabel("환자 / 진행 방식"); h.setObjectName("h"); l1.addWidget(h)
        self.cmb_patient = QComboBox(); self.cmb_patient.setEditable(True)
        self.cmb_patient.addItems(self.store.list_patients() or ["default"])
        self.cmb_patient.currentTextChanged.connect(lambda _t: self._refresh_profile_info())
        l1.addWidget(QLabel("환자 ID")); l1.addWidget(self.cmb_patient)
        self.rb_full = QRadioButton("전체 캘리브레이션 + 치료"); self.rb_quick = QRadioButton("빠른 시작 (저장된 치료값)")
        self.rb_full.setChecked(True)
        l1.addSpacing(4); l1.addWidget(self.rb_full); l1.addWidget(self.rb_quick)
        self.lbl_profile = QLabel(); self.lbl_profile.setWordWrap(True); self.lbl_profile.setStyleSheet(f"color:{MUTED};font-size:12px;")
        l1.addWidget(self.lbl_profile)
        self.btn_start = QPushButton("세션 시작"); self.btn_start.setObjectName("primary"); self.btn_start.setMinimumHeight(52)
        self.btn_start.clicked.connect(self.start_session); l1.addSpacing(4); l1.addWidget(self.btn_start)
        left.addWidget(c1)
        c2 = _card(); l2 = QVBoxLayout(c2); l2.setContentsMargins(18, 16, 18, 10)
        h2 = QLabel("진행 단계"); h2.setObjectName("h"); l2.addWidget(h2)
        self.stepper = Stepper(); l2.addWidget(self.stepper)
        left.addWidget(c2); left.addStretch(1)
        wl = QWidget(); wl.setLayout(left); wl.setFixedWidth(318)
        grid.addWidget(wl, 0, 0, 2, 1)

        # ---- 중: 안내 / 게이지 / 추이
        mid = QVBoxLayout(); mid.setSpacing(14)
        cg = _card(); lg = QVBoxLayout(cg); lg.setContentsMargins(22, 18, 22, 18); lg.setSpacing(8)
        self.lbl_title = QLabel("세션을 시작하세요"); self.lbl_title.setStyleSheet("font-size:26px;font-weight:800;")
        self.lbl_body = QLabel("환자 ID와 진행 방식을 선택한 뒤 [세션 시작]을 누르세요."); self.lbl_body.setWordWrap(True)
        self.lbl_body.setStyleSheet(f"font-size:15px;color:{MUTED};")
        self.btn_confirm = QPushButton("다음"); self.btn_confirm.setObjectName("primary"); self.btn_confirm.setMinimumHeight(54)
        self.btn_confirm.clicked.connect(lambda: self.session and self.session.confirm()); self.btn_confirm.hide()
        self.lbl_hint = QLabel("Space 키로도 진행할 수 있습니다"); self.lbl_hint.setStyleSheet(f"color:{MUTED};font-size:12px;"); self.lbl_hint.hide()
        for w in (self.lbl_title, self.lbl_body, self.btn_confirm, self.lbl_hint):
            lg.addWidget(w)
        mid.addWidget(cg)

        cv = _card(); lv = QHBoxLayout(cv); lv.setContentsMargins(14, 12, 14, 12); lv.setSpacing(10)
        self.gauge = ArcGauge(); lv.addWidget(self.gauge, 5)
        right_v = QVBoxLayout()
        hh = QLabel("속도 후보 (deg/s)"); hh.setObjectName("h"); right_v.addWidget(hh)
        self.armbar = ArmBar(); right_v.addWidget(self.armbar)
        hs = QLabel("안전 점수"); hs.setObjectName("h"); right_v.addSpacing(8); right_v.addWidget(hs)
        self.bar_safety = QProgressBar(); self.bar_safety.setRange(0, 100); self.bar_safety.setTextVisible(False); right_v.addWidget(self.bar_safety)
        right_v.addStretch(1)
        lv.addLayout(right_v, 3)
        mid.addWidget(cv, 1)

        ct = _card(); lt = QVBoxLayout(ct); lt.setContentsMargins(14, 10, 14, 10)
        ht = QLabel("실시간 추이 (손가락 위치 % / 구동 속도)"); ht.setObjectName("h"); lt.addWidget(ht)
        self.trace = TraceWidget(self.cfg.ui.live_window_s); lt.addWidget(self.trace)
        mid.addWidget(ct, 1)
        wm = QWidget(); wm.setLayout(mid)
        grid.addWidget(wm, 0, 1)

        # ---- 우: KPI / 통증 / 정지
        right = QVBoxLayout(); right.setSpacing(10)
        kg = QGridLayout(); kg.setSpacing(10)
        self.k_cycle, self.k_time, self.k_rom, self.k_speed = Kpi("사이클"), Kpi("남은 시간"), Kpi("목표 가동범위"), Kpi("현재 속도")
        self.k_p1, self.k_p2 = Kpi("뻐근함 누적"), Kpi("고통 누적")
        for i, k in enumerate((self.k_cycle, self.k_time, self.k_rom, self.k_speed, self.k_p1, self.k_p2)):
            kg.addWidget(k, i // 2, i % 2)
        right.addLayout(kg)
        cp = _card(); lp = QVBoxLayout(cp); lp.setContentsMargins(14, 12, 14, 12); lp.setSpacing(8)
        hp = QLabel("환자 통증 입력"); hp.setObjectName("h"); lp.addWidget(hp)
        self.btn_p1 = QPushButton("⚠  뻐근함  (1)"); self.btn_p1.setObjectName("amber"); self.btn_p1.setMinimumHeight(60)
        self.btn_p2 = QPushButton("✖  고통  (2)"); self.btn_p2.setObjectName("pain2"); self.btn_p2.setMinimumHeight(60)
        self.btn_p1.clicked.connect(lambda: self.session and self.session.report_pain(1))
        self.btn_p2.clicked.connect(lambda: self.session and self.session.report_pain(2))
        lp.addWidget(self.btn_p1); lp.addWidget(self.btn_p2)
        right.addWidget(cp)
        self.btn_estop = QPushButton("■  긴급정지\nSPACE"); self.btn_estop.setObjectName("estop"); self.btn_estop.setMinimumHeight(100)
        self.btn_estop.clicked.connect(lambda: self.session and self.session.request_estop("화면 긴급정지 버튼"))
        right.addWidget(self.btn_estop)
        self.btn_finish = QPushButton("정상 종료 (현재 사이클 후)"); self.btn_finish.setObjectName("ghost")
        self.btn_finish.clicked.connect(lambda: self.session and self.session.request_finish()); right.addWidget(self.btn_finish)
        self.btn_hwsim = QPushButton("로봇 비상버튼 누름 (시뮬레이션)"); self.btn_hwsim.setObjectName("ghost")
        self.btn_hwsim.clicked.connect(lambda: isinstance(self.hw, SimulatedHardware) and self.hw.press_physical_estop())
        self.btn_hwsim.setVisible(self.cfg.simulation); right.addWidget(self.btn_hwsim)
        right.addStretch(1)
        wr = QWidget(); wr.setLayout(right); wr.setFixedWidth(330)
        grid.addWidget(wr, 0, 2)

        # ---- 하: 로그
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True); self.log_view.setFixedHeight(110)
        self.log_view.setMaximumBlockCount(600)
        grid.addWidget(self.log_view, 1, 1, 1, 2)
        grid.setColumnStretch(1, 1); grid.setRowStretch(0, 1)
        _noFocus(self.btn_start, self.btn_confirm, self.btn_p1, self.btn_p2, self.btn_estop, self.btn_finish,
                 self.btn_hwsim, self.rb_full, self.rb_quick)
        return page

    def _build_report_page(self) -> QWidget:
        page = QWidget(); lay = QVBoxLayout(page); lay.setContentsMargins(20, 16, 20, 16); lay.setSpacing(12)
        card = _card(); cl = QVBoxLayout(card); cl.setContentsMargins(10, 10, 10, 10)
        self.rep_label = QLabel(); self.rep_label.setAlignment(Qt.AlignCenter)
        self.rep_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored); self.rep_img: Optional[QImage] = None
        cl.addWidget(self.rep_label)
        lay.addWidget(card, 1)
        row = QHBoxLayout()
        self.lbl_files = QLabel(""); self.lbl_files.setStyleSheet(f"color:{MUTED};font-size:12px;")
        b_new = QPushButton("새 세션"); b_new.setObjectName("primary"); b_new.setMinimumHeight(48); b_new.clicked.connect(self._back_to_control)
        b_dir = QPushButton("리포트 폴더 열기"); b_dir.setObjectName("ghost"); b_dir.setMinimumHeight(48); b_dir.clicked.connect(lambda: self._open("dir"))
        b_xls = QPushButton("엑셀 열기"); b_xls.setObjectName("ghost"); b_xls.setMinimumHeight(48); b_xls.clicked.connect(lambda: self._open("xlsx"))
        _noFocus(b_new, b_dir, b_xls)
        row.addWidget(self.lbl_files, 1); row.addWidget(b_dir); row.addWidget(b_xls); row.addWidget(b_new)
        lay.addLayout(row)
        return page

    # ------------------------------------------------------------------ 상태 표시
    def _refresh_header(self, estop: bool = False) -> None:
        sim = self.cfg.simulation
        self.lbl_mode.setText("<span style='background:#7a5af8;color:white;border-radius:8px;padding:4px 10px;font-weight:700'>가상 모드</span>" if sim
                              else "<span style='background:#1f2a3d;color:#9fb3cf;border-radius:8px;padding:4px 10px;font-weight:700'>실제 로봇</span>")
        ok = self.hw_ok
        color = GREEN if ok else RED
        txt = (self.hw.label if ok else "연결 안 됨")
        self.lbl_conn.setText(f"<span style='color:{color};font-size:18px'>●</span> <span style='color:#e2e8f0'>{txt}</span>")

    def _tick_clock(self) -> None:
        self.lbl_clock.setText(time.strftime("%Y-%m-%d  %H:%M:%S"))

    def _refresh_profile_info(self) -> None:
        pid = self.cmb_patient.currentText().strip() or "default"
        if not self.store.exists(pid):
            self.lbl_profile.setText("저장된 치료값 없음 → 빠른 시작을 선택하면 보수적 기본값(저속·소범위)으로 시작합니다.")
            return
        p = self.store.load(pid)
        age = p.age_days()
        cal = f"캘리브레이션 {age:.0f}일 전" if p.calibrated else "캘리브레이션 없음"
        self.lbl_profile.setText(f"저장된 치료값 있음 · {cal}\n세션 {len(p.sessions)}회 · 마지막 가동범위 {p.rom_frac*100:.0f}%"
                                 + (f"\n⚠ 직전 종료: {p.last_end_reason}" if p.last_end_reason.startswith("ESTOP") else ""))

    def _set_idle_ui(self) -> None:
        self.btn_start.setEnabled(self.hw_ok)
        self.btn_start.setText("세션 시작" if self.hw_ok else "하드웨어 연결 필요")
        for b in (self.btn_p1, self.btn_p2, self.btn_estop, self.btn_finish, self.btn_hwsim):
            b.setEnabled(False)
        self.btn_confirm.hide(); self.lbl_hint.hide()
        for w in (self.cmb_patient, self.rb_full, self.rb_quick):
            w.setEnabled(True)

    def _show_banner(self, text: str, color: str) -> None:
        self.banner.setText(text)
        self.banner.setStyleSheet(f"background:{color};color:white;font-size:18px;font-weight:800;")
        self.banner.setFixedHeight(46 if text else 0)

    # ------------------------------------------------------------------ 세션 제어
    def start_session(self) -> None:
        if not self.hw_ok or (self.session and self.session.is_alive()):
            return
        pid = self.cmb_patient.currentText().strip() or "default"
        mode = RunMode.FULL if self.rb_full.isChecked() else RunMode.QUICK
        self.log_view.clear(); self._show_banner("", RED)
        self.trace_t.clear(); self.trace_pct.clear(); self.trace_spd.clear()
        self.session = RehabSession(self.cfg, self.hw, self.store, pid, mode, self.bridge)
        self.stepper.set_stage(None, skip_cal=(mode == RunMode.QUICK))
        for w in (self.cmb_patient, self.rb_full, self.rb_quick, self.btn_start):
            w.setEnabled(False)
        self.btn_estop.setEnabled(True)
        self.btn_hwsim.setEnabled(True)
        self.btn_finish.setEnabled(False)
        self.session.start()

    def on_hotkey(self, key: str) -> None:
        s = self.session
        if not s or not s.is_alive():
            return
        if key == "space":
            if s.stage == Stage.THERAPY:
                s.request_estop("키보드 Space")
            elif s.stage in (Stage.CALIBRATION, Stage.READY):
                s.confirm()
        elif key in ("1", "2"):
            s.report_pain(int(key))

    def on_log(self, text: str, level: str) -> None:
        color = {"error": "#ff8f86", "warn": "#ffd479"}.get(level, "#cfe3ff")
        stamp = time.strftime("%H:%M:%S")
        for line in str(text).splitlines():
            self.log_view.appendHtml(f"<span style='color:#7c8ca5'>{stamp}</span> <span style='color:{color}'>{line}</span>")

    def on_stage(self, st: Stage) -> None:
        self.stage = st
        quick = bool(self.session and self.session.mode == RunMode.QUICK)
        self.stepper.set_stage(st, skip_cal=quick)
        therapy = st == Stage.THERAPY
        self.btn_p1.setEnabled(therapy); self.btn_p2.setEnabled(therapy); self.btn_finish.setEnabled(therapy)
        self.lbl_hint.setText("치료 중: Space = 긴급정지,  1 = 뻐근함,  2 = 고통" if therapy else "Space 키로도 진행할 수 있습니다")
        if therapy:
            self.btn_confirm.hide()
            self.lbl_title.setText("치료 진행 중")
            self.lbl_body.setText("통증이 느껴지면 [뻐근함] / [고통] 버튼을 누르세요. 위급 시 Space, 화면의 긴급정지, 로봇의 비상버튼 중 아무거나 사용하세요.")
            self.lbl_hint.show()

    def on_prompt(self, title: str, body: str, button: str, needs_confirm: bool) -> None:
        self.lbl_title.setText(title); self.lbl_body.setText(body)
        self.btn_confirm.setText(button)
        self.btn_confirm.setVisible(needs_confirm); self.lbl_hint.setVisible(needs_confirm)

    def on_snap(self, s: Dict[str, Any]) -> None:
        self.gauge.set_state(s["pct"], s["zones"], s["zone"], s["phase"], s["speed"], s["rom"])
        arms = self.session.arms if self.session else []
        self.armbar.set_state(arms, s["arm"])
        th = s["stage"] == Stage.THERAPY
        self.k_cycle.set(str(s["cycle"]) if s["cycle"] else "-", "완료 사이클 포함")
        rem = int(s["remaining"]); self.k_time.set(f"{rem//60:02d}:{rem%60:02d}" if th else "-", "세션 종료까지")
        self.k_rom.set(f"{s['rom']*100:.0f}%" if th else "-", "캘리브레이션 범위 대비")
        self.k_speed.set(f"{s['speed']:.0f}" if th and s["speed"] else "-", "deg/s")
        self.k_p1.set(str(s["pain1"]), "", AMBER if s["pain1"] else INK)
        self.k_p2.set(str(s["pain2"]), "", RED if s["pain2"] else INK)
        sv = int(s["safety"] * 100)
        self.bar_safety.setValue(sv)
        col = GREEN if sv >= 60 else AMBER if sv >= 30 else RED
        self.bar_safety.setStyleSheet(f"QProgressBar::chunk{{background:{col};border-radius:5px;}}")
        if th:
            self.trace_t.append(s["t"]); self.trace_pct.append(s["pct"]); self.trace_spd.append(s["speed"])
        if s["buzzer"] and not self._last_buzz:
            QApplication.beep()
        self._last_buzz = s["buzzer"]

    def _update_trace(self) -> None:
        if self.pages.currentIndex() == 0 and self.trace_t:
            self.trace.set_data(list(self.trace_t), list(self.trace_pct), list(self.trace_spd))

    def _render_report(self, log) -> None:
        fig = analytics.build_dashboard(log)
        canvas = FigureCanvasAgg(fig); fig.set_dpi(130); canvas.draw()
        buf = canvas.buffer_rgba(); w, h = canvas.get_width_height()
        self.rep_img = QImage(bytes(buf), w, h, QImage.Format_RGBA8888).copy()
        self._fit_report()

    def _fit_report(self) -> None:
        if self.rep_img is None:
            return
        sz = self.rep_label.size()
        self.rep_label.setPixmap(QPixmap.fromImage(self.rep_img).scaled(sz, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit_report()

    def on_done(self, info: Dict[str, Any]) -> None:
        self.last_info = info
        self._set_idle_ui()
        reason = info["summary"]["end_reason"]
        self._refresh_profile_info()
        if reason.startswith("ESTOP"):
            self._show_banner(f"■ 긴급정지로 종료되었습니다 — {info['summary']['estop_source']}", RED)
        if info["log"].cycles:
            self.pages.setCurrentIndex(1)
            QApplication.processEvents()
            self._render_report(info["log"])
            files = info.get("files", {})
            self.lbl_files.setText("저장됨: " + "  ·  ".join(f"{k}: {v.name}" for k, v in files.items()) + f"\n{info.get('report_dir','')}")
            self.pages.setCurrentIndex(1)
        else:
            self.lbl_title.setText("세션이 중단되었습니다")
            self.lbl_body.setText("로그를 확인한 뒤 다시 시작하세요.")

    def _back_to_control(self) -> None:
        self.pages.setCurrentIndex(0); self._show_banner("", RED)
        self.lbl_title.setText("세션을 시작하세요")
        self.lbl_body.setText("환자 ID와 진행 방식을 선택한 뒤 [세션 시작]을 누르세요. 이전 치료값이 있으면 [빠른 시작]으로 이어서 진행할 수 있습니다.")
        self.stepper.set_stage(None)
        self.rb_quick.setChecked(self.store.exists(self.cmb_patient.currentText()))
        self.cmb_patient.clear(); self.cmb_patient.addItems(self.store.list_patients() or ["default"])

    def _open(self, what: str) -> None:
        if not self.last_info:
            return
        path = self.last_info.get("report_dir") if what == "dir" else self.last_info.get("files", {}).get("xlsx")
        if not path:
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))                      # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            QMessageBox.information(self, "파일 위치", f"{path}\n\n({exc})")

    def closeEvent(self, e) -> None:
        if self.session and self.session.is_alive():
            if QMessageBox.question(self, "종료", "세션이 진행 중입니다. 긴급정지 후 종료할까요?") != QMessageBox.Yes:
                e.ignore(); return
            self.session.request_estop("프로그램 종료")
            self.session.join(5)
        self.hotkeys.stop()
        self.hw.close()
        e.accept()


def run_app(cfg: Config) -> int:
    app = QApplication(sys.argv)
    win = MainWindow(cfg)
    win.show()
    return app.exec_()
