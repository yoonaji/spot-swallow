# EC2 Spot 복구 시간(Tready) 실측

측정일: 2026-11-19 (UTC 15:21~15:55). 원자료: [`docs/data/tready-c6i-large-2026-11-19.csv`](data/tready-c6i-large-2026-11-19.csv)

## 조건

| 항목 | 값 |
| --- | --- |
| 리전 / 영역 | ap-northeast-2 (서울) / 20회 모두 ap-northeast-2d 에 배치됨 |
| 인스턴스 | c6i.large 스팟, one-time, Amazon Linux 2023 최신 AMI (x86_64) |
| 반복 | 20회 순차 (측정 사이 20초 대기), 실패 0회 |
| 도구 | `tools/measure/ec2_tready.py`, 요약 `tools/measure/summarize_tready.py` |
| 시계 | 로컬 시계와 AWS 응답 Date 헤더 차이 1초 이내 확인 (Date 헤더 해상도가 1초) |
| 비용 | 21대 × 약 2분. 1달러 훨씬 아래 |

## 결과 (초)

| 구간 | 뜻 | p50 | p95 | 최대 |
| --- | --- | --- | --- | --- |
| provision_s | RunInstances 요청 → running 상태 | 4.4 | 5.0 | 5.2 |
| boot_s | 요청 → 사용자 스크립트 시작 (OS 부팅 포함) | 9.9 | 10.5 | 10.9 |
| install_s | JDK 설치 (기본 AMI 라서 생기는 구간) | 27.6 | 28.8 | 29.1 |
| app_start_s | 장난감 JVM 프로그램 기동 | 0.8 | 0.8 | 0.9 |
| **tready_baked_s** | 미리 구운 이미지 기준 = boot + app_start | **10.7** | **11.3** | 11.7 |
| tready_this_ami_s | 기본 AMI 에서 설치까지 포함 | 38.3 | 40.3 | 40.5 |

`app_start_s` 는 스크립트 안의 작은 Java 프로그램이라 실제 워커보다 짧다. 그래서 실제 `settlement-worker` jar 를
2코어(c6i.large 와 같은 vCPU 수)로 제한해 로컬에서 10회 띄웠다 ([원자료](data/worker-start-local-2cpu-2026-11-19.txt)).
JVM 시작부터 Spring 의 "Started" 로그(RabbitMQ 연결 포함)까지 **p50 4.3초, 최대 5.2초**였다.

따라서 **실제 워커의 Tready ≈ 9.9 + 4.3 ≈ 15초 (q95 약 16초)** 로 본다. 시뮬레이터의 `measured` 시나리오는
중앙값 14.6초, 로그정규 σ = 0.06 (q95/중앙값 ≈ 1.1) 을 쓴다.

## 해석

1. **가정값(중앙값 150초)보다 10배 짧고, 흔들림이 거의 없다.** 20회 모두 같은 영역, 같은 시간대에 용량 부족 없이 확보됐다.
   이것은 "용량이 충분한 평상시" 한 점이다. 회수가 몰리는 시간에는 같은 풀의 용량이 부족해 확보 자체가 늦어지거나
   실패하는데(InsufficientInstanceCapacity), 이번 측정에서는 한 번도 나오지 않았다.
2. **복구 시간 대부분은 부팅이고, 이미지에 무엇을 미리 넣느냐가 가장 큰 손잡이다.** JDK 를 부팅 후 설치하면 38초로 3.6배가 된다.
   컨테이너 이미지를 레지스트리에서 받는 구성, 큰 모델·캐시를 내려받는 구성은 이보다 훨씬 길어진다(SkyServe 의 모델 서버 183초).
3. **연구 질문과의 관계.** 2주차 경계선(약 4분 이하면 AWS 경고 후 교체로 충분)에 비추면, 이번 실측값은 경계 훨씬 왼쪽이다.
   즉 "정산 워커를 미리 구운 이미지로, 용량이 넉넉할 때 띄우는" 조건에서는 AWS 기본 기능으로 충분하다.
   제안 기법의 가치는 복구 시간이 늘어나는 조건(용량 부족, 무거운 부트스트랩, 보안 절차)에서 나오므로,
   다음 측정은 **복구 시간을 늘리는 요인**을 하나씩 더해 경계선 위에 찍는 방향이 맞다.

