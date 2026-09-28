#!/usr/bin/env python3
"""Install generated presets into a sandbox datadir so they appear on launch.

Both slicers read presets at startup, so this must run BEFORE the app starts.
Neither needs a GUI import once the files are in the right place — that was the
whole point of confirming the on-disk layouts.

    ./install-presets.py prusa <datadir> [--no-shrink]
    ./install-presets.py orca  <datadir> [--no-shrink] [--chamber-shadow-test]

--personal installs this operator's printer connections and disables fallback
skirts in the installed presets because PURGE_LINE is available there. Do not
use it for a stock printer; public generated bundles retain their skirts.

--no-shrink zeroes the filament shrinkage compensation in the INSTALLED presets
only, leaving the pack itself untouched.

--accel-cap N caps every acceleration in the installed presets at N mm/s^2. This
is a SAFETY control, not a preference. Klipper's SET_VELOCITY_LIMIT does not
clamp -- toolhead.py assigns self.max_accel outright -- so a sliced file asking
ACCEL=40000 silently RAISES a machine whose printer.cfg says 5000. Lowering
max_accel in printer.cfg does not protect a machine from its own slicer. Zero2
runs a reduced 5000 after the 2026-08-17 crash and needs --accel-cap 5000.

Shrinkage compensation is not universally desirable, and the split is not about
confidence in the number. It helps a standalone dimensional part. It actively
hurts anything that mates with other people's prints -- Gridfinity, Voron parts,
any published system -- because every baseplate and bracket out there was printed
with no compensation at all. Scaling up by 0.3% puts a 5-wide Gridfinity bin
about 0.6 mm out against the 42 mm pitch, which is the difference between
seating and binding.

So the pack ships the nominal values and this flag turns them off for the prints
that want raw dimensions.

WHY THE FIRST ATTEMPT SHOWED NOTHING

*PrusaSlicer*: a config **bundle** only imports through the GUI. Its storage is
one flat .ini per preset under print/ filament/ printer/ physical_printer/. The
bundle was staged but never imported, so those directories stayed empty.

*OrcaSlicer*: presets were in the right directory but their parents did not
exist. Orca had only the Custom, OrcaFilamentLibrary and Prusa vendors installed
— no Sovol — so `inherits: SOVOL ZERO 0.4 nozzle` resolved to nothing and the
presets were dropped silently. A preset with an unresolvable parent produces no
error; it simply never appears. Orca also needs the printer model listed in
`models` in OrcaSlicer.conf, which is what the setup wizard normally writes.
"""
import collections, json, os, shutil, sys

# The Orca start G-code lives in start_gcode.py so generate.py and
# install-presets.py cannot drift apart again. Tests load these scripts by path
# with importlib, which does not put this directory on sys.path, so add it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start_gcode import (  # noqa: E402
    ORCA_CHAMBER_BLOCK as ORCA_NORMAL_CHAMBER_START,
    ORCA_SHADOW_CHAMBER_BLOCK as ORCA_SHADOW_CHAMBER_START,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# Reported by PrusaSlicer itself, and present inside the flatpak runtime. Not
# host-side: this machine has no /etc/pki/tls, so probing the host would silently
# choose a different bundle than the one the application sees.
PS_CERT_STORE = "/etc/pki/tls/certs/ca-bundle.crt"
# SuperSlicer runs its ConfigWizard whenever its ini has no `version`,
# regardless of how many presets are on disk. Read from the model so the
# stamp and the emitters cannot disagree about which release this targets.
SS_VERSION = json.load(open(os.path.join(ROOT, "source", "model.json"),
                           encoding="utf-8"))["targets"]["superslicer"]["version"]


