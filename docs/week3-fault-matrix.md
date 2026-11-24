# 3주차: 장애 주입 매트릭스 자동 실행

플랜의 "장애 주입 매트릭스" 8개 행을 `scripts/fault_matrix.py` 하나로 자동 실행한다. 행마다 시스템을 새로 띄우고
(`docker compose down -v` → `up`, 워커 4대), 가맹점 50 × 8일 = 작업 400건을 넣고, 처리 도중 장애를 주입한 뒤,
모두 처리되면 대사(`GET /reconciliation`)로 유실 0, 이중 확정 0, 금액 일치를 확인한다. 꺼진 워커·API 는
감독 스레드가 5초 뒤 다시 띄운다(Auto Scaling 역할).

```
python3 scripts/fault_matrix.py                    # 전체 8개 행 (약 10분)
python3 scripts/fault_matrix.py --rounds 3         # 행마다 3번
python3 scripts/fault_matrix.py postgres-restart   # 한 행만
```

## 결과 (2026-11-24, 로컬 docker, 행마다 3회, 작업 9,600건)

| 죽는 지점 | 주입 | 막는 장치 | 작업 | 유실 | 금액 불일치 | 이중 확정 | 흡수한 중복 | DLQ | 최대 완료시간(초) | 결과 |
|---|---|---|---|---|---|---|---|---|---|---|
| API 커밋 직후, 발행 전 | 발행기(API) SIGKILL | Outbox | 1200/1200 | 0 | 0 | 0 | 0 | 0 | 49.2–49.8 | 3/3 PASS |
| 발행 후, PUBLISHED 표시 전 | 브로커 수신 직후 1% 확률로 발행기 종료 (6~11회) | Outbox 재시도 + 결과 PK | 1200/1200 | 0 | 0 | 0 | 98–189 | 0 | 46.9–116.3 | 3/3 PASS |
| 워커 계산 중 | 3초마다 워커 SIGKILL 10회 | 수동 Ack | 1200/1200 | 0 | 0 | 0 | 0 | 0 | 73.7–75.3 | 3/3 PASS |
| 워커 커밋 후, Ack 전 | 커밋 직후 5% 확률로 워커 종료 (12~23회) | 결과 PK 멱등성 | 1200/1200 | 0 | 0 | 0 | 12–23 | 0 | 80.7–117.0 | 3/3 PASS |
| RabbitMQ 재시작 | `docker compose restart rabbitmq` | durable 큐, persistent 메시지 | 1200/1200 | 0 | 0 | 0 | 4 | 0 | 59.9–60.2 | 3/3 PASS |
| PostgreSQL 재시작 | `docker compose restart postgres` | 커밋 후 Ack, 일시 장애 재시도 | 1200/1200 | 0 | 0 | 0 | 0 | 0 | 50.0–50.4 | 3/3 PASS |
| 독성 메시지 | 존재하지 않는 작업 ID 투입 | DLQ | 1200/1200 | 0 | 0 | 0 | 0 | 1 (기대값) | 49.3–49.4 | 3/3 PASS |
| 동시 대량 회수 | 워커 4대 중 2대 동시 SIGKILL | 교체 + 수동 Ack | 1200/1200 | 0 | 0 | 0 | 0 | 0 | 55.0–55.8 | 3/3 PASS |

원자료: [data/fault-matrix-2026-11-24.csv](data/fault-matrix-2026-11-24.csv). "흡수한 중복"은 결과 PK 충돌로 반영 없이
Ack 만 보낸 재전달 수다. 최대 완료시간은 접수부터 완료까지 가장 오래 걸린 작업이고, 모두 목표 5분(300초) 안이다.
API 강제 종료는 3회를 시도하지만 두 번째 종료 뒤 API 가 아직 재시작 중이면 세 번째는 죽일 대상이 없어 실제 주입은 2회였다.

## 실행하며 찾은 문제

### 1. PostgreSQL 재시작 중 정상 작업이 DLQ 로 갔다 (버그, 고침)

첫 실행에서 PostgreSQL 재시작 행만 FAIL: 400건 중 398건 완료, 2건이 DLQ. 같은 조건으로 다시 돌려도 2/2 재현됐다.
유실은 아니지만(작업은 DB 에 남고 DLQ 재처리로 되살릴 수 있다) 사람이 개입해야 하므로 실패로 본다.

원인: 워커는 `TransientDataAccessException`, `DataAccessResourceFailureException` 만 일시 장애로 보고 재시도했다.
DB 가 재시작되는 순간에는 롤백조차 실패해 `TransactionSystemException: JDBC rollback failed` 가 나오는데, 이것은
`DataAccessException` 이 아니라서 "다시 해도 실패할 메시지"로 분류되어 DLQ 로 갔다.

수정(`SettlementWorker.isTransientDbFailure`): 예외의 원인 사슬을 따라가며 Spring 트랜잭션 예외(`TransactionException`),
`SQLTransientException`·`SQLRecoverableException`, 연결 계열 SQLState(`08xxx` 연결 실패, `57P0x` 관리자 종료)도 일시 장애로 본다.
작업이 없음·잘못된 메시지 같은 진짜 독성 메시지는 그대로 DLQ 로 간다(`TransientFailureTest`). 수정 후 이 행은 5/5 PASS(단독 2회 + 매트릭스 3회).

교훈: "재시도할 오류 / 격리할 오류" 분류가 틀리면 DLQ 가 장애 중 정상 작업의 무덤이 된다. 분류는 예외 타입 하나가 아니라
원인 사슬로 해야 하고, 장애 주입으로만 드러났다(단위 테스트와 강제 종료 1,449회 실험에서는 DB 를 죽이지 않아 나오지 않았다).

### 2. 발행기 종료 확률 5%에서는 발행이 거의 진행되지 않는다 (실험 설정 조정)

발행기는 최대 50건을 한 트랜잭션으로 보내고 PUBLISHED 표시를 커밋한다. 보낸 직후 건마다 5% 확률로 죽으면
배치 하나가 끝까지 갈 확률은 0.95^50 ≈ 7.7% 다. 나머지 92%는 그 배치의 앞선 메시지까지 모두 PENDING 으로 남아
재시작 뒤 다시 발행되고, docker 재시작 대기 시간도 길어져 API 가 10분 넘게 접수를 받지 못했다. 유실은 없지만
실험이 끝나지 않으므로 확률을 1%로 낮췄다(배치 완주 확률 0.99^50 ≈ 60%). 이때도 한 번 죽을 때마다 최대 49건이 다시
발행되어 흡수한 중복이 98~189건으로 가장 많다.

이 설계(배치 단위 커밋)는 평상시 처리량에는 유리하지만 발행기가 자주 죽는 환경에서는 중복 발행을 늘린다.
건마다 PUBLISHED 를 커밋하면 중복이 최대 1건으로 줄어드는 대신 DB 왕복이 늘어난다. 현재는 결과 PK 가 중복을 막으므로
정합성 문제는 아니고, 트레이드오프로 기록만 해 둔다.
