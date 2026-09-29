# Release workflow

GitHub owns releases. Printables mirrors selected reviewed releases. Build once
and use the same ZIPs and SHA256SUMS on both platforms.

1. Update `VERSION`, the handshake in `tools/start_gcode.py`, `klipper/config/svzero_pack.cfg`
   and `source/presets.json`, plus numeric vendor versions in the generator.
   Update the changelog and validation record. Regenerate and run the suite.
2. Record current actual-slicer and physical test results. Describe limitations
   explicitly. Resolve the upstream licence questions in `NOTICE` before the
   first public upload; a passing software test is not a licensing decision.
3. Review the exact repository and downloadable contents for personal settings,
   credentials, private quotations, missing attribution and broken citations.
4. Commit the reviewed tree. Run `python tools/build-release.py`. The script
   requires a clean tracked tree and produces deterministic archives plus
   checksums in `dist/`: exactly three ZIPs, one per slicer.
   Each includes only installation files and the optional consolidated macro
   pack. Builders and individual sources stay in Git; no source ZIP is built.
   Use a fresh output directory (`--output PATH`) and review file lists and
   extracted installation. Never upload a stale source ZIP from an older build.
5. Create the matching GitHub prerelease tag and attach the files. The workflow
   validates builds; it does **not** automatically publish a release or post an
   announcement. Obtain community feedback on the GitHub prerelease first.
6. When that version is selected for Printables, upload those exact archives,
   repeat the version and link the GitHub release. Do not rebuild or edit an
   archive for Printables. Record the mirrored version in the release notes.

After a release is published, update the prominent README tag and direct ZIP
links to that release. Use its explicit tag URL: GitHub's stable-release
shortcut does not identify a prerelease. Repository layout changes apply to
the next release; keep already-published tags and download files unchanged.

Numeric vendor versions cannot express a prerelease suffix. This candidate maps
release `2.0.3-beta.1` to PS vendor `2.0.3` and Orca vendor `02.00.03.01`.
Advance vendor versions for each distributed update so loaders see the change.
The workflow has read-only repository permissions and no publishing credentials.
