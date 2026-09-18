#!/usr/bin/env python3
"""Unit tests for the host-side Spoolman pre-flight gate."""

import importlib.util
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


MODULE = _load("spool_guard")

evaluate = MODULE.evaluate
normalize_material = MODULE.normalize_material


def check(job_weight=34.0, job_material="PLA", spool_weight=500.0,
          spool_material="PLA", margin=0.0, check_material=True):
    return evaluate(
        job_weight, job_material, spool_weight, spool_material,
        margin, check_material,
    )


class TestNormalizeMaterial(unittest.TestCase):
    def test_case_and_spacing_are_ignored(self):
        self.assertEqual(normalize_material("PLA"), "pla")
        self.assertEqual(normalize_material(" pla "), "pla")
        self.assertEqual(normalize_material("PLA Plus"), "plaplus")

    def test_punctuation_is_stripped(self):
        self.assertEqual(normalize_material("PLA+"), "pla")
        self.assertEqual(normalize_material("PETG"), "petg")

    def test_missing_and_empty_become_none(self):
        self.assertIsNone(normalize_material(None))
        self.assertIsNone(normalize_material(""))
        self.assertIsNone(normalize_material("  "))
        self.assertIsNone(normalize_material("+++"))


class TestWeight(unittest.TestCase):
    def test_ample_spool_passes(self):
        result = check(job_weight=34.0, spool_weight=500.0)
        self.assertTrue(result.ok)
        self.assertIn("500.0g", result.message)

    def test_short_spool_fails(self):
        # The live case on 2026-08-25: 13.25g left, 34.03g job.
        result = check(job_weight=34.03, spool_weight=13.25)
        self.assertFalse(result.ok)
        self.assertIn("13.2g", result.message)
        self.assertIn("34.0g", result.message)

    def test_exactly_enough_passes(self):
        self.assertTrue(check(job_weight=34.0, spool_weight=34.0).ok)

    def test_margin_is_added_to_the_requirement(self):
        self.assertTrue(check(job_weight=34.0, spool_weight=39.0, margin=5.0).ok)
        result = check(job_weight=34.0, spool_weight=38.9, margin=5.0)
        self.assertFalse(result.ok)
        self.assertIn("margin", result.message)

    def test_margin_is_absent_from_the_message_when_zero(self):
        result = check(job_weight=34.0, spool_weight=1.0, margin=0.0)
        self.assertNotIn("margin", result.message)

    def test_depleted_spool_reports_zero_not_negative(self):
        # Spoolman clamps remaining_weight at 0, so an overrun arrives as 0.0.
        result = check(job_weight=5.0, spool_weight=0.0)
        self.assertFalse(result.ok)
        self.assertIn("0.0g", result.message)


class TestUnknowns(unittest.TestCase):
    """Fail-closed: anything unverifiable must stop the print."""

    def test_missing_job_weight_fails(self):
        result = check(job_weight=None)
        self.assertFalse(result.ok)
        self.assertIn("no filament weight", result.message)

    def test_missing_spool_weight_fails(self):
        result = check(spool_weight=None)
        self.assertFalse(result.ok)
        self.assertIn("no remaining weight", result.message)

    def test_missing_job_material_fails_when_checked(self):
        result = check(job_material=None)
        self.assertFalse(result.ok)
        self.assertIn("no filament type", result.message)

    def test_missing_spool_material_fails_when_checked(self):
        result = check(spool_material=None)
        self.assertFalse(result.ok)
        self.assertIn("no material", result.message)

    def test_weight_is_judged_before_material(self):
        # A short spool of the wrong material reports the shortfall, which is
        # the condition the operator asked to see.
        result = check(job_weight=34.0, spool_weight=1.0, spool_material="PETG")
        self.assertFalse(result.ok)
        self.assertIn("needs", result.message)
        self.assertNotIn("PETG", result.message)


class TestMaterial(unittest.TestCase):
    def test_mismatch_fails(self):
        result = check(job_material="PLA", spool_material="PETG")
        self.assertFalse(result.ok)
        self.assertIn("PETG", result.message)
        self.assertIn("PLA", result.message)

    def test_match_passes_despite_formatting(self):
        self.assertTrue(check(job_material="PLA", spool_material="pla").ok)
        self.assertTrue(check(job_material="PLA+", spool_material="PLA").ok)

    def test_message_preserves_the_original_spelling(self):
        result = check(job_material="PETG", spool_material="ABS")
        self.assertIn("spool is ABS", result.message)
        self.assertIn("needs PETG", result.message)

    def test_disabled_check_ignores_mismatch(self):
        result = check(
            job_material="PLA", spool_material="PETG", check_material=False
        )
        self.assertTrue(result.ok)

    def test_disabled_check_ignores_missing_materials(self):
        result = check(
            job_material=None, spool_material=None, check_material=False
        )
        self.assertTrue(result.ok)


class TestRefusalIsCheapToRepeat(unittest.TestCase):
    """Klipper prints a raised error once per macro nesting level."""

    class Gcode:
        def __init__(self):
            self.responses = []

        def respond_info(self, message):
            self.responses.append(message)

        def error(self, message):
            return RuntimeError(message)

    def test_detail_goes_to_responses_and_the_error_stays_short(self):
        guard = MODULE.SpoolGuard.__new__(MODULE.SpoolGuard)
        guard.gcode = self.Gcode()
        error = guard._refuse(
            "spool has 0.1g, job needs 4.4g",
            "print from Mainsail's file list to confirm",
        )
        self.assertEqual(str(error), MODULE.SpoolGuard.REFUSED)
        # Short enough that seeing it twice costs one line, not four.
        self.assertLess(len(str(error)), 40)
        # One line, not two: the whole refusal fits on it.
        self.assertEqual(len(guard.gcode.responses), 1)
        self.assertIn("0.1g", guard.gcode.responses[0])
        self.assertIn("4.4g", guard.gcode.responses[0])
        self.assertIn("Mainsail", guard.gcode.responses[0])
        self.assertLess(len(guard.gcode.responses[0]), 100)
        # The numbers must not be inside the doubled part.
        self.assertNotIn("0.1g", str(error))


