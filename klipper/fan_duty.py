# Report a fan's duty cycle as if it were a temperature, so it can be charted.
#
# WHY THIS EXISTS
#
# 1.4.x stock ships the exhaust as [temperature_fan exhaust_fan]. A
# temperature_fan has both a temperature and a speed, so Mainsail charts it for
# free -- temperature on the primary axis, duty on the secondary. That is the
# plot the operator remembers, and it was a side effect of Klipper owning the
# fan.
#
# chamber_fan.cfg deletes that section, because a temperature_fan cannot be
# driven by SET_FAN_SPEED and cannot be converted in place; the exhaust becomes
# a fan_generic driven by our own PI loop. A fan_generic has no temperature, so
# it leaves the temperature chart entirely. We took the control and paid for it
# with the instrument.
#
# This buys the instrument back without giving up the control. It registers a
# sensor type that reads a fan's duty and reports it as a temperature, so
#
#   [fan_duty]
#   [temperature_sensor exhaust_duty]
#   sensor_type: fan_duty
#   fan: fan_generic exhaust_fan
#
# puts a 0-100 trace on the chart alongside the heaters. It is an INSTRUMENT and
# nothing more: it drives no pin and holds no state anyone else reads.
#
# THE BARE [fan_duty] SECTION IS LOAD-BEARING, and leaving it out is why the
# first attempt halted the printer with "Unknown temperature sensor 'fan_duty'".
# heaters.setup_sensor() resolves sensor_type against a registry that is
# populated by heaters.load_config(), which loads ONLY the modules named in
# Klipper's own extras/temperature_sensors.cfg. A third-party module is never on
# that list, so nothing ever imports it and its factory never registers. Klipper
# does load a module named by a config section, though -- so the section exists
# purely to get this file imported. It must appear BEFORE the temperature_sensor
# that uses it, because PrinterTemperatureSensor resolves its sensor_type during
# its own load and sections load in file order. Both live in exhaust_duty.cfg,
# in that order, so the ordering cannot be got wrong by an installer.
#
# IT CANNOT SHUT THE PRINTER DOWN. Every other sensor in Klipper calls
# invoke_shutdown when it leaves min_temp/max_temp, which is right for something
# measuring a heater and wrong for something measuring a fan -- a miswired
# min_temp would let a chart annotation abort a running print. The reported
# value is clamped into the configured window instead, and the clamp is logged
# once rather than every report.

import logging

REPORT_TIME = 1.0


class FanDutySensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        # Full object name, e.g. "fan_generic exhaust_fan". Resolved at ready
        # rather than here: config order is not dependency order, and the fan
        # section may not be loaded yet.
        self.fan_name = config.get("fan")
        self.scale = config.getfloat("scale", 100.0)
        # "speed" reports what the fan was COMMANDED; "rpm" reports what the
        # tachometer measured, normalised against rpm_max so both land on the
        # same 0-100 scale and can share an axis.
        #
        # They are not the same curve and are not meant to be. Measured on
        # Zero2 across 47847 samples, the exhaust runs 2127 rpm at duty 0.10 --
        # 29% of its 7310 rpm full-duty median, not 10%. A DC fan has a spin-up
        # offset rather than a proportional response, so the commanded trace
        # understates airflow at the bottom of the range and the two lines
        # separate most exactly where the assist lives.
        #
        # The measured one is also the only one that can show a fan that was
        # commanded and did not turn, which is the failure the ch_minfan floor
        # exists to avoid and which the commanded trace structurally cannot see.
        self.source = config.getchoice("source",
                                       {"speed": "speed", "rpm": "rpm"},
                                       "speed")
        # REQUIRED for source: rpm, with no default on purpose. configfile
        # returns a default WITHOUT validating it (configfile.py _get_wrapper
        # returns early), so `above=0.0` would not catch an omitted value and
        # the normalisation would divide by zero at the first sample. Omitting
        # it now raises at config time with the option named.
        self.rpm_max = (config.getfloat("rpm_max", above=0.0)
                        if self.source == "rpm" else 0.0)
        self.fan = None
        self.warned = False
        self.temp = 0.0
        self.min_temp = 0.0
        self.max_temp = 0.0
        self._callback = None
        self.sample_timer = self.reactor.register_timer(self._sample)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        try:
            self.fan = self.printer.lookup_object(self.fan_name)
        except Exception:
            # Not fatal. A pack that ships this alongside an optional chamber
            # mod should not refuse to start because the mod is not installed.
            logging.warning("fan_duty %s: no such object '%s', reporting 0",
                            self.name, self.fan_name)
            self.fan = None
        self.reactor.update_timer(self.sample_timer, self.reactor.NOW)

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, cb):
        self._callback = cb

    def get_report_time_delta(self):
        return REPORT_TIME

    def _read_duty(self, eventtime):
        if self.fan is None:
            return 0.0
        try:
            status = self.fan.get_status(eventtime)
        except Exception:
            return 0.0
        if self.source == "rpm":
            # A fan with no tach reports rpm None rather than 0. That is
            # "unknown", and reporting it as 0 would draw a stalled fan that is
            # actually running, so it holds the last value instead.
            rpm = status.get("rpm")
            if rpm is None:
                return self.temp
            return float(rpm) / self.rpm_max * self.scale
        # fan_generic and fan both report `speed`; temperature_fan reports it
        # too, so this works whatever the exhaust happens to be configured as.
        return float(status.get("speed", 0.0)) * self.scale

    def _sample(self, eventtime):
        value = self._read_duty(eventtime)
        clamped = min(max(value, self.min_temp), self.max_temp)
        if clamped != value and not self.warned:
            self.warned = True
            logging.warning(
                "fan_duty %s: %.1f outside [%.1f, %.1f], clamping. This sensor "
                "never shuts down the printer; widen min_temp/max_temp to see "
                "the real trace.", self.name, value, self.min_temp,
                self.max_temp)
        self.temp = clamped
        if self._callback is not None:
            mcu = self.printer.lookup_object('mcu')
            measured_time = self.reactor.monotonic()
            self._callback(mcu.estimated_print_time(measured_time), self.temp)
        return self.reactor.monotonic() + REPORT_TIME

    def get_status(self, eventtime):
        return {'temperature': round(self.temp, 2)}


class FanDutyRegistrar:
    """Exists only so [fan_duty] is a loadable section. See the note above."""
    def __init__(self, config):
        pheaters = config.get_printer().load_object(config, "heaters")
        pheaters.add_sensor_factory("fan_duty", FanDutySensor)


def load_config(config):
    # Must return an object: klippy stores the return value as the printer
    # object for this section.
    return FanDutyRegistrar(config)