## 시뮬레이션 재실행 (실측 Tready)

`sim/run_experiments.py` 에 `measured` 시나리오(회수 집중 trace + 실측 Tready)를 넣고, 복구 시간 스윕에 0.1배(15초)·0.25배(37.5초)를 더해
전체 실험(1,125회)을 다시 돌렸다. 기존 시나리오 결과는 이전과 똑같이 재현됐다(시드 고정).
[경계선 그림](figures/crossover.png)에 실측 구간(15~38초)을 초록 띠로 표시했다.

| 정책 (회수 집중 + 실측 Tready) | 위반율 | 1,000건당 비용 | 온디맨드 전용 대비 |
| --- | --- | --- | --- |
| AWS 재조정 | 0% | $0.025 | 0.35 |
| 제안 기법 | 0% | $0.025 | 0.36 |
| 제안 기법 + AWS 재조정 | 0% | $0.025 | 0.35 |
| 고정 여유 +2 | 0% | $0.039 | 0.56 |
| 온디맨드 전용 | 0% | $0.069 | 1.00 |

- 복구가 15초면 모든 스팟 정책이 위반 0 이다. 차이는 비용뿐이고, **제안 기법은 AWS 재조정과 같은 비용**으로 내려온다.
  고정 여유 +2 보다 37% 싸다. p = 0.99 로 올려도 비용이 그대로였다(위험 구간이 짧아 여유를 거의 두지 않는다).
- 즉 제안 기법은 복구가 빠를 때 손해가 없고(2주차의 3.75분 구간에서 보였던 과잉 비용 0.48 vs 0.37 은 이 영역에서는 생기지 않는다),
  복구가 5분을 넘을 때 이득이 난다. "언제나 켜 둬도 되는 기법"이라는 주장을 할 수 있는 근거다.

## 다음 측정 (보안 부트스트랩)

`ec2_tready.py` 에 JDK 설치 후·앱 기동 전에 임의의 셸 단계를 끼우는 옵션을 넣었다.
`bootstrap_start` / `bootstrap_done` 마크로 그 구간(`bootstrap_s`)을 따로 재고, `tready_baked_s` 에 포함한다.

```bash
python3 tools/measure/ec2_tready.py --runs 20 --variant baseline   --out data/tready-sec.csv
python3 tools/measure/ec2_tready.py --runs 20 --variant dyn-secret --bootstrap-script infra/bootstrap/dyn-secret.sh --out data/tready-sec.csv
python3 tools/measure/summarize_tready.py data/tready-sec.csv     # 조건별 표 + baseline 대비 p50/p95 증가분
```

부트스트랩 스크립트 후보: Vault 에 인스턴스 신원(AWS IAM auth)으로 로그인 → 수명 짧은 DB 자격 증명 발급 → 워커 환경 변수로 전달.
Vault 서버가 필요하므로 안정 영역 쪽 배치를 먼저 정해야 한다.

안전장치도 추가했다: 인스턴스 안에서 `shutdown -h +30` 을 걸고 `InstanceInitiatedShutdownBehavior=terminate` 로 띄워,
측정 스크립트(로컬)가 중간에 죽어도 30분 뒤 스스로 종료된다. 결과 파일에 `variant`, `bootstrap_s` 열이 추가됐다.

## 측정 계정 권한 메모

측정 전용 IAM 사용자(`spot-swallow`)에는 `ec2:DescribeSpotPriceHistory`, `pricing:GetProducts` 가 없어 실제 가격을
읽지 못했다. 시뮬레이터의 가격(`capacity.policies.Prices`: 스팟 0.037, 온디맨드 0.107 $/h)은 아직 가정값이다.
