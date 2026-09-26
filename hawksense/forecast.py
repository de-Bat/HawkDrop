"""Buy-now vs wait advisor.

Model
-----
The best landed price across all stores forms a daily series. For every sales
event coming up within ``max_wait_days`` that one of the item's stores takes
part in, the price on the event day is simulated as a mixture:

* with probability ``p`` (event participation) the item is discounted by
  ``d`` (event depth) from its *regular* price (recent median);
* otherwise it stays at today's price;

and on top of that drifts with the item's own trend and volatility. ``p`` and
``d`` start from category priors and are updated with how this item actually
behaved in past occurrences of the same event (Beta-style shrinkage).

Waiting is not free (the price may rise, stock may run out, you go without the
item), so each day of waiting costs ``wait_cost_per_day`` of the price. We wait
only if the probability of beating today's price by more than
``min_saving`` + waiting cost is above 50%. Confidence scales that probability
by how much data backs it.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from hawksense.calendar_events import EventOccurrence, SalesEvent, upcoming_events

RANDOM_WALK = 0.003  # daily sd of unexplained price level changes
PROMO_CORRELATION = 0.6  # how strongly promotion at one event predicts promotion at the next


@dataclass
class Settings:
    max_wait_days: int = 75
    min_saving: float = 0.03  # a wait must save at least this fraction...
    wait_cost_per_day: float = 0.0006  # ...plus this much per day waited
    simulations: int = 4000


@dataclass
class EventStats:
    participation: float
    depth: float
    observed: int  # past occurrences found in this item's history
    hits: int


@dataclass
class Candidate:
    label: str
    date: date
    days: int
    event: SalesEvent | None
    stats: EventStats | None
    p_win: float  # P(price at this checkpoint beats today's by min_saving + wait cost)
    plan_p_win: float  # P(some checkpoint up to this one does) - "watch until here" strategy
    net_saving: float  # expected saving of that strategy, net of waiting cost
    buy_below: float  # trigger price at this checkpoint
    expected: float  # expected purchase price under the strategy
    p10: float
    p50: float
    p90: float


@dataclass
class Advice:
    action: str  # "BUY_NOW" | "WAIT" | "NO_DATA"
    confidence: float
    current: float
    regular: float | None
    expected: float
    low: float
    high: float
    wait: Candidate | None
    candidates: list[Candidate]
    active_events: list[EventOccurrence]
    reasons: list[str] = field(default_factory=list)
    data_quality: float = 0.0

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.80:
            return "high"
        if self.confidence >= 0.65:
            return "medium"
        return "low"


# ---- statistics helpers ----------------------------------------------------------

def _ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Slope and residual standard deviation of y ~ a + b*x."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    resid = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
    sd = math.sqrt(sum(r * r for r in resid) / max(1, n - 2))
    return slope, sd


def _window(series: list[tuple[date, float]], start: date, end: date) -> list[float]:
    return [p for d, p in series if start <= d <= end]


def learn_event(event: SalesEvent, category: str, series: list[tuple[date, float]], today: date,
                prior_weight: float = 4.0) -> EventStats:
    """Blend the category prior for an event with the item's own history."""
    p0, d0 = event.participation, event.depth_for(category)
    observed = hits = 0
    hit_depths: list[float] = []
    if series:
        for s, e in event.occurrences(series[0][0], today - timedelta(days=1)):
            if e >= today:
                continue
            baseline = _window(series, s - timedelta(days=35), s - timedelta(days=4))
            during = _window(series, s, e)
            if len(baseline) < 5 or not during:
                continue
            observed += 1
            disc = 1 - min(during) / statistics.median(baseline)
            if disc >= 0.03:
                hits += 1
                hit_depths.append(disc)
    participation = (p0 * prior_weight + hits) / (prior_weight + observed)
    depth = (d0 * 2 + sum(hit_depths)) / (2 + len(hit_depths))
    return EventStats(participation, depth, observed, hits)


