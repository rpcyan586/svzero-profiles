#!/usr/bin/env python3
"""Unit tests for the host-side chamber preheat boost planner."""

import importlib.util
import pathlib
import unittest


PATH = (
    pathlib.Path(__file__).parents[1]
    / "slicer profiles/v103/klipper/chamber_preheat.py"
)
SPEC = importlib.util.spec_from_file_location("chamber_preheat", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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
        early = [target for eventtime, target in bed.commands
                 if eventtime <= 300.0]
        self.assertTrue(all(t <= 65.0 for t in early), early)
        self.assertEqual(state.state["proxy_cooling_seen"], 1.0)
        self.assertEqual(bed.target, 65.0)

    def test_boost_waits_for_qualification_then_sizes_from_headroom(self):
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 25.0 + 0.5 * seconds / 60.0
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        )
        commands = bed.commands
        # Nothing above the printing target before the settle dwell completes.
        self.assertTrue(all(
            target <= 65.0 for eventtime, target in commands
            if eventtime < ctl.settle_dwell
        ))
        peak = max(target for unused_time, target in commands)
        self.assertGreater(peak, 65.0)
        self.assertLessEqual(peak, 75.0)
        self.assertEqual(commands[-1][1], 65.0)
        self.assertEqual(bed.target, 65.0)

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

    def test_boost_is_skipped_when_the_slope_is_unusable(self):
        ctl, unused_reactor, bed, state, unused_fan = controller(
            lambda seconds: 25.0 if seconds <= 400.0
            else 25.0 + (seconds - 400.0) / 60.0
        )
        ctl.cmd_CHAMBER_PREHEAT_WAIT(
            FakeGcmd(MINIMUM=30.0, BED_TARGET=65.0)
        )
        early = [target for eventtime, target in bed.commands
                 if eventtime <= 400.0]
        self.assertTrue(all(t <= 65.0 for t in early), early)
        self.assertEqual(bed.target, 65.0)

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
        self.assertEqual(bed.target, 65.0)

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
        self.assertGreaterEqual(state.state["proxy_valid_dwell"], 60.0)
        self.assertEqual(bed.target, 65.0)
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
        self.assertEqual(bed.target, 65.0)

    def test_handoff_uses_reactor_clock_domain(self):
        ctl, reactor, unused_bed, state, unused_fan = controller(
            lambda seconds: 25.0
        )
        reactor.now = 123.5
        ctl.cmd_CHAMBER_RECORD_HANDOFF(FakeGcmd())
        self.assertEqual(state.state["handoff_eventtime"], 123.5)


if __name__ == "__main__":
    unittest.main()
