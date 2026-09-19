#!/usr/bin/env python3
"""Exercise startup tuning control flow without a printer or HTTP server."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parent.parent
package=types.ModuleType('camera_test_extras');package.__path__=[str(ROOT/'klipper')]
sys.modules[package.__name__]=package
spec=importlib.util.spec_from_file_location(package.__name__+'.camera_tune',ROOT/'klipper/camera_tune.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class Tests(unittest.TestCase):
    def setup_tuner(self,state='standby',calibrated=True):
        config=Mock();config.get.return_value='http://127.0.0.1:8091';config.getfloat.return_value=35.;config.getboolean.return_value=True
        printer=config.get_printer.return_value;reactor=printer.get_reactor.return_value
        reactor.monotonic.return_value=0.
        gcode=Mock();stats=Mock();stats.get_status.return_value={'state':state};toolhead=Mock()
        printer.lookup_object.side_effect=lambda name:{'gcode':gcode,'print_stats':stats,'toolhead':toolhead}[name]
        tuner=m.CameraTune(config);tuner.request=Mock(side_effect=lambda exposure,apply_only=False:dict(calibrated=True,metrics=dict(adequate=True,score=abs(exposure-60))))
        client=Mock();client.run.return_value={'health':{'meter_calibrated':calibrated}}
        gcmd=Mock();gcmd.get_int.return_value=0;gcmd.error.side_effect=lambda text:ValueError(text)
        return tuner,gcmd,gcode,client

    def test_picks_bounded_winner_and_emits_only_led_commands(self):
        tuner,cmd,gcode,client=self.setup_tuner()
        with patch.object(m.moonraker,'MoonrakerClient',return_value=client):tuner.cmd_CAMERA_TUNE(cmd)
        self.assertFalse(tuner.active);self.assertEqual(tuner.last_result['exposure'],60)
        self.assertEqual(tuner.last_result['white'],1.)
        self.assertEqual(tuner.request.call_args.args,(60,True))
        for call in gcode.run_script_from_command.call_args_list:
            self.assertTrue(call.args[0].startswith('SET_LED LED=nozzle_light RED=0 GREEN=0 BLUE=0 WHITE='))

    def test_uncalibrated_region_uses_explicit_default(self):
        tuner,cmd,gcode,client=self.setup_tuner(calibrated=False)
        with patch.object(m.moonraker,'MoonrakerClient',return_value=client):tuner.cmd_CAMERA_TUNE(cmd)
        self.assertEqual(tuner.last_result['status'],'default-meter-uncalibrated')
        tuner.request.assert_called_once_with(40,True)

    def test_unacceptable_samples_do_not_choose_highest_contrast(self):
        tuner,cmd,gcode,client=self.setup_tuner()
        tuner.request.side_effect=lambda *a,**k:dict(calibrated=True,metrics=dict(adequate=False,score=0))
        with patch.object(m.moonraker,'MoonrakerClient',return_value=client):tuner.cmd_CAMERA_TUNE(cmd)
        self.assertEqual(tuner.last_result['status'],'default-no-adequate-sample')
        self.assertEqual(tuner.last_result['exposure'],40)

    def test_midprint_manual_command_rejected(self):
        tuner,cmd,gcode,client=self.setup_tuner(state='printing')
        with self.assertRaises(ValueError):tuner.cmd_CAMERA_TUNE(cmd)
        tuner.request.assert_not_called();gcode.run_script_from_command.assert_not_called()
        self.assertFalse(tuner.active)

    def test_service_failure_releases_ownership(self):
        tuner,cmd,gcode,client=self.setup_tuner();tuner.request.side_effect=TimeoutError('offline')
        with patch.object(m.logging,'exception'):tuner.cmd_CAMERA_TUNE(cmd)
        self.assertFalse(tuner.active)
        self.assertEqual(tuner.last_result['status'],'interrupted-current-settings-retained')

    def test_startup_is_explicit_and_uses_no_heater_or_motion_command(self):
        tuner,cmd,gcode,client=self.setup_tuner(state='printing',calibrated=False);cmd.get_int.return_value=1
        with patch.object(m.moonraker,'MoonrakerClient',return_value=client):tuner.cmd_CAMERA_TUNE(cmd)
        self.assertFalse(tuner.active);self.assertEqual(len(gcode.run_script_from_command.call_args_list),1)


if __name__=='__main__':unittest.main()
