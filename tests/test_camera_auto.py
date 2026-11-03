#!/usr/bin/env python3
"""camera_auto's decisions and its crowsnest.conf edit, without a camera.

The extra itself runs on a printer; what is tested here is everything that
decides: the start phase's bounded correction, the end phase's pick, the
dark-plate trap, the colour check, and the one line of crowsnest.conf
it may change. Frame statistics need NumPy and are skipped without it.
"""
import importlib.util
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("camera_auto", ROOT / "klipper/camera_auto.py")
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)

CONF = """[crowsnest]
log_path: /home/sovol/printer_data/logs/crowsnest.log

[cam 1]
mode: ustreamer                         # ustreamer - Provides mjpg and snapshots.
port: 8080                              # HTTP/MJPG Stream/Snapshot Port
device: /dev/video0                     # See Log for available ...
resolution: 720x540                     # widthxheight format
max_fps: 15

[cam 2]
device: /dev/null
v4l2ctl: brightness=10
"""


def sample(exposure, median, clip=0.0, dark=0.0, sharp=100.0, noise=1.0):
    return dict(exposure=exposure, median=median, clip=clip, dark=dark, sharp=sharp, noise=noise)


class StartPhaseTests(unittest.TestCase):
    def test_restores_the_reference_brightness(self):
        # A darker room halves the plate: double the exposure, back to it.
        self.assertEqual(C.start_exposure(120, 30, 60, 60, 2.0, 20, 600)[0], 240)
        # A brighter room: shorten.
        self.assertEqual(C.start_exposure(120, 120, 60, 60, 2.0, 20, 600)[0], 60)

    def test_correction_is_bounded(self):
        new, ratio, _ = C.start_exposure(120, 5, 60, 60, 2.0, 20, 600)
        self.assertEqual((new, ratio), (240, 2.0))
        self.assertEqual(C.start_exposure(500, 5, 60, 60, 2.0, 20, 600)[0], 600)

    def test_the_response_exponent_damps_the_step(self):
        # Zero1's MGS1 at brightness 0: the median goes as exposure ** 1.4, so
        # doubling the median takes 2 ** (1 / 1.4) = 1.64x the exposure, not 2x.
        new, ratio, _ = C.start_exposure(120, 30, 60, 60, 2.0, 20, 600, 1.4)
        self.assertEqual((new, ratio), (197, 1.641))

    def test_first_run_meters_toward_the_target(self):
        new, _, goal = C.start_exposure(120, 40, None, 60, 2.0, 20, 600)
        self.assertEqual((new, goal), (180, 60))


class EndPhaseTests(unittest.TestCase):
    band, max_clip, max_dark = (40, 100), 0.02, 0.15

    def pick(self, samples):
        return C.choose(samples, self.band, self.max_clip, self.max_dark)

    def test_bracket_is_inside_the_range(self):
        self.assertEqual(C.bracket(120, 1.4, 20, 600), [86, 120, 168])
        self.assertEqual(C.bracket(590, 1.4, 20, 600), [421, 590, 600])
        self.assertEqual(C.bracket(20, 1.4, 20, 600), [20, 28])

    def test_a_clipping_part_pulls_exposure_down(self):
        best, how = self.pick([sample(86, 50), sample(120, 70, clip=0.05), sample(168, 95, clip=0.2)])
        self.assertEqual((best["exposure"], how), (86, "feasible"))

    def test_sharpest_per_noise_wins_and_ties_go_short(self):
        best, _ = self.pick([sample(86, 50, sharp=80), sample(120, 70, sharp=120), sample(168, 90, sharp=120)])
        self.assertEqual(best["exposure"], 120)

    def test_the_dark_plate_trap_moves_one_step_per_print_and_stops_at_the_cap(self):
        # Nothing ever clips and the plate never reaches the band: the case that
        # ran Zero2's one-shot tuner from 120 to 900. Here each print may move
        # one bracket step, and exposure_max holds.
        exposure = 120
        for _ in range(12):
            tried = C.bracket(exposure, 1.4, 20, 600)
            best, how = self.pick([sample(e, 10 + e / 40.0) for e in tried])
            self.assertEqual(how, "least-violation")
            self.assertLessEqual(best["exposure"], round(exposure * 1.4))
            exposure = best["exposure"]
        self.assertEqual(exposure, 600)

    def test_a_white_part_after_the_climb_brings_it_back(self):
        best, _ = self.pick([sample(428, 60), sample(600, 85, clip=0.3)])
        self.assertEqual(best["exposure"], 428)


