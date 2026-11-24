**English** | [한국어](README.ko.md)

# Re:Hand — Safety-Constrained Control Software for a Wearable Finger Rehabilitation Robot

Re:Hand is the control software for a low-cost, wire-driven soft-glove finger rehabilitation robot.
Two servo motors pull UHMWPE wires. A flex sensor and an FSR grip module take the measurements. An Arduino Nano runs the control loop. The key change is the control algorithm. The earlier LinUCB-based speed selector is replaced by **SafeLadderTS**, a Thompson sampling algorithm with a safety constraint. It treats pain as a safety condition that limits speed selection, not as a simple reward signal.

This software was developed as part of the Kwangwoon University Regional Capstone Design program. The target region is Nowon-gu, Seoul.

> **Scope.** This is research and education software. Every algorithm result in this document comes from a **simulated patient**. No human subject was tested. It is not a medical device and must not be used on patients.

---

## Table of Contents

1. [Features](#1-features)
2. [System Overview](#2-system-overview)
3. [Repository Structure](#3-repository-structure)
4. [Installation and Environment Setup](#4-installation-and-environment-setup)
5. [How to Run](#5-how-to-run)
6. [Simulation Mode and Real Hardware Mode](#6-simulation-mode-and-real-hardware-mode)
7. [Session Flow](#7-session-flow)
8. [Safety Design](#8-safety-design)
9. [SafeLadderTS Algorithm](#9-safeladderts-algorithm)
10. [Serial Protocol and Firmware](#10-serial-protocol-and-firmware)
11. [Configuration Guide](#11-configuration-guide)
12. [Outputs and Data Files](#12-outputs-and-data-files)
13. [Benchmark](#13-benchmark)
14. [Current Limitations](#14-current-limitations)
15. [Future Work](#15-future-work)

---

## 1. Features

| Area | What it does |
|---|---|
| Control | SafeLadderTS selects the motor speed per movement direction and range-of-motion zone. |
| Safety | Three emergency-stop paths, a heartbeat watchdog, slew-limited servos and automatic rollback after pain. |
| Run modes | Full mode runs calibration and then therapy. Quick-start mode skips calibration and uses the patient's saved values. |
| Patient profiles | Calibration values, learned posteriors and safety state are saved per patient in JSON. |
| UI | PyQt5 clinician interface with a guidance card, a live finger-position gauge, live traces and a large emergency-stop button. |
| Analytics | A 9-panel dashboard PNG, an Excel report and a JSON summary are written after every session. |
| Simulation | A virtual robot and a virtual patient run the whole pipeline without hardware. |
| Benchmark | Compares SafeLadderTS with LinUCB, two ablations and three fixed-speed policies. |

---

## 2. System Overview

```
 ┌────────────── Wearable module (glove) ──────────────┐        ┌────────────────── PC software (Python) ──────────────────┐
 │                                                     │        │                                                          │
 │  Servo x2 (flexion / extension)  <──┐               │        │  Serial layer (hardware.py)                              │
 │  Flex sensor ─────────────────────> │ Arduino Nano  │  USB   │     │  DATA / EVT in, M / STOP / BUZ / HB out            │
 │  FSR grip module ─────────────────> │ firmware      │<──────>│     v                                                    │
 │  E-stop button + buzzer <─────────> │ (.ino)        │ 115200 │  Session controller (session.py)                         │
 │                                     └───────────────┘  bps   │     │  calibration, therapy, e-stop, rollback            │
 └─────────────────────────────────────────────────────┘        │     v                                                    │
                                                                │  SafeLadderTS (agent.py) <-> Profile store (store.py)   │
                                                                │     │                                                    │
                                                                │     v                                                    │
                                                                │  UI (ui.py)   Analytics (analytics.py)   Logs (records)  │
                                                                └──────────────────────────────────────────────────────────┘
```

The session logic talks only to the `HardwareInterface` abstraction. `SerialHardware` drives the real Arduino. `SimulatedHardware` replaces both the robot and the patient. The same session code runs in both cases.

---

## 3. Repository Structure

```
rehab_system/
├── main.py              Entry point. UI mode and headless console mode.
├── config.py            All settings. Contains the SIMULATION_MODE switch.
├── session.py           Session state machine, safety logic, emergency stop, rollback.
├── agent.py             SafeLadderTS (proposed) and LinUCBAgent (baseline for comparison).
├── hardware.py          HardwareInterface, SerialHardware, SimulatedHardware.
├── simulation.py        Virtual patient model (sigmoid pain hazard).
├── store.py             Per-patient profile save and load.
├── records.py           Session log data classes.
├── analytics.py         9-panel dashboard, Excel report and JSON summary.
├── ui.py                PyQt5 clinician interface.
├── benchmark.py         Algorithm comparison on a simulated patient.
├── firmware/
│   └── rehab_controller.ino   Arduino Nano firmware.
└── data/
    ├── profiles/<patient>.json            Saved calibration and learning state.
    ├── reports/<patient>/<timestamp>/     Session outputs.
    └── benchmark/                         benchmark.json and benchmark.png.
```

---

## 4. Installation and Environment Setup

Python 3.9 or newer is required.

```bash
pip install numpy pandas matplotlib openpyxl PyQt5 pyserial pynput
```

| Package | Used for |
|---|---|
| numpy, pandas | Numerics and the Excel report tables |
| matplotlib | Dashboard and benchmark figures |
| openpyxl | Excel export |
| PyQt5 | Clinician UI |
| pyserial | Arduino communication (real mode only) |
| pynput | Global Space-key emergency stop |

Simulation mode with `--headless` needs only `numpy`, `pandas`, `matplotlib` and `openpyxl`. The UI also needs PyQt5.

The dashboard font is chosen from Malgun Gothic, AppleGothic, NanumGothic or Noto Sans CJK KR. If none is installed, Korean labels fall back to DejaVu Sans and may show as empty boxes.

---

## 5. How to Run

```bash
# UI in simulation mode (no hardware needed)
python main.py --sim

# UI with the real robot
python main.py --real

# Console test: quick-start mode for patient P1, 10 cycles, accelerated virtual time
python main.py --sim --headless --mode quick --patient P1 --cycles 10

# Console test: full calibration and therapy
python main.py --sim --headless --mode full --cycles 20

# Algorithm benchmark
python benchmark.py --cycles 40 --seeds 100
```

Command-line options of `main.py`:

| Option | Meaning |
|---|---|
| `--sim` / `--real` | Force simulation or real hardware. Overrides `config.SIMULATION_MODE`. |
| `--headless` | Run without the UI. Supported only with `--sim`. |
| `--mode full\|quick` | Calibration plus therapy, or saved values only. |
| `--patient ID` | Patient identifier. Selects the profile file. |
| `--cycles N` | Stop after N cycles. |
| `--minutes M` | Session length in minutes. The default is 30. |
| `--speedup X` | Virtual time acceleration in headless mode. The default is 40. |

---

## 6. Simulation Mode and Real Hardware Mode

The default switch is in `config.py`:

```python
SIMULATION_MODE: bool = False   # True: virtual robot and patient. False: Arduino over serial.
```

The command-line options `--sim` and `--real` override it. In simulation mode:

- The virtual robot reproduces servo motion and sensor readings so the full session logic runs unchanged.
- The virtual patient has a position-dependent speed tolerance. It is lower near both ends of the range.
- Pain starts with a sigmoid hazard when the speed exceeds the tolerance. The tolerance rises slowly as safe cycles accumulate.
- A virtual patient presses the pain button by itself when `sim.auto_patient` is `True`.
- Calibration confirm prompts can be skipped with `sim.auto_confirm`.

The patient model is an **assumption for algorithm comparison**. It is not clinical data.

---

## 7. Session Flow

```
IDLE -> CALIBRATION -> READY -> THERAPY -> FINISHED
                                   |
                                   +----> ABORTED   (emergency stop, comm loss, software error)
```

**Full mode**

1. Motor range. Motor 2 pulls the extension wire until the operator confirms that the hand is fully open. Motor 1 then pulls the flexion wire until the fist is fully closed. Both pulls are slow (20 deg/s).
2. Sensor baselines. Each motor moves to its limit and the flex sensor is averaged over 1 s at the open and the closed position.
3. Grip force. The patient squeezes the FSR module for 5 s. The mean FSR value sets the base speed `k0` (section 9).
4. Validity checks. Calibration aborts if the motor travel is below 10 deg or the flex sensor span is below 60 ADC counts.
5. The profile is saved and therapy cycles start. They run until the time limit or `max_cycles`.

**Quick-start mode**

1. Load `data/profiles/<patient>.json`.
2. A warning appears if the calibration is older than 14 days.
3. With no saved profile the system starts with conservative defaults: 45 deg travel on both motors, ROM 0.6 and base speed 15 deg/s.
4. The first 2 cycles use a lowered speed cap (`warmup_max_arm = 1`).

**One therapy cycle** has a bend phase and an extend phase. Each phase is divided into 4 zones. At every zone boundary the agent chooses a speed. The patient reports the outcome through the pain button:

| Level | Meaning | System response |
|---|---|---|
| 0 | No pain | Counts as a safe pass. |
| 1 | Discomfort | Soft penalty. The safety score drops by 20%. |
| 2 | Pain | Immediate rollback to 30% speed, a lower speed cap for the zone and a 3-cycle cooldown. |

After each cycle the range of motion grows by 2% if no level-2 pain occurred. Level-2 pain resets it to the starting ratio.

---

## 8. Safety Design

| Mechanism | Detail |
|---|---|
| Emergency stop, 3 paths | Space key (global hotkey), on-screen button, and the physical robot button. All three stop the motors, sound the buzzer for 2,500 ms, return the glove to a safe position and save a report. |
| Hardware e-stop | The Arduino stops the servos, sounds the buzzer and sends `EVT,ESTOP` on its own. It does not wait for the PC. The input is debounced for 20 ms. |
| Heartbeat watchdog | The PC sends `HB` every 0.5 s. If the Arduino hears nothing for 2,000 ms it stops the servos and sounds the buzzer. The PC also treats 2 s of silence as a communication loss. |
| Slew limiting | The firmware updates servo angles every 10 ms and never exceeds the commanded speed. |
| Speed ladder | The agent can try only one speed step above the fastest speed already certified as safe. |
| Pain rollback | Level-2 pain cuts the speed to 30% of the current value, lowers the zone speed cap and starts a 3-cycle cooldown. |
| Safety score | Starts at 0.8. Multiplied by 0.4 after level-2 pain and by 0.8 after level-1 pain. Gains 0.05 after a cycle without level-2 pain. Limited to 0.1–1.0. It scales the pain tolerance used for speed selection. |
| ROM limit | Starts at 0.8 of the calibrated range (0.6 by default without a profile). Grows by 0.02 per cycle without level-2 pain up to 1.0. |
| Over-flex guard | A zone ends if the flex reading exceeds the calibrated range by more than 10%. |
| Calibration validity | Calibration stops if the sensor span or motor travel is implausible. |
| Stale calibration | A warning appears when the saved calibration is older than 14 days. |
| Conservative defaults | Without a profile the system uses low speed and a small range. |
| UI safety | The emergency-stop button is always in the same position and is the largest control. Buttons do not take keyboard focus, so Space cannot press an unrelated button. |

---

## 9. SafeLadderTS Algorithm

SafeLadderTS finds the **fastest speed whose pain probability stays below a tolerance**. It learns separately for each context.

**Context.** A context is one movement direction (bend or extend) in one zone. There are 2 × 4 = 8 contexts.

**Arms.** Speed candidates are `k0 + {0, 10, 20, 30}` deg/s, capped at 100 deg/s. The base speed `k0` comes from the calibration grip force: `k0 = clip(int(FSR/1023 · 45) + 15, 15, 80)`. A stronger grip starts the ladder at a higher speed. Without a profile `k0 = 15`.

**State.** For each context `c` and arm `k` the agent keeps a Beta posterior of the pain probability with parameters `(f[c,k], s[c,k])`. The priors are `f0 = 0.5` and `s0 = 1.5`.

**Selection** for context `c`:

1. Pain tolerance: `ε = 0.20 · max(S, 0.3)`, where `S` is the safety score.
2. Certified arm `k_cert`: the fastest arm with at least 3.0 safe observations and a posterior mean pain of 0.15 or less.
3. Ladder cap: `k_cap = min(k_cert + 1, k_pain)`, where `k_pain` is the speed cap set by earlier level-2 pain in this context.
4. Draw `p̃[k] ~ Beta(f[c,k], s[c,k])` for each arm.
5. Choose the fastest arm `k ≤ k_cap` with `p̃[k] ≤ ε`. If none qualifies, choose the slowest arm.

**Update** after an outcome `y ∈ {0, 1, 2}` on arm `k`:

- `y = 0`: `s[c,k] += 1`, and `s[c,j] += 0.5` for slower arms `j < k`.
- `y > 0`: `f[c,k] += w`, and `f[c,j] += 0.5·w` for faster arms `j > k`, with `w = 1` for discomfort and `w = 2` for pain.
- `y = 2`: set `k_pain = k − 1` and start a 3-cycle cooldown.

**End of cycle.** The speed cap rises by one step when its cooldown ends. All evidence is discounted toward the prior by `γ = 0.99` so that old observations fade as the patient changes.

**Variable table**

| Symbol | Meaning | Unit |
|---|---|---|
| `c` | Context (direction × zone) | none |
| `k` | Speed arm index | none |
| `s`, `f` | Safe and pain evidence for the Beta posterior | counts |
| `S` | Safety score | none, 0.1–1.0 |
| `ε` | Pain-probability tolerance | probability |
| `γ` | Per-cycle discount factor | none |

The monotone propagation assumes that pain risk never decreases with speed. That is reasonable for passive finger motion but it is an assumption.

`LinUCBAgent` in `agent.py` is the baseline from the earlier project. It is kept only for benchmarking.

---

## 10. Serial Protocol and Firmware

Serial link: 115,200 bps, ASCII, one command per line. The default port is `COM3`. If it is not found the software searches for an Arduino automatically (`serial.auto_detect`).

**PC to Arduino**

| Command | Meaning |
|---|---|
| `M<n>,<angle>,<speed>` | Move servo `n` to `angle` [deg] at `speed` [deg/s]. |
| `STOP` | Hold all servos at the current position. |
| `BUZ,<ms>` | Sound a beep pattern for `ms` milliseconds. |
| `HB` | Heartbeat. Any received command also resets the watchdog. |

**Arduino to PC**

| Message | Meaning |
|---|---|
| `DATA,<flex>,<fsr>` | Raw ADC readings every 50 ms. |
| `EVT,ESTOP` | The hardware e-stop button is pressed. Repeated while it is held. |

**Firmware pin assignment — assumed values.** Check them against your real wiring before powering the robot.

| Function | Pin |
|---|---|
| Servo 1 (flexion) | D9 |
| Servo 2 (extension) | D10 |
| Flex sensor | A0 |
| FSR | A1 |
| Emergency-stop button | D2, between the pin and GND, `INPUT_PULLUP`, pressed = LOW |
| Buzzer | D8 |

Initial servo angles must match `m1_init = 0` and `m2_init = 180` in `config.py`. To merge the safety functions into an existing sketch, copy the e-stop, buzzer and heartbeat sections into your own code.

---

## 11. Configuration Guide

All settings are dataclasses in `config.py`.

| Group | Parameter | Default | Meaning |
|---|---|---|---|
| Serial | `port`, `baud` | `COM3`, 115200 | Link settings. |
| Serial | `heartbeat_period_s` | 0.5 | PC heartbeat period. |
| Serial | `comm_timeout_s` | 2.0 | Silence that counts as communication loss. |
| Motion | `m1_init`, `m2_init` | 0, 180 | Home angles [deg]. |
| Motion | `cal_speed` | 20 | Calibration speed [deg/s]. |
| Motion | `return_speed` | 60 | Normal return speed [deg/s]. |
| Motion | `speed_min`, `speed_max` | 15, 100 | Speed range [deg/s]. |
| Motion | `arm_offsets` | 0, 10, 20, 30 | Arm speeds relative to the base speed. |
| Therapy | `zones` | 4 | Zones per phase. |
| Therapy | `session_duration_s` | 1,800 | Session length. |
| Therapy | `rom_start`, `rom_max`, `rom_step` | 0.80, 1.00, 0.02 | ROM schedule. |
| Therapy | `safety_init` | 0.8 | Initial safety score. |
| Therapy | `safety_pain2_factor`, `safety_pain1_factor` | 0.4, 0.8 | Score multipliers after pain. |
| Therapy | `safety_recover` | 0.05 | Score gain per cycle without level-2 pain. |
| Therapy | `rollback_speed_ratio` | 0.3 | Speed after level-2 pain. |
| Therapy | `warmup_cycles`, `warmup_max_arm` | 2, 1 | Quick-start warm-up. |
| Therapy | `default_m1_travel`, `default_m2_travel` | 45, 45 | Defaults without a profile [deg]. |
| Agent | `pain_tolerance` | 0.20 | Base tolerance `ε`. |
| Agent | `prior_safe`, `prior_pain` | 1.5, 0.5 | Beta priors. |
| Agent | `certify_min_safe`, `certify_max_mean_pain` | 3.0, 0.15 | Certification thresholds. |
| Agent | `propagate` | 0.5 | Monotone propagation weight. |
| Agent | `cooldown_cycles` | 3 | Cycles before a lowered cap is relaxed. |
| Agent | `discount` | 0.99 | Per-cycle evidence discount. |
| Sim | `time_scale`, `auto_confirm`, `auto_patient`, `seed` | 8, False, True, 7 | Simulation controls. |
| General | `profile_max_age_days` | 14 | Stale-calibration warning. |

---

## 12. Outputs and Data Files

After each session:

```
data/reports/<patient>/<timestamp>/
├── dashboard.png       9-panel analysis figure
├── rehab_report.xlsx   Sheets: Summary, Cycles, Decisions, PainEvents, PainProb_Final, AgentHistory, Steps
└── summary.json        Key metrics
```

**Dashboard panels**

| Row | Panels |
|---|---|
| 1 | A. ROM and safety score over cycles. B. Selected speed per zone and cycle. C. Posterior pain probability per zone and speed. |
| 2 | D. Cumulative pain and safe passes. E. Learning curve (speed and uncertainty). F. Exploration versus exploitation ratio. |
| 3 | G. Where pain occurred. H. Zone outcome ratios. I. Key metrics. |

`summary.json` contains the patient ID, mode, duration, total and successful cycles, level-1 and level-2 pain counts, mean speed, final ROM, final safety score and the end reason (finished, max cycles or emergency-stop source).

**Patient profile** `data/profiles/<patient>.json` stores motor travel limits, sensor baselines, grip strength, speed arms, safety score, ROM ratio, the agent posteriors and recent session history. The next quick-start session continues from it.

---

## 13. Benchmark

`benchmark.py` compares policies on a simulated patient without any hardware.

**Setup**

- Patient speed tolerances of 40, 55 and 70 deg/s. The tolerance falls by up to 40% toward the ends of the range.
- Pain hazard `h = h0 · σ((v − v_tol) / β)` with `h0 = 3.0` per second and `β = 4.0` deg/s.
- Arms 37, 47, 57 and 67 deg/s. 4 zones per phase. 40 cycles per run.
- 100 seeds × 3 tolerances = 300 runs per policy.
- Regret `= Σ [ max(0, v* − v) / v* + 0.5·1(y=1) + 2.0·1(y=2) ]`, where `v*` is the oracle speed.

**Policies:** SafeLadderTS, LinUCB, SafeLadderTS without the ladder, SafeLadderTS without monotone propagation, and fixed low, mid and high speed.

**Main results (simulation only)**

| Policy | Level-2 pain per run | Std. dev. | Speed ratio to oracle |
|---|---|---|---|
| SafeLadderTS | 7.5 | 10.3 | 0.94 |
| LinUCB | 20.6 | 29.4 | 1.03 |
| SafeLadderTS without ladder | 11.9 | — | — |
| SafeLadderTS without propagation | 7.7 | — | — |
| Fixed low speed | 7.2 | — | 0.88 |
| Fixed mid speed | 25.1 | — | — |
| Fixed high speed | 62.2 | — | — |

- SafeLadderTS reduces level-2 pain by 13.1 events (64%) against LinUCB. The 95% bootstrap CI of the difference is −16.6 to −9.6.
- A speed ratio above 1.0 means the policy moved faster than the patient tolerates. LinUCB averages 1.03.
- Without the ladder, pain in cycles 1–10 rises from 1.86 to 5.47 events. The ladder is what protects the early cycles.
- Without monotone propagation the result barely changes. The simulation does not show a benefit from propagation.

**Per tolerance, level-2 pain per run**

| Tolerance | SafeLadderTS | LinUCB |
|---|---|---|
| 40 deg/s | 21.4 | 58.3 |
| 55 deg/s | 0.7 | 3.6 |
| 70 deg/s | 0.4 | 0.0 |

Outputs go to `data/benchmark/benchmark.json` and `benchmark.png`. Running the benchmark overwrites them. The reported numbers use `--seeds 100`.

---

## 14. Current Limitations

1. **Simulation stage.** The system has never been used or evaluated with real patients.
2. **Simplified patient model.** The simulated patient and SafeLadderTS both assume that pain risk does not decrease as speed increases.
3. **Speed floor at 40 deg/s tolerance.** The slowest arm (37 deg/s) already exceeds the tolerance near the range ends, so every policy produces about 21 pain events. A lower minimum speed is needed to improve this case.
4. **Exploration cost at 70 deg/s tolerance.** SafeLadderTS caused 0.4 pain events on average during exploration. LinUCB and the fixed low speed caused none under the same condition.
5. **Single flex sensor.** The measured angle depends on glove fit and sensor placement. A secondary angle sensor is not implemented.
6. **Dependence on the pain button.** Users who cannot press the button cannot use the current system. Auxiliary signals such as EMG could be considered later.
7. **Firmware pins must be verified.** The pin numbers in this document are assumed values and must be checked against the real wiring.
8. **Not a medical device.** Clinical use requires ethics review, clinical evaluation and the necessary medical-device approval.

---

## 15. Future Work

1. Prototype measurements: e-stop response time, stop time on communication loss and joint-angle repeatability.
2. Adjust the minimum speed candidate for users with low speed tolerance.
3. Add a secondary sensor to reduce angle measurement error.
4. Run a pilot at one or two community sites before widening the scope of use.
