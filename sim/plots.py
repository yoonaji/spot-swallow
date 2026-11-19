#!/usr/bin/env python3
"""
실험 결과(data/results/*.csv)를 표와 그래프로 만든다.

  python3 -m sim.plots          # docs/figures/*.png, docs/results-week2-tables.md
"""
import csv
import os
from collections import defaultdict
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RES = "data/results"
FIG = "docs/figures"
TABLES = "docs/results-week2-tables.md"

INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
BLUE, ORANGE, AQUA, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#9a9993"
SCEN_ORDER = ["normal", "concentrated", "surge", "delay-x2", "delay-x4"]
SCEN_KO = {"normal": "평상시", "concentrated": "회수 집중", "surge": "부하 급증",
           "delay-x2": "복구 지연 ×2", "delay-x4": "복구 지연 ×4"}

plt.rcParams.update({
    "font.family": ["NanumGothic", "DejaVu Sans"],
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "font.size": 9,
    "axes.spines.top": False, "axes.spines.right": False, "axes.unicode_minus": False,
})


def load(name):
    path = os.path.join(RES, f"{name}.csv")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            try:
                r[k] = float(v)
            except (TypeError, ValueError):
                pass
    return rows


def agg(rows, keys, metrics=("violation_rate", "cost_per_1k_jobs", "spot_hours", "od_hours", "mean_spare",
                             "p99_s", "need_met_rate", "reprocessed")):
    g = defaultdict(list)
    for r in rows:
        g[tuple(r[k] for k in keys)].append(r)
    out = {}
    for k, rs in g.items():
        out[k] = {m: mean(r[m] for r in rs if r[m] != "") for m in metrics}
        out[k]["n"] = len(rs)
    return out


def pct(x):
    return f"{x * 100:.2f}%"


# ---------------------------------------------------------------- 그래프 1: 파레토
LABELS = {"queue-reactive": "큐 반응형", "fixed-1": "고정 +1", "fixed-2": "고정 +2", "fixed-4": "고정 +4",
          "on-demand-only": "온디맨드 전용", "aws-rebalance": "AWS 재조정", "proposed+rebalance": "제안+재조정",
          "oracle": "오라클"}
OFFSETS = {"queue-reactive": (6, 4), "fixed-1": (6, 4), "fixed-2": (6, 4), "fixed-4": (6, 4),
           "on-demand-only": (-30, 8), "aws-rebalance": (-20, 8), "proposed+rebalance": (-78, 2), "oracle": (-8, -14)}


def pareto(main, sweep):
    a = agg(main, ("scenario", "policy"))
    s = agg(sweep, ("scenario", "p"))
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4), gridspec_kw={"width_ratios": [1, 1, 0.8]})
    for ax, sc in zip(axes[:2], ["delay-x2", "delay-x4"]):
        od = a[(sc, "on-demand-only")]["cost_per_1k_jobs"]
        for (scn, pol), v in a.items():
            if scn != sc or pol == "proposed":
                continue
            x, y = v["cost_per_1k_jobs"] / od, v["violation_rate"] * 100
            color = {"aws-rebalance": ORANGE, "proposed+rebalance": AQUA, "oracle": INK}.get(pol, GRAY)
            ax.scatter([x], [y], s=40, color=color, zorder=3, edgecolor=SURFACE, linewidth=1.5)
            if pol in LABELS and not (sc == "delay-x2" and pol in ("queue-reactive", "fixed-1", "proposed+rebalance")):
                ax.annotate(LABELS[pol], (x, y), xytext=OFFSETS[pol], textcoords="offset points", fontsize=8,
                            color=INK2)
        pts = sorted(((p, v) for (scn, p), v in s.items() if scn == sc), key=lambda t: t[0])
        xs = [v["cost_per_1k_jobs"] / od for _, v in pts]
        ys = [v["violation_rate"] * 100 for _, v in pts]
        ax.plot(xs, ys, color=BLUE, linewidth=2, zorder=4)
        ax.scatter(xs, ys, color=BLUE, s=44, zorder=5, edgecolor=SURFACE, linewidth=1.5)
        for (p, _), x, y in zip(pts, xs, ys):
            if sc == "delay-x2" and p < 0.95:
                continue
            ax.annotate(f"제안 p={p:g}", (x, y), xytext=(-8, -13), textcoords="offset points", fontsize=8,
                        color=BLUE, ha="right" if p < 0.9 else "left")
        ax.set_title(SCEN_KO[sc] + " (회수 집중 trace)", color=INK, fontsize=10, loc="left")
        ax.set_xlabel("비용 (온디맨드 전용 = 1)")
        ax.set_ylim(-0.5, max(ys + [11]) + 0.5)
        ax.set_xlim(0.35, 1.08)
    axes[0].set_ylabel("완료시간 목표(5분) 위반율 (%)")
    # 위반이 0 인 세 시나리오는 비용만 비교한다
    ax = axes[2]
    pols = ["on-demand-only", "fixed-4", "fixed-2", "fixed-1", "queue-reactive", "aws-rebalance", "proposed"]
    names = [LABELS.get(p, "제안 기법") for p in pols]
    vals = [mean(a[(sc, p)]["cost_per_1k_jobs"] / a[(sc, "on-demand-only")]["cost_per_1k_jobs"]
                 for sc in ("normal", "concentrated", "surge")) for p in pols]
    colors = [BLUE if p == "proposed" else ORANGE if p == "aws-rebalance" else GRAY for p in pols]
    ax.barh(names, vals, color=colors, height=0.6)
    for i, v in enumerate(vals):
        ax.text(v + 0.02, i, f"{v:.2f}", va="center", fontsize=8, color=INK2)
    ax.set_xlim(0, 1.2)
    ax.grid(axis="y", visible=False)
    ax.set_title("평상시·회수 집중·부하 급증 평균 비용\n(모든 정책 위반 0%)", color=INK, fontsize=10, loc="left")
    ax.set_xlabel("비용 (온디맨드 전용 = 1)")
    fig.suptitle("정책별 비용과 완료시간 목표 위반율 (12시간 × 5구간 평균, 왼쪽 아래일수록 좋음)", x=0.01, ha="left",
                 color=INK, fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG, "pareto.png"), dpi=160)
    plt.close(fig)


