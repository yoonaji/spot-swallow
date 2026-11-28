#!/usr/bin/env python3
"""
4주차 보강 그래프: 가장 강한 기준선(AWS 재조정 + 온디맨드 기본 용량)과의 비교.

  python3 -m sim.run_experiments --only frontier robust
  python3 -m sim.plots_week4        # docs/figures/frontier.png, robust.png, docs/results-week4-tables.md

frontier: 복구 시간마다, 정책별 조절 손잡이(여유 대수 k, 분위수 p)를 돌려 비용-위반율 곡선을 그린다.
robust:   설정 하나로 8개 환경을 모두 돌렸을 때 평균 비용과 최악 위반율.
"""
import os
from collections import defaultdict
from statistics import mean

from sim.plots import FIG, GRID, INK, INK2, SURFACE, load, plt

BLUE, ORANGE, AQUA, GRAY = "#257de0", "#f47230", "#00b894", "#9a9993"
TABLES = "docs/results-week4-tables.md"
FAMILIES = [
    # (이름 접두, 표시 이름, 색, 표식)
    ("proposed+rebalance p=", "제안 + 재조정 (p)", BLUE, "o"),
    ("aws-rebalance+odbase", "AWS 재조정 + 온디맨드 기본 k대", AQUA, "s"),
    ("aws-rebalance+", "AWS 재조정 + 스팟 여유 k대", ORANGE, "^"),
    ("fixed-", "고정 스팟 여유 k대", GRAY, "D"),
]
MINUTES = {2.0: "5분", 3.0: "7.5분", 4.0: "10분", 6.0: "15분"}


def family(policy):
    for prefix, label, color, marker in FAMILIES:
        if policy.startswith(prefix):
            if prefix == "aws-rebalance+" and ("od" in policy):
                continue
            return prefix, label, color, marker
    return None


def knob(policy, prefix):
    v = policy[len(prefix):]
    return float(v)


