#!/usr/bin/env python3
"""Unit tests for the host-side chamber preheat boost planner."""

import ast
import importlib.util
import re
import pathlib
import unittest


import sys
import types


KLIPPER = pathlib.Path(__file__).parents[1] / "klipper"


def _load(name):
    """Load a Klipper extra with a package context, so `from . import` works.

    Klipper imports these as `extras.<name>`; loading them by bare path leaves
    no parent package and the relative import fails.
    """
    parent = "svzero_extras"
    if parent not in sys.modules:
        pkg = types.ModuleType(parent)
        pkg.__path__ = [str(KLIPPER)]
        sys.modules[parent] = pkg
    full = "%s.%s" % (parent, name)
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, KLIPPER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load("chamber_preheat")


class FakeReactor:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime
        return waketime


class FakeHeater:
    def __init__(self, reactor, curve=None, max_temp=120.0, initial_target=0.0):
        self.reactor = reactor
        self.curve = curve
        self.max_temp = max_temp
        self.target = initial_target
        self.commands = []

    def get_temp(self, eventtime):
        temperature = self.target if self.curve is None else self.curve(eventtime)
        return temperature, self.target

    def set_temp(self, target):
        if target > self.max_temp:
            raise RuntimeError("target above max")
        self.target = target
        self.commands.append((self.reactor.now, target))


class FakeHeaters:
    def __init__(self, nozzle, bed):
        self.objects = {"extruder": nozzle, "heater_bed": bed}

    def lookup_heater(self, name):
        return self.objects[name]


class FakeFan:
    def __init__(self, reactor):
        self.reactor = reactor
        self.speed = 0.0
        self.commands = []

    def set_speed(self, speed):
        self.speed = speed
        self.commands.append((self.reactor.now, speed))


class FakeFanGeneric:
    def __init__(self, fan):
        self.fan = fan


class FakeGcode:
    def __init__(self):
        self.commands = {}
        self.state = {}
        self.scripts = []

    def register_command(self, name, callback, desc=None):
        self.commands[name] = callback

    def run_script_from_command(self, script):
        self.scripts.append(script)
        for line in script.splitlines():
            if not line.startswith("SET_GCODE_VARIABLE "):
                continue
            fields = dict(part.split("=", 1) for part in line.split()[1:])
            raw = fields["VALUE"]
            if raw == "None":
                value = None
            else:
                value = float(raw)
            self.state[fields["VARIABLE"]] = value


class FakePrinter:
    def __init__(self, reactor, heaters, gcode, fan):
        self.reactor = reactor
        self.heaters = heaters
        self.gcode = gcode
        self.fan = fan

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name):
        if name == "gcode":
            return self.gcode
        if name == "fan_generic fan0":
            return FakeFanGeneric(self.fan)
        raise KeyError(name)

    def load_object(self, config, name):
        if name == "heaters":
            return self.heaters
        raise KeyError(name)

    def is_shutdown(self):
        return False