# ---------------------------------------------------------------- 그래프 2: ablation
def ablation(rows):
    a = agg(rows, ("scenario", "policy"))
    variants = ["proposed", "-wait-based-slo", "-tready-tail", "-concurrency", "-reprocess", "-ondemand", "-scale-down-delay"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    for ax, metric, label in [(axes[0], "violation_rate", "위반율 (%)"),
                              (axes[1], "cost_per_1k_jobs", "비용 변화 (제안 기법 대비 %)")]:
        for i, sc in enumerate(SCEN_ORDER):
            base = a.get((sc, "proposed"))
            if not base:
                continue
            for j, v in enumerate(variants):
                r = a.get((sc, v))
                if not r:
                    continue
                y = j + (i - 2) * 0.13
                val = r[metric] * 100 if metric == "violation_rate" else (r[metric] / base[metric] - 1) * 100
                ax.scatter([val], [y], s=22, color=[BLUE, ORANGE, AQUA, "#eda100", "#6250d6"][i],
                           label=SCEN_KO[sc] if j == 0 else None, zorder=3)
        ax.set_yticks(range(len(variants)))
        ax.set_yticklabels(["전체(제안)", "식(8) 원형으로", "Tready 꼬리 제거", "동시 회수 제거", "재처리 제거", "온디맨드 제거",
                            "축소 지연 제거"])
        ax.invert_yaxis()
        ax.set_xlabel(label)
        if metric == "violation_rate":
            ax.set_xlim(-0.5, 11)
    axes[1].axvline(0, color=INK2, linewidth=0.8)
    fig.legend(*axes[0].get_legend_handles_labels(), fontsize=8, frameon=False, loc="upper right", ncol=5)
    fig.suptitle("구성 요소를 하나씩 뺐을 때 (ablation)", x=0.01, ha="left", color=INK, fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG, "ablation.png"), dpi=160)
    plt.close(fig)


# ---------------------------------------------------------------- 그래프 3: 복구 시간에 따른 경계선
def crossover(rows):
    a = agg(rows, ("tready_mult", "policy"))
    mults = sorted({k[0] for k in a})
    lines = [("aws-rebalance", "AWS 재조정 (여유 0 + 경고 시 교체)", ORANGE),
             ("fixed-2", "고정 여유 +2", GRAY),
             ("proposed+rebalance", "제안 기법 + AWS 재조정", BLUE)]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    xs = [150 * m / 60 for m in mults]   # Tready 중앙값(분)
    for pol, label, color in lines:
        v = [a[(m, pol)]["violation_rate"] * 100 for m in mults]
        c = [a[(m, pol)]["cost_per_1k_jobs"] / a[(m, "on-demand-only")]["cost_per_1k_jobs"] for m in mults]
        for ax, ys in ((axes[0], v), (axes[1], c)):
            ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=6, markeredgecolor=SURFACE,
                    markeredgewidth=1.5, label=label)
            ax.annotate(label, (xs[-1], ys[-1]), xytext=(6, 0), textcoords="offset points", fontsize=8,
                        color=INK2, va="center")
    for ax in axes:
        ax.axvline(2, color=INK2, linewidth=0.8, linestyle="--")
        ax.set_xlabel("복구 시간 Tready 중앙값 (분)")
        ax.set_xlim(0, max(xs) * 1.6)
    axes[0].text(2.1, axes[0].get_ylim()[1] * 0.92, "회수 경고 2분", fontsize=8, color=INK2)
    axes[0].set_ylabel("완료시간 목표(5분) 위반율 (%)")
    axes[1].set_ylabel("비용 (온디맨드 전용 = 1)")
    axes[1].set_ylim(0, 1.05)
    fig.suptitle("복구 시간이 길어질수록 '경고 후 교체'만으로는 부족해진다 (회수 집중 trace, 5구간 평균)", x=0.01,
                 ha="left", color=INK, fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG, "crossover.png"), dpi=160)
    plt.close(fig)


