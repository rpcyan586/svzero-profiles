"""Stock-camera exposure that follows the room, print to print.

Opt-in, through [include camera_auto.cfg]. Uses only what a stock Zero image
ships: v4l2-ctl, ffmpeg, crowsnest's ustreamer snapshot, and NumPy in
klippy-env. No motion, heater, fan or LED command, and no G-code at all.

TWO PHASES, each a background thread, so neither blocks Klipper's reactor or
the G-code queue:

  start  _PREFLIGHT, before the bed heats. Measures the plate at the stored
         exposure and corrects it by the drift since the last end-of-print
         reference, so this print starts at this room's light. On the very
         first run there is no reference, and it meters the plate toward
         target_median instead. The bed wait hides the time it takes.
  end    END_PRINT, with the finished part on the plate. Tries a short
         exposure bracket around the current value, keeps the best under the
         constraints, records the plate's median as the next reference, and
         writes the result into crowsnest.conf's v4l2ctl, so a crowsnest or
         machine restart starts there too.

WHY RELATIVE AT THE START. A bare dark plate clips at no exposure the camera
offers, so metering it for "brightest without clipping" runs exposure to the
maximum and the next white print blows out (notes/runbook-webcam.md, the
trap). The start phase never searches: it only restores the plate's brightness
to what the end phase chose with a real part in frame, by a bounded ratio. The
end phase is where clipping can bind, because there is something on the plate.

WHAT IS MEASURED is a fixed crop of the frame, `roi`, not the plate: the bed
moves in Z, so the plate moves through the frame. The two phases still compare
like with like, because the bed does not move between one print's end and the
next one's start. brightness is pinned (an additive offset would swamp the
exposure signal), and the start's correction allows for the median growing
roughly as exposure ** response_exponent (camera_auto.cfg has the sweep).

WHITE BALANCE is the camera's own automatic, kept on and persisted. Under the
stock white LED bar no manual temperature is neutral: the MGS1's one axis trades
red against blue, and on Zero1's dark-grey interior R/G stayed between 0.20 and
0.54 from 2800 to 6500 K, a green cast. The camera's AWB made it neutral (R/G
1.03, B/G 0.99) within 1.6 s, and switching AWB off returns to the manual
gains at once, so its result cannot be frozen (2026-10-31). Each phase reports
the interior's colour, R/G and B/G over the crop's mid-tones, as the check that
it is still grey. white_balance_temperature pins a manual value instead.

Crowsnest must own the camera. When its [cam] device is /dev/null or missing,
as on a go2rtc machine, every command reports that and does nothing.
"""
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request

try:
    import numpy as np
except ImportError:  # the pure functions below still import for tests
    np = None

# Gates go first: exposure_time_absolute is inactive until auto_exposure=1, and
# white_balance_temperature until white_balance_automatic=0, and a value written
# to an inactive control is silently dropped (notes/runbook-webcam.md).
MANAGED_ORDER = ("auto_exposure", "exposure_dynamic_framerate",
                 "white_balance_automatic", "brightness", "gain",
                 "white_balance_temperature", "exposure_time_absolute")


# ---------------------------------------------------------------- pure parts

def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def roi_slice(shape, roi):
    """Rows and columns of a [x0, y0, x1, y1] fraction box on an image shape."""
    h, w = shape[0], shape[1]
    x0, y0, x1, y1 = roi
    return slice(int(y0 * h), max(int(y1 * h), int(y0 * h) + 1)), \
        slice(int(x0 * w), max(int(x1 * w), int(x0 * w) + 1))


def frame_stats(rgb, roi, clip_level=250, dark_level=5):
    """Plate statistics on one RGB frame: median luminance, clipped and dark
    fractions, focus (variance of the Laplacian) and mid-tone chromaticity."""
    rows, cols = roi_slice(rgb.shape, roi)
    sub = rgb[rows, cols].astype(np.float32)
    lum = 0.299 * sub[..., 0] + 0.587 * sub[..., 1] + 0.114 * sub[..., 2]
    lap = (-4 * lum[1:-1, 1:-1] + lum[:-2, 1:-1] + lum[2:, 1:-1]
           + lum[1:-1, :-2] + lum[1:-1, 2:])
    mid = (lum > dark_level) & (lum < clip_level)
    g = float(sub[..., 1][mid].mean()) if mid.any() else 0.0
    return dict(
        median=round(float(np.median(lum)), 2),
        clip=round(float((lum >= clip_level).mean()), 4),
        dark=round(float((lum <= dark_level).mean()), 4),
        sharp=round(float(lap.var()), 2),
        rg=round(float(sub[..., 0][mid].mean()) / g, 4) if g else None,
        bg=round(float(sub[..., 2][mid].mean()) / g, 4) if g else None,
        lum=lum)


