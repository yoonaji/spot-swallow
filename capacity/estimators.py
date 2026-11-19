"""
제어기가 쓰는 추정기. 시뮬레이터와 실제 제어기(3주차)가 같은 코드를 쓴다.

- DecayingRate: 사건 발생률(작업 도착률 λ). 최근 기록에 큰 가중치를 두는 지수 감쇠 평균 (식 3).
- DecayingMean: 처리 시간 평균. 처리율 μ = 1 / 평균 처리 시간.
- QuantileHistory: 최근 Tready 기록의 p 분위수 (식 5).
- EvictionHistory: 시장(공개 trace)의 회수 기록에서 길이 W 구간별 동시 회수 비율 f_w 의
  가중 분위수 (식 6). 오래된 구간일수록 가중치를 반감기마다 절반으로 줄인다.
"""
import math
from bisect import bisect_right

LN2 = math.log(2)


def weighted_quantile(values, weights, q):
    """가중 분위수. 누적 가중치가 q 를 처음 넘는 값을 고른다(보간 없음, 보수적)."""
    if not values:
        return None
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[-1][0]
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * total - 1e-12:
            return v
    return pairs[-1][0]


class DecayingRate:
    """초당 사건 수의 지수 감쇠 추정. rate = (감쇠된 사건 수) / (감쇠된 관측 시간)."""

    def __init__(self, half_life_s, prior_rate=None, t0=0.0):
        self.h = half_life_s
        self.t = t0
        self.count = 0.0
        self.span = 0.0
        if prior_rate is not None:  # 반감기 하나 분량의 사전 관측으로 시작한다
            self.count = prior_rate * self.h / LN2
            self.span = self.h / LN2

    def _advance(self, t):
        if t <= self.t:
            return
        dt = t - self.t
        decay = 2.0 ** (-dt / self.h)
        self.count *= decay
        self.span = self.span * decay + (1 - decay) * self.h / LN2
        self.t = t

    def add(self, t, n=1):
        self._advance(t)
        self.count += n

    def rate(self, t):
        self._advance(t)
        return self.count / self.span if self.span > 0 else 0.0


class DecayingMean:
    """값의 지수 감쇠 가중 평균 (시간 기준 반감기)."""

    def __init__(self, half_life_s, prior=None, t0=0.0):
        self.h = half_life_s
        self.t = t0
        self.wsum = 0.0
        self.vsum = 0.0
        if prior is not None:
            self.wsum, self.vsum = 1.0, prior

    def add(self, t, v):
        if t > self.t:
            d = 2.0 ** (-(t - self.t) / self.h)
            self.wsum *= d
            self.vsum *= d
            self.t = t
        self.wsum += 1.0
        self.vsum += v

    def mean(self):
        return self.vsum / self.wsum if self.wsum > 0 else None


class QuantileHistory:
    """최근 max_len 개 기록의 분위수. 기록이 min_samples 보다 적으면 사전값을 쓴다."""

    def __init__(self, prior_values, max_len=50, min_samples=5):
        self.values = list(prior_values)[-max_len:]
        self.max_len = max_len
        self.min_samples = min_samples
        self.observed = 0

    def add(self, v):
        self.values.append(v)
        self.observed += 1
        if len(self.values) > self.max_len:
            self.values.pop(0)

    def quantile(self, p):
        xs = sorted(self.values)
        if not xs:
            return None
        idx = min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))
        return xs[idx]

    def mean(self):
        return sum(self.values) / len(self.values) if self.values else None


class EvictionHistory:
    """
    회수 기록(시장 신호). ticks: [(t_s, available, evicted)] (시간순).
    f_w 는 구간 시작 시점의 가용 수 a0 중 구간 안에서 회수된 비율 (convert.py 와 같은 정의).
    """

    def __init__(self, ticks, gap_s, half_life_s=24 * 3600, prior_f=0.33, min_windows=20):
        self.t = [x[0] for x in ticks]
        self.avail = [x[1] for x in ticks]
        self.drop = [x[2] for x in ticks]
        self.cum = [0]
        for d in self.drop:
            self.cum.append(self.cum[-1] + d)   # cum[i] = drop[0..i-1] 합
        self.gap = gap_s
        self.h = half_life_s
        self.prior_f = prior_f
        self.min_windows = min_windows
        self._cache = {}

    def _windows(self, now, window_s):
        """now 이전에 끝난 구간들의 (f_w, 끝 시각)."""
        ticks = max(1, round(window_s / self.gap))
        end_idx = bisect_right(self.t, now) - 1  # now 까지 관측된 마지막 tick
        out = []
        start = end_idx - ticks
        while start >= 0:
            a0 = self.avail[start]
            if a0 > 0:
                lost = self.cum[start + ticks + 1] - self.cum[start + 1]
                out.append((min(1.0, lost / a0), self.t[start + ticks]))
            start -= ticks
        return out

    def stats(self, now, window_s, p, weighted=True):
        """(f_p, f_mean). 같은 tick 안에서는 결과를 재사용한다."""
        idx = bisect_right(self.t, now) - 1
        key = (idx, max(1, round(window_s / self.gap)), p, weighted)
        if key in self._cache:
            return self._cache[key]
        ws = self._windows(now, window_s)
        if len(ws) < self.min_windows:
            res = (self.prior_f, self.prior_f / 2)
        else:
            vals = [f for f, _ in ws]
            if weighted:
                wts = [2.0 ** (-(now - te) / self.h) for _, te in ws]
            else:
                wts = [1.0] * len(ws)
            tot = sum(wts)
            res = (weighted_quantile(vals, wts, p), sum(f * w for f, w in zip(vals, wts)) / tot)
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = res
        return res

    def available_now(self, now):
        idx = bisect_right(self.t, now) - 1
        return self.avail[idx] if idx >= 0 else 1
