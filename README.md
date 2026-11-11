# spot-swallow

제비처럼 사라졌다가 다시 돌아오는 스팟 워커. 하지만 메시지는 절대 삼키지 않습니다. (*Never swallows a message.*)

스팟 인스턴스가 언제 회수되어도 **정산 데이터 유실·중복 0건**을 보장하는 비동기 정산 시스템과,
복구 시간과 동시 회수를 고려해 예비 워커 수를 정하는 **적응형 여유 용량 제어**를 다루는 졸업프로젝트입니다.

> 경희대학교 컴퓨터공학과 2026 졸업프로젝트
> "가변적 복구 시간과 동시 회수를 고려한 스팟 인스턴스 기반 비동기 작업 처리 시스템의 적응형 여유 용량 제어"

## 구조

```
 [안정 영역: 온디맨드]                                        [스팟 영역]
  클라이언트 ─POST─▶ settlement-api ──(1 트랜잭션)──▶ PostgreSQL
                        │  settlement_jobs + outbox      ▲   ▲
                        │                                │   │ 짧은 트랜잭션으로 결과 확정
                   Outbox 발행기 ──Publisher Confirm──▶ RabbitMQ ──수동 Ack──▶ settlement-worker × N
                                                                     (prefetch=1)
  Prometheus / Grafana ◀── 지표 (작업 나이, 미발행 아웃박스, 완료·중복 흡수·DLQ 건수)
```

| 장애 상황 | 일어나는 일 | 막는 장치 |
| --- | --- | --- |
| 작업 저장 직후, 큐 발행 전에 API 가 죽음 | 아웃박스에 남아 있어 재시작 후 발행 | Transactional Outbox |
| 브로커 수신 후, 발행 완료 표시 전에 죽음 | 같은 메시지가 한 번 더 발행됨 | 워커 멱등성 |
| 워커가 계산 중 회수됨 | Ack 가 없으므로 다른 워커에게 재전달 | 수동 Ack |
| 워커가 커밋 후, Ack 전 회수됨 | 재전달되지만 PK 충돌로 결과는 한 번만 확정 | `settlement_results.job_id` PK |
| 다시 해도 실패하는 메시지 | DLQ 로 격리, 큐 전체가 막히지 않음 | Dead Letter Queue |
| DB 일시 장애 | 커밋 전이므로 큐로 되돌려 재시도 | 커밋 후 Ack 순서 |

전달은 최소 한 번(at-least-once), 반영은 정확히 한 번(effectively-once)입니다.

## 실행

필요: JDK 21, Maven, Docker

```bash
mvn -q package -DskipTests
docker compose up -d --build --scale settlement-worker=3

python3 scripts/e2e.py --resubmit        # 작업 100건 처리 후 대사 결과 확인
```

| 주소 | 용도 |
| --- | --- |
| http://localhost:8080/reconciliation | 대사 결과 (유실·중복·금액 불일치) |
| http://localhost:15672 | RabbitMQ 관리 화면 (swallow / swallow) |
| http://localhost:9090 | Prometheus |
| http://localhost:3000 | Grafana |

### 장애 주입

```bash
# 워커가 "커밋 후, Ack 전" 에 30% 확률로 즉시 종료 (kill -9 와 같은 효과)
CHAOS_CRASH_POINT=AFTER_COMMIT_BEFORE_ACK CHAOS_CRASH_PROBABILITY=0.3 scripts/reset.sh 3
python3 scripts/e2e.py

# 계산 중 종료
CHAOS_CRASH_POINT=BEFORE_COMMIT CHAOS_CRASH_PROBABILITY=0.3 scripts/reset.sh 3

# 발행기가 브로커 수신 후, 발행 완료 표시 전에 종료
CHAOS_PUBLISHER_CRASH_PROBABILITY=0.2 scripts/reset.sh 3

# 실행 중인 워커를 4초마다 하나씩 docker kill (스팟 회수 흉내)
scripts/reset.sh 3 && python3 scripts/e2e.py --kill-every 4
```

워커는 스스로 재시작하지 않습니다(`restart: "no"`). `e2e.py` 가 Auto Scaling 그룹 역할을 맡아,
종료된 워커를 `--recovery` 초(기본 3초) 뒤에 다시 띄웁니다. 3주차에는 Python 제어기가 이 역할을 맡고,
EC2 에서 측정한 복구 시간 분포를 주입합니다.

### 1주차 검증 결과 (작업 100건, 워커 3대)

| 시나리오 | 강제 종료 | 흡수한 중복 전달 | 유실 | 중복 확정 | 금액 불일치 |
| --- | --- | --- | --- | --- | --- |
| 장애 없음 (+ 같은 요청 100건 재전송) | 0 | 0 | 0 | 0 | 0 |
| 커밋 후 Ack 전 30% 종료 | 약 50회 | 51 | 0 | 0 | 0 |
| 4초마다 회수 + 커밋 후 Ack 전 20% 종료 | 27회 회수, 48회 교체 | 24 | 0 | 0 | 0 |
| 4초마다 회수 + 발행기 2% 종료 | 회수 다수, 발행기 1회 | 4 | 0 | 0 | 0 |

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| POST | `/settlement-jobs` | `{"merchantId":"M0001","settlementDate":"2026-11-01"}` 접수. 새로 접수되면 202, 이미 있으면 200 (멱등) |
| GET | `/settlement-jobs/{jobId}` | 작업 상태와 정산 결과 |
| GET | `/reconciliation` | 대사: 입력 거래로 다시 계산한 금액과 확정 결과 비교 |
| POST | `/admin/seed` | 실험용 합성 거래 적재 |

## 데이터 모델

- `transactions`: 정산 입력 거래 (불변)
- `settlement_jobs`: 가맹점 × 정산일 = 작업 1건. `job_id` 가 요청에서 결정되어 같은 요청은 두 번 접수되지 않음
- `outbox`: 작업과 같은 트랜잭션으로 기록되는 발행 대기 메시지
- `settlement_results`: 확정 결과. `job_id` PK 가 멱등성의 핵심
- `job_events`: 처리 이력 (접수, 확정, 중복 흡수, DLQ 격리)

## 진행 상황

- [x] 1주차: 정산 백엔드 뼈대 (Outbox, 수동 Ack, 멱등성, 대사, 장애 주입 훅)
- [ ] 1주차: 처리 시간·EC2 Spot 복구 시간 측정, 공개 회수 기록 변환
- [ ] 2주차: 시뮬레이터와 정책 비교 (고정 여유분, 회수율 기반, 큐 반응형, 제안 기법, 오라클)
- [ ] 3주차: Python 제어기 연동, 회수 주입기, Grafana 대시보드·알림, DLQ 재처리
- [ ] 4주차: 실험, GameDay, 최종 보고서
