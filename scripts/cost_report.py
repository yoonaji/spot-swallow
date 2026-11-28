#!/usr/bin/env python3
"""
비전공자용 비용 보고서 한 장: 시뮬레이션의 워커 시간(스팟·온디맨드)을 실제 가격표로 월 금액으로 바꾼다.

  python3 scripts/cost_report.py            # docs/cost-report.md

입력: docs/data/sim-robust-2026-11-25.csv (설정 하나로 8개 환경, 12시간 구간 5개씩).
비율(온디맨드 전용 = 1)이 아니라 spot_hours, od_hours 를 가격에 곱하므로, 가격이 바뀌어도 다시 계산할 수 있다.
"""
import csv
from collections import defaultdict
from statistics import mean

SRC = "docs/data/sim-robust-2026-11-25.csv"
OUT = "docs/cost-report.md"
KRW_PER_USD = 1400          # 환율 가정
WINDOWS_PER_MONTH = 60      # 12시간 구간 × 60 = 30일

PRICES = {
    # 이름: (스팟 $/h, 온디맨드 $/h, 출처)
    "가정 (실험에 쓴 값)": (0.037, 0.107, "capacity/policies.py Prices"),
    "공개 가격, 스팟 4개 영역 평균": (0.0292, 0.0960,
                         "doit.com c6i.large ap-northeast-2, 온디맨드 $0.0960, 스팟 영역별 $0.0193·0.0304·0.0313·0.0356 (날짜 표기 없음, 2026-11-29 조회)"),
}
POLICIES = [
    ("on-demand-only", "온디맨드만 쓰기", "가장 안전, 가장 비쌈"),
    ("aws-rebalance", "AWS 기본 (재조정만)", "가장 쌈, 복구가 느리면 위험"),
    ("aws-rebalance+odbase2", "AWS 혼합: 온디맨드 2대 고정", "중간"),
    ("aws-rebalance+odbase3", "AWS 혼합: 온디맨드 3대 고정", "안전, 비쌈"),
    ("proposed+rebalance p=0.95", "제안 기법 p=0.95", "싸고 조금 위험"),
    ("proposed+rebalance p=0.99", "제안 기법 p=0.99", "안전하면서 덜 비쌈"),
]