# ---------------------------------------------------------------- 표
def table(a, rows_keys, cols, title, fmt):
    lines = [f"### {title}", "", "| " + " | ".join(["", *cols]) + " |", "|" + "---|" * (len(cols) + 1)]
    for rk in rows_keys:
        cells = [fmt(a.get((c, rk)) if isinstance(c, str) else a.get((*c, rk))) for c in cols]
        lines.append(f"| {rk} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def cell(v):
    if not v:
        return "-"
    return f"{pct(v['violation_rate'])} · ${v['cost_per_1k_jobs']:.3f}"


def main():
    os.makedirs(FIG, exist_ok=True)
    main_rows, sweep, abl = load("main"), load("p_sweep"), load("ablation")
    out = ["# 2주차 시뮬레이션 결과 표", "",
           "각 칸: 완료시간 목표(5분) 위반율 · 성공 작업 1,000건당 비용. 시나리오마다 trace 의 뒤 40% 에서 고른 12시간 구간 5개 평균.",
           "자동 생성: `python3 -m sim.run_experiments && python3 -m sim.plots`", ""]
    if main_rows:
        a = agg(main_rows, ("scenario", "policy"))
        pols = list(dict.fromkeys(r["policy"] for r in main_rows))
        out.append(table(a, pols, SCEN_ORDER, "정책 비교", cell))
        out.append("열 이름: " + ", ".join(f"{k} = {v}" for k, v in SCEN_KO.items()) + "\n")
        a2 = agg(main_rows, ("scenario", "policy"))
        lines = ["### 워커 사용 시간과 여유 용량 (회수 집중)", "",
                 "| 정책 | 스팟 시간 | 온디맨드 시간 | 평균 여유 대수 | 필요 대수 유지율 | 재처리 건수 | p99 완료시간(초) |",
                 "|---|---|---|---|---|---|---|"]
        for p in pols:
            v = a2.get(("concentrated", p))
            if v:
                lines.append(f"| {p} | {v['spot_hours']:.1f} | {v['od_hours']:.1f} | {v['mean_spare']:.2f} | "
                             f"{v['need_met_rate'] * 100:.1f}% | {v['reprocessed']:.0f} | {v['p99_s']:.0f} |")
        out.append("\n".join(lines) + "\n")
    if sweep:
        a = agg(sweep, ("scenario", "policy"))
        out.append(table(a, list(dict.fromkeys(r["policy"] for r in sweep)), SCEN_ORDER, "분위수 p 민감도", cell))
    if abl:
        a = agg(abl, ("scenario", "policy"))
        out.append(table(a, list(dict.fromkeys(r["policy"] for r in abl)), SCEN_ORDER, "Ablation", cell))
    for name, key, title in [("tready_sweep", "tready_mult", "복구 시간 배수 (중앙값 150초 × 배수)"),
                             ("notice", "notice_s", "회수 경고가 오는 시점 (초 전)"),
                             ("slo", "slo", "완료시간 목표 (초)"), ("cv", "cv", "처리 시간 변동계수 CV")]:
        rows = load(name)
        if not rows:
            continue
        a = agg(rows, ("scenario", key, "policy"))
        cols = list(dict.fromkeys((r["scenario"], r[key]) for r in rows))
        pols = list(dict.fromkeys(r["policy"] for r in rows))
        lines = [f"### {title}", "", "| 정책 | " + " | ".join(f"{SCEN_KO.get(s, '회수 집중')} / {k:g}" for s, k in cols) + " |",
                 "|" + "---|" * (len(cols) + 1)]
        for p in pols:
            lines.append(f"| {p} | " + " | ".join(cell(a.get((s, k, p))) for s, k in cols) + " |")
        out.append("\n".join(lines) + "\n")
    with open(TABLES, "w") as f:
        f.write("\n".join(out))
    if main_rows and sweep:
        pareto(main_rows, sweep)
    if abl:
        ablation(abl)
    sweep_rows = load("tready_sweep")
    if sweep_rows:
        crossover(sweep_rows)
    print("wrote", TABLES, "and", FIG)


if __name__ == "__main__":
    main()
