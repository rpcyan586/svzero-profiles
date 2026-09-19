"""Bounded startup camera/strip tuning; no homing, motion or heater commands.

Call from START_PRINT while the bed settles, after the normal nozzle-cleaning
sequence has homed and parked. Camera HTTP runs off the reactor via the existing
MoonrakerClient. LED macros run on the command's own G-code context, so nothing
is queued behind M109/M190 to execute unexpectedly later.
"""
import json
import logging
import uuid
from . import moonraker


class CameraTune:
    def __init__(self, config):
        self.printer=config.get_printer();self.reactor=self.printer.get_reactor()
        self.gcode=self.printer.lookup_object('gcode')
        self.url=config.get('service_url','http://127.0.0.1:8091')
        if self.url!='http://127.0.0.1:8091':raise config.error('camera_tune service must be local port 8091')
        self.budget=config.getfloat('max_seconds',35.,minval=20.,maxval=50.)
        self.enabled=config.getboolean('enabled',True)
        self.active=False;self.run_id='';self.last_result={'status':'not-run'}
        self.white=None
        self.gcode.register_command('CAMERA_TUNE',self.cmd_CAMERA_TUNE,desc='Tune manual exposure and white strip before printing, without motion')
        self.gcode.register_command('CAMERA_DEFAULT',self.cmd_CAMERA_DEFAULT,desc='Apply bounded manual print-camera defaults, without motion')

    def get_status(self,eventtime):
        return dict(active=self.active,run_id=self.run_id,last_result=self.last_result)

    def request(self,exposure,apply_only=False):
        client=moonraker.MoonrakerClient(self.reactor,self.url,7.)
        return client.run([('camera','/measure',dict(run_id=self.run_id,exposure=exposure,white=self.white,apply_only=apply_only))])['camera']

    def light(self,white):
        self.gcode.run_script_from_command('SET_LED LED=nozzle_light RED=0 GREEN=0 BLUE=0 WHITE=%.3f SYNC=0'%white)
        self.white=white

    def start(self,gcmd):
        if self.active:raise gcmd.error('Camera tuning is already active')
        state=self.printer.lookup_object('print_stats').get_status(self.reactor.monotonic())['state']
        if state in ('printing','paused') and not gcmd.get_int('STARTUP',0):
            raise gcmd.error('Camera tuning during a job is reserved for the START_PRINT startup hook')
        self.active=True;self.run_id=uuid.uuid4().hex

    def cmd_CAMERA_DEFAULT(self,gcmd):
        if not self.enabled:return
        self.start(gcmd)
        try:
            self.light(1.);self.request(40,True)
            self.last_result=dict(status='default',white=1.,exposure=40)
            gcmd.respond_info('camera: manual exposure 40, white strip 1.0')
        except Exception as e:
            self.last_result=dict(status='default-unavailable',error=str(e))
            logging.exception('camera default unavailable')
            gcmd.respond_info('camera: default unavailable, printing may continue with current camera settings')
        finally:self.active=False

    def cmd_CAMERA_TUNE(self,gcmd):
        if not self.enabled:return
        self.start(gcmd);rows=[];started=self.reactor.monotonic()
        selected=dict(white=1.,exposure=40,status='default')
        try:
            self.printer.lookup_object('toolhead').wait_moves()
            self.light(1.);self.request(40,True)
            health=moonraker.MoonrakerClient(self.reactor,self.url,2.).run([('health','/health',None)])['health']
            if not health['meter_calibrated']:
                selected['status']='default-meter-uncalibrated'
            else:
                # Cover all three light levels before spending the remaining
                # time refining exposure. A short budget must still tune both.
                for white,exposure in [(1.,40),(.6,60),(.3,130),(1.,20),(1.,60),(.6,40),(1.,90),(.6,90),(.3,90),(1.,130)]:
                    # One sample and the final apply can each consume an 8 s
                    # cooperative HTTP deadline. Reserve both, plus margin.
                    if self.reactor.monotonic()-started>self.budget-17:break
                    self.light(white)
                    try:
                        result=self.request(exposure)
                    except Exception as e:
                        rows.append(dict(white=white,exposure=exposure,error=str(e)));break
                    rows.append(dict(white=white,exposure=exposure,**result))
                eligible=[r for r in rows if r.get('calibrated') and r.get('metrics',{}).get('adequate')]
                if eligible:
                    best=min(eligible,key=lambda r:(r['metrics']['score'],r['exposure'],-r['white']))
                    selected=dict(status='selected',white=best['white'],exposure=best['exposure'],metrics=best['metrics'])
                else:selected['status']='default-no-adequate-sample'
                self.light(selected['white']);self.request(selected['exposure'],True)
            gcmd.respond_info('camera: %s, manual exposure %d, white %.2f'%(selected['status'],selected['exposure'],selected['white']))
        except Exception as e:
            selected=dict(status='interrupted-current-settings-retained',error=str(e))
            logging.exception('camera tune failed')
            gcmd.respond_info('camera: tuning unavailable, current bounded settings retained')
        finally:
            self.active=False
            self.last_result=dict(selected,seconds=self.reactor.monotonic()-started,samples=rows)
            logging.info('camera_tune %s',json.dumps(self.last_result,sort_keys=True))


def load_config(config):
    return CameraTune(config)
