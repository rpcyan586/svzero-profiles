"""String-vector options must reach PrusaSlicer and SuperSlicer as ONE element.

start_filament_gcode is a ConfigOptionStrings, one string per extruder. Written
unquoted it split at its leading `;` comment, so extruder 1 ran an empty string
and the Z-offset stub and PrusaSlicer's pressure advance never ran. These tests
read the shipped files the way the slicers do.
"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)


def unescape_strings_cstyle(s):
    """PrusaSlicer's ConfigOptionStrings deserializer, unescape_strings_cstyle.

    Transcribed from src/libslic3r/Config.cpp. A quoted element decodes
    backslash escapes; an unquoted one runs to the next `;` and keeps
    backslashes literally. SuperSlicer inherits the same function.
    """
    out, i, n = [], 0, len(s)
    if not s:
        return out
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i == n:
            return out
        buf = []
        if s[i] == '"':
            i += 1
            while i < n and s[i] != '"':
                c = s[i]
                if c == "\\":
                    i += 1
                    if i == n:
                        raise ValueError("dangling backslash: %r" % s)
                    c = {"n": "\n", "r": "\r"}.get(s[i], s[i])
                buf.append(c)
                i += 1
            if i == n:
                raise ValueError("unterminated quote: %r" % s)
            i += 1
        else:
            while i < n and s[i] != ";":
                buf.append(s[i])
                i += 1
        out.append("".join(buf))
        while i < n and s[i] in " \t":
            i += 1
        if i == n:
            return out
        if s[i] != ";":
            raise ValueError("text after a quoted element: %r" % s)
        i += 1
        if i == n:
            out.append("")
            return out


def ini_files():
    """Every PrusaSlicer-family .ini the pack ships, as {label: {section: kv}}."""
    b = ROOT / "bundles"
    found = {}
    for kind in ("ps-presets", "ss-presets"):
        for p in sorted((b / kind).glob("*/*.ini")):
            kv = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    kv[k.strip()] = v.strip()
            found["%s/%s/%s" % (kind, p.parent.name, p.stem)] = {p.parent.name + ":" + p.stem: kv}
    for rel in ("SVZero_PrusaSlicer.ini", "SVZero_SuperSlicer.ini", "ps-vendor/SVZero.ini"):
        found[rel] = G.parse_ini(b / rel)
    return found


class IniStringVectors(unittest.TestCase):
    def test_serializer_round_trips_through_the_slicer_parser(self):
        for master, element in [
                ("; comment\nSET_GCODE_OFFSET Z_ADJUST=0.000 MOVE=1",
                 "; comment\nSET_GCODE_OFFSET Z_ADJUST=0.000 MOVE=1"),
                ("; a\\nb", "; a\nb"),              # the master's escaped form
                ('say "x"; y \\\\ z', 'say "x"; y \\ z'),
                ('"All surfaces"', "All surfaces"),  # already serialized
                ("brass", "brass"), ("", "")]:
            got = unescape_strings_cstyle(G.ini_value(master, "start_filament_gcode"))
            self.assertEqual(got, [element], master)
        self.assertEqual(G.ini_value("brass", "tool_name"), "brass")
        self.assertEqual(G.ini_value("", "extruder_colour"), '""')

    def test_plain_strings_are_not_quoted(self):
        self.assertEqual(G.ini_value("G28 ; home\nG1 Z5", "start_gcode"), "G28 ; home\\nG1 Z5")
        for label, secs in ini_files().items():
            for sec, kv in secs.items():
                for key in ("start_gcode", "end_gcode", "before_layer_gcode", "notes",
                            "printer_notes"):
                    self.assertFalse(kv.get(key, "").startswith('"'),
                                     "%s [%s] %s gained quotes" % (label, sec, key))

    def test_every_string_vector_is_one_element(self):
        n = 0
        for label, secs in ini_files().items():
            for sec, kv in secs.items():
                for key in sorted(G.INI_STRING_VECTORS & set(kv)):
                    got = unescape_strings_cstyle(kv[key])
                    self.assertEqual(len(got), 1, "%s [%s] %s = %s -> %r"
                                     % (label, sec, key, kv[key][:60], got))
                    n += 1
        self.assertGreater(n, 50)

    def test_every_filament_start_gcode_runs_its_z_offset_stub(self):
        n = 0
        for label, secs in ini_files().items():
            for sec, kv in secs.items():
                if not sec.startswith("filament:") or "start_filament_gcode" not in kv:
                    continue
                got = unescape_strings_cstyle(kv["start_filament_gcode"])
                self.assertEqual(len(got), 1, "%s [%s]: %r" % (label, sec, got))
                lines = got[0].split("\n")
                self.assertTrue(any(ln.startswith("SET_GCODE_OFFSET Z_ADJUST=") for ln in lines),
                                "%s [%s]: no stub line in %r" % (label, sec, got[0]))
                self.assertNotIn("\\", got[0], "%s [%s]" % (label, sec))
                if label.startswith("ps-presets"):     # flattened: PA is appended here
                    self.assertTrue(lines[-1].startswith("SET_PRESSURE_ADVANCE ADVANCE="),
                                    "%s: no pressure advance in %r" % (label, got[0]))
                n += 1
        self.assertGreaterEqual(n, 18)


if __name__ == "__main__":
    unittest.main()
