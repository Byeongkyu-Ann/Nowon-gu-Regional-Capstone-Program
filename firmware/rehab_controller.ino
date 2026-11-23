/*
 * 상지 재활 로봇 - 아두이노 펌웨어 (PC 제어 프로그램과 짝)
 *
 * ※ 핀 번호는 '가정값'입니다. 반드시 기존 스케치의 실제 배선에 맞게 아래 상수를 수정하세요.
 * ※ 기존 스케치의 센서(굴곡/FSR) 읽기·서보 구동 코드가 이미 있다면 이 파일의 해당 부분을 기존 코드로 교체하고,
 *    "비상버튼 / 부저 / 하트비트 감시" 부분만 병합해도 됩니다.
 *
 * 프로토콜 (115200 bps, 줄 단위 ASCII)
 *   PC -> 아두이노   M<n>,<angle>,<speed>   n번 서보를 angle[deg]로, speed[deg/s]로 이동
 *                    STOP                   모든 서보 즉시 현재 위치 유지
 *                    BUZ,<ms>               부저 경보 (ms 동안 비프 패턴)
 *                    HB                     생존 신호
 *   아두이노 -> PC   DATA,<flex>,<fsr>      센서값 (50 ms 주기)
 *                    EVT,ESTOP              하드웨어 비상버튼 눌림 (눌린 동안 반복 전송)
 *
 * 안전 기능
 *   - 비상버튼: PC와 무관하게 아두이노가 직접 서보 정지 + 부저 + EVT,ESTOP 전송
 *   - 하트비트 감시: HB 또는 임의 명령이 COMM_TIMEOUT_MS 동안 없으면 서보 정지 + 부저
 */
#include <Servo.h>

// ---------------- 핀 설정 (가정값: 실제 배선에 맞게 수정) ----------------
const uint8_t PIN_SERVO[2] = {9, 10};      // 1번: 굴곡(주먹), 2번: 신전
const uint8_t PIN_FLEX     = A0;
const uint8_t PIN_FSR      = A1;
const uint8_t PIN_ESTOP    = 2;            // 비상버튼: 한쪽 GND, INPUT_PULLUP (눌림=LOW)
const uint8_t PIN_BUZZER   = 8;            // 능동 부저(+)  / 수동 부저면 tone() 사용
// -----------------------------------------------------------------------
const unsigned long BAUD            = 115200;
const unsigned long COMM_TIMEOUT_MS = 2000;
const unsigned long DATA_PERIOD_MS  = 50;
const unsigned long DEBOUNCE_MS     = 20;
const unsigned long SERVO_DT_MS     = 10;   // 슬루 제한 갱신 주기
const int           BEEP_ON_MS = 150, BEEP_OFF_MS = 100;

Servo servo[2];
float pos[2]    = {0, 180};   // 현재 각도 (PC의 m1_init/m2_init 과 동일하게 맞출 것)
float target[2] = {0, 180};
float speedDps[2] = {30, 30};
bool  halted = false;

unsigned long lastRx = 0, lastData = 0, lastServo = 0;
unsigned long buzUntil = 0;
bool lastEstopLevel = HIGH; unsigned long estopChangedAt = 0; bool estopActive = false;
String line;

void buzzerService() {
  unsigned long now = millis();
  bool on = false;
  if ((long)(buzUntil - now) > 0) {
    unsigned long phase = (buzUntil - now) % (BEEP_ON_MS + BEEP_OFF_MS);
    on = phase < BEEP_ON_MS;
  }
  digitalWrite(PIN_BUZZER, on ? HIGH : LOW);
}

void startBuzzer(unsigned long ms) { buzUntil = millis() + ms; }

void haltServos() {
  halted = true;
  for (int i = 0; i < 2; i++) target[i] = pos[i];   // 현재 위치 유지
}

void handleLine(const String &s) {
  lastRx = millis();
  if (s == "HB") return;
  if (s == "STOP") { haltServos(); return; }
  if (s.startsWith("BUZ,")) { startBuzzer(s.substring(4).toInt()); return; }
  if (s.length() > 2 && s[0] == 'M') {
    int c1 = s.indexOf(','), c2 = s.indexOf(',', c1 + 1);
    if (c1 < 0 || c2 < 0) return;
    int n = s.substring(1, c1).toInt();
    if (n < 1 || n > 2 || estopActive) return;      // 비상 중에는 이동 명령 무시
    halted = false;
    target[n - 1]   = constrain(s.substring(c1 + 1, c2).toInt(), 0, 180);
    speedDps[n - 1] = max(1, (int)s.substring(c2 + 1).toInt());
  }
}

void setup() {
  Serial.begin(BAUD);
  pinMode(PIN_ESTOP, INPUT_PULLUP);
  pinMode(PIN_BUZZER, OUTPUT);
  for (int i = 0; i < 2; i++) { servo[i].attach(PIN_SERVO[i]); servo[i].write((int)pos[i]); }
  lastRx = millis();
}

void loop() {
  unsigned long now = millis();

  // 1) 시리얼 수신
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') { line.trim(); if (line.length()) handleLine(line); line = ""; }
    else if (ch != '\r' && line.length() < 40) line += ch;
  }

  // 2) 비상버튼 (디바운스) - PC 판단과 무관하게 즉시 동작
  bool lvl = digitalRead(PIN_ESTOP);
  if (lvl != lastEstopLevel) { lastEstopLevel = lvl; estopChangedAt = now; }
  if (now - estopChangedAt > DEBOUNCE_MS) {
    bool pressed = (lvl == LOW);
    if (pressed && !estopActive) { estopActive = true; haltServos(); startBuzzer(2500); }
    if (!pressed) estopActive = false;
  }
  static unsigned long lastEvt = 0;
  if (estopActive && now - lastEvt > 100) { Serial.println(F("EVT,ESTOP")); lastEvt = now; }

  // 3) 통신 두절 감시
  static bool commLost = false;
  if (now - lastRx > COMM_TIMEOUT_MS) {
    if (!commLost) { commLost = true; haltServos(); startBuzzer(2500); }
  } else commLost = false;

  // 4) 서보 슬루 제한 구동
  if (now - lastServo >= SERVO_DT_MS) {
    float dt = (now - lastServo) / 1000.0f; lastServo = now;
    for (int i = 0; i < 2; i++) {
      float step = speedDps[i] * dt, d = target[i] - pos[i];
      pos[i] += (fabs(d) <= step) ? d : (d > 0 ? step : -step);
      servo[i].write((int)(pos[i] + 0.5f));
    }
  }

  // 5) 센서 송신
  if (now - lastData >= DATA_PERIOD_MS) {
    lastData = now;
    Serial.print(F("DATA,")); Serial.print(analogRead(PIN_FLEX));
    Serial.print(','); Serial.println(analogRead(PIN_FSR));
  }

  buzzerService();
}
