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

## 보안 부트스트랩 오버헤드 (2026-11-20)

원자료: [`docs/data/tready-security-2026-11-20.csv`](data/tready-security-2026-11-20.csv).
도구: `tools/measure/security_overhead.py`, 스크립트 `infra/bootstrap/vault-host.sh`, `infra/bootstrap/dyn-secret.sh`.

### 구성

- **안정 영역 역할 호스트** (측정하는 동안만, t4g.small 온디맨드, 워커와 같은 서브넷 ap-northeast-2d)
  - Postgres 15: 워커용 그룹 롤 `swallow_worker` 는 결과 테이블 `SELECT, INSERT` 만. `public` 스키마 기본 권한 회수
  - Vault (TLS, 사설 IP 인증서): database 엔진이 요청마다 `swallow_worker` 에 속한 1시간짜리 DB 계정을 만든다.
    워커 정책은 `database/creds/worker` 읽기만, AppRole secret_id 는 1회용, 전달은 응답 래핑(1회만 풀림)
  - 시작 때 자체 점검: 발급 계정으로 INSERT 는 되고 CREATE TABLE 은 거부되는지 확인 → 통과
- **스팟 워커 부트스트랩** (JDK 설치 후·앱 기동 전): 래핑 해제 → AppRole 로그인 → DB 계정 발급 → `/run/swallow/worker.env` (0600).
  이미지에는 비밀이 없고, 인바운드 포트를 열지 않는다(기본 보안 그룹: 같은 그룹끼리만 접근).
- baseline 과 dyn-secret 을 **번갈아** 20회씩 띄웠다(시간대 차이가 한쪽에 몰리지 않게). 40회 모두 성공, dyn-secret 20회 모두 자격 증명 수신 확인.

### 결과 (초)

| 구간 | baseline p50 / p95 | dyn-secret p50 / p95 |
| --- | --- | --- |
| provision_s | 4.4 / 4.5 | 4.4 / 4.5 |
| boot_s | 9.6 / 10.5 | 9.7 / 10.5 |
| bootstrap_s | - | **0.092 / 0.100** (최대 0.101) |
| app_start_s | 0.8 / 0.9 | 0.8 / 0.8 |
| tready_baked_s | 10.4 / 11.3 | 10.6 / 11.4 |

- 보안 부트스트랩 자체는 **약 90ms** (TLS 연결 3번 + Vault 처리 + DB 계정 생성)다. Tready 대비 약 1%로, 실행 간 흔들림(약 1초)보다 작다.
- 그래서 **이 구성의 보안 비용은 경계선(약 4분) 기준으로 사실상 0** 이다. "보안을 강하게 해도 복구 속도 손해가 없다"는 근거로 쓸 수 있다.
- 다만 이 숫자는 하한이다. 들어가지 않은 것: Vault 를 다른 영역·리전에 둘 때의 왕복, Vault 가 몰릴 때(동시 회수로 워커 여러 대가 한꺼번에 요청)의 대기,
  IAM 기반 인증(STS 서명 확인), 이미지 서명 확인, mTLS 인증서 발급, JVM 안에서 DB 에 TLS 로 붙는 시간.
  동시 요청 부하는 4주차 GameDay 에서 함께 볼 수 있다.

### 측정 방식의 한계

- 측정 계정이 보안 그룹을 만들 수 없어 Vault 포트에 밖에서 접근할 수 없다. 그래서 래핑된 secret_id 를 Vault 호스트가 미리 25개 만들어 콘솔 출력으로 넘겼다.
  운영에서는 제어기(안정 영역)가 워커를 띄우기 직전에 하나씩 발급한다(수십 ms, Tready 바깥).
- 콘솔 출력은 긴 줄을 잘라서, CA 인증서는 64자씩 나눠 보내고 SHA-256 으로 확인한다.
- 부트스트랩의 JSON 파싱은 python3 을 3번 띄운다(각 수십 ms 이하). 실제 워커는 Java 안에서 하므로 이 몫만큼 더 짧아진다.

### 재현

```bash
python3 tools/measure/security_overhead.py --runs 20 --subnet <기본 VPC 서브넷 ID>
python3 tools/measure/summarize_tready.py data/tready-security.csv
```

`ec2_tready.py` 에는 이 측정을 위해 `--variant`, `--bootstrap-script` 와 자체 종료 안전장치(`shutdown -h +30`,
`InstanceInitiatedShutdownBehavior=terminate`)를 넣었다. Vault 호스트도 180분 뒤 스스로 종료되고, 드라이버는 예외가 나도 지운다.

## 측정 계정 권한 메모

측정 전용 IAM 사용자(`spot-swallow`)에는 `ec2:DescribeSpotPriceHistory`, `pricing:GetProducts` 가 없어 실제 가격을
읽지 못했다. 시뮬레이터의 가격(`capacity.policies.Prices`: 스팟 0.037, 온디맨드 0.107 $/h)은 아직 가정값이다.