class TestRetryOverride(unittest.TestCase):
    """A refusal arms a one-shot override for that exact job."""

    class Reactor:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

    def guard(self, window=300.0):
        g = MODULE.SpoolGuard.__new__(MODULE.SpoolGuard)
        g.gcode = TestRefusalIsCheapToRepeat.Gcode()
        g.reactor = self.Reactor()
        g.override_window = window
        g._armed_file = None
        g._armed_until = 0.0
        g._armed_reason = ""
        return g

    def test_a_real_refusal_arms_the_same_file_only(self):
        g = self.guard()
        g._refuse("spool has 0.1g, job needs 4.4g", "unused", "bin.gcode")
        self.assertIn("start it again", g.gcode.responses[0])
        self.assertIsNone(g._take_override("other.gcode"))
        self.assertEqual(
            g._take_override("bin.gcode"), "spool has 0.1g, job needs 4.4g"
        )

    def test_the_override_is_one_shot(self):
        g = self.guard()
        g._refuse("short", "unused", "bin.gcode")
        self.assertIsNotNone(g._take_override("bin.gcode"))
        self.assertIsNone(g._take_override("bin.gcode"))

    def test_the_override_expires(self):
        g = self.guard(window=300.0)
        g._refuse("short", "unused", "bin.gcode")
        g.reactor.now = 301.0
        self.assertIsNone(g._take_override("bin.gcode"))

    def test_a_dry_run_never_arms_a_real_print(self):
        # SPOOL_GUARD FILE=... passes None, so rehearsing a refusal cannot
        # authorize the next actual print of that file.
        g = self.guard()
        g._refuse("short", "or SPOOL_GUARD BYPASS=1", None)
        self.assertIsNone(g._armed_file)
        self.assertIsNone(g._take_override("bin.gcode"))
        self.assertIn("BYPASS", g.gcode.responses[0])

    def test_a_zero_window_disables_the_override(self):
        g = self.guard(window=0.0)
        g._refuse("short", "or SPOOL_GUARD BYPASS=1", "bin.gcode")
        self.assertIsNone(g._armed_file)
        self.assertIsNone(g._take_override("bin.gcode"))

    def test_a_second_refusal_replaces_the_armed_job(self):
        g = self.guard()
        g._refuse("short", "unused", "first.gcode")
        g._refuse("short", "unused", "second.gcode")
        self.assertIsNone(g._take_override("first.gcode"))
        self.assertIsNotNone(g._take_override("second.gcode"))


class TestOptionalIntegration(unittest.TestCase):
    def guard(self):
        import types
        g = TestRetryOverride().guard()
        g.enabled = True
        g.moonraker_url = "http://127.0.0.1:7125"
        g.timeout = 8.0
        g._spool = {"material": "abs", "id": 1}
        stats = types.SimpleNamespace(get_status=lambda now: {"filename": "test.gcode"})
        g.printer = types.SimpleNamespace(lookup_object=lambda *args: stats)
        return g

    def command(self):
        import types
        return types.SimpleNamespace(get=lambda key, default=None: default,
                                     get_int=lambda key, default=0: default)

    def test_absent_spoolman_allows_print_and_warns_once(self):
        from unittest.mock import patch, Mock
        g = self.guard()
        client = Mock()
        client.quote.side_effect = lambda x: x
        client.run.side_effect = MODULE.moonraker.MoonrakerError(
            "not configured", status=404, key="ask Moonraker which spool is active")
        with patch.object(MODULE.moonraker, "MoonrakerClient", return_value=client):
            g.cmd_SPOOL_GUARD(self.command())
            g.cmd_SPOOL_GUARD(self.command())
        self.assertEqual(len(g.gcode.responses), 1)
        self.assertIn("checks are skipped", g.gcode.responses[0])
        self.assertIsNone(g._spool["material"])
        self.assertIsNone(g._armed_file)
        self.assertEqual(client.run.call_args[0][0][0][1], "/server/spoolman/spool_id")

    def test_configured_without_active_spool_still_refuses(self):
        from unittest.mock import patch
        g = self.guard()
        with patch.object(g, "_fetch", return_value={"spool_id": None}), \
                self.assertRaisesRegex(RuntimeError, "refused"):
            g.cmd_SPOOL_GUARD(self.command())

    def test_missing_metadata_and_server_failures_do_not_fail_open(self):
        from unittest.mock import patch, Mock
        for status, key in ((404, "read this job's metadata from Moonraker"),
                            (500, "ask Moonraker which spool is active"),
                            (None, "ask Moonraker which spool is active")):
            with self.subTest(status=status, key=key):
                g = self.guard()
                client = Mock()
                client.quote.side_effect = lambda x: x
                client.run.side_effect = MODULE.moonraker.MoonrakerError("failed", status=status, key=key)
                with patch.object(MODULE.moonraker, "MoonrakerClient", return_value=client), \
                        self.assertRaisesRegex(RuntimeError, "refused"):
                    g.cmd_SPOOL_GUARD(self.command())


if __name__ == "__main__":
    unittest.main()
