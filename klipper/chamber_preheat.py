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


def boost_for_eta(eta, max_boost, coast_rate):
    """Map filtered chamber minutes remaining directly to bed boost."""
    return min(max_boost, max(0.0, eta) * coast_rate)


def eta_gated_coast(current_boost, desired_boost, elapsed_minutes, coast_rate):
    """Follow a falling ETA without re-boosting or exceeding the coast rate."""
    slew_floor = max(0.0, current_boost - elapsed_minutes * coast_rate)
    return min(current_boost, max(desired_boost, slew_floor))


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

    def update(self, slope, elapsed_seconds):
        """Advance on a regressed slope; None means the window is not full."""
        if self.valid or slope is None:
            return self.valid
        if slope < self.ready_slope:
            # Still shedding its own heat, so it is reading itself.
            self.cooling_seen = True
            self.dwell = 0.0
            self.state = self.COOLING
            return False
        # At or above zero within the ready band: the settling clock runs.
        self.dwell += elapsed_seconds
        self.state = self.SETTLING
        if self.dwell >= self.dwell_seconds:
            self.valid = True
            self.state = self.VALID
        return self.valid

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
            "max_bed_boost", 10.0, minval=0.0
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
            "boost_fraction", 0.5, above=0.0, maxval=1.0
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
        heater.set_temp(target)
        self.commanded_bed_target = target

    def _temperatures(self, nozzle, bed, eventtime):
        nozzle_temp, unused_nozzle_target = nozzle.get_temp(eventtime)
        bed_temp, unused_bed_target = bed.get_temp(eventtime)
        return nozzle_temp, bed_temp

    def _pause(self, eventtime, seconds):
        return self.reactor.pause(eventtime + seconds)

    @staticmethod
    def _set_stir(fan, speed):
        # This asynchronous path bypasses G-code lookahead.  A queued
        # SET_FAN_SPEED does not flush during a blocking temperature wait.
        fan.set_speed(speed)

    def _new_proxy_gate(self):
        return ProxyGate(self.ready_slope, self.settle_dwell)

    def _update_proxy(self, gate, slope, stderr, elapsed_seconds):
        self.slope = slope
        self.slope_stderr = stderr
        gate.update(slope, elapsed_seconds)
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
        maximum_target = bed.max_temp - self.max_temp_margin
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
        gate = self.proxy_gate = self._new_proxy_gate()
        window = SlopeWindow(self.slope_window)
        window.push(start_time, start_temp)
        try:
            self._set_stir(fan, stir)
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
                    "M191 compat noz=%.1f floor=%.1f" % (start_temp, minimum)
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
            # The bed sits at its printing target throughout qualification.
            # Nothing is boosted until the nozzle has earned the right to
            # schedule it.
            self._set_bed(bed, bed_target)
            self._set_state(
                preheat_phase=1,
                boost_bed_target=None,
                boost_coast_eventtime=None,
                boost_coast_reason=0,
                chamber_time_remaining=None,
                bed_time_remaining=None,
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
                    gate, slope, stderr, eventtime - previous_time
                )
                previous_time = eventtime
                self._set_stir(fan, stir)
                if not gate.valid and eventtime >= deadline:
                    gate.force()
                    self._set_state(
                        proxy_valid=1, proxy_validity=gate.state
                    )
                    gcmd.respond_info(
                        "M191 unqualified after %.0fs, proceeding"
                        % self.qualify_timeout
                    )
                    break
                if eventtime >= next_report:
                    gcmd.respond_info(
                        "M191 settle noz=%.1f slope=%s dwell=%.0f/%.0f"
                        % (
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
            planned_boost = 0.0
            if boost_requested and boost_cap > 0.0 and not chamber_ready:
                safe_eta = early_arrival_eta(
                    nozzle_temp, minimum, self.slope, self.slope_stderr,
                    self.slope_confidence, self.minimum_rate,
                )
                if safe_eta is None:
                    coast_reason = 2
                else:
                    planned_boost = min(
                        initial_boost,
                        boost_for_eta(safe_eta, boost_cap, coast_rate)
                        * self.boost_fraction,
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
            gcmd.respond_info(
                "M191 ready noz=%.1f slope=%s+-%s eta=%s boost=%.1f"
                % (
                    nozzle_temp,
                    self._num(self.slope, "%+.2f"),
                    self._num(self.slope_stderr, "%.2f"),
                    self._num(self.chamber_eta, "%.1f"),
                    planned_boost,
                )
            )

            current_boost = planned_boost
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

                # The boost only ever decays from here. Re-boosting mid-soak
                # would need fresh unwind headroom that a falling ETA does not
                # have.
                if current_boost > 0.0:
                    safe_eta = 0.0 if chamber_ready else early_arrival_eta(
                        nozzle_temp, minimum, self.slope, self.slope_stderr,
                        self.slope_confidence, self.minimum_rate,
                    )
                    desired = (
                        0.0 if safe_eta is None
                        else boost_for_eta(safe_eta, boost_cap, coast_rate)
                    )
                    current_boost = eta_gated_coast(
                        current_boost, desired, interval_minutes, coast_rate
                    )
                command = quantize_target(bed_target + current_boost)
                if command != last_command:
                    self._set_bed(bed, command)
                    last_command = command
                self.bed_eta = (
                    0.0 if bed_ready else max(current_boost / coast_rate, 0.0)
                )
                if (
                    not bed_ready
                    and current_boost <= 0.0
                    and abs(bed_temp - bed_target) <= self.bed_tolerance
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
                if chamber_ready and bed_ready:
                    self._set_bed(bed, bed_target)
                    self._set_state(print_ready_eventtime=eventtime)
                    gcmd.respond_info(
                        "M191 done noz=%.1f bed=%.1f target=%.1f"
                        % (nozzle_temp, bed_temp, bed_target)
                    )
                    return
                if eventtime >= next_report:
                    gcmd.respond_info(
                        "M191 noz=%.1f eta=%s bed=%.1f>%.1f coast=%s"
                        % (
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
            # stir and restores the real printing target. MCU shutdown
            # independently disables both outputs.
            self._set_stir(fan, 0.0)
            self._set_bed(bed, bed_target)
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
            "proxy_validity": (
                None if self.proxy_gate is None else self.proxy_gate.state
            ),
            "proxy_valid_dwell": (
                0.0 if self.proxy_gate is None else self.proxy_gate.dwell
            ),
        }


def load_config(config):
    return ChamberPreheat(config)