class ColourTests(unittest.TestCase):
    def test_neutral_within_tolerance(self):
        # AWB on Zero1's interior, 2026-10-31.
        self.assertEqual(C.colour_note(1.03, 0.99, 0.15), "colour neutral (R/G 1.03 B/G 0.99)")

    def test_the_stock_green_cast_is_named(self):
        # Manual 4600 K under the stock LED bar, the same interior.
        self.assertEqual(C.colour_note(0.33, 0.81, 0.15), "colour cast green (R/G 0.33 B/G 0.81)")
        self.assertEqual(C.colour_note(1.3, 0.8, 0.15), "colour cast red (R/G 1.30 B/G 0.80)")
        self.assertEqual(C.colour_note(0.8, 1.3, 0.15), "colour cast blue (R/G 0.80 B/G 1.30)")

    def test_unmeasured_says_so(self):
        self.assertEqual(C.colour_note(None, 0.9, 0.15), "colour unmeasured")

    def test_awb_drops_a_stale_temperature_and_a_pin_writes_gates_first(self):
        base = dict(auto_exposure=1, exposure_dynamic_framerate=0, exposure_time_absolute=300)
        self.assertEqual(C.merge_v4l2ctl("white_balance_temperature=4000,white_balance_automatic=0",
                                         dict(base, white_balance_automatic=1)),
                         "auto_exposure=1,exposure_dynamic_framerate=0,"
                         "white_balance_automatic=1,exposure_time_absolute=300")
        self.assertEqual(C.merge_v4l2ctl("", dict(base, white_balance_automatic=0,
                                                  white_balance_temperature=5000)),
                         "auto_exposure=1,exposure_dynamic_framerate=0,white_balance_automatic=0,"
                         "white_balance_temperature=5000,exposure_time_absolute=300")


class CrowsnestTests(unittest.TestCase):
    managed = dict(auto_exposure=1, exposure_dynamic_framerate=0, gain=0, exposure_time_absolute=150)

    def test_brightness_is_managed_and_set_before_exposure(self):
        value = C.merge_v4l2ctl("brightness=64,contrast=32", dict(self.managed, brightness=0))
        self.assertEqual(value, "contrast=32,auto_exposure=1,exposure_dynamic_framerate=0,"
                                "brightness=0,gain=0,exposure_time_absolute=150")

    def test_gates_come_before_values_and_unmanaged_entries_stay(self):
        value = C.merge_v4l2ctl("contrast=24,exposure_time_absolute=999,auto_exposure=3", self.managed)
        self.assertEqual(value, "contrast=24,auto_exposure=1,exposure_dynamic_framerate=0,"
                                "gain=0,exposure_time_absolute=150")

    def test_adds_the_line_after_device_and_touches_nothing_else(self):
        new, old = C.rewrite_crowsnest(CONF, "1", "auto_exposure=1")
        self.assertEqual(old, "")
        lines = new.split("\n")
        i = lines.index("v4l2ctl: auto_exposure=1")
        self.assertTrue(lines[i - 1].startswith("device: /dev/video0"))
        self.assertEqual([l for l in lines if l != "v4l2ctl: auto_exposure=1"], CONF.split("\n"))

    def test_replaces_an_existing_line_in_its_own_section_only(self):
        new, old = C.rewrite_crowsnest(CONF, "2", "gain=0")
        self.assertEqual(old, "brightness=10")
        self.assertIn("v4l2ctl: gain=0", new)
        self.assertEqual(new.count("v4l2ctl"), 1)

    def test_reads_parameters_past_their_comments(self):
        self.assertEqual(C.crowsnest_param(CONF, "1", "device"), "/dev/video0")
        self.assertEqual(C.crowsnest_param(CONF, "2", "device"), "/dev/null")
        self.assertIsNone(C.crowsnest_param(CONF, "3", "device"))

    def test_a_missing_section_is_refused(self):
        with self.assertRaises(ValueError):
            C.rewrite_crowsnest(CONF, "9", "gain=0")


class ConfigDefaultsTests(unittest.TestCase):
    def test_the_commented_defaults_are_the_code_defaults(self):
        code = (ROOT / "klipper/camera_auto.py").read_text(encoding="utf-8")
        cfg = (ROOT / "klipper/config/camera_auto.cfg").read_text(encoding="utf-8")
        code_defaults = dict(re.findall(r'config\.get\w*\(\s*"(\w+)",\s*"?([^",)]*)"?', code))
        code_defaults.update(re.findall(r'_box\(config, "(\w+)", "([^"]*)"\)', code))
        documented = dict(re.findall(r"(?m)^#(\w+):[ \t]*(.*?)\s*$", cfg))
        for name, value in documented.items():
            if not value or name in ("crowsnest_conf", "state_file"):
                continue
            with self.subTest(option=name):
                self.assertIn(name, code_defaults)
                try:
                    want = [float(v) for v in value.split(",")]
                except ValueError:
                    self.assertEqual(code_defaults[name], value)
                    continue
                self.assertEqual([float(v) for v in code_defaults[name].split(",")], want)


@unittest.skipUnless(C.np is not None, "NumPy is not installed")
class FrameStatsTests(unittest.TestCase):
    def test_measures_only_the_region(self):
        np = C.np
        rgb = np.full((240, 320, 3), 60, dtype=np.uint8)
        rgb[:120, :] = 255  # bright room above the region
        rgb[150:160, 100:110] = 255  # a small white part inside it
        s = C.frame_stats(rgb, [0.25, 0.5, 0.75, 1.0])
        self.assertEqual(s["median"], 60.0)
        self.assertAlmostEqual(s["clip"], 100 / (120 * 160), places=4)
        self.assertEqual(s["bg"], 1.0)

    def test_temporal_noise_is_the_frame_difference(self):
        np = C.np
        a = np.zeros((10, 10), dtype=np.float32)
        self.assertEqual(C.temporal_noise(a, a + 2), 2.0)


if __name__ == "__main__":
    unittest.main()