def set_ini_keys(path, section, want):
    """Set keys inside one section of a PrusaSlicer.ini, creating what is absent.

    `section=None` targets the leading, unnamed block that PrusaSlicer writes its
    application settings into, before the first [section] header.

    Section-aware on purpose. The previous version rewrote any line whose key
    matched, anywhere in the file, and appended misses at the very end — outside
    every section, where PrusaSlicer ignores them. It happened to work because
    [presets] already contained the keys. It would not have survived a fresh
    datadir.
    """
    lines = open(path, encoding="utf-8").read().splitlines(True) \
        if os.path.exists(path) else []
    out, seen, cur, done = [], set(), None, False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if cur == section:                       # leaving ours: flush misses
                out += ["%s = %s\n" % (k, v) for k, v in want.items() if k not in seen]
                done = True
            cur = s[1:-1]
        elif cur == section and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in want:
                out.append("%s = %s\n" % (k, want[k])); seen.add(k); continue
        out.append(line)
    if cur == section:
        out += ["%s = %s\n" % (k, v) for k, v in want.items() if k not in seen]
    elif not done:
        out += ["\n[%s]\n" % section] + ["%s = %s\n" % kv for kv in want.items()]
    open(path, "w", encoding="utf-8").writelines(out)


def zero_shrinkage(datadir, kinds=("filament",), orca=False):
    """Zero the shrinkage compensation in already-installed presets."""
    n = 0
    if orca:
        import glob as _glob
        for f in _glob.glob(os.path.join(datadir, "system", "SVZero", "filament", "*.json")):
            d = json.load(open(f, encoding="utf-8"))
            if "filament_shrink" in d:
                d["filament_shrink"] = ["100%"]          # 100% == no compensation
                json.dump(d, open(f, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                n += 1
        return n
    for kind in kinds:
        d = os.path.join(datadir, kind)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            path = os.path.join(d, f)
            out, hit = [], False
            for line in open(path, encoding="utf-8"):
                k = line.split("=", 1)[0].strip()
                if k.startswith("filament_shrinkage_compensation"):
                    out.append("%s = 0%%\n" % k); hit = True
                else:
                    out.append(line)
            if hit:
                open(path, "w", encoding="utf-8").writelines(out); n += 1
    return n


def cap_acceleration_orca(datadir, cap):
    """Same cap, for Orca's JSON process presets.

    Orca stores presets as JSON under system/<vendor>/process, not as .ini, so
    the PrusaSlicer path silently caps nothing here -- and this is the more
    dangerous of the two, because it is the profile that would raise a machine
    deliberately limited to 5000 back up to 40000.
    """
    import glob as _glob
    n = 0
    # BOTH preset types, and both key shapes. The process presets carry
    # *_acceleration; the MACHINE presets carry machine_max_acceleration_x/_y/
    # _extruding/_travel/_e, none of which end in "_acceleration". Orca's Klipper
    # flavour emits the machine limits as SET_VELOCITY_LIMIT, so capping only the
    # process values leaves ACCEL=40000 in the sliced file -- which is exactly
    # what shipped, and what raises a machine deliberately limited to 5000.
    files = (_glob.glob(os.path.join(datadir, "system", "SVZero", "process", "*.json"))
             + _glob.glob(os.path.join(datadir, "system", "SVZero", "machine", "*.json")))
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        hit = False
        for k, v in list(d.items()):
            if not (k.endswith("_acceleration") or k.startswith("machine_max_acceleration")):
                continue
            vals = v if isinstance(v, list) else [v]
            out = []
            for x in vals:
                try:
                    out.append("%g" % cap if float(str(x)) > cap else str(x))
                    hit = hit or float(str(x)) > cap
                except ValueError:
                    out.append(x)
            d[k] = out if isinstance(v, list) else out[0]
        if hit:
            json.dump(d, open(f, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
            n += 1
    return n


def cap_acceleration(datadir, cap, kinds=("print",)):
    """Cap every acceleration in installed presets. See the module docstring."""
    n = 0
    for kind in kinds:
        d = os.path.join(datadir, kind)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            path, out, hit = os.path.join(d, f), [], False
            for line in open(path, encoding="utf-8"):
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k.endswith("_acceleration") and not v.endswith("%"):
                    try:
                        if float(v) > cap:
                            out.append("%s = %g\n" % (k, cap)); hit = True; continue
                    except ValueError:
                        pass
                out.append(line)
            if hit:
                open(path, "w", encoding="utf-8").writelines(out); n += 1
    return n


def install_orca_hosts(datadir):
    """Put the operator's machines back after the user-preset wipe. PERSONAL ONLY.

    install_orca clears user/default/{process,machine,filament} because those
    were the pre-vendor mechanism and would otherwise shadow the vendor presets
    under the same names. But Orca keeps a PHYSICAL PRINTER as a user machine
    preset -- host and all -- so that clear also deleted the operator's own
    printer, every single launch. Reported 2026-09-07: "my actual zero2 printer
    is no longer present" in both orca and orca-shadow, while PrusaSlicer and
    SuperSlicer still had theirs.

    Preserving it across the clear is the wrong fix: it would depend on the
    printer having been added by hand once, on this datadir, and a reset would
    lose it again. Writing it from source/model.json makes it reproducible, the
    same way the PrusaSlicer and SuperSlicer physical printers are.

    Orca has no separate physical-printer object: host_type and print_host live
    ON the machine preset. So the default printer is stamped onto every
    installed system machine, and each additional printer gets one user preset
    inheriting the 0.4 nozzle machine.
    """
    hosts = json.load(open(os.path.join(ROOT, "source", "model.json"),
                           encoding="utf-8")).get("hosts")
    if not hosts:
        return 0, 0
    htype = hosts.get("orcaslicer_host_type", "octoprint")
    printers = hosts.get("printers", [])
    default = next((p for p in printers if p.get("default")), printers[0] if printers else None)
    if not default:
        return 0, 0
    sysm = os.path.join(datadir, "system", "SVZero", "machine")
    stamped = 0
    for f in sorted(os.listdir(sysm)) if os.path.isdir(sysm) else []:
        if not f.endswith(".json") or f.startswith("fdm_"):
            continue
        path = os.path.join(sysm, f)
        doc = json.load(open(path, encoding="utf-8"))
        if "nozzle" not in doc.get("name", ""):
            continue
        doc["host_type"] = htype
        doc["print_host"] = default["host"]
        json.dump(doc, open(path, "w", encoding="utf-8"), indent=2)
        stamped += 1
    # Every printer that is not the default gets its own user machine preset.
    userm = os.path.join(datadir, "user", "default", "machine")
    os.makedirs(userm, exist_ok=True)
    extra = 0
    for pr in printers:
        if pr is default:
            continue
        doc = {
            "type": "machine", "name": pr["name"], "from": "User",
            "inherits": "SV Zero 0.4 nozzle", "instantiation": "true",
            "host_type": htype, "print_host": pr["host"],
            "printhost_apikey": "", "version": "2.4.2.0",
        }
        json.dump(doc, open(os.path.join(userm, "%s.json" % pr["name"]), "w",
                            encoding="utf-8"), indent=2)
        extra += 1
    return stamped, extra


def install_bed_assets(datadir):
    """Copy the bed model and texture in, and point the presets at them.

    `bed_custom_model = svzero_bed.stl` is a bare filename. A vendor bundle
    resolves that against its own resource directory; a USER preset has no such
    directory, so both slicers silently show a plain rectangle -- which is what
    SuperSlicer was doing on 2026-09-07 and PrusaSlicer had been doing all along
    without anyone noticing, because the plate is the same size either way.

    Copied beside the presets and rewritten to an absolute path, which is what
    the slicers' own file dialog would have written.
    """
    n = 0
    for name in ("svzero_bed.stl", "svzero_bed.svg", "svzero_bed.CREDITS.md"):
        src = os.path.join(ROOT, "assets", name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(datadir, name))
            n += 1
    if not n:
        return 0
    d = os.path.join(datadir, "printer")
    patched = 0
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        path, out, hit = os.path.join(d, f), [], False
        for line in open(path, encoding="utf-8"):
            k, _, v = line.partition("=")
            if k.strip() in ("bed_custom_model", "bed_custom_texture"):
                base = os.path.basename(v.strip())
                if base:
                    out.append("%s = %s\n" % (k.strip(),
                                              os.path.join(datadir, base)))
                    hit = True
                    continue
            out.append(line)
        if hit:
            open(path, "w", encoding="utf-8").writelines(out)
            patched += 1
    return patched


def install_physical_printers(datadir, slicer):
    """Write the operator's own machines into a sandbox datadir. PERSONAL ONLY.

    A [physical_printer:] section exists solely to carry an address, so there is
    nothing to publish -- a blank one would just be a broken entry in a
    stranger's printer list, and a filled one is a map of this house.

    DELIBERATELY NOT ROUTED THROUGH bundles/. generate.py has a --personal flag
    that puts these into the emitted preset tree, and that is one careless
    `--emit ps-presets` away from committing private addresses into an artefact
    directory that gets published. Injecting at INSTALL time means the addresses
    live in exactly two places -- source/model.json and a sandbox datadir --
    and bundles/ is address-free by construction rather than by discipline.

    PrusaSlicer and SuperSlicer both have a native `moonraker` host_type;
    SuperSlicer's binary carries the string, checked rather than assumed. Orca
    has none and uses octoprint against the same endpoint, which is why the
    model states the two separately.
    """
    hosts = json.load(open(os.path.join(ROOT, "source", "model.json"),
                           encoding="utf-8")).get("hosts")
    if not hosts:
        return 0, None
    d = os.path.join(datadir, "physical_printer")
    os.makedirs(d, exist_ok=True)
    for old in os.listdir(d):
        if old.endswith(".ini"):
            os.remove(os.path.join(d, old))
    # Every printer preset actually installed, so the machine offers all five
    # nozzles rather than only the one that happened to be default.
    pdir = os.path.join(datadir, "printer")
    names = sorted(os.path.splitext(f)[0] for f in os.listdir(pdir)
                   if f.endswith(".ini")) if os.path.isdir(pdir) else []
    default_preset = next((n for n in names if "0.4n" in n),
                          names[0] if names else "SV Zero 0.4n")
    default_printer = None
    n = 0
    for pr in hosts["printers"]:
        # Key set and quoting taken from a real PrusaSlicer export -- Tom's
        # Print Garden's bundle -- not from what seemed sufficient. Four keys
        # were missing on the first attempt (cafile, password,
        # ssl_ignore_revoke, user), and that export quotes a preset name
        # containing a space, which all of ours do.
        kv = collections.OrderedDict([
            ("host_type", hosts["prusaslicer_host_type"]),
            ("preset_name", default_preset),
            ("preset_names", ";".join('"%s"' % x for x in names)),
            ("print_host", pr["host"]),
            ("printer_technology", "FFF"),
            ("printhost_apikey", ""),
            ("printhost_authorization_type", "key"),
            ("printhost_cafile", ""),
            ("printhost_password", ""),
            ("printhost_port", ""),
            ("printhost_ssl_ignore_revoke", "0"),
            ("printhost_user", ""),
        ])
        with open(os.path.join(d, "%s.ini" % pr["name"]), "w", encoding="utf-8") as fh:
            for k, v in kv.items():
                fh.write("%s = %s\n" % (k, v))
        if pr.get("default") or default_printer is None:
            default_printer = "%s * %s" % (pr["name"], default_preset)
        n += 1
    return n, default_printer


def install_superslicer(datadir):
    """Install flattened SuperSlicer presets, in SuperSlicer's own dialect.

    The sandbox used to stage the config bundle and print an import instruction.
    That left a fresh datadir with no printer at all: the ConfigWizard runs,
    offers only the vendors SuperSlicer ships, and clicking through it produces a
    Prusa machine and no SV Zero. The bundle sat in staged/ waiting for a dialog
    nobody had been told was mandatory.

    Same fix as PrusaSlicer -- write the presets straight into the datadir --
    with one thing that must NOT be shared. `--emit ps-presets` resolves widths
    against the nozzle and speeds against `default_speed`, because PrusaSlicer
    has no `default_speed` and reads a bare percentage differently. SuperSlicer
    means those percentages literally, so installing the PrusaSlicer files here
    would silently re-author every preset. `--emit ss-presets` is the same split
    with none of those translations, and this installs that.
    """
    src = os.path.join(ROOT, "bundles", "ss-presets")
    if not os.path.isdir(src):
        raise SystemExit("no bundles/ss-presets -- run: ./tools/generate.py --emit ss-presets")
    n = 0
    kinds = ("print", "filament", "printer", "physical_printer")
    for kind in kinds:
        d = os.path.join(datadir, kind)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.startswith("SV Zero") or f.startswith("*SV Zero") or f.startswith("Zero"):
                os.remove(os.path.join(d, f))
    for kind in kinds:
        s_, d_ = os.path.join(src, kind), os.path.join(datadir, kind)
        os.makedirs(d_, exist_ok=True)
        for f in sorted(os.listdir(s_)) if os.path.isdir(s_) else []:
            shutil.copy2(os.path.join(s_, f), os.path.join(d_, f)); n += 1

    # SuperSlicer is a PrusaSlicer fork and keeps the same ini layout, but under
    # its own filename. Writing PrusaSlicer.ini here would be silently ignored.
    ini = os.path.join(datadir, "SuperSlicer.ini")
    set_ini_keys(ini, None, {
        "preset_update": "0",
        "show_splash_screen": "0",
        "show_hints": "0",
        "version_check": "0",
        "notify_release": "none",
        "version_system_info_sent": "99.99.99",
        # The wizard is what this whole function exists to avoid. SuperSlicer
        # runs it when the ini has no `version`, treating that as a first launch
        # no matter how many presets are on disk. Stamped with the version the
        # model says the emitters target.
        "version": SS_VERSION,
        # Expert mode. The sandbox exists to look at settings; simple mode
        # hides most of them, including every speed field this pack tunes.
        "view_mode": "expert",
    })
    set_ini_keys(ini, "presets", {
        "printer": "SV Zero 0.4n",
        "print": "SV Zero 0.4n - 0.20mm (Standard)",
        "filament": "SV Zero PLA - Brass",
    })
    return n


def install_prusa(datadir):
    """Install flattened user presets. NOT a vendor bundle -- see below.

    generate.py writes a correct PrusaSlicer vendor bundle (`--emit ps-vendor`)
    and it is the architecturally right shape: system presets, hidden `*Base*`
    parents, a printer model in the wizard. It cannot be installed unattended.
    PrusaSlicer 2.9.6 discards any `[vendor:...]` section written into
    PrusaSlicer.ini by hand, so a vendor is only ever enabled by clicking through
    the ConfigWizard.

    That was verified rather than guessed. A hand-written `[vendor:PrusaResearch]`
    naming a model from a bundle PrusaSlicer ships itself was discarded on the
    next launch exactly like ours, so it is PrusaSlicer's behaviour, not a defect
    in the SVZero bundle.

    So the presets are flattened at generation time and installed as ordinary
    user presets: no `*Base*` presets to appear in the dropdown, no inheritance
    needing a system parent, and no wizard on a fresh sandbox.
    """
    src = os.path.join(ROOT, "bundles", "ps-presets")
    n = 0

    # Clear ours first. Renaming or dropping a preset otherwise leaves the old
    # file behind and it keeps showing up -- which is how the *Base* presets
    # survived the move to flattened output.
    for kind in ("print", "printer", "filament", "physical_printer"):
        d = os.path.join(datadir, kind)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.startswith("SV Zero") or f.startswith("*SV Zero") or f.startswith("Zero"):
                os.remove(os.path.join(d, f))

    for kind in ("print", "filament", "printer", "physical_printer"):
        s_, d_ = os.path.join(src, kind), os.path.join(datadir, kind)
        os.makedirs(d_, exist_ok=True)
        for f in sorted(os.listdir(s_)) if os.path.isdir(s_) else []:
            shutil.copy2(os.path.join(s_, f), os.path.join(d_, f)); n += 1

    # A stale vendor/SVZero.ini would reintroduce the *Base* presets as system
    # presets the moment the wizard was ever completed.
    for f in ("SVZero.ini", "SVZero.idx"):
        p = os.path.join(datadir, "vendor", f)
        if os.path.exists(p):
            os.remove(p)

    # One-time dialogs that a throwaway datadir would otherwise ask on EVERY
    # launch, which defeats the point of a sandbox. Answered here rather than
    # clicked.
    #
    # tls_* is the SSL certificate-store prompt. The pair matters: PrusaSlicer
    # remembers that a SPECIFIC store was accepted, so setting "accepted" without
    # the location it was accepted for leaves it asking again. The path is the
    # one PrusaSlicer itself reports, and it is resolved inside the FLATPAK
    # RUNTIME, not on the host -- the host here has no /etc/pki/tls at all, which
    # is why a host-side existence check would have picked the wrong file.
    #
    # preset_update is off because this datadir is for testing OUR bundle. Left
    # on, PrusaSlicer fetches vendor indices on launch and may update vendor
    # profiles underneath the thing being tested -- exactly what Orca's updater
    # already does to the Sovol vendor.
    set_ini_keys(os.path.join(datadir, "PrusaSlicer.ini"), None, {
        "tls_cert_store_accepted": "yes",
        "tls_accepted_cert_store_location": PS_CERT_STORE,
        "preset_update": "0",
        "show_splash_screen": "0",
        "show_hints": "0",
        "wifi_config_dialog_declined": "1",
        # Expert mode. The sandbox exists to look at settings; simple mode
        # hides most of them, including every speed field this pack tunes.
        "view_mode": "expert",

        # Telemetry and phone-home, off.
        #   notify_release  = none  -- "Don't notify about new releases any more".
        #                              The other values are "all" and "release".
        #   version_system_info_sent is a VERSION, not a flag: PrusaSlicer sends
        #                              system info once per version and records
        #                              which one it sent for. A sentinel above any
        #                              real version means the comparison never
        #                              triggers, and unlike pinning the current
        #                              version it does not need updating on every
        #                              upgrade.
        # preset_update above already stops the vendor-index fetch.
        "notify_release": "none",
        "version_system_info_sent": "99.99.99",
    })
    set_ini_keys(os.path.join(datadir, "PrusaSlicer.ini"), "presets", {
        "printer": "SV Zero 0.4n",
        "print": "SV Zero 0.4n - 0.20mm (Standard)",
        # PLA, not the ABS that alphabetical order was selecting.
        "filament": "SV Zero PLA - Brass",
        # physical_printer is NOT set here. It used to name "Zero2 * SV Zero
        # 0.4n" unconditionally, which pointed at a printer that only exists
        # when --personal wrote one -- a dangling selection otherwise.
        # install_physical_printers sets it, and only when it made one.
    })
    return n


def install_orca(datadir):
    # 0. our own vendor bundle. Loose user presets cannot work: Orca validates a
    #    machine preset against a registered machine_model, and Sovol's SOVOL
    #    ZERO model declares nozzle_diameter "0.4" only, so 0.2/0.6/0.8/1.0
    #    presets are dropped silently. Our vendor declares all five.
    sysdir0 = os.path.join(datadir, "system")
    os.makedirs(sysdir0, exist_ok=True)
    ours_dir = os.path.join(ROOT, "bundles", "orca-vendor", "SVZero")
    ours = os.path.join(ROOT, "bundles", "orca-vendor")
    if os.path.isdir(ours):
        for item in os.listdir(ours):
            s_, d_ = os.path.join(ours, item), os.path.join(sysdir0, item)
            if os.path.isdir(s_):
                shutil.rmtree(d_, ignore_errors=True); shutil.copytree(s_, d_)
            else:
                shutil.copy2(s_, d_)

    # The SVZero vendor is self-contained. Do not install unrelated vendors.

    # 2. clear the user copies. These were the pre-vendor mechanism and are now
    #    the same presets under the same names in two places at once; a user
    #    preset shadows the system one it collides with, which would put us back
    #    to unresolved parents while the vendor sits there looking correct.
    #    Same reasoning as install_prusa. Removed by name, never wholesale.
    u = os.path.join(datadir, "user", "default")
    n = 0
    for sub in ("process", "machine", "filament"):
        p = os.path.join(u, sub)
        for f in sorted(os.listdir(p)) if os.path.isdir(p) else []:
            if "SV Zero" in f:
                os.remove(os.path.join(p, f)); n += 1

    # 3. register the printer model and select it — what the wizard would do
    conf = os.path.join(datadir, "OrcaSlicer.conf")
    d = json.load(open(conf, encoding="utf-8")) if os.path.exists(conf) else {}
    models = d.get("models", [])
    models = [m for m in models if m.get("vendor") != "SVZero"]
    models.insert(0, {"model": "SV Zero",
                      "nozzle_diameter": "0.2;0.4;0.6;0.8;1.0",
                      "vendor": "SVZero"})
    models.insert(1, {"model": "SOVOL ZERO", "nozzle_diameter": "0.4",
                      "vendor": "Sovol"})
    d["models"] = models
    # Orca calls the process preset "print" in the conf. Left unset it opens on
    # nothing, which reads as "our profiles are missing" even when they loaded.
    d.setdefault("presets", {})["machine"] = "SV Zero 0.4 nozzle"
    d["presets"]["print"] = "0.20mm Standard @SV Zero 0.4 nozzle"
    # "filaments", plural, and a list -- the selection is per extruder. The
    # singular "filament" is silently ignored, and Orca then opens on
    # Generic PLA @System from its own library while our presets sit there loaded
    # but unselected.
    d["presets"]["filaments"] = ["SV Zero PLA - Brass"]

    # Orca keeps a separate "installed filaments" list, normally populated by the
    # setup wizard, and a preset that parses and loads is still unselectable if it
    # is not in this list. But UNIONING ours into the stock list is what made the
    # dropdown unusable: Orca ships ~292 entries, ours land alphabetically among
    # them, and the combo box becomes a wall the user cannot scroll to the bottom
    # of. They were visible in "set filaments to use" and effectively absent from
    # the dropdown.
    #
    # A profile pack should narrow this list, not widen it. Ours plus a short
    # generic fallback set, so the dropdown is short and ours are the obvious
    # choice. Anything removed here is one checkbox away in the same dialog.
    ours = sorted(f[:-5] for f in os.listdir(os.path.join(ours_dir, "filament"))
                  if f.endswith(".json") and not f.startswith("fdm_"))
    # `or []` rather than a get() default: Orca writes "filaments": null -- not a
    # missing key -- when it has no filaments to record, which is exactly the
    # state a dead vendor bundle leaves behind. get(k, []) returns the None, so
    # the broken bundle crashed the tool meant to reinstall it. Same trap as
    # Jinja's `in printer` versus `|default`: the key existing says nothing about
    # the value being usable.
    keep = [x for x in (d.get("filaments") or [])
            if x.startswith(("Generic PLA", "Generic PETG", "Generic ABS", "Generic TPU"))]
    d["filaments"] = ours + sorted(set(keep))

    d["firstguide"] = {"finish": True}          # do not re-run the setup wizard
    d.setdefault("app", {})["show_unsupported_presets"] = "1"
    json.dump(d, open(conf, "w", encoding="utf-8"), indent=1)
    return n





def enable_orca_chamber_shadow_test(datadir):
    """Patch installed sandbox machines, never the generated publication bundle."""
    machine_dir = os.path.join(datadir, "system", "SVZero", "machine")
    changed = 0
    for filename in sorted(os.listdir(machine_dir)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(machine_dir, filename)
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        start = doc.get("machine_start_gcode", "")
        if ORCA_NORMAL_CHAMBER_START not in start:
            continue
        doc["machine_start_gcode"] = start.replace(
            ORCA_NORMAL_CHAMBER_START, ORCA_SHADOW_CHAMBER_START, 1)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
        changed += 1
    if changed == 0:
        raise RuntimeError("normal Orca chamber start block not found")
    return changed


def disable_personal_skirts(datadir, which):
    """Operator-only: PURGE_LINE is installed, so no fallback skirt is needed.

    Published profiles keep their priming skirt. Like physical-printer hosts,
    this preference is applied only to the installed personal sandbox.
    """
    from pathlib import Path
    root = Path(datadir)
    count = 0
    if which == "orca":
        for path in (root / "system/SVZero/process").glob("*.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if "@SV Zero " not in doc.get("name", ""):
                continue
            doc.update(skirt_loops="0", min_skirt_length="0")
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            count += 1
    else:
        for path in (root / "print").glob("SV Zero *.ini"):
            set_ini_keys(str(path), None, {"skirts": "0", "min_skirt_length": "0"})
            count += 1
    return count


if __name__ == "__main__":
    argv = sys.argv[1:]
    args = [a for i, a in enumerate(argv)
            if not a.startswith("--") and not (i and argv[i-1].startswith("--accel-cap"))]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--personal" in flags:
        sys.exit("--personal is not part of the public installer; configure your own connection in the slicer")
    personal = False
    if len(args) != 2 or args[0] not in ("prusa", "orca", "superslicer"):
        sys.exit(__doc__)
    which, datadir = args
    os.makedirs(datadir, exist_ok=True)
    n = {"prusa": install_prusa, "orca": install_orca,
         "superslicer": install_superslicer}[which](datadir)
    print("installed %d presets into %s" % (n, datadir))
    if which in ("prusa", "superslicer"):
        nb = install_bed_assets(datadir)
        print("pointed %d printer preset(s) at the bed model and texture" % nb)
    if personal and which == "orca":
        st, ex = install_orca_hosts(datadir)
        print("stamped %d Orca machine(s) with the default host, %d extra printer(s)"
              % (st, ex))
    if personal and which in ("prusa", "superslicer"):
        # PERSONAL ONLY, and never written into bundles/. See the function.
        npp, default_printer = install_physical_printers(datadir, which)
        ini = "PrusaSlicer.ini" if which == "prusa" else "SuperSlicer.ini"
        if npp and default_printer:
            set_ini_keys(os.path.join(datadir, ini), "presets",
                         {"physical_printer": default_printer})
        print("installed %d physical printer(s); default %s" % (npp, default_printer))
    if which == "orca" and "--chamber-shadow-test" in flags:
        c = enable_orca_chamber_shadow_test(datadir)
        print("enabled chamber shadow test in %d Orca machine presets" % c)
        print("selected base PLA chamber profile at exact 33/33")
    if personal:
        nsk = disable_personal_skirts(datadir, which)
        print("disabled fallback skirts in %d personal presets (requires PURGE_LINE)" % nsk)
    caps = [f for f in flags if f.startswith("--accel-cap")]
    if caps:
        try:
            cap = float(sys.argv[sys.argv.index(caps[0]) + 1])
        except (ValueError, IndexError):
            sys.exit("--accel-cap needs a number, e.g. --accel-cap 5000")
        # Percentage accelerations are skipped by cap_acceleration, which is
        # what makes it safe on SuperSlicer's dialect as well as PrusaSlicer's.
        c = (cap_acceleration_orca(datadir, cap) if which == "orca"
             else cap_acceleration(datadir, cap))
        print("capped acceleration at %g in %d presets" % (cap, c))
    if "--no-shrink" in flags:
        z = zero_shrinkage(datadir, orca=(which == "orca"))
        print("zeroed shrinkage compensation in %d presets" % z)
