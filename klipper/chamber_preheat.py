# Fan-stirred nozzle qualification and ETA-scheduled chamber preheat.
#
# The public interface remains M191 in chamber_fan.cfg.  This host-side extra
# owns the live wait because Klipper's TEMPERATURE_WAIT holds the G-code mutex;
# delayed_gcode and Jinja macros therefore cannot adjust a bed target during it.
# Fan writes use Fan.set_speed(), not SET_FAN_SPEED: the latter queues a
# lookahead callback which cannot flush while the temperature wait is blocking.
#
# The nozzle is only a chamber thermometer once it has stopped exchanging heat
# with its own mass.  Qualification therefore waits for the regressed nozzle
# slope to reach zero from below and then hold for a settling dwell, and no bed
# boost is scheduled before that.  A nozzle merely starting below some warm
# threshold proves nothing: on 2026-08-25 one started at 31.7 C, was still
# falling, was trusted immediately, and drove a full boost off noise.

import math

from . import moonraker


def linear_rate(start_temp, end_temp, elapsed_seconds):
    """Return the two-point temperature rate in degrees C per minute."""
    if elapsed_seconds <= 0.0:
        return None
    return (end_temp - start_temp) * 60.0 / elapsed_seconds


def exponential_filter(previous, sample, alpha):
    """Update a recursive first-order filter, seeding it from the first sample."""
    if sample is None:
        return previous
    if previous is None:
        return sample
    return previous + alpha * (sample - previous)


def least_squares_slope(samples):
    """Return (C/min, standard error) for (seconds, temperature) samples.

    Measured on 2026-08-25 from four recorded preheats: the nozzle thermistor
    carries 0.10-0.18 C of noise while the real chamber trend is 0.21-0.50
    C/min. A two-point rate over one 5 s interval multiplies that noise by
    twelve, so the previous estimator returned +-0.24 C/min of pure noise on a
    0.3 C/min signal and the ETA swung between 1 and 18 minutes. Regressing a
    trailing window costs latency and buys back roughly a factor of three; the
    standard error it yields is what lets the boost be sized conservatively
    rather than optimistically.
    """
    n = len(samples)
    if n < 3:
        return None, None
    mean_t = sum(t for t, _ in samples) / n
    mean_v = sum(v for _, v in samples) / n
    sxx = sum((t - mean_t) ** 2 for t, _ in samples)
    if sxx <= 0.0:
        return None, None
    sxy = sum((t - mean_t) * (v - mean_v) for t, v in samples)
    slope = sxy / sxx
    residual = sum((v - (mean_v + slope * (t - mean_t))) ** 2
                   for t, v in samples)
    variance = residual / (n - 2) if n > 2 else 0.0
    stderr = (variance / sxx) ** 0.5
    return slope * 60.0, stderr * 60.0


class SlopeWindow:
    """Trailing least-squares view of the nozzle temperature."""

    def __init__(self, span_seconds, minimum_fill=0.8):
        self.span = span_seconds
        self.minimum_fill = minimum_fill
        self.samples = []

    def push(self, eventtime, temperature):
        # Seeding walks forward through history and the live loop continues
        # from there, so samples are monotonic. Drop anything that is not:
        # an out-of-order push silently collapses span_seconds and the window
        # then reports no slope at all.
        if self.samples and eventtime <= self.samples[-1][0]:
            return
        self.samples.append((eventtime, temperature))
        horizon = eventtime - self.span
        self.samples = [s for s in self.samples if s[0] >= horizon]

    def span_seconds(self):
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1][0] - self.samples[0][0]

    def value(self):
        """(slope, stderr), or (None, None) until the window is mostly full."""
        if self.span_seconds() < self.span * self.minimum_fill:
            return None, None
        return least_squares_slope(self.samples)


def eta_minutes(temperature, target, rate, minimum_rate):
    """Linear minutes to target, or None when a warming trend is unusable."""
    if temperature >= target:
        return 0.0
    if rate is None or rate < minimum_rate:
        return None
    return (target - temperature) / rate


def early_arrival_eta(temperature, target, slope, stderr, confidence,
                      minimum_rate):
    """The soonest the chamber could plausibly arrive, for sizing the boost.

    Sizing from the point estimate lets an over-long ETA authorize a boost the
    bed cannot coast back down in time, which is what left a print waiting on a
    cooling bed. Assume instead that the chamber is rising as fast as the
    regression's uncertainty allows: a shorter ETA buys a smaller boost.
    """
    if slope is None:
        return None
    return eta_minutes(
        temperature, target, slope + confidence * (stderr or 0.0),
        minimum_rate,
    )


def confidently_warming(slope, stderr, confidence, minimum_rate):
    """Is the chamber demonstrably rising, not just not-falling?

    Sizing a boost needs a trustworthy ETA, and `early_arrival_eta` will happily
    produce one from a slope indistinguishable from zero. On the 2026-08-25 cold
    start the first usable ETA was 291 minutes, off a slope of about
    0.02 C/min -- which maps to the entire boost cap. Requiring the *lower*
    confidence bound to clear the minimum rate refuses that, while the upper
    bound still does the sizing.
    """
    if slope is None:
        return False
    return slope - confidence * (stderr or 0.0) >= minimum_rate