def temporal_noise(lum_a, lum_b):
    """Mean absolute difference of two frames of a still scene: sensor noise,
    which a spatial measure cannot separate from the plate's own texture."""
    return round(float(np.abs(lum_a - lum_b).mean()), 3)


def start_exposure(exposure, median, reference, target, max_step, lo, hi, response=1.0):
    """The start phase's correction: back to the reference brightness, or to the
    target when there is none. The plate median goes roughly as exposure to the
    power `response`, so exposure moves by the brightness ratio to 1/response,
    bounded to max_step either way."""
    goal = reference if reference else target
    ratio = clamp((goal / max(median, 1.0)) ** (1.0 / response), 1.0 / max_step, max_step)
    return int(clamp(round(exposure * ratio), lo, hi)), round(ratio, 3), goal


def bracket(exposure, step, lo, hi):
    """Short, current and long exposures, de-duplicated and inside the range."""
    out = []
    for e in (exposure / step, exposure, exposure * step):
        e = int(clamp(round(e), lo, hi))
        if e not in out:
            out.append(e)
    return sorted(out)


def choose(samples, band, max_clip, max_dark):
    """The end phase's pick. Feasible means nothing over max_clip, max_dark,
    and a plate median inside band. Among feasible samples the sharpest per
    unit of noise wins; ties go to the shorter exposure. With none feasible,
    the least violation wins, again the shorter on a tie."""
    lo, hi = band

    def violation(s):
        return (max(0.0, s["clip"] - max_clip) * 1000
                + max(0.0, s["dark"] - max_dark) * 100
                + max(0.0, lo - s["median"]) + max(0.0, s["median"] - hi))
    feasible = [s for s in samples if violation(s) == 0]
    if feasible:
        best = max(feasible, key=lambda s: (s["sharp"] / (s["noise"] + 0.05), -s["exposure"]))
        return best, "feasible"
    return min(samples, key=lambda s: (violation(s), s["exposure"])), "least-violation"


def colour_note(rg, bg, tolerance):
    """The interior's colour, for the console: neutral when both ratios are
    within tolerance of 1, otherwise which way it is cast."""
    if rg is None or bg is None:
        return "colour unmeasured"
    if abs(rg - 1) <= tolerance and abs(bg - 1) <= tolerance:
        return "colour neutral (R/G %.2f B/G %.2f)" % (rg, bg)
    cast = "green" if rg < 1 and bg < 1 else "red" if rg > bg else "blue"
    return "colour cast %s (R/G %.2f B/G %.2f)" % (cast, rg, bg)


