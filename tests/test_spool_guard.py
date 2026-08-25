#!/usr/bin/env python3
"""Unit tests for the host-side Spoolman pre-flight gate."""

import importlib.util
import pathlib
import unittest


PATH = (
    pathlib.Path(__file__).parents[1]
    / "slicer profiles/v103/klipper/spool_guard.py"
)
SPEC = importlib.util.spec_from_file_location("spool_guard", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

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


if __name__ == "__main__":
    unittest.main()
