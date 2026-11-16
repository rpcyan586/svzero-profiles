# SV Zero profiles

**[Download the latest release — v2.2.0](https://github.com/rpcyan586/svzero-profiles/releases/tag/v2.2.0)**

[OrcaSlicer ZIP](https://github.com/rpcyan586/svzero-profiles/releases/download/v2.2.0/svzero-profiles-2.2.0-orcaslicer.zip) ·
[PrusaSlicer ZIP](https://github.com/rpcyan586/svzero-profiles/releases/download/v2.2.0/svzero-profiles-2.2.0-prusaslicer.zip) ·
[SuperSlicer ZIP](https://github.com/rpcyan586/svzero-profiles/releases/download/v2.2.0/svzero-profiles-2.2.0-superslicer.zip)

[Source in Git](https://github.com/rpcyan586/svzero-profiles) ·
[Checksums](https://github.com/rpcyan586/svzero-profiles/releases/download/v2.2.0/SHA256SUMS) ·
[All releases](https://github.com/rpcyan586/svzero-profiles/releases)

Each ZIP includes its installation guide and the optional printer macro pack.

Profiles for **OrcaSlicer, PrusaSlicer and SuperSlicer** on the
Sovol Zero, and an alpha bundle for PrusaSlicer 3 in Git, not yet a download.
One source generates them all.

Automated checks and slicing have passed;
physical testing of the latest stock-firmware fallback is still pending.
Read [validation and limitations](docs/VALIDATION.md) before testing.

The profiles cover ten nozzle sizes (0.2–1.4 mm), material settings and
Fine/Optimal/Standard/Draft/Strength tiers where supported. An optional
[precision mode](docs/PRECISION.md) adds slow, quiet, fine-detail printing on
the 0.4 nozzle and the smaller ones. Added nozzle sizes
are starting points, not evidence that a nozzle fits or has been flow-calibrated.

<p><img src="docs/images/stock-vs-pack.png" alt="Sovol's one stock profile against the pack's tiers on the 0.4 nozzle: speeds as run with the clamped requests in parentheses, infill flow, peak acceleration, corner velocity, X/Y microsteps and current"></p>

<p><img src="docs/images/nozzle-growth.png" alt="A hot nozzle reaches about 0.14 mm further down at ABS's 271 C first layer than at the 150 C tap, in real degrees: eight tap pairs beside the sensor on one Zero, the published rate of 1.15 um per degree against Sovol stock, which taps at 130 C (127 real) and has no temperature term, so at 271 C it sits 0.15 to 0.17 mm closer, the first-layer lift for each material, and four printed single-layer squares, where the lifts for k 0.70 and 1.05 came out over-squished and the lift for 1.37 came out flat"></p>

<p>
<img src="docs/images/presets.png" width="49%" alt="Every print preset by nozzle and tier, 56 across 10 nozzles, with precision on 0.2 to 0.4">
<img src="docs/images/bed-boost.png" width="49%" alt="Bed boost against none from a 27 C box: 13.3 minutes to the chamber soak against 25.6">
<img src="docs/images/preheat-watts.png" width="49%" alt="Bed watts into the chamber at nine bed positions, against the draw with the part fan off">
<img src="docs/images/exhaust.png" width="49%" alt="The PI-controlled exhaust against the stock rule, with the noise curve on the same fan axis">
<img src="docs/images/cooldown.png" width="49%" alt="The controlled cooldown against the bed switched off, as stock does">
<img src="docs/images/fan-strength.png" width="49%" alt="How much each fan pulls from the plate, behind the cooldown fan weighting">
<img src="docs/images/camera-auto.png" width="49%" alt="camera_auto over four prints on a Zero's stock camera as a desk lamp came and went: each print's end tries up to three exposures with the part in frame and keeps the sharpest per unit of noise under 2% clipped, and its plate brightness becomes the next start's target. With the lamp aimed in, the next start read 79 and stepped back to 44 against 43; with the lamp off it climbed to the 600 exposure cap. Gain stays at 0, and the camera's own white balance stays on">
</p>

## Try the profiles

Start with **profiles only** on stock Sovol firmware. Added printer macros and
Python helpers are optional. Public startup retains `PURGE_LINE` and a priming
skirt: stock firmware warns about the missing macro and continues; the skirt
provides priming. Do not disable it unless another priming routine is installed.
The profiles' temperatures are set for the macro pack's PT1000 reading of the
hotend, so on stock firmware the nozzle runs 5–9 °C below them; the download
README says how to correct that.

Download the ZIP named for your slicer and follow its included README. Its
installation layout can differ from the development checkout below. See the
[download contents](docs/DOWNLOADS.md) for its installation files.

From a **Git checkout**:

1. Read [INSTALL.md](INSTALL.md) for the firmware requirements.
2. **PrusaSlicer / SuperSlicer:** import the corresponding INI from `bundles/`
   using File → Import → Import Config Bundle.
3. **PrusaSlicer 3 (alpha12 or later):** add `bundles/SVZero_PrusaSlicer3.zip`,
   unextracted, with **Add ZIP** under *Local sources* in **Preset sources &
   updates**.
4. **OrcaSlicer:** with Orca closed, copy `bundles/orca-vendor/SVZero.json` and
   `bundles/orca-vendor/SVZero/` together into its configuration directory's
   `system/` directory. **Help → Show Configuration Folder** in Orca opens that
   directory; on Windows it is `%APPDATA%\OrcaSlicer`, and [INSTALL.md](INSTALL.md)
   lists the others. Enable SV Zero in printer selection and select the
   physically installed nozzle. Keep the entire vendor folder together.
   Orca 2.5.0 and later reread the profiles only when their version rises, so
   before reinstalling the same version, delete `system/SVZero.opc`.
5. Select a print and filament preset, configure your own printer connection,
   and inspect the first-layer preview. Old projects can retain old settings;
   explicitly reselect the new presets and re-slice.

For an isolated **Linux trial from a Git clone**, the installer can prepare a
new data directory:

```sh
python3 tools/install-presets.py orca "$HOME/svzero-orca"
flatpak run com.orcaslicer.OrcaSlicer --datadir "$HOME/svzero-orca"
```

Use a dedicated directory: the installer replaces SVZero presets and selects
its defaults. It is not a general-purpose configuration merger. Keep the data
directory under your home directory for Flatpak access. It does not launch a
print or configure a printer connection. `--accel-cap N` caps installed motion
settings when your machine uses a lower acceleration limit than stock.
Windows/macOS manual installation needs feedback; it has not been tested
by this project.

## Optional enhancements

| Install | Gain | Without it |
|---|---|---|
| Profiles only | Nozzle/material settings, temperature waits, skirt | Uses stock Sovol start/end macros |
| Firmware-matched macro pack | Chamber control, rear-sensor soak, brush routine, positioned purge, first-layer nozzle growth compensation, cancellation and bed cooldown | Profiles still use stock behavior, with the nozzle 5–9 °C below their temperatures |
| Preheat Python helper | Nozzle-air qualification, history-assisted waiting, bed boost (on by default) and bounded early handover | Native rear-sensor wait |
| Spoolman helper | Material and remaining-filament check before heating | Check the spool yourself |
| Fan/camera helpers | Additional telemetry or tuning for separately configured hardware/services | Core profiles do not require them |

Copy one generated firmware file from `bundles/klipper/` and add its include;
the separate source files and build script remain available for development.
The macro pack needs commissioning, particularly brush clearances. The 1.4.x
installation replaces a stock fan section. Missing *configured* Python modules
prevent Klipper from loading: enable their includes only after installing the
modules. Exact dependencies, benefits, removal and fallback behavior are in
[INSTALL.md](INSTALL.md).

## Feedback and development

Report the release version, slicer version, firmware, nozzle, filament and
installation level. Include expected/actual behavior and a small reproducer;
remove host addresses, API keys and other personal data from attached projects
and logs. Use the [feedback template](.github/ISSUE_TEMPLATE/feedback.md).

- [Build and test](docs/DEVELOPMENT.md)
- [Download contents](docs/DOWNLOADS.md)
- [Release workflow](docs/RELEASING.md)
- [Changes](CHANGELOG.md)
- [Licences and upstream attribution](NOTICE)

## Repository layout

| Directory | Contents |
|---|---|
| [bundles/](bundles/) | Generated profiles and single-file firmware configs |
| [klipper/config/](klipper/config/) | Separate macro and hardware configuration sources |
| [klipper/](klipper/) | Optional Python helpers |
| [assets/](assets/) | Bed preview model, texture and credits |
| [source/](source/) | Profile settings, schemas and pinned upstream inputs |
| [tools/](tools/) | Generators, installers, release manifest and development dependencies |
| [tests/](tests/) | Automated checks |
| [docs/](docs/) | Development, download and validation guides |

GitHub is the intended source of truth. Printables downloads will mirror
reviewed GitHub releases, with matching versions and checksums. This repository
contains the profile product; research archives and personal printer settings
are not part of it.