def parse_v4l2ctl(value):
    """crowsnest's v4l2ctl value as an ordered list of (name, value)."""
    out = []
    for part in (value or "").replace(" ", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out.append((k, v))
    return out


def merge_v4l2ctl(existing, managed):
    """The v4l2ctl value with `managed` set. camera_auto owns every control in
    MANAGED_ORDER, so an old value for one of them goes even when it is not set
    now (a pinned temperature once AWB is back on). Other entries keep their
    order and go first, then the managed ones, gates before values."""
    keep = [(k, v) for k, v in parse_v4l2ctl(existing) if k not in MANAGED_ORDER]
    ordered = [(k, str(managed[k])) for k in MANAGED_ORDER if k in managed]
    return ",".join("%s=%s" % kv for kv in keep + ordered)


def rewrite_crowsnest(text, cam, value):
    """crowsnest.conf with [cam <cam>]'s v4l2ctl set to `value`, every other
    line untouched. Adds the line after the section's device: when absent.
    Returns (new_text, old_value); raises ValueError without the section."""
    lines = text.split("\n")
    header = "[cam %s]" % cam
    start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
    if start is None:
        raise ValueError("no %s in crowsnest.conf" % header)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        key = lines[i].split(":", 1)[0].strip()
        if key == "v4l2ctl":
            old = lines[i].split(":", 1)[1].split("#", 1)[0].strip()
            lines[i] = "v4l2ctl: %s" % value
            return "\n".join(lines), old
    at = next((i + 1 for i in range(start + 1, end) if lines[i].split(":", 1)[0].strip() == "device"), end)
    lines.insert(at, "v4l2ctl: %s" % value)
    return "\n".join(lines), ""


def crowsnest_param(text, cam, name):
    """One [cam <cam>] parameter from crowsnest.conf, or None."""
    section = None
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("["):
            section = s
            continue
        if section == "[cam %s]" % cam and ":" in s and not s.startswith("#"):
            k, v = s.split(":", 1)
            if k.strip() == name:
                return v.split("#", 1)[0].strip()
    return None


# ---------------------------------------------------------------- Klipper

class CameraAuto:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        home = os.path.expanduser("~")
        self.conf_path = os.path.expanduser(config.get(
            "crowsnest_conf", os.path.join(home, "printer_data/config/crowsnest.conf")))
        self.state_path = os.path.expanduser(config.get(
            "state_file", os.path.join(home, "printer_data/config/camera_auto_state.json")))
        self.cam = config.get("cam", "1")
        self.device_cfg = config.get("device", "")
        self.snapshot_url = config.get("snapshot_url", "http://127.0.0.1:8080/?action=snapshot")
        self.roi = self._box(config, "roi", "0.10,0.10,0.90,0.90")
        self.lo = config.getint("exposure_min", 20, minval=1, maxval=5000)
        self.hi = config.getint("exposure_max", 600, minval=self.lo, maxval=5000)
        self.first_exposure = config.getint("exposure_initial", 120, minval=self.lo, maxval=self.hi)
        self.target = config.getfloat("target_median", 60., above=0., maxval=250.)
        self.band = (config.getfloat("median_min", 40., minval=0.),
                     config.getfloat("median_max", 100., maxval=255.))
        self.max_clip = config.getfloat("max_clip", 0.02, minval=0., maxval=1.)
        self.max_dark = config.getfloat("max_dark", 0.15, minval=0., maxval=1.)
        self.max_step = config.getfloat("start_max_step", 2.0, minval=1.0, maxval=4.0)
        self.bracket_step = config.getfloat("end_bracket_step", 1.4, minval=1.05, maxval=2.0)
        self.gain = config.getint("gain", 0, minval=0, maxval=100)
        self.brightness = config.getint("brightness", 0, minval=-64, maxval=64)
        self.response = config.getfloat("response_exponent", 1.4, minval=0.5, maxval=3.)
        self.wb_temperature = config.getint("white_balance_temperature", 0, minval=0, maxval=6500)
        if 0 < self.wb_temperature < 2800:
            raise config.error("camera_auto white_balance_temperature is 0 (automatic) or 2800..6500")
        self.colour_tolerance = config.getfloat("colour_tolerance", 0.15, above=0., maxval=1.)
        self.settle = config.getfloat("settle_seconds", 0.8, minval=0.2, maxval=5.)
        self.budget = config.getfloat("max_seconds", 30., minval=5., maxval=120.)
        self.width = config.getint("analysis_width", 320, minval=64, maxval=1280)
        self.job = None
        self.cancel = threading.Event()
        self.last = {"status": "not-run"}
        self.gcode.register_command("CAMERA_AUTO", self.cmd_CAMERA_AUTO,
                                    desc="Adapt the stock camera's exposure in the background (PHASE=start|end), without motion")
        self.gcode.register_command("CAMERA_AUTO_RESET", self.cmd_CAMERA_AUTO_RESET,
                                    desc="Forget camera_auto's reference, so the next start meters from scratch")

    @staticmethod
    def _box(config, name, default):
        try:
            box = [float(v) for v in config.get(name, default).split(",")]
        except ValueError:
            raise config.error("camera_auto %s must be four fractions x0,y0,x1,y1" % name)
        if len(box) != 4 or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
            raise config.error("camera_auto %s must be four fractions x0,y0,x1,y1" % name)
        return box

    def get_status(self, eventtime):
        return dict(active=self.job is not None and self.job.is_alive(), last=self.last)

    # -- reporting, always from the reactor thread
    def _say(self, msg):
        self.reactor.register_async_callback(lambda e: self.gcode.respond_info("camera_auto: " + msg))

    # -- commands
    def cmd_CAMERA_AUTO(self, gcmd):
        phase = gcmd.get("PHASE").lower()
        if phase not in ("start", "end"):
            raise gcmd.error("CAMERA_AUTO PHASE must be start or end")
        previous = self.job if self.job is not None and self.job.is_alive() else None
        if previous is not None:
            if phase == "end":
                gcmd.respond_info("camera_auto: a run is still active, end phase skipped")
                return
            # A new print's start outranks a previous print's end trials: stop
            # them, and let the new thread wait for them rather than the reactor.
            self.cancel.set()
        self.cancel = threading.Event()
        self.job = threading.Thread(target=self._run, args=(phase, self.cancel, previous),
                                    daemon=True, name="camera_auto")
        self.job.start()
        gcmd.respond_info("camera_auto: %s phase started in the background" % phase)

    def cmd_CAMERA_AUTO_RESET(self, gcmd):
        state = self._load_state()
        state.pop("reference", None)
        self._save_state(state)
        gcmd.respond_info("camera_auto: reference cleared; the next start meters toward %g" % self.target)

    # -- the worker
    def _run(self, phase, cancel, previous=None):
        if previous is not None:
            previous.join(timeout=15)
        started = time.monotonic()
        result = dict(phase=phase, status="error")
        try:
            device = self._device()
            if not device:
                result["status"] = "no-crowsnest-camera"
                self._say("crowsnest owns no camera here ([cam %s] device is %s); nothing done"
                          % (self.cam, self._crowsnest_device() or "absent"))
                return
            result["device"] = device
            state = self._load_state()
            exposure = int(state.get("exposure") or self._read_exposure(device) or self.first_exposure)
            exposure = int(clamp(exposure, self.lo, self.hi))
            self._gates(device)
            if phase == "start":
                result.update(self._start(device, exposure, state, cancel, started))
            else:
                result.update(self._end(device, exposure, state, cancel, started))
        except Exception as e:
            logging.exception("camera_auto %s failed", phase)
            result.update(status="error", error=str(e))
            self._say("%s phase failed, camera left as it was: %s" % (phase, e))
        finally:
            result["seconds"] = round(time.monotonic() - started, 1)
            self.last = result
            logging.info("camera_auto %s", json.dumps(result, sort_keys=True, default=str))

    def _start(self, device, exposure, state, cancel, started):
        ref = state.get("reference") or {}
        reference = ref.get("median")
        steps = []
        for _ in range(3):
            if cancel.is_set() or time.monotonic() - started > self.budget:
                break
            s = self._measure(device, exposure)
            new, ratio, goal = start_exposure(exposure, s["median"], reference, self.target,
                                              self.max_step, self.lo, self.hi, self.response)
            steps.append(dict(exposure=exposure, median=s["median"], clip=s["clip"], ratio=ratio, next=new,
                              rg=s["rg"], bg=s["bg"]))
            if abs(s["median"] - goal) <= 0.1 * goal or new == exposure:
                break
            exposure = new
        self._set(device, exposure_time_absolute=exposure)
        state.update(exposure=exposure)
        if not reference:
            # First run: this becomes the baseline until an end phase replaces it.
            state["reference"] = dict(median=self.target, exposure=exposure, source="first-start")
            self._persist(exposure)
        self._save_state(state)
        last = steps[-1] if steps else {}
        colour = colour_note(last.get("rg"), last.get("bg"), self.colour_tolerance)
        self._say("start: exposure %d (reference median %s, steps %s), %s" % (
            exposure, reference if reference else "none, target %g" % self.target,
            " ".join("%d:%.0f" % (st["exposure"], st["median"]) for st in steps), colour))
        return dict(status="started", exposure=exposure, reference=reference, steps=steps, colour=colour)

    def _end(self, device, exposure, state, cancel, started):
        samples = []
        for e in bracket(exposure, self.bracket_step, self.lo, self.hi):
            if cancel.is_set() or time.monotonic() - started > self.budget:
                break
            a = self._measure(device, e)
            b = self._capture_stats()
            samples.append(dict(exposure=e, median=a["median"], clip=a["clip"], dark=a["dark"],
                                sharp=a["sharp"], noise=temporal_noise(a["lum"], b["lum"]),
                                rg=a["rg"], bg=a["bg"]))
        if not samples:
            self._set(device, exposure_time_absolute=exposure)
            return dict(status="cancelled", exposure=exposure)
        best, how = choose(samples, self.band, self.max_clip, self.max_dark)
        exposure = best["exposure"]
        self._set(device, exposure_time_absolute=exposure)
        colour = colour_note(best.get("rg"), best.get("bg"), self.colour_tolerance)
        state.update(exposure=exposure, reference=dict(
            median=best["median"], exposure=exposure, source="end-of-print",
            at=time.strftime("%Y-%m-%dT%H:%M:%S")))
        self._save_state(state)
        self._persist(exposure)
        self._say("end: kept exposure %d (%s; tried %s); reference median %.0f, %s; written to crowsnest.conf"
                  % (exposure, how, " ".join("%d:%.0f/%.3f" % (s["exposure"], s["median"], s["clip"])
                                             for s in samples), best["median"], colour))
        return dict(status=how, exposure=exposure, samples=samples, colour=colour)

    def _white_balance(self):
        """The white-balance controls: the camera's automatic, or a pinned value."""
        if self.wb_temperature:
            return dict(white_balance_automatic=0, white_balance_temperature=self.wb_temperature)
        return dict(white_balance_automatic=1)

    # -- camera and files
    def _crowsnest_device(self):
        try:
            with open(self.conf_path) as f:
                return crowsnest_param(f.read(), self.cam, "device")
        except OSError:
            return None

    def _device(self):
        dev = self.device_cfg or self._crowsnest_device()
        if not dev or dev == "/dev/null" or not os.path.exists(dev):
            return None
        return dev

    def _gates(self, device):
        gates = dict(auto_exposure=1, exposure_dynamic_framerate=0, gain=self.gain,
                     brightness=self.brightness, **self._white_balance())
        self._set(device, **gates)
        time.sleep(self.settle)  # AWB settles within 1.6 s; a measurement follows a further settle

    def _set(self, device, **controls):
        for name in MANAGED_ORDER:
            if name in controls:
                subprocess.run(["v4l2-ctl", "-d", device, "-c", "%s=%s" % (name, controls[name])],
                               check=True, capture_output=True, timeout=5)
        if "exposure_time_absolute" in controls or "white_balance_temperature" in controls:
            time.sleep(self.settle)

    def _read_exposure(self, device):
        try:
            out = subprocess.run(["v4l2-ctl", "-d", device, "-C", "exposure_time_absolute"],
                                 check=True, capture_output=True, text=True, timeout=5).stdout
            return int(out.split(":")[1])
        except Exception:
            return None

    def _frame(self):
        with urllib.request.urlopen(self.snapshot_url, timeout=4) as r:
            jpeg = r.read()
        h = self.width * 3 // 4
        raw = subprocess.run(["nice", "-n", "19", "ffmpeg", "-v", "error", "-i", "pipe:0",
                              "-vf", "scale=%d:%d" % (self.width, h), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                             input=jpeg, check=True, capture_output=True, timeout=10).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape(h, self.width, 3)

    def _capture_stats(self):
        return frame_stats(self._frame(), self.roi)

    def _measure(self, device, exposure):
        self._set(device, exposure_time_absolute=exposure)
        self._frame()  # discard: the stream may still hold a frame from before
        return self._capture_stats()

    def _load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def _persist(self, exposure):
        """Write the chosen controls into crowsnest.conf, atomically, once a
        backup of the original exists. Crowsnest applies v4l2ctl each time its
        stream starts (libs/v4l2_control.sh), so a restart starts here."""
        managed = dict(auto_exposure=1, exposure_dynamic_framerate=0, gain=self.gain,
                       brightness=self.brightness, exposure_time_absolute=exposure,
                       **self._white_balance())
        with open(self.conf_path) as f:
            text = f.read()
        old = crowsnest_param(text, self.cam, "v4l2ctl") or ""
        new_text, _ = rewrite_crowsnest(text, self.cam, merge_v4l2ctl(old, managed))
        if new_text == text:
            return
        backup = self.conf_path + ".camera-auto-orig"
        if not os.path.exists(backup):
            with open(backup, "w") as f:
                f.write(text)
        tmp = self.conf_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(new_text)
        os.replace(tmp, self.conf_path)


def load_config(config):
    return CameraAuto(config)