def approach_eta(samples, target, min_span=60.0, min_samples=12):
    """Minutes to `target` for a chamber approaching an asymptote.

    A linear extrapolation cannot see deceleration, and the chamber decelerates
    the whole way: measured against ground truth on the 2026-08-26 cold soak,
    the linear estimate under-predicted by a median of **8.6 minutes** and no
    window length or smoothing moved it, because the error is the model's, not
    noise. Under-predicting is what made the bed turn around far too early.

    Newton's law again, warming instead of cooling: dT/dt = (T_inf - T)/tau, so
    regressing each sub-block's rate against its mean temperature gives both the
    asymptote and the time constant, and the target falls out analytically. Same
    machinery as `cooling_asymptote`, opposite sign of travel.

    Returns None whenever the fit does not support an answer -- too little data,
    no curvature, or an asymptote at or below the target, which means the
    chamber is not going to get there at all on the present trajectory.
    """
    if len(samples) < min_samples:
        return None
    if samples[-1][0] - samples[0][0] < min_span:
        return None
    blocks = []
    size = max(3, len(samples) // 4)
    for start in range(0, len(samples) - size + 1, size):
        chunk = samples[start:start + size]
        rate, unused = least_squares_slope(chunk)
        if rate is None:
            continue
        blocks.append((sum(v for unused_t, v in chunk) / len(chunk), rate))
    if len(blocks) < 3:
        return None
    gradient, intercept = _line(blocks)
    if gradient is None or gradient >= 0.0:
        return None
    tau = -1.0 / gradient
    asymptote = -intercept / gradient
    now = samples[-1][1]
    if asymptote <= target or now >= asymptote:
        return None
    return -tau * math.log((asymptote - target) / (asymptote - now))


def boost_for_eta(eta, max_boost, coast_rate, lead_minutes=0.0):
    """Map chamber minutes remaining to bed boost, landing early by `lead`.

    Waiting on the bed is always wasted time, so the schedule aims for the
    plate to be back at its printing setpoint before the chamber arrives rather
    than at the same moment. Subtracting the lead from the ETA does that
    naturally: as the ETA falls a minute per minute, the desired boost falls at
    exactly `coast_rate` per minute, so the slew limit never binds and the bed
    reaches target with `lead` to spare.
    """
    usable = max(0.0, eta - lead_minutes)
    return min(max_boost, usable * coast_rate)


def eta_gated_coast(current_boost, desired_boost, elapsed_minutes, coast_rate):
    """Follow a falling ETA without re-boosting or exceeding the coast rate."""
    slew_floor = max(0.0, current_boost - elapsed_minutes * coast_rate)
    return min(current_boost, max(desired_boost, slew_floor))


def cooling_asymptote(samples, min_decay, tau_min, tau_max, drop_max,
                      min_span=60.0, min_samples=12):
    """Where a cooling nozzle is heading, from Newton's law of cooling.

    dT/dt = -(T - T_inf)/tau, so regressing each sub-block's rate against its
    mean temperature gives a line whose intercept and gradient yield
    T_inf = -b/m. The nozzle asymptotes to chamber air, so T_inf is a chamber
    estimate available long before the nozzle actually gets there.

    This is admitted only for a nozzle that is *clearly* shedding heat. On the
    four recorded cold starts, where the nozzle rises with the chamber, noise
    supplies a spurious negative curvature and -b/m explodes: replayed without
    this guard the estimate overshot the true settling point by +62 to +125 C.
    Replayed with it, all four are refused and the one genuine warm start is
    accepted with the estimate never once reading high.

    Returns (temperature, reason). A None temperature means "do not use".
    """
    if len(samples) < min_samples:
        return None, "too few samples"
    if samples[-1][0] - samples[0][0] < min_span:
        return None, "span too short"
    slope, stderr = least_squares_slope(samples)
    if slope is None:
        return None, "no fit"
    # Require the decay to be real, not a noise excursion.
    if slope + 2.0 * (stderr or 0.0) >= -min_decay:
        return None, "not clearly cooling"
    blocks = []
    size = max(3, len(samples) // 4)
    for start in range(0, len(samples) - size + 1, size):
        chunk = samples[start:start + size]
        rate, unused = least_squares_slope(chunk)
        if rate is None:
            continue
        mean_temp = sum(v for unused_t, v in chunk) / len(chunk)
        blocks.append((mean_temp, rate))
    if len(blocks) < 3:
        return None, "too few blocks"
    gradient, intercept = _line(blocks)
    if gradient is None or gradient >= 0.0:
        return None, "no decay curvature"
    tau = -60.0 / gradient
    if not tau_min <= tau <= tau_max:
        return None, "time constant %.0fs implausible" % tau
    estimate = -intercept / gradient
    now = samples[-1][1]
    if estimate >= now or estimate < now - drop_max:
        return None, "asymptote not below the nozzle"
    return estimate, "ok"


def _line(points):
    """Ordinary least squares through (x, y) pairs."""
    n = len(points)
    if n < 3:
        return None, None
    mean_x = sum(x for x, unused in points) / n
    mean_y = sum(y for unused, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, unused in points)
    if sxx <= 0.0:
        return None, None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    gradient = sxy / sxx
    return gradient, mean_y - gradient * mean_x


def usable_history(temperatures, targets, interval, now, span_seconds):
    """Trailing (eventtime, temperature) pairs the nozzle can be judged on.

    Moonraker keeps 1200 one-second samples of every sensor, which is the same
    scrollback Mainsail draws. On a machine that has been idle overnight the
    last two minutes of it already prove the nozzle is settled, so there is no
    reason to spend two more minutes re-deriving that from scratch.

    Samples are taken newest-first and stop at the first commanded nozzle
    target, because anything at or before that point describes a heater, not a
    chamber. A nozzle still plunging after an M104 S0 keeps its steep negative
    slope and correctly fails to qualify.
    """
    if not temperatures or interval <= 0.0:
        return []
    targets = targets or []
    wanted = int(span_seconds / interval)
    picked = []
    for offset in range(1, min(len(temperatures), wanted) + 1):
        temperature = temperatures[-offset]
        target = targets[-offset] if offset <= len(targets) else 0.0
        if temperature is None:
            break
        if target is None or target > 0.0:
            break
        picked.append(temperature)
    picked.reverse()
    if len(picked) < 3:
        return []
    oldest = now - (len(picked) - 1) * interval
    return [(oldest + i * interval, t) for i, t in enumerate(picked)]


def quantize_target(value, step=0.1):
    """Command the bed at sensible resolution; 0.01 C was meaningless."""
    return round(round(value / step) * step, 3)


class ProxyGate:
    """Qualify the nozzle as a chamber thermometer from its own trend.

    Wire values 2, 3 and 4 keep their recorded meaning. 1 (COLD_VALID) is
    retired and never emitted: trusting a nozzle because it merely started
    below a warm threshold is what let a still-cooling 31.7 C nozzle drive a
    boost. Every start now qualifies the same way.
    """

    COOLING = 2
    SETTLING = 3
    VALID = 4

    def __init__(self, ready_slope, dwell_seconds):
        self.ready_slope = ready_slope
        self.dwell_seconds = dwell_seconds
        self.cooling_seen = False
        self.dwell = 0.0
        self.valid = False
        self.state = self.COOLING
        self.forced = False
        self.by_settling = False
        self.by_asymptote = None

    def update(self, slope, elapsed_seconds, stderr=None, temperature=None,
               minimum=None, confidence=2.0):
        """Advance on a regressed slope; None means the window is not full."""
        if self.valid or slope is None:
            return self.valid
        if slope < self.ready_slope:
            # Still shedding its own heat, so it is reading itself.
            self.cooling_seen = True
            self.dwell = 0.0
            self.state = self.COOLING
            return False
        # A nozzle that was clearly cooling from above, has flattened to within
        # the regression's own uncertainty of zero, and sits above the
        # threshold has already reached chamber air. The settling dwell exists
        # to reject a momentary flat spot in noise; the standard error answers
        # that question directly and without spending another minute on it.
        settled = slope + confidence * (stderr or 0.0) >= 0.0
        above = (
            minimum is not None and temperature is not None
            and temperature >= minimum
        )
        if self.cooling_seen and settled and above:
            self.valid = True
            self.state = self.VALID
            self.by_settling = True
            return True
        self.dwell += elapsed_seconds
        self.state = self.SETTLING
        if self.dwell >= self.dwell_seconds:
            self.valid = True
            self.state = self.VALID
        return self.valid

    def qualify_by_asymptote(self, estimate):
        """Accept a still-cooling nozzle whose destination is already hot."""
        self.valid = True
        self.state = self.VALID
        self.by_asymptote = estimate
        return True

    def force(self):
        """Give up waiting and proceed unqualified, recording that we did."""
        self.forced = True
        self.valid = True
        self.state = self.VALID
        return self.valid


class ChamberPreheat:
    cmd_CHAMBER_PREHEAT_WAIT_help = (
        "Qualify the stirred nozzle and apply an optional ETA bed boost"
    )
    cmd_CHAMBER_RECORD_HANDOFF_help = (
        "Record chamber observer handoff in the reactor clock domain"
    )

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.heaters = self.printer.load_object(config, "heaters")
        self.sample_seconds = config.getfloat(
            "slope_sample_seconds", 60.0, minval=5.0
        )
        self.update_seconds = config.getfloat(
            "update_seconds", 5.0, minval=1.0
        )
        self.minimum_rate = config.getfloat(
            "minimum_rate", 0.02, above=0.0
        )
        self.default_max_boost = config.getfloat(
            "max_bed_boost", 1000.0, minval=0.0
        )
        self.default_coast_rate = config.getfloat(
            "coast_rate", 1.0, above=0.0
        )
        self.default_initial_boost = config.getfloat(
            "initial_bed_boost", 5.0, minval=0.0
        )
        self.default_filter_alpha = config.getfloat(
            "rate_filter_alpha", 0.25, above=0.0, maxval=1.0
        )
        # Qualification. A nozzle counts as a chamber thermometer once its
        # regressed slope reaches this band from below and holds for the dwell.
        self.ready_slope = config.getfloat("ready_slope", -0.02, maxval=0.0)
        self.settle_dwell = config.getfloat(
            "settle_dwell", 60.0, minval=5.0
        )
        # Give up qualifying rather than hang a print forever. Proceeding is
        # recorded and announced; the soak is then no better than stock.
        self.qualify_timeout = config.getfloat(
            "qualify_timeout", 900.0, minval=60.0
        )
        # Trailing regression window for the nozzle slope, and how many
        # standard errors to assume in the chamber's favour when sizing boost.
        self.slope_window = config.getfloat(
            "slope_window_seconds", 120.0, minval=30.0
        )
        self.slope_confidence = config.getfloat(
            "slope_confidence", 2.0, minval=0.0
        )
        # Commit only a fraction of the apparent headroom. The boost warms the
        # chamber it is being scheduled against, so arrival is always sooner
        # than the pre-boost slope predicts. Replaying the 2026-08-25 traces,
        # the full headroom left the bed holding the print open 1.8 minutes;
        # half left none. That is one qualifying run, not a tuned constant.
        self.boost_fraction = config.getfloat(
            "boost_fraction", 1.0, above=0.0, maxval=1.0
        )
        # Do not bother committing a boost smaller than this; near the end of a
        # soak it is churn on the bed for no useful heat.
        self.min_boost_step = config.getfloat(
            "min_boost_step", 0.5, minval=0.0
        )
        # Defaults here MUST match chamber_preheat.cfg. A code default that
        # silently disagrees with the shipped config gives anyone who installs
        # the extra without the full file different behaviour from the one
        # documented; scripts/test-exhaust-adaptive.py pins the pair.
        #
        # A slope worth scheduling against, distinct from minimum_rate, which
        # only asks whether an ETA can be displayed at all. This is not the
        # safety mechanism -- sizing from the earliest plausible arrival is,
        # and boost_fraction is the margin on top. It exists because a trailing
        # regression LAGS a chamber whose slope is still climbing, and that bias
        # is systematic, so the standard error cannot see it. Committing while
        # the window is still full of pre-ramp data reads the ETA far too long:
        # on the 2026-08-25 cold start, 0.02 C/min authorized a boost against a
        # 188-minute estimate. Every threshold from 0.02 to 0.40 produced the
        # same 5 C on that run, capped by initial_boost, so this is a judgment
        # call rather than a tuned constant.
        self.boost_min_rate = config.getfloat(
            "boost_min_rate", 0.02, above=0.0
        )
        # Land the bed back on its setpoint this far ahead of the chamber.
        # Measured 2026-08-26: with no lead the bed held the print open 15 s.
        self.bed_lead_seconds = config.getfloat(
            "bed_lead_seconds", 60.0, minval=0.0
        )
        # The ceiling is an absolute plate temperature, not a boost magnitude.
        # A magnitude cap does not survive changing the printing bed target,
        # and this machine will eventually run a 124 C bed with 60 C chamber
        # targets. What limits the boost today is the filament, not the plate.
        self.max_bed_temp = config.getfloat("max_bed_temp", 100.0, above=0.0)
        # The rise after the opening guess is rate limited rather than a jump
        # to the ETA-permitted level, so the plate climbs monotonically until
        # the ETA says it must turn around and come back down.
        # BOOST POLICY. "hold" is the operator's model and the default from
        # 2026-09-03: put the plate on its ceiling at once, leave it there
        # while it is doing the work, and hand it back to the printing target
        # the moment the chamber crosses its minimum.
        #
        # "eta" is the original planner, kept whole and reachable in one config
        # line rather than deleted. It sizes the boost from the chamber ETA --
        # boost_for_eta maps minutes remaining to degrees, so the plate coasts
        # down at coast_rate and lands on target just before the chamber
        # arrives. That reasoning is sound and its failure is empirical: the
        # coast holds the plate BELOW the ceiling with the heater for as long
        # as it lasts, and every one of those minutes is one the chamber is
        # being heated less. Observed 2026-09-03 on a real ABS print -- 111 to
        # 124 at 3 C/min, then twenty minutes walking back down at the 1 C/min
        # floor, with the bed sitting on target and idle at the end of it while
        # the chamber ETA still read 4 to 11 minutes. The schedule delays the
        # thing it is waiting for.
        #
        # Under "hold" the descent is not scheduled at all: the plate is simply
        # released and falls at whatever passive loss gives, which the measured
        # curve puts near 5 C/min up there against the 1 C/min it was being
        # walked down at. See notes/data/cooldown/zero2-passive-decay-20260903.md.
        # Passive fall rate used ONLY to report a bed ETA under the "hold"
        # policy. It estimates, it does not command: nothing schedules the
        # descent there. 5 C/min is the measured plate loss near print
        # temperature, not a design constant.
        self.bed_release_rate = config.getfloat(
            "bed_release_rate", 5.0, above=0.0
        )
        self.boost_policy = config.getchoice(
            "boost_policy", {"hold": "hold", "eta": "eta"}, "hold"
        )
        self.climb_rate = config.getfloat("climb_rate", 3.0, above=0.0)
        # Before any ETA exists the plate ramps blind at climb_rate, commanded
        # in whole degrees so the moves are legible on the graph. Holding the
        # opening guess flat was a soft plateau that bought nothing.
        self.blind_step = config.getfloat("blind_step", 1.0, above=0.0)
        self.blind_step_seconds = config.getfloat(
            "blind_step_seconds", 20.0, above=0.0
        )
        # How far the blind ramp may run before it needs an ETA to justify
        # going further. In practice a trend appears inside a couple of
        # minutes, so this only bounds the pathological case -- a nozzle that
        # never yields one would otherwise ramp to the absolute ceiling with
        # no idea whether it can be unwound in time.
        self.blind_max = config.getfloat("blind_max", 15.0, minval=0.0)
        # Ease into the plateau instead of arriving at full rate: within this
        # many minutes of the target the climb rate steps down one stage per
        # minute, 3 -> 2 -> 1.
        self.climb_ease_minutes = config.getfloat(
            "climb_ease_minutes", 2.0, minval=0.0
        )
        # The turn is a threshold crossing on a noisy signal, so it fires on the
        # ETA's DOWNWARD excursions and turns the plate around early and low.
        # A high hold fraction afterwards is the evidence: the schedule keeps
        # saying the boost was still supported, which means the peak should
        # have been higher or later. Deciding the turn on the highest ETA seen
        # in this window resists a single dip. The descent still floors on the
        # live value, so it never holds on a stale one.
        self.turn_eta_window = config.getfloat(
            "turn_eta_window", 180.0, minval=0.0
        )
        # The plate settles here rather than exactly on the printing target.
        # Nozzle and motion after the soak add their own heat, and a small bump
        # nudges what becomes a glacially slow chamber rise at the end without
        # measurably hurting bed stability.
        # Klipper's ControlPID.check_busy tests the ABSOLUTE error, so a plate
        # ABOVE its printing target is as busy as one below, and the following
        # M190 blocks while it cools. PID_SETTLE_DELTA is 1.0, so a carry of
        # exactly 1.0 sits on the boundary: 2026-08-26 finished at 65.99 and
        # cleared it by a hundredth of a degree. Anything over, and the carry
        # buys chamber heat by making the operator wait for the bed, which is
        # the opposite of the intent. Kept strictly inside the window.
        self.bed_carry = config.getfloat(
            "bed_carry", 0.5, minval=0.0, maxval=0.9
        )
        # Hold at the peak before turning around, so the maximum is a plateau
        # rather than an instant.
        self.peak_plateau = config.getfloat("peak_plateau", 60.0, minval=0.0)
        # The approach ETA is unbiased but jumpy; smooth it before acting.
        self.eta_alpha = config.getfloat(
            "eta_alpha", 0.25, above=0.0, maxval=1.0
        )
        # How fast the plate may be dumped once it is at or past its deadline.
        self.descent_max_rate = config.getfloat(
            "descent_max_rate", 6.0, above=0.0
        )
        # Hold the descent only when the chamber has demonstrably SLOWED: the
        # smoothed ETA growing by at least this much since the last sample.
        # Holding merely because the ETA read higher than the current boost was
        # not new information, and it stood still for half the descent.
        self.eta_grow_hold = config.getfloat(
            "eta_grow_hold", 0.75, minval=0.0
        )
        # Seed the slope window from Moonraker's temperature store so a machine
        # that has been idle does not re-derive what the scrollback already
        # shows. Purely an optimization: any failure just costs the wait.
        self.history_seconds = config.getfloat(
            "history_seconds", 300.0, minval=0.0
        )
        self.history_interval = config.getfloat(
            "history_interval", 1.0, above=0.0
        )
        self.moonraker_url = config.get(
            "moonraker_url", "http://127.0.0.1:7125"
        )
        self.moonraker_timeout = config.getfloat(
            "moonraker_timeout", 5.0, above=0.0
        )
        # The store's newest sample and the live reading are the same sensor a
        # second apart. If they disagree the store is stale or belongs to a
        # different session, and splicing it on would fabricate a step change
        # that reads as a huge slope.
        self.history_match = config.getfloat(
            "history_match_tolerance", 2.0, above=0.0
        )
        # Newton-cooling extrapolation: accept a still-cooling nozzle when its
        # projected destination already clears the threshold by this margin.
        # Zero disables it and restores plain settling.
        self.asymptote_margin = config.getfloat(
            "asymptote_margin", 2.0, minval=0.0
        )
        self.asymptote_min_decay = config.getfloat(
            "asymptote_min_decay", 0.10, above=0.0
        )
        self.asymptote_tau_min = config.getfloat(
            "asymptote_tau_min", 30.0, above=0.0
        )
        self.asymptote_tau_max = config.getfloat(
            "asymptote_tau_max", 1800.0, above=0.0
        )
        self.asymptote_drop_max = config.getfloat(
            "asymptote_drop_max", 60.0, above=0.0
        )
        # Do not project from a sliver of data. Replayed fresh, the warm run
        # would otherwise fire at 48 s off ten samples.
        self.asymptote_min_span = config.getfloat(
            "asymptote_min_span", 60.0, above=0.0
        )
        self.max_temp_margin = config.getfloat(
            "max_temp_margin", 1.0, minval=0.0
        )
        self.bed_tolerance = config.getfloat(
            "bed_ready_tolerance", 0.5, minval=0.0
        )
        self.report_seconds = config.getfloat(
            "report_seconds", 30.0, minval=5.0
        )
        # ORBITING STIR, off until it has been watched on hardware.
        #
        # The parked nozzle sits in fan0's downdraft for the whole soak and digs
        # a cold spot in the plate that it then reads as the chamber. Sweeping a
        # spiral spreads that extraction evenly -- chamber_orbit.cfg carries the
        # geometry and why it is semicircles.
        #
        # ONE ARC PER LOOP, never a whole sweep. toolhead._process_moves pauses
        # its caller as soon as the queued buffer passes BUFFER_TIME_HIGH
        # (toolhead.py:400-408), so handing it 60 s of motion from in here would
        # stop this loop sampling until the sweep drained. Feeding one semicircle
        # lets that pause pace the loop against the motion rather than a timer.
        # It costs sampling cadence, which is why this is a flag and not yet the
        # default.
        self.orbit = config.getboolean("orbit", False)
        # WAIT FOR THE BED, OR HAND IT TO THE M190 THAT ALREADY FOLLOWS?
        #
        # M191 has two phases. It waits for the chamber minimum, and then it
        # keeps waiting for the boosted plate to coast back down to the
        # printing target. Measured on the 2026-09-07 ABS jobs, the second
        # phase ran 90 s AFTER the chamber minimum was crossed, with nothing
        # else happening:
        #
        #   +2s  M191 bed released to 100.5 -- chamber minimum crossed
        #   +72s M191 noz=45.6 eta=0.0 bed=102.8>100.5
        #   +92s M191 done bed=100.8
        #
        # That is 90 s of dead time on the critical path, and START_PRINT's
        # very next act is CLEAN_NOZZLE -- a wipe that needs no plate at all.
        # The documented reason the wipe cannot start earlier is that heating
        # the nozzle corrupts the chamber measurement M191 is waiting on; that
        # reason expires the moment the chamber minimum is crossed. Phase two
        # is a BED wait, and the nozzle cannot disturb it.
        #
        # With this false, M191 returns on the chamber alone, having already
        # commanded the plate to its carried target. The wipe then runs while
        # the plate coasts, and START_PRINT's existing `M190 S{target}` --
        # today a no-op, because the plate has always arrived long before it --
        # absorbs whatever coast is left. The probe and the mesh still get a
        # settled plate, because that M190 is what guards them.
        # DEFAULT FALSE as of 2026-09-08: the published stance is that the
        # plate's coast belongs to the M190 that follows, not to M191. It was
        # True while this was an operator-only setting; the measurement that
        # moved it is in kb/chamber-soak.md, and the precondition -- a start
        # sequence with an M190 after the wipe and before the probe -- is
        # documented in notes/personal-tuning-register.md. Set True to restore
        # the serialised behaviour for a start sequence that lacks one.
        self.bed_wait = config.getboolean("bed_wait", False)
        # FINISH THE WIPE AS THE CHAMBER ARRIVES, rather than starting it then.
        #
        # bed_wait above stops the wipe being serialised behind the plate's
        # coast. This goes further and stops it being serialised behind the
        # CHAMBER: M191 returns while the chamber is still `wipe_lead_seconds`
        # short of its minimum, so the heat/wipe/cool finishes just as the
        # chamber gets there instead of beginning there.
        #
        # THIS COMMITS TO A PREDICTION, and that is not a small thing. The
        # chamber minimum is measured through the NOZZLE -- the stock chamber
        # sensor is corrupted by bed proximity, so a nozzle sitting in chamber
        # air is the better proxy -- and heating that nozzle for the wipe ends
        # the measurement. There is no way to run the wipe early AND confirm
        # arrival afterwards; the proxy is gone the moment the heater turns on.
        # So past this point the chamber minimum is asserted from the ETA, not
        # observed, and the run must be audited afterwards against the
        # independent chamber_temp sensor rather than trusted.
        #
        # A second, subtler cost: releasing the plate from its boost is what
        # today marks the crossing, and returning early moves that release
        # earlier by the same lead. The plate is still 110-120 C through the
        # coast, so the chamber keeps climbing, but the ETA it was predicted
        # from assumed the boost stayed on.
        #
        # THE ERROR IS ONE-SIDED. With L the lead and W the real wipe time, the
        # wipe ends L - W relative to arrival: W > L is safe and merely
        # reclaims less, W < L starts the first layer before the chamber got
        # there and nothing re-checks, because the proxy is gone. So L must sit
        # UNDER the shortest observed W, never over the longest.
        #
        # Default 0.0 -- OFF. Turn it on only with a MEASURED wipe duration;
        # CLEAN_NOZZLE reports its own elapsed time for exactly that purpose.
        # DEFAULT 45 s as of 2026-09-08, up from 0 (off). The measured wipe on
        # this machine was 97.0 s and its heat phase alone 35-37 s, so 45 sits
        # under the shortest wipe anyone is likely to see while still reclaiming
        # most of the window. It is a PUBLISHED number, chosen to be safe on a
        # machine nobody has measured; the operator's own is higher and lives in
        # svzero-personal.cfg.
        #
        # 0 still disables the early handover entirely, and remains the right
        # value for a machine whose wipe is unusually short or whose chamber ETA
        # is not trusted.
        self.wipe_lead_seconds = config.getfloat(
            "wipe_lead_seconds", 45.0, minval=0.0
        )
        # No arcs-per-tick knob. A fixed count was the wrong shape: the queue
        # tops out at BUFFER_TIME_HIGH regardless of how many are handed over,
        # so the number never controlled anything useful. _pause feeds until the
        # tick is up instead.
        self.active = False
        self.slope = None
        self.chamber_eta = None
        self.bed_eta = None
        self.peak_boost = 0.0
        self.commanded_bed_target = 0.0
        self.max_boost = self.default_max_boost
        self.coast_rate = self.default_coast_rate
        self.initial_boost = self.default_initial_boost
        self.filter_alpha = self.default_filter_alpha
        self.proxy_gate = None
        self.slope_stderr = None
        self.eta_filtered = None
        self.eta_history = []
        self.hold_minutes = 0.0
        self.fall_minutes = 0.0
        self.stir = 0.0
        self.gcode.register_command(
            "CHAMBER_PREHEAT_WAIT",
            self.cmd_CHAMBER_PREHEAT_WAIT,
            desc=self.cmd_CHAMBER_PREHEAT_WAIT_help,
        )
        self.gcode.register_command(
            "CHAMBER_RECORD_HANDOFF",
            self.cmd_CHAMBER_RECORD_HANDOFF,
            desc=self.cmd_CHAMBER_RECORD_HANDOFF_help,
        )

    @staticmethod
    def _number(value):
        if value is None:
            return "None"
        if isinstance(value, int):
            return str(value)
        return "%.6f" % (value,)

    def _set_state(self, **values):
        commands = [
            "SET_GCODE_VARIABLE MACRO=_CH_STATE VARIABLE=%s VALUE=%s"
            % (name, self._number(value))
            for name, value in values.items()
        ]
        self.gcode.run_script_from_command("\n".join(commands))

    def _set_bed(self, heater, target):
        # 0.01 C of bed command was false precision on a 65 C plate.
        target = quantize_target(target)
        if self.commanded_bed_target == target:
            return
        heater.set_temp(target)
        self.commanded_bed_target = target

    def _temperatures(self, nozzle, bed, eventtime):
        nozzle_temp, unused_nozzle_target = nozzle.get_temp(eventtime)
        bed_temp, unused_bed_target = bed.get_temp(eventtime)
        return nozzle_temp, bed_temp

    def _pause(self, eventtime, seconds):
        """Wait out a sampling tick. When orbiting, run one full cycle instead.

        OUT AND BACK, PAUSE, CHECK THE TEMPERATURE, REPEAT -- which is what was
        asked for, and what the very first version did by accident because it
        queued the whole sweep in one script.

        Everything after that was me protecting a sampling cadence nobody asked
        for. Feeding the arcs a few at a time keeps the loop sampling every five
        seconds, but it cannot keep the motion continuous: the queue tops out at
        BUFFER_TIME_HIGH, and each time it drains the last move is finalised and
        the toolhead stops. One script per cycle blends through the lookahead
        because the junctions are tangent.

        This blocks for most of the cycle, so the nozzle is sampled once per
        out-and-back rather than every update_seconds. That is the shape that
        was wanted; the slope window takes irregular spacing.
        """
        if not self.orbit:
            return self.reactor.pause(eventtime + seconds)
        # RE-ARM EACH CYCLE so the stagger advances: BEGIN recomputes the arc
        # list from the next start radius, which shifts this cycle's tracks
        # between the last one's. It is cheap, and the only motion it adds is a
        # few millimetres of radial hop at the hub.
        self.gcode.run_script_from_command(
            "_CHAMBER_ORBIT_BEGIN\nCHAMBER_ORBIT_CYCLE")
        now = self.reactor.monotonic()
        deadline = eventtime + seconds
        return now if now >= deadline else self.reactor.pause(deadline)

    @staticmethod
    def _set_stir(fan, speed):
        # This asynchronous path bypasses G-code lookahead.  A queued
        # SET_FAN_SPEED does not flush during a blocking temperature wait.
        fan.set_speed(speed)


    def _seed_from_history(self, gcmd, window, gate, now, current_temp):
        """Pre-fill the slope window from Moonraker's stored nozzle history.

        Never fatal. If the history is missing, short, or interrupted by a
        commanded nozzle target, qualification simply runs the long way.
        """
        if self.history_seconds <= 0.0:
            return 0.0
        client = moonraker.MoonrakerClient(
            self.reactor, self.moonraker_url, self.moonraker_timeout
        )
        try:
            result = client.run(
                [("history", "/server/temperature_store", None)]
            )["history"]
        except Exception as error:
            gcmd.respond_info("%s history unavailable (%s)" % (self._stamp(now), error))
            return 0.0
        extruder = (result or {}).get("extruder") or {}
        samples = usable_history(
            extruder.get("temperatures"),
            extruder.get("targets"),
            self.history_interval,
            now,
            self.history_seconds,
        )
        if not samples:
            return 0.0
        drift = abs(samples[-1][1] - current_temp)
        if drift > self.history_match:
            gcmd.respond_info(
                "%s history rejected, %.1fC from live reading"
                % (self._stamp(now), drift)
            )
            return 0.0
        previous = samples[0][0]
        for eventtime, temperature in samples:
            window.push(eventtime, temperature)
            slope, unused_stderr = window.value()
            gate.update(slope, eventtime - previous)
            previous = eventtime
        return samples[-1][0] - samples[0][0]

    def _new_proxy_gate(self):
        return ProxyGate(self.ready_slope, self.settle_dwell)

    def _climb_rate_near(self, remaining):
        """Full rate far out, stepping down by 1 C/min per minute near the top.

        Arriving at the plateau at full speed overshoots the schedule and then
        has to give it straight back. `remaining` is degrees still to climb, so
        at 1 C/min it is also the minutes left of climbing.
        """
        if self.climb_ease_minutes <= 0.0:
            return self.climb_rate
        rate = self.climb_rate
        while rate > 1.0 and remaining < rate * 1.0:
            rate -= 1.0
        return max(1.0, min(self.climb_rate, rate))

    def _turn_eta(self, eventtime, eta):
        """Highest recent ETA, so the turn does not fire on a downward blip."""
        if eta is None:
            return None
        self.eta_history.append((eventtime, eta))
        horizon = eventtime - self.turn_eta_window
        self.eta_history = [x for x in self.eta_history if x[0] >= horizon]
        return max(e for unused_t, e in self.eta_history)

    def _chamber_eta(self, window, temperature, minimum):
        """Smoothed minutes to the chamber minimum, exponential where it fits."""
        if temperature >= minimum:
            self.eta_filtered = 0.0
            return 0.0
        raw = approach_eta(window.samples, minimum)
        if raw is None:
            # No usable curvature yet. Fall back to the linear estimate, which
            # under-predicts a decelerating chamber but is better than nothing.
            # The weak path keeps the boost_min_rate gate; approach_eta needs no
            # such guard, because it already refuses without real curvature and
            # an asymptote above the target.
            raw = eta_minutes(
                temperature, minimum, self.slope, self.minimum_rate
            ) if confidently_warming(
                self.slope, self.slope_stderr, self.slope_confidence,
                self.boost_min_rate,
            ) else None
        if raw is None:
            return self.eta_filtered
        self.eta_filtered = exponential_filter(
            self.eta_filtered, raw, self.eta_alpha
        )
        return self.eta_filtered

    def _update_proxy(self, gate, slope, stderr, elapsed_seconds,
                      temperature=None, minimum=None):
        self.slope = slope
        self.slope_stderr = stderr
        gate.update(
            slope, elapsed_seconds, stderr, temperature, minimum,
            self.slope_confidence,
        )
        self._set_state(
            proxy_valid=1 if gate.valid else 0,
            proxy_slope=slope,
            proxy_validity=gate.state,
            proxy_cooling_seen=1 if gate.cooling_seen else 0,
            proxy_valid_dwell=gate.dwell,
        )
        return gate.valid

    def cmd_CHAMBER_RECORD_HANDOFF(self, gcmd):
        self._set_state(handoff_eventtime=self.reactor.monotonic())

    @staticmethod
    def _stamp(eventtime):
        """Seconds-within-minute, so Mainsail's minute stamp can be read.

        Several lines can land inside one console minute and the count varies,
        which makes the order and spacing unreadable at minute resolution.
        """
        return ":%02d M191" % (int(eventtime) % 60)

    @staticmethod
    def _num(value, spec="%.1f"):
        return "--" if value is None else spec % value

    def cmd_CHAMBER_PREHEAT_WAIT(self, gcmd):
        minimum = gcmd.get_float("MINIMUM")
        bed_target = gcmd.get_float("BED_TARGET", 0.0, minval=0.0)
        boost_value = gcmd.get_float("BOOST", 1.0, minval=0.0, maxval=1.0)
        if boost_value not in (0.0, 1.0):
            raise gcmd.error("BOOST must be 0 or 1")
        boost_requested = int(boost_value)
        stir = gcmd.get_float("STIR", 1.0, minval=0.0, maxval=1.0)
        # 1 = bare-M191 compatibility floor, 2 = a slicer's exact minimum.
        policy = gcmd.get_int("POLICY", 2, minval=1, maxval=2)
        max_boost = gcmd.get_float(
            "MAX_BOOST", self.default_max_boost, minval=0.0
        )
        coast_rate = gcmd.get_float(
            "COAST_RATE", self.default_coast_rate, above=0.0
        )
        bed_wait = bool(gcmd.get_int("BED_WAIT", 1 if self.bed_wait else 0,
                                     minval=0, maxval=1))
        wipe_lead = gcmd.get_float(
            "WIPE_LEAD", self.wipe_lead_seconds, minval=0.0
        )
        initial_boost = gcmd.get_float(
            "INITIAL_BOOST", self.default_initial_boost, minval=0.0
        )
        filter_alpha = gcmd.get_float(
            "FILTER_ALPHA", self.default_filter_alpha, above=0.0, maxval=1.0
        )
        if initial_boost > max_boost:
            raise gcmd.error("INITIAL_BOOST must not exceed MAX_BOOST")

        nozzle = self.heaters.lookup_heater("extruder")
        bed = self.heaters.lookup_heater("heater_bed")
        fan = self.printer.lookup_object("fan_generic fan0").fan
        maximum_target = min(bed.max_temp - self.max_temp_margin,
                             self.max_bed_temp)
        if bed_target > maximum_target:
            raise gcmd.error(
                "BED_TARGET %.2f exceeds the safe bed maximum %.2f"
                % (bed_target, maximum_target)
            )
        available_boost = max(0.0, maximum_target - bed_target)
        boost_cap = min(max_boost, available_boost)

        self.active = True
        self.slope = None
        self.slope_stderr = None
        self.chamber_eta = None
        self.bed_eta = None
        self.peak_boost = 0.0
        self.max_boost = max_boost
        self.coast_rate = coast_rate
        self.initial_boost = initial_boost
        self.filter_alpha = filter_alpha
        self.stir = stir
        start_time = self.reactor.monotonic()
        start_temp, unused_bed_temp = self._temperatures(
            nozzle, bed, start_time
        )
        completed = False
        gate = self.proxy_gate = self._new_proxy_gate()
        window = SlopeWindow(self.slope_window)
        try:
            self._set_stir(fan, stir)
            if self.orbit:
                # The M191 macro has already homed and parked at bed centre, so
                # the spiral starts from where the toolhead already is. BEGIN
                # only computes the arc list and steps to the first radius; the
                # sweeping is done one arc at a time by the loop below.
                self.gcode.run_script_from_command("_CHAMBER_ORBIT_BEGIN")
            if policy == 1 and start_temp >= minimum and not boost_requested:
                # The compatibility floor is room temperature and there is no
                # boost to schedule, so there is no decision the slope could
                # inform. Return as the bare command always has. An exact
                # slicer minimum never takes this path: a cooling nozzle reads
                # high, and that is precisely what qualification is for.
                self._set_state(
                    preheat_phase=4,
                    preheat_result=1,
                    chamber_time_remaining=0.0,
                    bed_time_remaining=0.0,
                    chamber_ready_eventtime=start_time,
                    print_ready_eventtime=start_time,
                )
                gcmd.respond_info(
                    "%s compat noz=%.1f floor=%.1f"
                    % (self._stamp(start_time), start_temp, minimum)
                )
                return
            self._set_state(
                proxy_valid=0,
                proxy_start_temp=start_temp,
                proxy_slope=None,
                proxy_validity=gate.state,
                proxy_cooling_seen=0,
                proxy_valid_dwell=0.0,
            )
            # The opening guess is blind by definition, so gating it on
            # qualification only wasted heating time -- on 2026-08-26 the plate
            # sat at its printing target for four minutes while the nozzle
            # settled. Whether a boost is possible is decided immediately;
            # only the ETA-driven parts need a qualified nozzle. Skip it when
            # the nozzle already clears the minimum, where the soak is likely
            # over before any boost could be unwound.
            opening = 0.0
            if (boost_requested and boost_cap > 0.0
                    and start_temp < minimum):
                # Hold goes to the ceiling here, at the very first command.
                # This is the earliest the plate can be told anything, and
                # under this policy there is nothing to learn first: the
                # ceiling does not depend on the ETA, the slope, or whether the
                # nozzle has qualified as a chamber proxy yet.
                opening = (
                    boost_cap if self.boost_policy == "hold"
                    else min(initial_boost, boost_cap)
                )
            self._set_bed(bed, quantize_target(bed_target + opening))
            if opening > 0.0:
                gcmd.respond_info(
                    "%s guess bed %.1f while the nozzle settles"
                    % (self._stamp(start_time), bed_target + opening)
                )
            self._set_state(
                preheat_phase=1,
                boost_bed_target=None,
                boost_coast_eventtime=None,
                boost_coast_reason=0,
                chamber_time_remaining=None,
                bed_time_remaining=None,
            )

            # History first, then the live sample, so the window stays ordered.
            seeded = self._seed_from_history(
                gcmd, window, gate, start_time, start_temp
            )
            window.push(start_time, start_temp)
            if seeded:
                slope, stderr = window.value()
                self.slope, self.slope_stderr = slope, stderr
                self._set_state(
                    proxy_slope=slope,
                    proxy_validity=gate.state,
                    proxy_valid_dwell=gate.dwell,
                    proxy_valid=1 if gate.valid else 0,
                    proxy_cooling_seen=1 if gate.cooling_seen else 0,
                )
                gcmd.respond_info(
                    "%s history %.0fs slope=%s %s"
                    % (
                        self._stamp(start_time),
                        seeded,
                        self._num(slope, "%+.2f"),
                        "qualified" if gate.valid else "settling",
                    )
                )

            eventtime = previous_time = start_time
            nozzle_temp = start_temp
            deadline = start_time + self.qualify_timeout
            next_report = start_time + self.report_seconds
            while not gate.valid and not self.printer.is_shutdown():
                eventtime = self._pause(eventtime, self.update_seconds)
                nozzle_temp, unused_bed = self._temperatures(
                    nozzle, bed, eventtime
                )
                window.push(eventtime, nozzle_temp)
                slope, stderr = window.value()
                self._update_proxy(
                    gate, slope, stderr, eventtime - previous_time,
                    nozzle_temp, minimum,
                )
                previous_time = eventtime
                self._set_stir(fan, stir)
                if not gate.valid and self.asymptote_margin > 0.0:
                    estimate, reason = cooling_asymptote(
                        window.samples,
                        self.asymptote_min_decay,
                        self.asymptote_tau_min,
                        self.asymptote_tau_max,
                        self.asymptote_drop_max,
                        self.asymptote_min_span,
                    )
                    if estimate is not None and (
                        estimate >= minimum + self.asymptote_margin
                    ):
                        # Still cooling, but it is heading somewhere already
                        # warm enough. Take the destination as the chamber and
                        # stop charging the operator for the descent.
                        gate.qualify_by_asymptote(estimate)
                        self._set_state(
                            proxy_valid=1,
                            proxy_validity=gate.state,
                            proxy_anchor=estimate,
                        )
                        gcmd.respond_info(
                            "%s projected %.1fC >= %.1f, qualified early"
                            % (self._stamp(eventtime), estimate,
                               minimum + self.asymptote_margin)
                        )
                if not gate.valid and eventtime >= deadline:
                    gate.force()
                    self._set_state(
                        proxy_valid=1, proxy_validity=gate.state
                    )
                    gcmd.respond_info(
                        "%s unqualified after %.0fs, proceeding"
                        % (self._stamp(eventtime), self.qualify_timeout)
                    )
                    break
                if eventtime >= next_report:
                    gcmd.respond_info(
                        "%s settle noz=%.1f slope=%s dwell=%.0f/%.0f"
                        % (
                            self._stamp(eventtime),
                            nozzle_temp,
                            self._num(self.slope, "%+.2f"),
                            gate.dwell,
                            self.settle_dwell,
                        )
                    )
                    next_report = eventtime + self.report_seconds
            if self.printer.is_shutdown():
                raise gcmd.error(
                    "Chamber preheat interrupted by Klipper shutdown"
                )

            # Qualified. Size one boost from the soonest arrival the slope's
            # uncertainty allows, so the bed can always coast back in time.
            chamber_ready = nozzle_temp >= minimum
            self.chamber_eta = 0.0 if chamber_ready else eta_minutes(
                nozzle_temp, minimum, self.slope, self.minimum_rate
            )
            coast_reason = 0
            planned_boost = opening
            if boost_requested and boost_cap > 0.0 and not chamber_ready:
                # INITIAL_BOOST is the opening guess, applied before any slope
                # exists -- that is its entire purpose. A 65 C bed goes to 70
                # the moment the nozzle qualifies, and climbs toward the cap
                # later if the ETA turns out to permit it. Qualification, not
                # slope quality, is the gate: a nozzle that is not yet a
                # chamber thermometer must not schedule anything, but one that
                # is need not wait for a measurable trend to start heating.
                # Under "hold" the opening guess IS the ceiling. INITIAL_BOOST
                # exists because the ETA planner cannot size a boost before a
                # slope exists, so it commits a small one and grows it later.
                # Hold never sizes anything, so there is nothing to be
                # tentative about and a 5 C opening would just be the first
                # stair of the ramp this policy exists to remove.
                planned_boost = (
                    boost_cap if self.boost_policy == "hold"
                    else min(initial_boost, boost_cap)
                )
                coast_reason = 1 if planned_boost > 0.0 else 3
            elif boost_requested and available_boost <= 0.0:
                coast_reason = 3

            self.peak_boost = planned_boost
            command = quantize_target(bed_target + planned_boost)
            self._set_bed(bed, command)
            self.bed_eta = planned_boost / coast_rate if planned_boost else 0.0
            self._set_state(
                preheat_phase=2 if planned_boost > 0.0 else 1,
                boost_bed_target=command if planned_boost > 0.0 else None,
                boost_coast_reason=coast_reason,
                chamber_time_remaining=self.chamber_eta,
                bed_time_remaining=self.bed_eta,
            )
            # A chamber already at target, or a bed that was never boosted, is
            # ready here and the live loop's transition tests will never fire.
            # Without this the fast path recorded only print_ready_eventtime,
            # leaving the chamber and bed timestamps null and the run
            # unusable for "how long did each take".
            if chamber_ready:
                self._set_state(chamber_ready_eventtime=eventtime)
            if planned_boost <= 0.0:
                self._set_state(bed_ready_eventtime=eventtime)
            gcmd.respond_info(
                "%s ready noz=%.1f slope=%s+-%s eta=%s boost=%.1f"
                % (
                    self._stamp(eventtime),
                    nozzle_temp,
                    self._num(self.slope, "%+.2f"),
                    self._num(self.slope_stderr, "%.2f"),
                    self._num(self.chamber_eta, "%.1f"),
                    planned_boost,
                )
            )

            current_boost = planned_boost
            decaying = False
            eta_seen = False
            announced = planned_boost
            at_peak = None
            previous_eta = None
            blind_stepped = eventtime
            self.hold_minutes = 0.0
            self.fall_minutes = 0.0
            bed_gated = planned_boost > 0.0
            bed_ready = not bed_gated
            last_command = command
            next_report = eventtime + self.report_seconds
            while not self.printer.is_shutdown():
                eventtime = self._pause(eventtime, self.update_seconds)
                nozzle_temp, bed_temp = self._temperatures(
                    nozzle, bed, eventtime
                )
                window.push(eventtime, nozzle_temp)
                slope, stderr = window.value()
                if slope is not None:
                    self.slope = slope
                    self.slope_stderr = stderr
                    self._set_state(proxy_slope=slope)
                interval_minutes = (eventtime - previous_time) / 60.0
                previous_time = eventtime
                self._set_stir(fan, stir)

                if not chamber_ready and nozzle_temp >= minimum:
                    chamber_ready = True
                    self._set_state(chamber_ready_eventtime=eventtime)
                live_eta = 0.0 if chamber_ready else eta_minutes(
                    nozzle_temp, minimum, self.slope, self.minimum_rate
                )
                self.chamber_eta = live_eta

                # The boost climbs while the ETA still permits it, then only
                # falls. "Do not step up again" applies once it has STARTED
                # DECAYING -- before that, a longer-than-guessed ETA is exactly
                # the case INITIAL_BOOST was guessing at, and the bed should
                # take the extra headroom. On a 65 C target that is 70 C at
                # qualification and up to 75 C once the trend is known.
                safe_eta = 0.0 if chamber_ready else early_arrival_eta(
                    nozzle_temp, minimum, self.slope, self.slope_stderr,
                    self.slope_confidence, self.minimum_rate,
                ) if confidently_warming(
                    self.slope, self.slope_stderr, self.slope_confidence,
                    self.boost_min_rate,
                ) else None
                desired = (
                    0.0 if safe_eta is None
                    else boost_for_eta(
                        safe_eta, boost_cap, coast_rate,
                        self.bed_lead_seconds / 60.0,
                    ) * self.boost_fraction
                )

                # `turn` is the boost whose unwind exactly fills the time
                # remaining, less the bed lead. Below it the plate climbs; at it
                # it plateaus, then only falls.
                self.chamber_eta = self._chamber_eta(
                    window, nozzle_temp, minimum
                )
                live_eta = self.chamber_eta
                lead_minutes = self.bed_lead_seconds / 60.0
                supported = (
                    None if live_eta is None
                    else boost_for_eta(
                        live_eta, boost_cap, coast_rate, lead_minutes,
                    ) * self.boost_fraction
                )
                robust_eta = self._turn_eta(eventtime, live_eta)
                turn = (
                    None if robust_eta is None
                    else boost_for_eta(
                        robust_eta, boost_cap, coast_rate, lead_minutes,
                    ) * self.boost_fraction
                )

                if self.boost_policy == "hold":
                    # THE WHOLE POLICY. On the ceiling while the chamber is
                    # still climbing, on the printing target the instant it is
                    # not. No ETA sizing, no climb rate, no coast: the plate is
                    # released rather than walked down, and passive loss up
                    # here is roughly five times the rate it was being walked
                    # at. `turn`, `supported` and `decaying` are computed above
                    # and deliberately unused -- they stay live so the telemetry
                    # still records what the ETA planner WOULD have done, which
                    # is what makes the two comparable on the same print.
                    want = self.bed_carry if chamber_ready else boost_cap
                    if abs(want - current_boost) > 1e-9:
                        rising = want > current_boost
                        current_boost = want
                        self.peak_boost = max(self.peak_boost, current_boost)
                        bed_ready = False
                        coast_reason = 1 if rising else 2
                        self._set_state(
                            preheat_phase=2,
                            boost_bed_target=quantize_target(
                                bed_target + current_boost
                            ),
                            boost_coast_reason=coast_reason,
                        )
                        gcmd.respond_info(
                            "%s bed %s %.1f%s"
                            % (
                                self._stamp(eventtime),
                                "to ceiling" if rising else "released to",
                                bed_target + current_boost,
                                "" if rising
                                else " -- chamber minimum crossed",
                            )
                        )
                    bed_gated = not chamber_ready
                elif turn is None and not eta_seen:
                    # No ETA yet, so ramp blind rather than sitting on the
                    # opening guess. A flat hold there was a soft plateau that
                    # bought nothing; whole-degree steps keep it legible.
                    if eventtime - blind_stepped >= self.blind_step_seconds:
                        blind_stepped = eventtime
                        stepped = min(
                            current_boost + self.blind_step,
                            boost_cap, self.blind_max,
                        )
                        if stepped > current_boost + 1e-9:
                            current_boost = stepped
                            self.peak_boost = max(
                                self.peak_boost, current_boost
                            )
                            bed_gated = True
                            bed_ready = False
                            coast_reason = 1
                            self._set_state(
                                preheat_phase=2,
                                boost_bed_target=quantize_target(
                                    bed_target + current_boost
                                ),
                                boost_coast_reason=1,
                            )
                elif turn is None:
                    decaying = True
                elif not decaying and current_boost >= turn - 1e-9:
                    if at_peak is None:
                        at_peak = eventtime
                    elif eventtime - at_peak >= self.peak_plateau:
                        decaying = True
                elif not decaying:
                    rate = self._climb_rate_near(turn - current_boost)
                    climbed = min(
                        current_boost + rate * interval_minutes,
                        turn, boost_cap,
                    )
                    if climbed > current_boost + 1e-9:
                        at_peak = None
                        if climbed >= announced + self.min_boost_step:
                            announced = climbed
                            gcmd.respond_info(
                                "%s bed %.1f climbing to %.1f at %.0fC/min eta=%s"
                                % (
                                    self._stamp(eventtime),
                                    bed_target + current_boost,
                                    bed_target + turn, rate,
                                    self._num(live_eta, "%.1f"),
                                )
                            )
                        current_boost = climbed
                        self.peak_boost = max(self.peak_boost, current_boost)
                        bed_gated = True
                        bed_ready = False
                        coast_reason = 1
                        self._set_state(
                            preheat_phase=2,
                            boost_bed_target=quantize_target(
                                bed_target + current_boost
                            ),
                            boost_coast_reason=1,
                        )
                if turn is not None:
                    eta_seen = True

                if (self.boost_policy == "eta" and decaying
                        and current_boost > self.bed_carry):
                    # coast_rate is a FLOOR, not a target. Descending slower
                    # than it would leave the plate still ramping when the
                    # print starts, and a plate with a live gradient in it
                    # cups. Arriving early is not waste: it is soak time, and a
                    # settled plate is a flat one.
                    slack = (
                        None if live_eta is None
                        else live_eta - self.bed_lead_seconds / 60.0
                    )
                    if slack is None or slack <= 0.0:
                        # At or past the deadline. The bed is never allowed to
                        # be later than the chamber, so it comes down hard.
                        rate = self.descent_max_rate
                    else:
                        rate = min(
                            self.descent_max_rate,
                            max(coast_rate,
                                (current_boost - self.bed_carry) / slack),
                        )
                    # Pause only when the chamber has actually slowed.
                    grew = (
                        live_eta is not None and previous_eta is not None
                        and live_eta - previous_eta >= self.eta_grow_hold
                    )
                    # Never descend below what the current ETA still supports.
                    # Without this floor the plate ran all the way to the carry
                    # regardless: on 2026-08-26 it reached it with the nozzle at
                    # 37.8 of 41 and seventeen minutes of soak left, throttling
                    # the chamber for no reason.
                    floor_at = max(
                        self.bed_carry, supported or self.bed_carry
                    )
                    stepped = max(
                        floor_at,
                        current_boost - rate * interval_minutes,
                    )
                    stepped = max(self.bed_carry, min(current_boost, stepped))
                    if grew or stepped >= current_boost - 1e-9:
                        self.hold_minutes += interval_minutes
                    else:
                        current_boost = stepped
                        self.fall_minutes += interval_minutes
                if live_eta is not None:
                    previous_eta = live_eta
                command = quantize_target(bed_target + current_boost)
                if command != last_command:
                    self._set_bed(bed, command)
                    last_command = command
                if bed_ready:
                    self.bed_eta = 0.0
                elif self.boost_policy == "hold":
                    # Only meaningful once released; while the plate is held on
                    # the ceiling on purpose there is no descent to estimate.
                    self.bed_eta = (
                        max((current_boost - self.bed_carry), 0.0)
                        / self.bed_release_rate
                        if chamber_ready else None
                    )
                else:
                    self.bed_eta = max(current_boost / coast_rate, 0.0)
                carried = bed_target + self.bed_carry
                if (
                    not bed_ready
                    and current_boost <= self.bed_carry + 1e-9
                    and abs(bed_temp - carried) <= self.bed_tolerance
                ):
                    bed_ready = True
                    self.bed_eta = 0.0
                    self._set_state(
                        bed_ready_eventtime=eventtime, bed_time_remaining=0.0
                    )
                self._set_state(
                    chamber_time_remaining=self.chamber_eta,
                    bed_time_remaining=self.bed_eta,
                )
                if (not chamber_ready and wipe_lead > 0.0
                        and self.chamber_eta is not None
                        and self.chamber_eta * 60.0 <= wipe_lead):
                    # Hand over early so the wipe lands on the minimum instead
                    # of starting from it. The plate goes to its carried target
                    # so the following M190 waits on the right number, and the
                    # chamber reading is stamped so the prediction can be
                    # checked against what actually happened.
                    completed = True
                    self._set_bed(bed, quantize_target(carried))
                    self._set_state(
                        print_ready_eventtime=eventtime,
                        wipe_lead_committed=round(self.chamber_eta * 60.0, 1),
                        wipe_lead_noz=round(nozzle_temp, 2),
                    )
                    gcmd.respond_info(
                        "%s committing %.0fs early on ETA -- wipe should land "
                        "on the minimum. noz=%.1f target>=%.1f, NOT confirmed"
                        % (
                            self._stamp(eventtime),
                            self.chamber_eta * 60.0, nozzle_temp, minimum,
                        )
                    )
                    return
                if chamber_ready and not bed_ready and not bed_wait:
                    # Hand the remaining coast to the M190 that follows. The
                    # plate is commanded to its carried target FIRST, so that
                    # M190 waits on the right number rather than on the boost.
                    completed = True
                    self._set_bed(bed, quantize_target(carried))
                    self._set_state(print_ready_eventtime=eventtime)
                    gcmd.respond_info(
                        "%s chamber met, handing %.1fC of bed coast to M190 "
                        "(eta %s) -- wipe runs now"
                        % (
                            self._stamp(eventtime),
                            max(bed_temp - carried, 0.0),
                            self._num(self.bed_eta, "%.1fmin"),
                        )
                    )
                    return
                if chamber_ready and bed_ready:
                    completed = True
                    self._set_bed(bed, quantize_target(carried))
                    self._set_state(print_ready_eventtime=eventtime)
                    moving = self.hold_minutes + self.fall_minutes
                    gcmd.respond_info(
                        "%s done noz=%.1f bed=%.1f peak=%.1f held=%s"
                        % (
                            self._stamp(eventtime),
                            nozzle_temp, bed_temp,
                            bed_target + self.peak_boost,
                            "--" if moving <= 0 else "%.0f%%" % (
                                100.0 * self.hold_minutes / moving
                            ),
                        )
                    )
                    return
                if eventtime >= next_report:
                    gcmd.respond_info(
                        "%s noz=%.1f eta=%s bed=%.1f>%.1f coast=%s"
                        % (
                            self._stamp(eventtime),
                            nozzle_temp,
                            self._num(self.chamber_eta, "%.1f"),
                            bed_temp,
                            command,
                            self._num(self.bed_eta, "%.1f"),
                        )
                    )
                    next_report = eventtime + self.report_seconds
            raise gcmd.error("Chamber preheat interrupted by Klipper shutdown")
        except Exception:
            # An error aborts the outer M191 before it advances telemetry.
            try:
                self.gcode.run_script_from_command("_CH_PREHEAT_ABORT_STATE")
            except Exception:
                # A Klipper shutdown can reject cleanup G-code. Heater shutdown
                # is handled by the heater object itself.
                pass
            raise
        finally:
            # Any ordinary return, command error or Klipper shutdown stops the
            # stir. A completed soak keeps bed_carry, which is the whole point
            # of it -- the nudge is wanted through the handoff. Anything that
            # did NOT complete gives the plate straight back to its printing
            # target. MCU shutdown independently disables both outputs.
            self._set_stir(fan, 0.0)
            if self.orbit:
                # Back to centre before the print's own start G-code moves.
                self.gcode.run_script_from_command("_CHAMBER_ORBIT_END")
            self._set_bed(
                bed,
                quantize_target(bed_target + self.bed_carry) if completed
                else bed_target,
            )
            self.active = False

    def get_status(self, eventtime):
        return {
            "active": self.active,
            "slope": self.slope,
            "chamber_eta": self.chamber_eta,
            "bed_eta": self.bed_eta,
            "peak_boost": self.peak_boost,
            "bed_target": self.commanded_bed_target,
            "max_boost": self.max_boost,
            "coast_rate": self.coast_rate,
            "initial_boost": self.initial_boost,
            "filter_alpha": self.filter_alpha,
            "stir": self.stir,
            "slope_stderr": self.slope_stderr,
            "hold_minutes": self.hold_minutes,
            "fall_minutes": self.fall_minutes,
            "proxy_validity": (
                None if self.proxy_gate is None else self.proxy_gate.state
            ),
            "proxy_valid_dwell": (
                0.0 if self.proxy_gate is None else self.proxy_gate.dwell
            ),
        }


def load_config(config):
    return ChamberPreheat(config)
