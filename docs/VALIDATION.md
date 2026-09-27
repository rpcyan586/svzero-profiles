# Validation and known limitations

## Candidate 2.0.2-beta.1

The offline suite passes 131 tests. The profile repairs retain the native
SuperSlicer travel-deceleration option
and add layer-limit/precision checks. Macro consolidation compares all effective
settings and renders both firmware configurations under Jinja 2.11.3. Release
tests extract each slicer ZIP, install into a fresh isolated data directory,
check macro rebuilds and reproduce the original ZIP byte for byte without Git.
The complete source archive also reproduces from its extracted contents.

These are offline checks. Physical-print and platform limitations below remain.


## Candidate 2.0.1-beta.1

Recorded 2026-09-18. The standalone public candidate passes 113 unit tests,
macro/purge checks, local documentation links and a byte-identical rebuild from
a temporary tree containing no generated bundles. The source repair was tested with OrcaSlicer 2.4.2,
PrusaSlicer 2.9.6 and SuperSlicer 2.7.62.0-beta2.

| Check | Evidence |
|---|---|
| Cross-slicer matrix | 27 slices; eight nozzles, five tiers, nine filaments and four test objects; no refused keys |
| Normal 0.4 mm Standard ABS | All three slicers emit G-code with the skirt and `PURGE_LINE` |
| Personal configuration regression | Separate test slice omitted skirt, retained purge and respected its reduced acceleration cap; personal settings are not distributed |
| Native/Python fallback | Include-dependency checks, Jinja 2.11.3 rendering, helper/service failure tests |
| Missing optional commands | Captured Sovol 1.3.7 and 1.4.7 dispatchers warn and continue; a missing target of a known mux command still errors |

The archived-dispatch check was performed privately; its captured sources are
not shipped and that check is not counted as a public CI test. Public tests
exercise the distributed source and generated profiles. Automated render tests
do not instantiate every hardware object or verify mechanical clearances.

## Still pending

- A watched physical print of the latest **profiles-only stock startup**, with
  the default skirt, on each advertised firmware family.
- Feedback on manual installation in Windows and macOS.
- Physical flow/temperature calibration of added nozzle sizes; slicing alone
  cannot establish nozzle suitability or print quality.
- Optional macro-pack commissioning on other machines, especially brush pad
  geometry and the 1.4.x fan/probe path.

The basic bed preview is an original schematic square, not a mechanical model.
The beta does not include the earlier third-party bed graphics.

The optional Python modules are opt-in, not auto-installed dependencies. A
configured feature can deliberately refuse a failed check; absence of a feature
and an actual failure have different behavior. See [INSTALL.md](../INSTALL.md).

The standalone candidate also passed the full 27-slice matrix. Its three
archives have reproducible SHA-256 checksums; the extracted profiles archive
installs into fresh Orca, PrusaSlicer and SuperSlicer data directories.
[Machine-readable results](validation-2.0.1-beta.1.json) distinguish local
Python 3.14 validation from the not-yet-run hosted Python 3.11 workflow.
