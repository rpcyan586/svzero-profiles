# Slicer downloads

The next packaging revision provides **three ZIPs**, one per slicer. Each contains
ready-to-install profiles, the optional consolidated macro pack, instructions and
licence notices. Development sources and build tools stay in Git.
Published `2.0.2-beta.1` ZIPs retain their older layout; follow the README inside
your download. The streamlined layout below applies to the next release.

| Download | Import/copy | Other contents |
|---|---|---|
| `orcaslicer` | `profiles/SVZero.json` and `profiles/SVZero/` into Orca's `system/` directory | Bed previews inside the vendor directory |
| `prusaslicer` | Import `SVZero_PrusaSlicer.ini` | Bed previews under `assets/` |
| `superslicer` | Import `SVZero_SuperSlicer.ini` | Bed previews under `assets/` |

PrusaSlicer and SuperSlicer each use a single INI; individual presets are not
repeated in their downloads. Orca's separate JSON files are required by its
vendor format and must stay together. Its bed assets are not duplicated.

Every ZIP has an `optional-macros/` directory containing exactly three configs:
choose **one** native firmware file (`svzero-1.3.7.cfg` or `svzero-1.4.x.cfg`),
plus `svzero-python.cfg` only if enabling adaptive preheat and Spoolman. The three
Python modules needed for those enhancements are beside them. The ZIP's
`INSTALL.md` explains the 1.4.x fan edit, commissioning and rollback. There are
no individual macro sources or build steps in the download. Fan/camera extras
and individual-feature configurations remain available from Git.

`RELEASE.json` records the version, source commit and a hash for every packaged
file. External `SHA256SUMS` covers the three complete ZIPs. Checksums detect
changes relative to the record; they do not authenticate a download's origin.

## Source and development

Clone [the repository](https://github.com/rpcyan586/svzero-profiles) for the
individual macro sources, profile generators, tests, isolated-directory installer
and experimental PrusaSlicer 3 output. No separate source ZIP is built.

Both `tools/build-macros.py` and `tools/build-release.py` remain in Git. Build
from a clean committed checkout following [DEVELOPMENT.md](DEVELOPMENT.md);
release selection and archive paths live in `tools/release-files.json`.