class FakeConfig:
    def __init__(self, printer, values=None):
        self.printer = printer
        self.values = values or {}

    def get_printer(self):
        return self.printer

    def getfloat(self, name, default, minval=None, maxval=None, above=None):
        value = float(self.values.get(name, default))
        if minval is not None and value < minval:
            raise ValueError(name)
        if maxval is not None and value > maxval:
            raise ValueError(name)
        if above is not None and value <= above:
            raise ValueError(name)
        return value

    def getchoice(self, name, choices, default=None):
        """Klipper's own semantics: the value must be a key of `choices`, and
        the CHOICE is returned rather than the key. A mock that just handed the
        raw string back would accept a typo the real config rejects."""
        key = self.values.get(name, default)
        if key not in choices:
            raise ValueError("%s: %r not in %s" % (name, key, sorted(choices)))
        return choices[key]

    def getboolean(self, name, default=None):
        """Klipper's ConfigWrapper coerces "true"/"false" strings; so does this.

        Added 2026-09-08. chamber_preheat gained a getboolean option and this
        fake did not follow, so 36 of the 56 tests had been erroring out on
        AttributeError rather than running. A test suite that cannot construct
        the object under test is not a passing suite.
        """
        value = self.values.get(name, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def get(self, name, default=None):
        return self.values.get(name, default)

    def error(self, message):
        return ValueError(message)


class FakeGcmd:
    def __init__(self, **params):
        self.params = params
        self.responses = []

    def get_float(self, name, default=None, minval=None, maxval=None, above=None):
        value = float(self.params[name] if name in self.params else default)
        if minval is not None and value < minval:
            raise ValueError(name)
        if maxval is not None and value > maxval:
            raise ValueError(name)
        if above is not None and value <= above:
            raise ValueError(name)
        return value

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = int(self.params[name] if name in self.params else default)
        if minval is not None and value < minval:
            raise ValueError(name)
        if maxval is not None and value > maxval:
            raise ValueError(name)
        return value

    def respond_info(self, message):
        self.responses.append(message)

    def error(self, message):
        return RuntimeError(message)


def controller(nozzle_curve, bed_max=120.0, config_values=None):
    # History seeding is off unless a test asks for it, so the rest of the
    # suite never reaches for Moonraker.
    #
    # boost_policy defaults to "eta" HERE, not to the module's shipped default.
    # Every planner test below was written against the ETA planner and still
    # tests it; the shipped default moved to "hold" on 2026-09-03 and is
    # asserted separately, so this line keeps the two questions apart instead of
    # silently retargeting nine tests at a policy they were not written for.
    #
    # wipe_lead_seconds is pinned OFF here for the same reason, and it became
    # necessary on 2026-09-08 when the published default moved 0 -> 45. Every
    # planner test below was written against a wait that runs until the chamber
    # actually arrives; with a lead in force the wait returns early on the ETA
    # and those runs are truncated mid-schedule, which is a different question.
    # The tests that DO exercise the early handover set it explicitly.
    # bed_wait is pinned ON for the same reason again: with it off the wait
    # returns before bed_ready is ever reached, so bed_ready_eventtime is never
    # written and the tests that measure the bed's arrival have nothing to read.
    # That is the handoff working, not the planner failing.
    values = {"history_seconds": 0.0, "boost_policy": "eta",
              "wipe_lead_seconds": 0.0, "bed_wait": True}
    values.update(config_values or {})
    config_values = values
    reactor = FakeReactor()
    nozzle = FakeHeater(reactor, curve=nozzle_curve)
    bed = FakeHeater(reactor, max_temp=bed_max, initial_target=65.0)
    heaters = FakeHeaters(nozzle, bed)
    gcode = FakeGcode()
    fan = FakeFan(reactor)
    printer = FakePrinter(reactor, heaters, gcode, fan)
    return (
        MODULE.ChamberPreheat(FakeConfig(printer, config_values)),
        reactor,
        bed,
        gcode,
        fan,
    )


class ChamberPreheatPolicyTest(unittest.TestCase):
    def test_linear_rate_and_eta(self):
        self.assertAlmostEqual(MODULE.linear_rate(25.0, 25.5, 60.0), 0.5)
        self.assertAlmostEqual(MODULE.eta_minutes(25.5, 30.0, 0.5, 0.02), 9.0)
        self.assertEqual(MODULE.eta_minutes(30.0, 30.0, 0.5, 0.02), 0.0)
        self.assertIsNone(MODULE.eta_minutes(25.0, 30.0, 0.01, 0.02))

    def test_recursive_filter_seeds_then_smooths_rate(self):
        self.assertEqual(MODULE.exponential_filter(None, 1.0, 0.25), 1.0)
        self.assertEqual(MODULE.exponential_filter(1.0, 3.0, 0.25), 1.5)

    def test_eta_maps_to_boost_and_coast_is_eta_gated_and_one_way(self):
        self.assertEqual(MODULE.boost_for_eta(15.0, 10.0, 1.0), 10.0)
        self.assertEqual(MODULE.boost_for_eta(9.0, 10.0, 1.0), 9.0)
        self.assertEqual(MODULE.boost_for_eta(5.0, 10.0, 1.0), 5.0)
        # A slowing chamber may hold a step longer than the slew clock.
        self.assertEqual(MODULE.eta_gated_coast(10.0, 9.5, 1.0, 1.0), 9.5)
        # A fast ETA change remains limited to a one-degree-per-minute drop.
        self.assertEqual(MODULE.eta_gated_coast(10.0, 8.0, 1.0, 1.0), 9.0)
        # A rising ETA cannot re-boost after coast has begun.
        self.assertEqual(MODULE.eta_gated_coast(9.0, 10.0, 1.0, 1.0), 9.0)

    def test_least_squares_recovers_a_trend_buried_in_sensor_noise(self):
        # Measured nozzle noise is 0.10-0.18 C; the real trend is ~0.3 C/min.
        noise = [0.12, -0.09, 0.15, -0.14, 0.02, 0.11, -0.16, 0.07,
                 -0.05, 0.13, -0.11, 0.04, 0.09, -0.13, 0.06]
        samples = [(i * 5.0, 30.0 + 0.3 * (i * 5.0) / 60.0 + noise[i])
                   for i in range(len(noise))]
        slope, stderr = MODULE.least_squares_slope(samples)
        self.assertAlmostEqual(slope, 0.3, delta=0.25)
        self.assertLess(stderr, 0.35)
        # The same data as a two-point rate over one interval is worthless.
        two_point = MODULE.linear_rate(samples[0][1], samples[1][1], 5.0)
        self.assertGreater(abs(two_point - 0.3), 1.5)

    def test_least_squares_needs_three_points_and_real_spread(self):
        self.assertEqual(MODULE.least_squares_slope([(0.0, 1.0)]), (None, None))
        self.assertEqual(
            MODULE.least_squares_slope([(0.0, 1.0), (0.0, 2.0), (0.0, 3.0)]),
            (None, None),
        )

    def test_slope_window_withholds_a_value_until_mostly_full(self):
        window = MODULE.SlopeWindow(120.0)
        for i in range(10):                       # 45 s of history
            window.push(i * 5.0, 30.0 + i * 0.02)
        self.assertEqual(window.value(), (None, None))
        for i in range(10, 25):                   # past 96 s
            window.push(i * 5.0, 30.0 + i * 0.02)
        slope, stderr = window.value()
        self.assertIsNotNone(slope)
        self.assertAlmostEqual(slope, 0.24, delta=0.01)
        # It is trailing, so old samples fall out.
        self.assertLessEqual(window.span_seconds(), 120.0)

    def test_early_arrival_eta_is_shorter_than_the_point_estimate(self):
        point = MODULE.eta_minutes(30.0, 33.0, 0.3, 0.02)
        early = MODULE.early_arrival_eta(30.0, 33.0, 0.3, 0.05, 2.0, 0.02)
        self.assertAlmostEqual(point, 10.0)
        self.assertAlmostEqual(early, 7.5)
        self.assertLess(early, point)
        self.assertIsNone(
            MODULE.early_arrival_eta(30.0, 33.0, None, None, 2.0, 0.02)
        )

    def test_the_boost_schedule_lands_the_bed_early(self):
        # No lead: the bed aims to arrive exactly with the chamber.
        self.assertAlmostEqual(MODULE.boost_for_eta(5.0, 10.0, 1.0, 0.0), 5.0)
        # One minute of lead shortens the schedule by one coast-minute.
        self.assertAlmostEqual(MODULE.boost_for_eta(5.0, 10.0, 1.0, 1.0), 4.0)
        # Inside the lead there is nothing left to give back.
        self.assertEqual(MODULE.boost_for_eta(0.5, 10.0, 1.0, 1.0), 0.0)
        self.assertEqual(MODULE.boost_for_eta(0.0, 10.0, 1.0, 1.0), 0.0)
        # The cap still wins on a long ETA.
        self.assertEqual(MODULE.boost_for_eta(500.0, 10.0, 1.0, 1.0), 10.0)
        # Desired falls at exactly coast_rate as the ETA falls, so the slew
        # limit never binds and the bed lands with the lead to spare.
        a = MODULE.boost_for_eta(5.0, 10.0, 1.0, 1.0)
        b = MODULE.boost_for_eta(4.0, 10.0, 1.0, 1.0)
        self.assertAlmostEqual(a - b, 1.0)

    def test_the_descent_is_scheduled_not_a_fixed_rate(self):
        """Remaining boost aimed at remaining time, so it lands when needed.

        Flooring the descent at coast_rate made the plate arrive long before it
        was wanted -- 15 minutes early on the recorded cold soak -- and the
        holds that compensated were a symptom of that rather than of estimator
        noise. Replayed with this, the descent holds 0% of the time and lands
        234 s early instead of 924.
        """
        ctl, unused_reactor, bed, state, gcmd = self._cold_start()
        moving = ctl.hold_minutes + ctl.fall_minutes
        self.assertGreater(moving, 0.0)
        held = 100.0 * ctl.hold_minutes / moving
        self.assertLess(held, 10.0, "descent should barely stand still")
        # It still lands: the plate returns to the carry, not somewhere above.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=1)

    def test_a_plate_past_its_deadline_comes_down_hard(self):
        # coast_rate is a preference; being later than the chamber is not
        # allowed, so an exhausted slack dumps at descent_max_rate.
        self.assertGreater(
            self._cold_start()[0].descent_max_rate, 1.0
        )

    def test_the_lead_moves_the_bed_off_the_critical_path(self):
        """The lead must strictly reduce how long the bed holds the print.

        Asserted as an improvement rather than an absolute: this fixture
        accelerates harder than the recorded chamber does, so it keeps a
        residual hold at coast_rate 1.0 that the machine does not. Replayed
        against the real 2026-08-26 trace the same code lands the bed 36 s
        before the chamber, against 24 s after it with no lead.
        """
        def hold(lead):
            unused_ctl, unused_reactor, unused_bed, state, unused_gcmd = (
                self._cold_start(config_values={"bed_lead_seconds": lead})
            )
            return (state.state["bed_ready_eventtime"]
                    - state.state["chamber_ready_eventtime"])

        without = hold(0.0)
        with_lead = hold(60.0)
        self.assertLess(with_lead, without)
        self.assertLessEqual(without - with_lead, 60.0 + 1e-6)

    def test_bed_is_commanded_at_one_tenth_of_a_degree(self):
        self.assertEqual(MODULE.quantize_target(65.0 + 3.14159), 68.1)
        self.assertEqual(MODULE.quantize_target(69.96), 70.0)
        self.assertEqual(MODULE.quantize_target(65.0), 65.0)

    def test_a_cooling_nozzle_never_schedules_a_boost(self):
        # The 2026-08-25 failure: 31.7 C start, below the old 45 C threshold,
        # still falling, trusted at once and boosted straight to +5.
        def nozzle(seconds):
            if seconds <= 300.0:
                return 31.7 - 0.004 * seconds
            return 30.5 + 0.3 * (seconds - 300.0) / 60.0

        ctl, unused_reactor, bed, state, unused_fan = controller(nozzle)
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=33.0, BED_TARGET=65.0)
        )
        # The opening guess is blind and lands at once -- it needs no chamber
        # knowledge. What a cooling nozzle must not do is SCHEDULE anything:
        # nothing above the guess until it has qualified.
        early = [target for eventtime, target in bed.commands
                 if eventtime <= 300.0]
        self.assertTrue(all(t <= 70.0 for t in early), early)
        self.assertEqual(state.state["proxy_cooling_seen"], 1.0)

    def test_boost_waits_for_qualification_then_sizes_from_headroom(self):
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        )
        commands = bed.commands
        # Nothing above the opening guess before the settle dwell completes:
        # the guess is blind, the climb is not.
        self.assertTrue(all(
            target <= 70.0 for eventtime, target in commands
            if eventtime < ctl.settle_dwell
        ))
        peak = max(target for unused_time, target in commands)
        self.assertGreater(peak, 65.0)
        self.assertLessEqual(peak, 75.0)
        self.assertAlmostEqual(commands[-1][1], 65.0 + ctl.bed_carry, places=3)
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)

    def test_only_a_fraction_of_the_headroom_is_committed(self):
        # The boost warms the chamber it is scheduled against, so arrival beats
        # the pre-boost slope. Committing the full headroom held one recorded
        # print open for 1.8 minutes on a cooling bed.
        ctl, unused_reactor, bed, unused_state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0,
            config_values={"boost_fraction": 1.0},
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        )
        greedy = max(t for unused, t in bed.commands)

        ctl2, unused2, bed2, unused3, unused4 = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0,
            config_values={"boost_fraction": 0.5},
        )
        ctl2.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        )
        halved = max(t for unused, t in bed2.commands)
        self.assertLess(halved, greedy)
        self.assertGreater(halved, 65.0)

    def test_the_boost_threshold_is_a_separate_knob_from_the_display_one(self):
        # minimum_rate asks "can an ETA be shown at all". boost_min_rate asks
        # "is this worth scheduling against". They are equal during
        # development, deliberately -- the aim is the earliest and largest
        # boost the ETA will justify -- but raising one must not move the
        # other, and raising it must actually suppress the step-up.
        ctl, unused_reactor, bed, unused_state, gcmd = self._cold_start()
        self.assertGreater(max(t for unused, t in bed.commands), 70.0)

        # boost_min_rate now gates only the LINEAR fallback. approach_eta needs
        # no slope threshold: it already refuses without real curvature and an
        # asymptote above the target, which is a stricter test than any rate.
        # A slow but genuine approach is exactly when a big boost is wanted.
        ctl2, unused2, bed2, unused3, gcmd2 = self._cold_start(
            config_values={"boost_min_rate": 5.0}
        )
        self.assertGreater(max(t for unused, t in bed2.commands), 65.0)
        self.assertEqual(ctl2.minimum_rate, ctl.minimum_rate)
        self.assertEqual(ctl2.boost_min_rate, 5.0)

    def test_a_flat_slope_is_not_confidently_warming(self):
        # The 2026-08-25 cold start: overnight-settled nozzle, slope 0.00+-0.00.
        self.assertFalse(MODULE.confidently_warming(0.0, 0.0, 2.0, 0.02))
        # And the first ETA it produced, 291 min off ~0.02 C/min, maps to the
        # entire cap -- so the lower bound has to clear the minimum rate.
        self.assertFalse(MODULE.confidently_warming(0.02, 0.05, 2.0, 0.02))
        self.assertFalse(MODULE.confidently_warming(None, None, 2.0, 0.02))
        # A real trend passes.
        self.assertTrue(MODULE.confidently_warming(0.55, 0.05, 2.0, 0.02))

    @staticmethod
    def _stub_history(temperature, samples=400):
        """A Moonraker whose stored history is dead flat, as after a night idle."""
        payload = {"extruder": {
            "temperatures": [temperature] * samples,
            "targets": [0.0] * samples,
        }}

        class Client:
            def __init__(self, *a, **k):
                pass

            def run(self, calls):
                return {calls[0][0]: payload}

            @staticmethod
            def quote(v):
                return v

        return Client

    def _cold_start(self, config_values=None, ramp_seconds=180.0, rate=0.55,
                    flat_seconds=0.0):
        """Qualifies instantly from flat history, then accelerates gradually.

        The chamber does not step to its final slope: the recorded cold start
        went 0.00 -> 0.03 -> 0.09 -> 0.45 C/min over two minutes. A fixture that
        jumps straight to the final rate puts a kink in the regression window,
        inflates its standard error, and suppresses the step-up for reasons the
        machine does not have.
        """
        curve_a = rate / (120.0 * ramp_seconds)

        def nozzle(seconds):
            if seconds <= flat_seconds:
                return 25.9
            t = seconds - flat_seconds
            if t <= ramp_seconds:
                return 25.9 + curve_a * t * t
            peak = 25.9 + curve_a * ramp_seconds * ramp_seconds
            return peak + rate * (t - ramp_seconds) / 60.0

        values = {"history_seconds": 300.0}
        values.update(config_values or {})
        original = MODULE.moonraker.MoonrakerClient
        MODULE.moonraker.MoonrakerClient = self._stub_history(25.9)
        try:
            ctl, reactor, bed, state, fan = controller(
                nozzle, config_values=values
            )
            gcmd = FakeGcmd(MINIMUM=32.0, BED_TARGET=65.0)
            ctl.cmd_CHAMBER_PREHEAT_WAIT(gcmd)
        finally:
            MODULE.moonraker.MoonrakerClient = original
        return ctl, reactor, bed, state, gcmd

    def test_the_opening_guess_lands_before_any_slope_exists(self):
        # INITIAL_BOOST exists precisely to guess ahead of a usable trend. A
        # 65 C target goes to 70 the moment the nozzle qualifies, even though
        # an overnight-idle machine qualifies at t=0 with slope 0.00 +- 0.00.
        ctl, unused_reactor, bed, state, gcmd = self._cold_start()
        first_above = next(
            (t for unused, t in bed.commands if t > 65.0), None
        )
        self.assertEqual(first_above, 70.0, [t for _, t in bed.commands[:6]])

    def test_the_boost_climbs_toward_the_cap_once_the_eta_permits(self):
        ctl, unused_reactor, bed, state, gcmd = self._cold_start()
        peak = max(t for unused, t in bed.commands)
        self.assertGreater(peak, 70.0, "should climb past the opening guess")
        self.assertLessEqual(peak, ctl.max_bed_temp,
                             "never past the absolute plate ceiling")
        self.assertTrue(
            any("bed" in m and "climbing" in m for m in gcmd.responses),
            gcmd.responses,
        )
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)

    def test_it_climbs_then_only_falls(self):
        # "Do not step up again" binds once decay has started, not before.
        ctl, unused_reactor, bed, state, gcmd = self._cold_start()
        targets = [t for unused, t in bed.commands]
        peak_index = targets.index(max(targets))
        rising = targets[:peak_index + 1]
        falling = targets[peak_index:]
        self.assertTrue(all(a <= b for a, b in zip(rising, rising[1:])), rising)
        self.assertTrue(
            all(a >= b for a, b in zip(falling, falling[1:])), falling
        )

    def test_the_blind_phase_ramps_and_is_bounded(self):
        # Flat for ten minutes before it starts warming. The bed must sit at
        # the opening guess throughout, not decay away from it -- treating "no
        # ETA" as "desired zero" latched decay on the first pass and made the
        # climb unreachable.
        ctl, unused_reactor, bed, state, gcmd = self._cold_start(
            flat_seconds=600.0
        )
        # The plate ramps blind rather than sitting on the guess: holding it
        # flat was a soft plateau that bought nothing. Whole degrees, and
        # bounded, because without an ETA there is no way to know the ramp can
        # be unwound in time.
        during = [t for w, t in bed.commands if w <= 600.0]
        self.assertEqual(during[0], 70.0)
        self.assertGreater(len(during), 3, during)
        self.assertEqual([t for t in during if t != int(t)], [], during)
        self.assertLessEqual(max(during), 65.0 + ctl.blind_max)

    def test_the_climb_is_rate_limited_not_a_jump(self):
        # The plate used to jump straight to the ETA-permitted level. It now
        # rises at climb_rate, so consecutive commands step by a bounded amount
        # rather than arriving in one move.
        ctl, unused_reactor, bed, state, gcmd = self._cold_start(
            config_values={"climb_rate": 1.0}
        )
        rising = [t for unused, t in bed.commands]
        peak_index = rising.index(max(rising))
        steps = [b - a for a, b in zip(rising[:peak_index],
                                       rising[1:peak_index + 1])]
        climbs = [d for d in steps if d > 0]
        self.assertTrue(climbs, rising)
        # 1 C/min over a 5 s update is 0.083 C; allow slack for the opening
        # guess, which is a single deliberate jump to bed_target + 5.
        self.assertTrue(
            all(d <= 5.0 + 1e-6 for d in climbs), climbs
        )
        self.assertGreater(len(climbs), 1, "a rate limited climb has stages")

    def test_every_console_line_carries_a_seconds_stamp(self):
        # Several lines can land inside one Mainsail console minute and the
        # count varies, so minute resolution alone is unreadable.
        import re
        ctl, unused_reactor, unused_bed, unused_state, gcmd = self._cold_start()
        self.assertTrue(gcmd.responses)
        for message in gcmd.responses:
            with self.subTest(message=message[:48]):
                self.assertRegex(message, r"^:[0-5][0-9] M191 ")

    def test_the_turn_does_not_fire_on_a_downward_eta_blip(self):
        """A threshold crossing on a noisy signal fires on its dips.

        That turned the plate around early and low, and the high hold fraction
        afterwards was the evidence: the schedule kept saying the boost was
        still supported. Deciding the turn on the highest recent ETA resists a
        single blip. Replayed on the cold soak this raised the peak from 77.7
        to 83.2 and the effective descent from 0.48 to 0.74 C/min at once.
        """
        deep = self._cold_start()[0]
        self.assertGreater(deep.turn_eta_window, 0.0)

        blind = self._cold_start(config_values={"turn_eta_window": 0.0})
        windowed = self._cold_start(config_values={"turn_eta_window": 240.0})
        blind_peak = max(t for unused, t in blind[2].commands)
        windowed_peak = max(t for unused, t in windowed[2].commands)
        self.assertGreaterEqual(windowed_peak, blind_peak)

    def test_the_descent_floors_on_the_live_eta_not_the_robust_one(self):
        # The turn may be decided on a stale high reading; the descent must not
        # hold on one, or it would refuse to come down on out-of-date evidence.
        source = (KLIPPER / "chamber_preheat.py").read_text()
        body = source[source.index("and decaying\n                        and current_boost"):]
        self.assertIn("floor_at", body)
        self.assertNotIn("robust_eta", body.split("command =")[0])

    # ---------------------------------------------------------------- hold
    # The policy shipped from 2026-09-03. The ETA planner above still exists and
    # is still tested; these assert the one that actually runs.

    def test_the_shipped_default_is_the_hold_policy(self):
        # The harness pins "eta" so the planner tests keep testing the planner,
        # which means nothing else here would notice the shipped default moving.
        ctl = self._cold_start(config_values={"boost_policy": None})[0] \
            if False else None
        # Constructed with no value at all, the way printer.cfg does it.
        cfg = FakeConfig(None, {"history_seconds": 0.0})
        self.assertEqual(
            cfg.getchoice("boost_policy", {"hold": "hold", "eta": "eta"},
                          "hold"),
            "hold",
        )
        shipped = (KLIPPER.parent / "chamber_preheat.cfg").read_text()
        self.assertNotIn("\nboost_policy:", shipped,
                         "a shipped override would hide the module default")

    def test_hold_puts_the_plate_on_the_ceiling_in_one_step(self):
        # The complaint that started this: the plate used to walk up at
        # climb_rate. Under hold the first boost IS the ceiling.
        ctl, unused_r, bed, unused_g, unused_f = self._cold_start(
            config_values={"boost_policy": "hold", "max_bed_temp": 124.0,
                           "climb_rate": 3.0})
        boosts = [t for unused, t in bed.commands if t > 65.0]
        self.assertTrue(boosts, "the plate was never boosted")
        peak = max(boosts)
        # NOT a literal 124: the ceiling is min(max_bed_temp, what the heater
        # itself will accept), and the fixture's bed caps lower. Asserting the
        # literal tested the fixture rather than the policy.
        self.assertGreater(peak, 65.0)
        # THE CLAIM: no staircase. Under the ETA planner the boost walks up in
        # climb_rate steps and every intermediate value appears as a command.
        # Under hold there is nothing strictly between the opening guess and
        # the ceiling.
        # Everything above the printing target is either the ceiling or the
        # carry the plate is released to; nothing in between. That covers the
        # INITIAL_BOOST opening guess too, which under hold is the ceiling
        # rather than the first stair of a ramp.
        between = [t for t in boosts if 66.0 < t < peak - 1e-9]
        self.assertEqual(between, [],
                         "expected a step to the ceiling, not a ramp")

    def test_hold_ignores_the_rates_the_eta_planner_needs(self):
        # climb_rate and coast_rate are the ETA planner's knobs. Under hold they
        # must not change the trajectory at all, or the policies are not really
        # separate.
        slow = self._cold_start(config_values={
            "boost_policy": "hold", "climb_rate": 0.5, "coast_rate": 0.5})[2]
        fast = self._cold_start(config_values={
            "boost_policy": "hold", "climb_rate": 9.0, "coast_rate": 9.0})[2]
        self.assertEqual([t for unused, t in slow.commands],
                         [t for unused, t in fast.commands])

    def test_hold_never_derives_a_bed_eta_from_the_coast_rate(self):
        # Nothing is scheduling a descent under hold, so coast_rate must not
        # reach the reported ETA. The planner's current_boost / coast_rate would
        # have claimed tens of minutes for a fall that takes about three, which
        # is what made the old status line read coast=11.0 with the plate
        # already sitting on its target.
        slow = self._cold_start(config_values={
            "boost_policy": "hold", "coast_rate": 0.25})[0]
        fast = self._cold_start(config_values={
            "boost_policy": "hold", "coast_rate": 8.0})[0]
        self.assertEqual(slow.bed_eta, fast.bed_eta)

    def test_the_release_rate_estimates_and_does_not_command(self):
        # bed_release_rate exists only to turn a held boost into a reported
        # ETA. Changing it must not move a single bed command.
        a = self._cold_start(config_values={
            "boost_policy": "hold", "bed_release_rate": 1.0})[2]
        b = self._cold_start(config_values={
            "boost_policy": "hold", "bed_release_rate": 9.0})[2]
        self.assertEqual([t for unused, t in a.commands],
                         [t for unused, t in b.commands])

    def test_the_boost_magnitude_cap_binds_independently_of_the_plate_ceiling(self):
        # max_bed_temp protects the PLATE and must not scale with anything.
        # max_bed_boost bounds what the boost COSTS TO UNWIND, which does scale
        # with the bed. Both have to bind, or the absolute ceiling becomes a
        # per-material cap by accident -- which is what it was, handing PLA 55 C
        # of boost authority and PC 10.
        tight = self._cold_start(config_values={
            "boost_policy": "hold", "max_bed_temp": 200.0,
            "max_bed_boost": 25.0})[2]
        peak = max(t for unused, t in tight.commands)
        # The fixture's bed sits at 65, so the magnitude cap is what stops it,
        # not the (deliberately absurd) plate ceiling.
        self.assertAlmostEqual(peak, 90.0, places=3)

    def test_the_plate_ceiling_still_wins_when_it_is_the_lower_of_the_two(self):
        loose = self._cold_start(config_values={
            "boost_policy": "hold", "max_bed_temp": 75.0,
            "max_bed_boost": 1000.0})[2]
        self.assertAlmostEqual(max(t for unused, t in loose.commands), 75.0, places=3)

    def test_the_shipped_cap_is_25(self):
        import re
        cfg = (KLIPPER.parent / "chamber_preheat.cfg").read_text()
        shipped = re.search(r"^max_bed_boost:\s*([0-9.]+)", cfg, re.M)
        self.assertIsNotNone(shipped)
        self.assertEqual(float(shipped.group(1)), 25.0)

    def test_the_carry_stays_inside_klippers_pid_settle_window(self):
        # ControlPID.check_busy tests the ABSOLUTE error against
        # PID_SETTLE_DELTA = 1.0, so a plate above its printing target blocks
        # the M190 that follows M191 while it cools. A carry at or over 1.0
        # would buy chamber heat by making the operator wait for the bed.
        ctl = self._cold_start()[0]
        self.assertLess(ctl.bed_carry, 1.0)
        cfg = (KLIPPER.parent / "chamber_preheat.cfg").read_text()
        import re
        shipped = re.search(r"^bed_carry:\s*([0-9.]+)", cfg, re.M)
        self.assertIsNotNone(shipped)
        self.assertLess(float(shipped.group(1)), 1.0)

    def test_the_ceiling_is_an_absolute_plate_temperature(self):
        # Not a boost magnitude: it must not move when the printing target does.
        ctl, unused_reactor, bed, state, gcmd = self._cold_start(
            config_values={"max_bed_temp": 72.0}
        )
        self.assertLessEqual(max(t for unused, t in bed.commands), 72.0)
        self.assertEqual(ctl.max_bed_temp, 72.0)

    def test_boost_never_exceeds_the_time_available_to_coast_it_off(self):
        # The complaint: the print ended up waiting on a bed that could not
        # unwind before the chamber arrived.
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0, COAST_RATE=1.0)
        )
        chamber_at = state.state.get("chamber_ready_eventtime")
        print_at = state.state.get("print_ready_eventtime")
        self.assertIsNotNone(chamber_at)
        self.assertIsNotNone(print_at)
        # The bed must not hold the print open for long after the chamber.
        self.assertLessEqual(print_at - chamber_at, 90.0)

    def test_an_unusable_slope_still_gets_the_opening_guess(self):
        # Previously this asserted no boost at all. INITIAL_BOOST is the guess
        # made before a usable slope exists, so the bed goes to target+5 and
        # simply never climbs past it.
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 25.0 if seconds <= 400.0
            else 25.0 + (seconds - 400.0) / 60.0,
            config_values={"history_seconds": 0.0},
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        )
        early = [t for when, t in bed.commands if when <= 400.0]
        self.assertTrue(all(t <= 65.0 + ctl.blind_max for t in early), early)
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)

    def test_bed_heater_limit_reduces_the_peak_without_changing_coast_rate(self):
        ctl, unused_reactor, bed, unused_state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0,
            bed_max=70.0,
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        )
        peak = max(target for unused_time, target in bed.commands)
        self.assertLessEqual(peak, 69.0)
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)

    def test_per_call_policy_parameters_override_config_defaults(self):
        ctl, unused_reactor, bed, unused_state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(FakeGcmd(
            MINIMUM=30.0,
            BED_TARGET=65.0,
            MAX_BOOST=6.0,
            COAST_RATE=2.0,
            INITIAL_BOOST=3.0,
            FILTER_ALPHA=0.5,
        ))
        self.assertLessEqual(max(
            target for unused_time, target in bed.commands
        ), 71.0)
        self.assertEqual(ctl.max_boost, 6.0)
        self.assertEqual(ctl.coast_rate, 2.0)
        self.assertEqual(ctl.initial_boost, 3.0)
        self.assertEqual(ctl.filter_alpha, 0.5)

    def test_error_restores_bed_stops_stir_and_marks_preheat_aborted(self):
        def nozzle(seconds):
            if seconds > 60.0:
                raise RuntimeError("sensor read failed")
            return 25.0 + 0.5 * seconds / 60.0

        ctl, unused_reactor, bed, gcode, fan = controller(nozzle)
        with self.assertRaisesRegex(RuntimeError, "sensor read failed"):
            ctl.cmd_CHAMBER_PREHEAT_WAIT(
                FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
            )
        self.assertEqual(bed.target, 65.0)
        self.assertTrue(any(
            "_CH_PREHEAT_ABORT_STATE" in script
            for script in gcode.scripts
        ))
        self.assertEqual(fan.commands[-1][1], 0.0)

    def test_warm_nozzle_cools_then_qualifies_without_aborting(self):
        def nozzle(seconds):
            if seconds <= 90.0:
                return 52.0 - 0.12 * seconds
            return 41.2 + 0.02 * (seconds - 90.0)

        ctl, reactor, bed, state, fan = controller(nozzle)
        ctl.cmd_CHAMBER_PREHEAT_WAIT(FakeGcmd(
            MINIMUM=33.0,
            BED_TARGET=65.0,
            BOOST=0,
            STIR=1.0,
        ))
        self.assertGreater(reactor.now, 90.0)
        self.assertEqual(state.state["proxy_cooling_seen"], 1.0)
        self.assertEqual(
            state.state["proxy_validity"], MODULE.ProxyGate.VALID
        )
        self.assertEqual(state.state["proxy_valid"], 1.0)
        # It qualified without serving the old fixed 60 s dwell: either the
        # projection reached a warm-enough destination, or it flattened to
        # within the regression's own uncertainty of zero while above the
        # threshold. Both are conclusive; the dwell was a cruder proxy.
        gate = ctl.proxy_gate
        self.assertTrue(gate.by_settling or gate.by_asymptote is not None)
        self.assertFalse(gate.forced)
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)
        self.assertEqual(fan.commands[0], (0.0, 1.0))
        self.assertEqual(fan.commands[-1][1], 0.0)

    def test_compatibility_floor_returns_at_once_but_exact_minimum_does_not(self):
        ctl, reactor, unused_bed, state, fan = controller(lambda seconds: 29.0)
        ctl.cmd_CHAMBER_PREHEAT_WAIT(FakeGcmd(
            MINIMUM=22.0, BED_TARGET=65.0, BOOST=0, STIR=1.0, POLICY=1,
        ))
        self.assertEqual(reactor.now, 0.0)
        self.assertEqual(state.state["preheat_phase"], 4.0)
        self.assertEqual(fan.commands, [(0.0, 1.0), (0.0, 0.0)])

        # An exact slicer minimum must still qualify: a cooling nozzle reads
        # high, so being above the threshold on arrival proves nothing.
        ctl2, reactor2, unused2, state2, unused_fan2 = controller(
            lambda seconds: 29.0
        )
        ctl2.cmd_CHAMBER_PREHEAT_WAIT(FakeGcmd(
            MINIMUM=22.0, BED_TARGET=65.0, BOOST=0, STIR=1.0, POLICY=2,
        ))
        self.assertGreater(reactor2.now, 60.0)
        self.assertEqual(state2.state["proxy_validity"], MODULE.ProxyGate.VALID)

    def test_qualification_gives_up_rather_than_hanging_the_print(self):
        ctl, reactor, bed, state, unused_fan = controller(
            lambda seconds: 60.0 - 0.02 * seconds,      # cools forever
            config_values={"qualify_timeout": 300.0},
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(FakeGcmd(
            MINIMUM=33.0, BED_TARGET=65.0, BOOST=0, STIR=1.0,
        ))
        self.assertTrue(ctl.proxy_gate.forced)
        self.assertGreaterEqual(reactor.now, 300.0)
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)

    def test_history_stops_at_the_last_commanded_nozzle_target(self):
        # 60 s of heating, then 120 s cooling after M104 S0. Only the tail
        # describes the chamber; the heated part must not be regressed.
        temps = [200.0] * 60 + [180.0 - i for i in range(120)]
        targets = [210.0] * 60 + [0.0] * 120
        got = MODULE.usable_history(temps, targets, 1.0, 1000.0, 300.0)
        self.assertEqual(len(got), 120)
        self.assertEqual(got[0][1], 180.0)
        self.assertEqual(got[-1][1], 61.0)
        self.assertAlmostEqual(got[-1][0], 1000.0)
        self.assertAlmostEqual(got[0][0], 1000.0 - 119.0)

    def test_history_is_capped_by_the_requested_span(self):
        temps = [25.0 + i * 0.001 for i in range(1200)]
        got = MODULE.usable_history(temps, [0.0] * 1200, 1.0, 500.0, 180.0)
        self.assertEqual(len(got), 180)

    def test_history_is_refused_when_too_short_or_absent(self):
        self.assertEqual(MODULE.usable_history([], [], 1.0, 0.0, 300.0), [])
        self.assertEqual(
            MODULE.usable_history([25.0, 25.0], [0.0, 0.0], 1.0, 0.0, 300.0), []
        )
        # A run of Nones is not data.
        self.assertEqual(
            MODULE.usable_history([None] * 10, [0.0] * 10, 1.0, 0.0, 300.0), []
        )

    def test_overnight_history_qualifies_without_waiting(self):
        # A machine idle overnight: five minutes of flat scrollback should
        # settle the nozzle immediately rather than spending 148 s proving it.
        history = {"extruder": {
            "temperatures": [25.3] * 400,
            "targets": [0.0] * 400,
        }}

        class Client:
            def __init__(self, *a, **k):
                pass

            def run(self, calls):
                return {calls[0][0]: history}

            @staticmethod
            def quote(v):
                return v

        original = MODULE.moonraker.MoonrakerClient
        MODULE.moonraker.MoonrakerClient = Client
        try:
            ctl, reactor, bed, state, unused_fan = controller(
                lambda seconds: 25.3 + 0.5 * seconds / 60.0,
                config_values={"history_seconds": 300.0},
            )
            gcmd = FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0, BOOST=0)
            ctl.cmd_CHAMBER_PREHEAT_WAIT(gcmd)
        finally:
            MODULE.moonraker.MoonrakerClient = original
        self.assertEqual(state.state["proxy_validity"], MODULE.ProxyGate.VALID)
        # Qualified from the scrollback, before the live loop ran at all. The
        # command still waits for the nozzle to actually reach the minimum.
        self.assertTrue(
            any("history" in m and "qualified" in m for m in gcmd.responses),
            gcmd.responses,
        )
        self.assertNotIn(
            "settling", " ".join(m for m in gcmd.responses if "history" in m)
        )

    def _decay(self, start, final, tau):
        import math
        return lambda seconds: final + (start - final) * math.exp(-seconds / tau)

    def test_asymptote_refuses_a_rising_nozzle(self):
        # The four recorded cold starts: noise supplies spurious curvature and
        # an ungated fit overshot the true settling point by +62 to +125 C.
        rise = [(i * 5.0, 25.0 + 0.3 * (i * 5.0) / 60.0 + (0.05, -0.04, 0.03,
                 -0.06, 0.02)[i % 5]) for i in range(40)]
        estimate, reason = MODULE.cooling_asymptote(rise, 0.10, 30.0, 1800.0, 60.0)
        self.assertIsNone(estimate)
        self.assertEqual(reason, "not clearly cooling")

    def test_asymptote_recovers_the_destination_of_a_real_decay(self):
        import math
        decay = [(i * 5.0, 40.0 + 20.0 * math.exp(-(i * 5.0) / 200.0))
                 for i in range(40)]
        estimate, reason = MODULE.cooling_asymptote(
            decay, 0.10, 30.0, 1800.0, 60.0
        )
        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(estimate, 40.0, delta=1.0)

    def test_asymptote_refuses_an_implausible_time_constant(self):
        # Near-linear cooling has almost no curvature, so tau runs away.
        linear = [(i * 5.0, 60.0 - 0.02 * (i * 5.0)) for i in range(40)]
        estimate, reason = MODULE.cooling_asymptote(
            linear, 0.10, 30.0, 600.0, 60.0
        )
        self.assertIsNone(estimate)
        self.assertIn(reason, ("time constant", "no decay curvature",
                               "asymptote not below the nozzle"),)
        if reason.startswith("time constant"):
            pass

    def test_a_warm_printer_is_not_charged_for_the_descent(self):
        # 60 C nozzle heading for 40 C air with a 33 C minimum: the chamber is
        # already hot enough, so the wait should end well before the nozzle
        # finishes settling.
        ctl, reactor, bed, state, unused_fan = controller(
            self._decay(60.0, 40.0, 200.0)
        )
        gcmd = FakeGcmd(MINIMUM=33.0, BED_TARGET=65.0, BOOST=0)
        ctl.cmd_CHAMBER_PREHEAT_WAIT(gcmd)
        self.assertIsNotNone(ctl.proxy_gate.by_asymptote)
        self.assertGreaterEqual(ctl.proxy_gate.by_asymptote, 35.0)
        # Settling alone would have taken many minutes at tau=200 s.
        self.assertLess(reactor.now, 240.0)
        self.assertTrue(
            any("qualified early" in m for m in gcmd.responses), gcmd.responses
        )

    def test_a_nozzle_heading_below_the_minimum_is_not_qualified_early(self):
        # Same descent, but the air it is heading for is too cold. The
        # projection must not authorize an early finish.
        import math
        samples = [(i * 5.0, 25.0 + 35.0 * math.exp(-(i * 5.0) / 200.0))
                   for i in range(40)]
        estimate, reason = MODULE.cooling_asymptote(
            samples, 0.10, 30.0, 1800.0, 60.0
        )
        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(estimate, 25.0, delta=1.5)
        self.assertLess(estimate, 33.0 + 2.0)

    def test_history_that_disagrees_with_the_live_reading_is_rejected(self):
        history = {"extruder": {
            "temperatures": [80.0] * 400,     # nowhere near the live 25.3
            "targets": [0.0] * 400,
        }}

        class Client:
            def __init__(self, *a, **k):
                pass

            def run(self, calls):
                return {calls[0][0]: history}

            @staticmethod
            def quote(v):
                return v

        original = MODULE.moonraker.MoonrakerClient
        MODULE.moonraker.MoonrakerClient = Client
        try:
            ctl, unused_reactor, bed, state, unused_fan = controller(
                lambda seconds: 25.3 + 0.5 * seconds / 60.0,
                config_values={"history_seconds": 300.0},
            )
            gcmd = FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0, BOOST=0)
            ctl.cmd_CHAMBER_PREHEAT_WAIT(gcmd)
        finally:
            MODULE.moonraker.MoonrakerClient = original
        self.assertTrue(
            any("history rejected" in m for m in gcmd.responses),
            gcmd.responses,
        )
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)

    def test_unreachable_moonraker_costs_the_wait_but_never_the_print(self):
        class Client:
            def __init__(self, *a, **k):
                pass

            def run(self, calls):
                raise MODULE.moonraker.MoonrakerError("connection refused")

            @staticmethod
            def quote(v):
                return v

        original = MODULE.moonraker.MoonrakerClient
        MODULE.moonraker.MoonrakerClient = Client
        try:
            ctl, unused_reactor, bed, state, unused_fan = controller(
                lambda seconds: 25.0 + 0.5 * seconds / 60.0,
                config_values={"history_seconds": 300.0},
            )
            ctl.cmd_CHAMBER_PREHEAT_WAIT(
                FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
            )
        finally:
            MODULE.moonraker.MoonrakerClient = original
        # A completed soak keeps bed_carry: the plate settles one degree
        # above the printing target, not on it.
        self.assertAlmostEqual(bed.target, 65.0 + ctl.bed_carry, places=3)
        self.assertEqual(state.state["proxy_validity"], MODULE.ProxyGate.VALID)

    def test_a_chamber_already_at_target_still_records_its_timestamps(self):
        # 2026-08-25: the nozzle qualified already above the minimum with no
        # boost, so neither transition test in the live loop ever fired and
        # only print_ready_eventtime was recorded. The run could not be
        # measured afterwards.
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 31.5, config_values={"history_seconds": 0.0}
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=31.0, BED_TARGET=65.0, BOOST=0)
        )
        for field in ("chamber_ready_eventtime", "bed_ready_eventtime",
                      "print_ready_eventtime"):
            with self.subTest(field=field):
                self.assertIn(field, state.state)
                self.assertIsNotNone(state.state[field])

    def test_bed_wait_false_hands_the_coast_to_m190(self):
        # 2026-09-07, measured: M191 crossed the chamber minimum and then spent
        # a further 90 s waiting for the boosted plate to coast down, with
        # nothing else running. START_PRINT's next act is CLEAN_NOZZLE, which
        # needs no plate. With bed_wait off, M191 must return on the chamber
        # alone so the wipe fills that window.
        # A RISING nozzle, so the boost actually qualifies -- bed_ready is
        # `not bed_gated`, and an unboosted plate is never gated at all, so a
        # flat curve would exercise nothing.
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0,
            config_values={"history_seconds": 0.0, "bed_wait": False},
        )
        # A plate that never reaches its target, so bed_ready can never be the
        # thing that ends the wait. With bed_wait on, this would spin forever --
        # which is exactly the serialisation being removed.
        bed.curve = lambda seconds: 118.0
        gcmd = FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        ctl.cmd_CHAMBER_PREHEAT_WAIT(gcmd)

        # It returned at all -- the assertion this test exists for.
        self.assertIsNotNone(state.state.get("print_ready_eventtime"))
        # And it left the plate commanded to the CARRIED target, not to the
        # boost, so the M190 that follows waits on the right number.
        self.assertTrue(bed.commands, "the bed was never commanded")
        final = bed.commands[-1][1]
        self.assertLessEqual(final, 66.0, "M191 left the boost in place")
        self.assertGreaterEqual(final, 65.0)
        self.assertTrue(
            any("handing" in r for r in gcmd.responses),
            "the handoff was not announced: %r" % (gcmd.responses,),
        )

    def test_wipe_lead_returns_before_the_chamber_arrives(self):
        # The operator's ask, 2026-09-08: the nozzle heat, wipe and cool should
        # IMMEDIATELY PRECEDE readiness rather than be triggered by it. That
        # means returning while the chamber is still short of its minimum and
        # committing to the ETA -- the nozzle proxy cannot survive the wipe, so
        # there is no confirming afterwards.
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0,
            config_values={"history_seconds": 0.0, "bed_wait": False,
                           "wipe_lead_seconds": 200.0},
        )
        gcmd = FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        ctl.cmd_CHAMBER_PREHEAT_WAIT(gcmd)

        committed = state.state.get("wipe_lead_committed")
        self.assertIsNotNone(committed, "never took the early-commit path")
        self.assertLessEqual(committed, 200.0)
        # It must have left BEFORE the minimum, or it bought nothing.
        self.assertLess(
            state.state["wipe_lead_noz"], 30.0,
            "returned only once the chamber had already arrived",
        )
        # And it must say the quiet part out loud.
        self.assertTrue(
            any("NOT confirmed" in r for r in gcmd.responses),
            "the commit was not announced as unconfirmed: %r" % (gcmd.responses,),
        )
        # The plate is still handed over at its carried target.
        self.assertAlmostEqual(bed.commands[-1][1], 65.0 + ctl.bed_carry,
                               places=3)

    def test_published_defaults_are_the_unserialised_ones(self):
        """These are the PUBLISHED values, and moving them is a decision.

        Both were operator-only overrides until 2026-09-08. They are defaults
        now, so anyone installing the pack gets the wipe overlapping the plate's
        coast and landing on the chamber minimum, without editing anything.

        45 s is chosen to be safe on a machine nobody has measured: the wipe
        here took 97.0 s and its heat phase alone 35-37 s. The operator's own
        70 is an override in svzero-personal.cfg, and the learned bound can
        only ever pull either of them down. See
        notes/personal-tuning-register.md.
        """
        # Built WITHOUT controller(), deliberately: that helper pins
        # wipe_lead_seconds off so the planner tests are not truncated, which
        # would mask exactly the value this test exists to check.
        reactor = FakeReactor()
        printer = FakePrinter(
            reactor,
            FakeHeaters(FakeHeater(reactor, curve=lambda s: 25.0),
                        FakeHeater(reactor, max_temp=120.0, initial_target=65.0)),
            FakeGcode(), FakeFan(reactor),
        )
        ctl = MODULE.ChamberPreheat(FakeConfig(printer, {"history_seconds": 0.0}))
        self.assertEqual(ctl.wipe_lead_seconds, 45.0)
        self.assertFalse(ctl.bed_wait)

    def test_every_state_key_is_declared_on_CH_STATE(self):
        """A state key the macro does not declare KILLS THE JOB.

        SET_GCODE_VARIABLE raises on an unknown name, so _set_state writing a
        key that [gcode_macro _CH_STATE] never declared aborts whatever called
        it. On 2026-09-08 the first job after the wipe_lead deploy cancelled at
        the end of its preheat for exactly this: M191 committed early, wrote
        wipe_lead_committed, and the print died with the nozzle still at 45 C.

        Nothing in the unit tests could see it -- the fake gcode object accepts
        any key -- and nothing in check-macros.py could either, because the
        writer is Python and the declaration is in a different file. This is
        the seam, so the check belongs here.
        """
        cfg = KLIPPER.parent / "chamber_fan.cfg"
        if not cfg.exists():                       # pragma: no cover
            self.skipTest("chamber_fan.cfg not beside the module")
        text = cfg.read_text(encoding="utf-8")
        section = text.split("[gcode_macro _CH_STATE]", 1)[1].split("\n[", 1)[0]
        declared = set(re.findall(r"^variable_([a-z_0-9]+)\s*:", section, re.M))

        source = (KLIPPER / "chamber_preheat.py").read_text(encoding="utf-8")
        written = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "_set_state":
                written.update(kw.arg for kw in node.keywords if kw.arg)
        self.assertTrue(written, "found no _set_state calls to check")
        missing = sorted(written - declared)
        self.assertEqual(
            missing, [],
            "chamber_preheat writes state keys that [gcode_macro _CH_STATE] "
            "does not declare, which raises and aborts the caller: %s" % missing,
        )

    def test_handoff_uses_reactor_clock_domain(self):
        ctl, reactor, unused_bed, state, unused_fan = controller(
            lambda seconds: 25.0
        )
        reactor.now = 123.5
        ctl.cmd_CHAMBER_RECORD_HANDOFF(FakeGcmd())
        self.assertEqual(state.state["handoff_eventtime"], 123.5)


if __name__ == "__main__":
    unittest.main()
