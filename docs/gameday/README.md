# GameDay: 모르는 장애를 대시보드와 런북만으로 복구하기

플랜 "장애 대응 경험"의 훈련이다. 주입기는 장애 4종 중 1~2개를 2~10분 사이 무작위 시각에 몰래 넣는다.
대응하는 사람은 **무엇이 들어갔는지 모른 채** Grafana 알림과 대시보드, [런북](../runbook.md)만 보고 원인을 찾고 복구한다.
3회 반복해 감지 시간(MTTD)과 복구 시간(MTTR)이 줄어드는지 본다.

## 준비 (터미널 3개)

```
# 1. 시스템과 부하
docker compose up -d --scale settlement-worker=3
python3 scripts/load.py --rate 1.0 --duration 3600 &

# 2. 브라우저: Grafana http://localhost:3000/d/spot-swallow (로그인 없이 열림), 알림 목록 Alerting → Alert rules

# 3. 주입기 (이 창은 보지 않는다. 출력에 정답이 나오지 않지만 그래도 최소화해 둔다)
python3 scripts/gameday.py start
```

## 진행

1. 알림이 울리거나 대시보드에서 이상을 본 순간: `python3 scripts/gameday.py detected`
2. 런북대로 원인을 찾고 복구한다. 어떤 명령을 왜 쳤는지 메모해 둔다(보고서 타임라인).
3. `curl -s localhost:8080/reconciliation` 이 `consistent: true` 이고 알림이 모두 꺼지면: `python3 scripts/gameday.py recovered`
   (이 명령이 대사 결과도 함께 기록한다)
4. `python3 scripts/gameday.py reveal` 로 정답과 MTTD/MTTR 을 확인한다.
5. [장애 보고서 양식](report-template.md)을 복사해 `docs/gameday/report-<회차>.md` 로 쓰고, 런북에서 막힌 곳을 고친다.

## 리허설 (2026-11-24, 대응 자동, 부하 초당 1건)

후보 6종을 미리 주입해 알림이 울리는지와 런북 절차로 복구되는지만 확인했다. 어느 알림이 울렸는지는 적지 않는다(정답 노출).

| 후보 | 알림 | 복구 후 대사 | GameDay 에 넣음 |
| --- | --- | --- | --- |
| 4종 | 15~167초 안에 울림 (대사 변조는 [알림 점검](../runbook.md) 결과 21초) | 유실·불일치·이중 확정 0 | 넣음 |
| 워커 3대 중 2대 강제 종료 | 5분간 없음 | 0 | 뺌 |
| PostgreSQL 재시작 | 5분간 없음 (스스로 회복) | 0 | 뺌 |

찾은 감시 공백: **제어기 없이 돌릴 때 워커가 줄어도 알림이 없다.** 남은 워커가 부하를 따라가는 동안에는 작업 나이도 기준(150초)에
닿지 않는다. "준비된 워커가 Nbase보다 적음" 알림은 제어기 지표가 있어야 울린다. 운영에서는 제어기가 항상 돌므로 큰 문제는 아니지만,
큐 소비자 수 기반 알림을 따로 둘지 보고서에서 다룬다.

## 끝난 뒤 정리

```
docker compose unpause rabbitmq 2>/dev/null; docker compose up -d --scale settlement-worker=3
```

## 기록

| 회차 | 날짜 | 주입 | MTTD | MTTR | 대사 | 보고서 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | | | | | | |
| 2 | | | | | | |
| 3 | | | | | | |