def _quantile(sorted_vals: list[float], q: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, int(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _sample_paths(specs: list[tuple[int, EventStats | None]], current: float, regular: float, drift: float,
                  sigma: float, n: int, rng: random.Random) -> list[list[float]]:
    """Monte-Carlo draws of the best landed price at each checkpoint ``(days, stats)``.

    Checkpoints of one draw are correlated: they share the slow price-level
    random walk and a latent "this item gets promoted" propensity (an item
    skipped by 11.11 is more likely to be skipped by Black Friday too).
    """
    out: list[list[float]] = [[] for _ in specs]
    rho = PROMO_CORRELATION
    for _ in range(n):
        level_shock, propensity = rng.gauss(0, 1), rng.gauss(0, 1)
        for j, (days, stats) in enumerate(specs):
            walk = min(0.10, RANDOM_WALK * math.sqrt(max(days, 1)))
            noise = math.exp(drift * days + walk * level_shock + rng.gauss(0, sigma))
            promo = stats and _phi(rho * propensity + math.sqrt(1 - rho ** 2) * rng.gauss(0, 1)) < stats.participation
            if promo:
                d = min(0.7, max(0.0, rng.gauss(stats.depth, 0.35 * stats.depth)))
                out[j].append(regular * (1 - d) * noise)
            else:
                out[j].append(current * noise)
    return out


def _plan(checkpoints: list[tuple[str, date, int, SalesEvent | None, EventStats | None, list[float]]],
          current: float, settings: Settings) -> list[Candidate]:
    """Evaluate "watch through checkpoint k" strategies.

    Strategy k: at each checkpoint j <= k buy if the price beats today's by
    ``min_saving`` + the cost of having waited ``days_j``; if none does, buy
    after checkpoint k at whatever the price is then.
    """
    n = len(checkpoints[0][5]) if checkpoints else 0
    thresholds = [current * (1 - settings.min_saving - settings.wait_cost_per_day * c[2]) for c in checkpoints]
    out = []
    for k, (label, when, days, event, stats, samples) in enumerate(checkpoints):
        outcomes, wins, net = [], 0, 0.0
        for i in range(n):
            price, bought_day = checkpoints[k][5][i], days
            for j in range(k + 1):
                if checkpoints[j][5][i] <= thresholds[j]:
                    price, bought_day = checkpoints[j][5][i], checkpoints[j][2]
                    wins += 1
                    break
            outcomes.append(price)
            net += current - price - current * settings.wait_cost_per_day * bought_day
        outcomes.sort()
        standalone = sum(1 for s in samples if s <= thresholds[k]) / n
        out.append(Candidate(label, when, days, event, stats, standalone, wins / n, net / n,
                             thresholds[k], statistics.fmean(outcomes),
                             _quantile(outcomes, 0.10), _quantile(outcomes, 0.50), _quantile(outcomes, 0.90)))
    return out


# ---- main entry point --------------------------------------------------------------

def advise(
    series: list[tuple[date, float]],
    category: str,
    event_keys: set[str],
    today: date,
    settings: Settings | None = None,
    target_price: float | None = None,
    n_offers: int = 1,
    currency: str = "",
) -> Advice:
    settings = settings or Settings()
    series = sorted((d, p) for d, p in series if d <= today)
    if not series:
        return Advice("NO_DATA", 0.0, 0, None, 0, 0, 0, None, [], [],
                      ["No prices recorded yet - run `hawksense check` or `hawksense price`."])

    rng = random.Random(1729)
    current = series[-1][1]
    cur = f" {currency}" if currency else ""
    fmt = lambda v: f"{v:,.0f}{cur}"  # noqa: E731
    reasons: list[str] = []

    # --- history statistics -------------------------------------------------------
    span = (series[-1][0] - series[0][0]).days
    recent = [(d, p) for d, p in series if d >= today - timedelta(days=60)]
    regular = statistics.median(p for _, p in recent) if len(recent) >= 7 else None
    all_prices = sorted(p for _, p in series)
    hist_min = all_prices[0]

    trend = [(d, p) for d, p in series if d >= today - timedelta(days=90)]
    if len(trend) >= 10:
        drift, sigma = _ols([(d - today).days for d, _ in trend], [math.log(p) for _, p in trend])
    else:
        drift, sigma = 0.0, 0.03
    trend_weight = min(1.0, len(trend) / 45)
    drift = max(-0.002, min(0.002, drift)) * trend_weight
    sigma = max(0.005, min(0.08, sigma))

    # --- events -----------------------------------------------------------------------
    occurrences = [o for o in upcoming_events(today, settings.max_wait_days) if o.event.key in event_keys]
    active = [o for o in occurrences if o.is_active(today)]
    future = [o for o in occurrences if o.start > today]

    base = regular if regular is not None else current
    specs = []
    learned_obs = 0
    for occ in future:
        stats = learn_event(occ.event, category, series, today)
        learned_obs += stats.observed
        specs.append((occ.event.name, occ.start, occ.days_until(today) + 1, occ.event, stats))
    if drift * 30 < -0.03:  # a clear downtrend is worth waiting for even without an event
        specs.append(("Downtrend (no event)", today + timedelta(days=30), 30, None, None))
        specs.sort(key=lambda c: c[2])
    paths = _sample_paths([(c[2], c[4]) for c in specs], current, base, drift, sigma, settings.simulations, rng)
    checkpoints = [(*spec, samples) for spec, samples in zip(specs, paths)]
    candidates = _plan(checkpoints, current, settings)

    # --- data quality ---------------------------------------------------------------
    quality = (0.40 * min(1.0, span / 180)
               + 0.25 * min(1.0, len(series) / 60)
               + 0.15 * min(1.0, n_offers / 3)
               + 0.20 * min(1.0, learned_obs / 3))

    # --- decision ----------------------------------------------------------------------
    best = max(candidates, key=lambda c: c.net_saving, default=None)
    p_win = best.plan_p_win if best else 0.0
    wait = best if best and p_win >= 0.5 and best.net_saving > 0 else None

    if target_price is not None and current <= target_price:
        wait = None
        reasons.append(f"Target price reached ({fmt(current)} <= {fmt(target_price)}).")

    near_low = span >= 30 and current <= hist_min * 1.02
    if near_low:
        reasons.append(f"Price is at/near its lowest in {span} days of tracking ({fmt(hist_min)}).")
        if wait and p_win < 0.65:
            wait = None  # don't gamble on a modest edge when already at the historical low

    if regular:
        rel = current / regular - 1
        if rel <= -0.05:
            reasons.append(f"Current price is {-rel:.0%} below its recent regular price ({fmt(regular)}).")
        elif rel >= 0.05:
            reasons.append(f"Current price is {rel:.0%} above its recent regular price ({fmt(regular)}).")
    if abs(drift) * 30 >= 0.01:
        reasons.append(f"Trend: {'falling' if drift < 0 else 'rising'} about {abs(drift) * 30:.1%} per month.")
    for occ in active:
        reasons.append(f"{occ.event.name} is on now (until {occ.end:%b %d}).")
    if not future:
        reasons.append(f"No relevant sales event in the next {settings.max_wait_days} days.")

    signal = p_win if wait else 1 - p_win
    # the model itself is uncertain, so even perfect data never passes the full signal through
    confidence = 0.5 + (signal - 0.5) * (0.30 + 0.60 * quality)
    confidence = max(0.5, min(0.95, confidence))

    if wait:
        action = "WAIT"
        expected, low, high = wait.expected, wait.p10, wait.p90
        est = "estimated date" if wait.event and wait.event.date_certainty == "estimated" else "starts"
        watched = [c for c in candidates if c.days <= wait.days]
        names = ", ".join(c.label for c in watched)
        reasons.insert(0, (f"Watch through {names} (last one {est} {wait.date:%a %b %d}, in {wait.days} days): "
                           f"{wait.plan_p_win:.0%} chance one of them beats today's price by more than "
                           f"the cost of waiting; expected net saving ~{fmt(wait.net_saving)}."))
        reasons.insert(1, "Buy at the first sale where the landed price drops below: "
                          + ", ".join(f"{fmt(c.buy_below)} ({c.label})" for c in watched) + ".")
        for c in watched:
            if c.stats and c.stats.observed:
                reasons.append(f"This item dropped in {c.stats.hits}/{c.stats.observed} past {c.label} events.")
    else:
        action = "BUY_NOW"
        expected = low = high = current
        if best:
            reasons.insert(0, f"Best alternative is watching through {best.label}, but only "
                              f"{best.plan_p_win:.0%} chance of a worthwhile saving "
                              f"(expected net {fmt(best.net_saving)}).")

    if confidence < 0.6:
        reasons.append("Close call - either choice is reasonable; tracking longer will sharpen this.")
    if quality < 0.35:
        reasons.append("Limited price history - confidence is capped. Keep tracking to sharpen the estimate.")

    return Advice(action, confidence, current, regular, expected, low, high, wait, candidates, active,
                  reasons, quality)
