# Download contents

Choose the ZIP named for your slicer. Each contains only that slicer's generated
profiles, bed preview assets, a short installation README, and the same optional
printer macro pack. A separate `source` ZIP contains the whole development tree,
including the profile generator, tests and experimental PrusaSlicer 3 output.

| Download | Profile import | Optional printer install |
|---|---|---|
| `orcaslicer` | Complete `bundles/orca-vendor/` vendor | One firmware config from `bundles/klipper/` |
| `prusaslicer` | `bundles/SVZero_PrusaSlicer.ini` | Same macro pack |
| `superslicer` | `bundles/SVZero_SuperSlicer.ini` | Same macro pack |

The PS/SS individual presets remain included for the optional isolated-directory
installer. GUI import uses the single INI. Orca's vendor is a directory of JSON
files because that is its load format; keep the directory intact. No slicer ZIP
contains the other slicers' generated output or development-only vendor inputs.

The macro pack needs to be installed only once, even if you use several slicers.
Its generated configs consolidate the separate sources in this download. Follow
[INSTALL.md](../INSTALL.md): 1.4.x still requires disabling the original exhaust
temperature-fan section. Python helpers remain optional. The source `.cfg` files
under `klipper/config/` are supplied for clarity and rebuilding, not as extra
copy/edit steps. Bed previews and their credits are under `assets/`.

## Included builders

From the extracted download, with Python 3.11 or newer:

```sh
python3 tools/build-macros.py --check
python3 tools/build-release.py
```

The macro builder can regenerate `bundles/klipper/` after you edit the individual
macro sources. The release builder can reproduce the unchanged download into
`dist/` without Git or network access. `RELEASE.json` records its original source
commit and SHA-256 for each input; changed inputs fail this reproduction check.
Checksums detect changes relative to that record; they do not authenticate a
download's origin. The external `SHA256SUMS` covers each complete ZIP.

To develop and build a **changed release**, use the complete source ZIP or a Git
checkout. In a checkout, edit the sources, regenerate, verify, then commit before
running the release builder. Slicer downloads contain ready-made profiles, not
the full profile generator. Both builders and all inputs to rebuild the macro
pack are included in every slicer download; all profile-generation inputs are
in the source ZIP.