def summarize(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[(r[key], r["policy"])].append(r)
    od = {k: mean(r["cost_per_1k_jobs"] for r in g[(k, "on-demand-only")]) for k, _ in g}
    out = {}
    for (k, p), rs in g.items():
        out[(k, p)] = {"cost": mean(r["cost_per_1k_jobs"] for r in rs) / od[k],
                       "viol": mean(r["violation_rate"] for r in rs) * 100,
                       "viol_max": max(r["violation_rate"] for r in rs) * 100, "n": len(rs)}
    return out


def frontier(rows):
    s = summarize(rows, "tready_mult")
    fig, axes = plt.subplots(1, 4, figsize=(16, 4.4), sharey=False)
    for ax, m in zip(axes, sorted(MINUTES)):
        for prefix, label, color, marker in FAMILIES:
            pts = sorted((knob(p, prefix), v) for (mm, p), v in s.items()
                         if mm == m and family(p) and family(p)[0] == prefix)
            xs = [v["cost"] for _, v in pts]
            ys = [v["viol"] for _, v in pts]
            lw = 2 if prefix.startswith("proposed") else 1.4
            ax.plot(xs, ys, color=color, linewidth=lw, zorder=3 if prefix.startswith("proposed") else 2)
            ax.scatter(xs, ys, color=color, marker=marker, s=40, zorder=4, edgecolor=SURFACE, linewidth=2)
            # 끝점 하나에만 손잡이 값을 단다 (모든 점에 숫자를 달지 않는다)
            if prefix.startswith("proposed"):
                for (kv, v) in pts:
                    if kv in (0.95, 0.99):
                        ax.annotate(f"p={kv:g}", (v["cost"], v["viol"]), xytext=(-8, 7), ha="right", textcoords="offset points",
                                    fontsize=8, color=INK2)
        od = s[(m, "on-demand-only")]
        ax.scatter([od["cost"]], [od["viol"]], color=INK, marker="x", s=36, zorder=4)
        ax.annotate("온디맨드 전용", (od["cost"], od["viol"]), xytext=(-48, -13), textcoords="offset points",
                    fontsize=8, color=INK2)
        ax.set_title(f"복구 시간 {MINUTES[m]}", loc="left", color=INK, fontsize=10)
        ax.set_xlabel("비용 (온디맨드 전용 = 1)")
        ax.set_xlim(0.3, 1.1)
        ax.set_ylim(-1.8, 17)
    axes[0].set_ylabel("완료시간 목표(5분) 위반율 (%)")
    handles = [plt.Line2D([], [], color=c, marker=mk, linewidth=2 if p.startswith("proposed") else 1.4, label=l)
               for p, l, c, mk in FAMILIES]
    fig.legend(handles=handles, loc="upper right", ncol=4, frameon=False, fontsize=9, labelcolor=INK2)
    fig.suptitle("손잡이를 돌려 만든 비용-위반율 곡선 (회수 집중 trace, 12시간 × 5구간 평균, 왼쪽 아래일수록 좋음)",
                 x=0.01, ha="left", color=INK, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(os.path.join(FIG, "frontier.png"), dpi=160)
    plt.close(fig)
    return s


ROBUST_ENVS = ["normal", "surge", "measured", "concentrated", "delay-x2", "delay-x3", "delay-x4", "delay-x6"]
ENV_KO = {"normal": "평상시", "surge": "부하 급증", "measured": "실측 CPU(15초)", "concentrated": "회수 집중(2.5분)",
          "delay-x2": "복구 5분", "delay-x3": "복구 7.5분", "delay-x4": "복구 10분", "delay-x6": "복구 15분"}
ROBUST_LABEL = {"aws-rebalance": "AWS 재조정", "aws-rebalance+1": "+스팟 1", "aws-rebalance+2": "+스팟 2",
                "aws-rebalance+3": "+스팟 3", "aws-rebalance+odbase1": "+온디맨드 기본 1",
                "aws-rebalance+odbase2": "+온디맨드 기본 2", "aws-rebalance+odbase3": "+온디맨드 기본 3",
                "aws-rebalance+od1": "+온디맨드 여유 1", "aws-rebalance+od2": "+온디맨드 여유 2",
                "proposed+rebalance p=0.95": "제안 p=0.95", "proposed+rebalance p=0.99": "제안 p=0.99",
                "on-demand-only": "온디맨드 전용"}


def robust(rows):
    s = summarize(rows, "scenario")
    pols = list(dict.fromkeys(r["policy"] for r in rows))
    summary = {}
    for p in pols:
        cs = [s[(e, p)]["cost"] for e in ROBUST_ENVS]
        vs = [s[(e, p)]["viol"] for e in ROBUST_ENVS]
        summary[p] = (mean(cs), max(vs), s[("normal", p)]["cost"])
    fig, ax = plt.subplots(figsize=(8, 5))
    for p, (c, v, _) in summary.items():
        if p.startswith("proposed"):
            color, mk = BLUE, "o"
        elif "odbase" in p or "+od" in p:
            color, mk = AQUA, "s"
        elif p == "on-demand-only":
            color, mk = INK, "x"
        else:
            color, mk = ORANGE, "^"
        ax.scatter([c], [v], color=color, marker=mk, s=60 if p.startswith("proposed") else 44, zorder=4,
                   edgecolor=SURFACE if mk != "x" else None, linewidth=2)
        ax.annotate(ROBUST_LABEL.get(p, p), (c, v), xytext=(6, 4), textcoords="offset points", fontsize=8,
                    color=INK if p.startswith("proposed") else INK2)
    ax.set_xlabel("8개 환경 평균 비용 (온디맨드 전용 = 1)")
    ax.set_ylabel("8개 환경 중 최악 위반율 (%)")
    ax.set_title("설정 하나로 평상시부터 복구 15분까지 8개 환경을 돌렸을 때 (왼쪽 아래일수록 좋음)", loc="left", color=INK,
                 fontsize=10)
    ax.set_xlim(0.35, 1.08)
    ax.set_ylim(-0.8, 16.5)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG, "robust.png"), dpi=160)
    plt.close(fig)
    return s, pols, summary


def main():
    out = ["# 4주차 보강: 가장 강한 기준선과의 비교", "",
           "자동 생성: `python3 -m sim.run_experiments --only frontier robust && python3 -m sim.plots_week4`. "
           "각 칸은 `위반율 · 비용(온디맨드 전용 = 1)`, 12시간 구간 5개 평균.", ""]
    fr = load("frontier")
    if fr:
        s = frontier(fr)
        pols = list(dict.fromkeys(r["policy"] for r in fr))
        out += ["## 복구 시간별 비용-위반율 (frontier)", "",
                "| 정책 | " + " | ".join(MINUTES[m] for m in sorted(MINUTES)) + " |", "|" + "---|" * 5]
        for p in pols:
            out.append(f"| {p} | " + " | ".join(f"{s[(m, p)]['viol']:.2f}% · {s[(m, p)]['cost']:.2f}"
                                                 for m in sorted(MINUTES)) + " |")
        out.append("")
    rb = load("robust")
    if rb:
        s, pols, summary = robust(rb)
        out += ["## 설정 하나로 8개 환경 (robust)", "",
                "| 정책 | " + " | ".join(ENV_KO[e] for e in ROBUST_ENVS) + " | 평균 비용 | 최악 위반율 |",
                "|" + "---|" * (len(ROBUST_ENVS) + 3)]
        for p in pols:
            c, v, _ = summary[p]
            out.append(f"| {p} | " + " | ".join(f"{s[(e, p)]['viol']:.2f}% · {s[(e, p)]['cost']:.2f}"
                                                 for e in ROBUST_ENVS) + f" | {c:.2f} | {v:.2f}% |")
        out.append("")
    with open(TABLES, "w") as f:
        f.write("\n".join(out))
    print("wrote", TABLES)


if __name__ == "__main__":
    main()