def main():
    rows = list(csv.DictReader(open(SRC)))
    by_pol = defaultdict(list)
    by_env = defaultdict(list)
    for r in rows:
        by_pol[r["policy"]].append(r)
        by_env[(r["policy"], r["scenario"])].append(r)
    envs = sorted({r["scenario"] for r in rows})
    stats = {}
    for p, _, _ in POLICIES:
        rs = by_pol[p]
        worst = max(mean(float(x["violation_rate"]) for x in by_env[(p, e)]) for e in envs) * 100
        normal = by_env[(p, "normal")]
        stats[p] = {
            "spot_h": mean(float(x["spot_hours"]) for x in rs),
            "od_h": mean(float(x["od_hours"]) for x in rs),
            "spot_h_normal": mean(float(x["spot_hours"]) for x in normal),
            "od_h_normal": mean(float(x["od_hours"]) for x in normal),
            "jobs": mean(float(x["jobs"]) for x in rs),
            "worst": worst,
        }
    od_workers = stats["on-demand-only"]["od_h"] / 12
    jobs_day = stats["on-demand-only"]["jobs"] * 2

    def monthly(p, spot, od, normal=False):
        s = stats[p]
        sh, oh = (s["spot_h_normal"], s["od_h_normal"]) if normal else (s["spot_h"], s["od_h"])
        return (sh * spot + oh * od) * WINDOWS_PER_MONTH

    out = ["# 비용 보고서 한 장: 한 달에 얼마인가", "",
           f"자동 생성: `python3 scripts/cost_report.py`. 입력 `{SRC}` (시뮬레이션, 8개 환경 × 12시간 구간 5개).", "",
           "## 기준", "",
           f"- 부하: 하루 약 {jobs_day / 10000:.0f}만 건 (실험의 처리량). 온디맨드만 쓰면 평균 워커 약 {od_workers:.1f}대가 필요하다.",
           "- 서버: c6i.large (2 vCPU, 서울 리전). 워커 시간 × 시간당 가격, 한 달 = 30일.",
           f"- 환율: 1달러 = {KRW_PER_USD:,}원 (가정).",
           "- \"최악 위반율\": 평상시부터 복구 15분까지 8개 환경 중 가장 나빴던 환경에서, 5분 안에 못 끝난 작업 비율.",
           "- \"8개 환경 평균\": 환경을 미리 모른다고 보고 8개 환경을 똑같은 비중으로 섞은 값. 실제로는 평상시가 대부분이므로 \"평상시\" 칸도 함께 본다.", ""]
    for name, (spot, od, src) in PRICES.items():
        base = monthly("on-demand-only", spot, od)
        out += [f"## 가격: {name}", "", f"스팟 ${spot}/h, 온디맨드 ${od}/h. 출처: {src}", "",
                "| 방식 | 월 비용 (8개 환경 평균) | 온디맨드만 대비 | 월 비용 (평상시) | 최악 위반율 | 한 줄 |",
                "|---|---|---|---|---|---|"]
        for p, label, note in POLICIES:
            m = monthly(p, spot, od)
            mn = monthly(p, spot, od, normal=True)
            out.append(f"| {label} | ${m:,.0f} (약 {m * KRW_PER_USD / 10000:,.0f}만 원) | {m / base:.2f} | "
                       f"${mn:,.0f} | {stats[p]['worst']:.2f}% | {note} |")
        p99 = monthly("proposed+rebalance p=0.99", spot, od)
        b3 = monthly("aws-rebalance+odbase3", spot, od)
        b2 = monthly("aws-rebalance+odbase2", spot, od)
        out += ["",
                f"- 제안 p=0.99 는 비슷하게 안전한 \"온디맨드 3대 고정\"보다 월 ${b3 - p99:,.0f} "
                f"(약 {(b3 - p99) * KRW_PER_USD / 10000:,.0f}만 원, {(1 - p99 / b3) * 100:.0f}%) 싸다.",
                f"- \"온디맨드 2대 고정\"보다도 월 ${b2 - p99:,.0f} 싸면서 최악 위반율은 "
                f"{stats['aws-rebalance+odbase2']['worst']:.2f}% → {stats['proposed+rebalance p=0.99']['worst']:.2f}% 로 낮다.",
                f"- 규모가 커지면 대략 비례한다. 워커 {od_workers:.1f}대가 아니라 100대 규모라면 위 금액에 약 "
                f"{100 / od_workers:.0f}배 (선형 가정, 확인 안 됨).", ""]
    out += ["## 읽는 법과 한계", "",
            "- 절감액보다 **\"얼마를 더 내고 어떤 위험을 줄이나\"** 로 읽는다. AWS 기본은 가장 싸지만 복구가 느린 환경에서 작업의 약 15% 가 5분을 넘긴다.",
            "  제안 기법은 그 위험을 1% 아래로 줄이면서, 같은 수준의 고정 설정보다 덜 낸다.",
            "- 환경을 미리 알고 그 환경 하나에 맞춰 온디맨드 대수를 고르면 AWS 혼합 설정이 같거나 낫다 (`week4-hardening.md` 3절). 이 표는 \"설정 하나로 여러 환경을 버텨야 할 때\"의 비교다.",
            "- 모두 시뮬레이션의 워커 시간이다. 실제 시스템으로는 위기 구간 두 곳만 확인했다.",
            "- 공개 가격은 제3자 사이트의 값이고 조회 시각이 표기되지 않았다. 스팟 가격은 영역과 시각마다 다르다. 측정 계정에 `ec2:DescribeSpotPriceHistory` 권한을 주면 실제 이력으로 다시 계산할 수 있다.",
            "- 시뮬레이션은 가정 가격(스팟/온디맨드 = 0.35)으로 제안 기법의 판단(언제 온디맨드로 옮길지)을 내렸다. 공개 가격(0.30)에서는 판단이 조금 달라질 수 있지만, 여기서는 같은 워커 시간에 가격만 바꿨다."]
    with open(OUT, "w") as f:
        f.write("\n".join(out) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
