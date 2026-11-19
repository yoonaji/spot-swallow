# 1주차 측정 결과

측정일: 2026-11-12. 제어 알고리즘의 입력 세 가지(처리율 μ, 복구 시간 Tready, 동시 회수 비율 f_w)를 준비한다.

## 1. 작업 처리 시간 (로컬 측정)

`scripts/e2e.py --merchants 100 --days 3` 로 작업 300건(거래 60,823건)을 워커 3대로 처리한 뒤
`tools/measure/processing_time.py` 로 집계했다. 가맹점별 거래 수는 평균 200건, ±80% 범위로 다르게 생성했다.

| 항목 | 값 |
| --- | --- |
| 작업당 평균 처리 시간 | 214.2 ms |
| 표준편차 / 변동계수(CV) | 122.5 ms / 0.57 |
| p50 / p95 / p99 / 최대 | 215 / 347 / 373 / 1,193 ms |
| 워커 한 대 처리율 μ | 4.67 건/초 |
| 거래 1건당 처리 시간 (기울기) | 1.03 ms (`WORK_MICROS_PER_TX=1000` 설정과 일치) |

- 처리 시간은 거래 수에 거의 비례한다. 실험에서는 `WORK_MICROS_PER_TX` 로 μ 를 조절한다.
  중간보고서 예시(μ = 0.5 건/초, 작업당 약 2초)에 맞추려면 `WORK_MICROS_PER_TX=10000` 을 쓴다.
- CV 0.57 은 식 (7)이 가정하는 "평균 처리율" 근사가 얼마나 틀리는지 볼 때의 기준값이다.

## 2. 복구 시간 Tready (EC2 Spot)

`tools/measure/ec2_tready.py` 로 측정한다. AWS 계정이 필요해 아직 실행하지 않았다.

```bash
pip install boto3 && aws configure
python3 tools/measure/ec2_tready.py --dry-run                 # 권한 확인
python3 tools/measure/ec2_tready.py --runs 20 --instance-type c6i.large --region ap-northeast-2
python3 tools/measure/summarize_tready.py data/tready.csv
```

구간을 나눠 기록한다: 인스턴스 확보(provision), OS 부팅(boot), 앱 기동(app_start).
워커 이미지를 미리 구운 경우의 복구 시간은 `tready_baked_s = boot_s + app_start_s` 로 본다.
분위수를 쓰므로 최소 20회는 측정한다. 측정 전까지는 SkyServe 논문의 값(모델 서버 기동 183초)과
중간보고서 예시(평상시 2분 30초, 지연 시 5분)를 임시로 쓴다.

## 3. 공개 회수 기록과 동시 회수 비율 f_w

원본: [skypilot-org/spot-traces](https://github.com/skypilot-org/spot-traces) (Can't Be Late, NSDI'24 저자 공개).
5분(일부 34초) 간격으로 살아 있는 인스턴스 수를 기록한 것이다. `tools/traces/convert.py` 로 tick 별 회수 수와
위험 구간 길이 W 별 f_w 분위수를 계산했다. 아래는 여러 인스턴스를 가진 trace 만 추린 것이고,
앞 60% 로 추정한 값(train)과 뒤 40% 의 값(test)을 나란히 적었다. W = 300초(한 tick).

| trace | 인스턴스 | 기간(시간) | 회수 수 | 2대 이상 동시 회수 | f_w q95 train → test | f_w q99 train → test |
| --- | --- | --- | --- | --- | --- | --- |
| AWS CPU r5.16xlarge, 3개 영역 풀 | 3 | 152 | 221 | 4 | 0.50 → 0.33 | 1.0 → 1.0 |
| AWS V100, 4개 영역 풀 | 4 | 177 | 200 | 5 | 0.33 → 0.33 | 1.0 → 1.0 |
| GCP CPU c3-88, 4개 영역 풀 | 4 | 46 | 3 | 0 | 0 → 0 | 0 → 0 |
| AWS V100 4-node, us-east-1f | 4 | 305 | 195 | 43 | 1.0 → 0.33 | 1.0 → 1.0 |
| AWS V100 4-node, us-east-2a | 4 | 428 | 75 | 13 | 0 → 0 | 0.5 → 0 |
| AWS V100 4-node, us-west-2c | 4 | 428 | 330 | 90 | 0 → 0.33 | 1.0 → 1.0 |
| AWS V100 가용성, 9개 영역 풀 | 9 | 1,092 | 1,717 | 187 | 0.22 → 0.20 | 0.50 → 0.33 |
| AWS V100/K80 가용성, 8개 풀 | 8 | 649 | 1,211 | 246 | 0.43 → 0.33 | 0.67 → 0.64 |
| AWS V100 16-node 가용성, us-east-2b | 16 | 271 | 910 | 118 | 0.06 → 1.0 | 1.0 → 1.0 |
| AWS V100 16-node 가용성, us-west-2a | 16 | 395 | 1,473 | 123 | 1.0 → 0 | 1.0 → 1.0 |
| AWS V100 16-node 가용성, us-west-2c | 16 | 395 | 1,366 | 114 | 1.0 → 0 | 1.0 → 1.0 |

## 알고리즘 설계에 주는 시사점

1. **한 영역에 몰린 풀은 "전부 회수"가 드물지 않다.** 다중 노드 trace 대부분에서 f_w 의 q99 가 1.0 이다.
   K(n) = ⌈f_p · n⌉ 이 n 과 같아지면 스팟 여유 용량만으로는 어떤 n 도 식 (8)을 만족할 수 없다.
   따라서 **온디맨드 대체(폴백)는 선택이 아니라 필수**이고, p 값(0.90/0.95/0.99)에 따른 비용 차이가 핵심 실험이 된다.
2. **분포가 시간에 따라 크게 바뀐다.** 16-node us-east-2b 는 앞 60% 에서 q95 = 0.06 이었지만 뒤 40% 에서는 1.0 이다.
   고정된 과거 분포보다 최근 기록에 가중치를 두는 추정(지수 감쇠)이 필요하다는 근거이고,
   train/test 를 나눈 평가를 반드시 보고해야 한다.
3. **여러 영역에 나눈 풀은 동시 회수가 줄어든다.** 9개 영역 풀은 q99 가 0.33~0.50 이다.
   다만 본 연구는 단일 클러스터를 다루므로, 이 결과는 한계와 향후 확장(여러 영역·인스턴스 종류)에서 언급한다.
4. **CPU 와 GPU 의 회수 양상이 다르다.** GCP CPU 풀은 46시간 동안 회수 3건뿐이었다. 정산 배치는 CPU 작업이므로
   "평상시" 시나리오는 CPU trace, "회수 집중" 시나리오는 다중 노드 GPU trace 를 쓰는 구성이 합리적이다.
5. **기록 간격이 5분이다.** 위험 구간이 5분보다 짧으면 한 tick 안의 회수 순서를 알 수 없다.
   시뮬레이터에서는 tick 안의 회수 시점을 균등 분포로 흩뿌리고, 이 가정을 한계로 적는다.

## 재현

```bash
git clone --depth 1 https://github.com/skypilot-org/spot-traces data/raw/spot-traces
python3 tools/traces/convert.py --src data/raw/spot-traces --out data/traces --train-fraction 0.6
```
