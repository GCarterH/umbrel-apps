"""When a daily measure stepped to a new level: Bayesian online changepoint detection (Adams & MacKay 2007,
https://arxiv.org/abs/0710.3742).

The measure is treated as a run of flat stretches, each with its own level and spread. Going through the days in
order, the method keeps a probability for every possible length of the current stretch ("the last 9 days belong
together, and the 10th day back was different"). After the last day, the most likely length says when the current
stretch began. Each stretch's level and spread are unknown (a normal-inverse-gamma prior, so a new day's chance under
a stretch is a Student-t), which keeps one odd day from looking like a new stretch on its own.

Days are put on a common scale first (median and MAD of the whole series), and pulled in to 6 MADs, so the same
settings work for heart rate, steps or sleep.
"""
import math, statistics

HAZARD = 1 / 60          # prior chance a new stretch starts on any given day (one every two months)
MIN_AFTER = 7            # days of the new level before it's reported
MIN_BEFORE = 14          # days of the old level before it
CONFIDENCE = 0.6         # probability that the current stretch started within those limits
MIN_EFFECT = 1.0         # how far the level moved, in the stretches' own spread (robust SD)
CLIP = 6.0
# Prior for each stretch, on the common scale: level unknown (kappa small), spread about 1 robust SD.
MU0, KAPPA0, ALPHA0, BETA0 = 0.0, 0.1, 2.0, 1.0


def robust_sd(xs):
    """1.4826 × the median absolute deviation: the standard deviation for normal data, but one wild value can't
    inflate it. Falls back to the standard deviation when more than half the values are identical."""
    if len(xs) < 2:
        return 0.0
    m = statistics.median(xs)
    mad = statistics.median(abs(x - m) for x in xs) * 1.4826
    return mad if mad > 0 else statistics.stdev(xs)


def _log_t(x, mu, kappa, alpha, beta):
    """log density of x under the Student-t predictive of a normal-inverse-gamma."""
    nu = 2 * alpha
    s2 = beta * (kappa + 1) / (alpha * kappa)
    z = (x - mu) ** 2 / (nu * s2)
    return (math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2) - 0.5 * math.log(nu * math.pi * s2)
            - (nu + 1) / 2 * math.log1p(z))


def _update(stats, x):
    mu, kappa, alpha, beta = stats
    return ((kappa * mu + x) / (kappa + 1), kappa + 1, alpha + 0.5, beta + kappa * (x - mu) ** 2 / (2 * (kappa + 1)))


def run_lengths(zs, hazard=HAZARD):
    """Posterior over the current stretch's length after the last value: {length: probability}, where length counts
    the values in the stretch, the last one included."""
    prior = (MU0, KAPPA0, ALPHA0, BETA0)
    runs = {}   # length -> (log probability, stats)
    lh, l1h = math.log(hazard), math.log1p(-hazard)
    for x in zs:
        if not runs:
            runs = {1: (0.0, _update(prior, x))}
            continue
        new_lp = lh + _log_t(x, *prior)   # x starts a new stretch: the earlier lengths' probabilities sum to 1
        grown = {L + 1: (lp + l1h + _log_t(x, *st), _update(st, x)) for L, (lp, st) in runs.items()}
        grown[1] = (new_lp, _update(prior, x))
        top = max(lp for lp, _ in grown.values())
        total = top + math.log(sum(math.exp(lp - top) for lp, _ in grown.values()))
        runs = {L: (lp - total, st) for L, (lp, st) in grown.items() if lp - total > -30}
    return {L: math.exp(lp) for L, (lp, _) in runs.items()}


def recent_shift(dates, values):
    """The latest step to a new level in a daily series (dates ascending, one value per date with data), or None.
    {start: first date of the new level, days: values since, before, after: medians of the up to 28 values before and
    everything since, confidence}."""
    n = len(values)
    if n < MIN_BEFORE + MIN_AFTER:
        return None
    m, s = statistics.median(values), robust_sd(values)
    if s <= 0:
        return None
    zs = [max(-CLIP, min(CLIP, (v - m) / s)) for v in values]
    post = run_lengths(zs)
    in_limits = {L: p for L, p in post.items() if MIN_AFTER <= L <= n - MIN_BEFORE}
    if not in_limits:
        return None
    confidence = sum(in_limits.values())
    L = max(post, key=post.get)
    if confidence < CONFIDENCE or L not in in_limits:
        return None
    after, before = values[n - L:], values[max(0, n - L - 28):n - L]
    spread = max(robust_sd(after), robust_sd(before))
    b, a = statistics.median(before), statistics.median(after)
    if spread <= 0 or abs(a - b) < MIN_EFFECT * spread:
        return None
    return dict(start=dates[n - L], days=L, before=b, after=a, confidence=round(confidence, 2))
