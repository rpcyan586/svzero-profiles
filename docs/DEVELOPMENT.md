# Building and testing

Use Python 3.11 for the reproducible macro-test environment:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r tools/requirements-dev.txt
python tools/verify.py
```

This runs unit tests, bundle validation, macro rendering in Jinja 2.11.3,
purge-geometry checks, documentation-link checks and a complete rebuild in a
temporary directory. Rebuilt bundles must match checked-in output byte for
byte. It needs neither a printer nor private files or network services.

`source/presets.json` owns authored settings, `source/model.json` owns machine
and tier structure, and the other source tables own derivations. Edit those
sources and regenerate; do not patch generated bundles:

```sh
python tools/generate.py --check --emit ss,ps,ps-vendor,ps-presets,ss-presets,orca,orca-filament,orca-vendor,ps3
python tools/build-macros.py
python tools/verify.py
```

Pinned public Orca inputs and their checksums live under
`source/vendor-profiles/`. Schema attribution is in
[source/ps3/PROVENANCE.md](../source/ps3/PROVENANCE.md).
PrusaSlicer 3 output remains experimental and is not in the user download.

Macro sources stay separate under `klipper/config/`. `tools/build-macros.py`
expands each firmware's explicit include tree into one config under
`bundles/klipper/`, with a separate opt-in config for the two Python helpers.
It requires only Python's standard library. Tests compare the effective
settings with the modular layout, including the 1.4.x probe overrides; the
full suite also renders the consolidated macros and checks a clean rebuild.

Bed preview assets live under `assets/`; installers copy them into the slicer's
data directory and Orca generation includes them in its vendor folder. Release
source-to-download path mappings are maintained in `tools/release-files.json`.
After committing a clean tree, `python tools/build-release.py --output PATH`
builds only the three slicer ZIPs and `SHA256SUMS`. Sources, builders and the
isolated-directory installer remain in the Git checkout; downloads use manual
INI import or Orca vendor copying. See [DOWNLOADS.md](DOWNLOADS.md).

## Actual slicing

The structural suite does not prove slicer acceptance. On Linux, install
OrcaSlicer and PrusaSlicer as Flatpaks, plus SuperSlicer. Set `SUPERSLICER_BIN`
if it is not installed under `~/opt/superslicer-*/superslicer`:

```sh
python tools/slice-harness.py --matrix --workdir "$HOME/svzero-slice-check" --keep
```

The harness currently expects these Linux installations. It is not a portable
installer for slicer binaries. See [VALIDATION.md](VALIDATION.md) for tested
versions and the difference between slicing and a physical print.

An optional upstream Orca validator can be downloaded with
`bash tools/fetch-vendor.sh`. It is not redistributed and is not required for
the offline suite. The checker explicitly reports when it is absent. Its older
loader is additional evidence, not a replacement for current-slicer tests.

## Ownership

Once public, this repository owns profile sources, generator code, public
macros and release artifacts. External research findings arrive as reviewed
changes. Do not periodically overwrite the repository from a private export.
The generator remains alongside its consumer until a stable, useful standalone
interface emerges. No separate generator release is required to build this pack.
