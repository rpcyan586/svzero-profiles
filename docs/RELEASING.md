# Release workflow

GitHub owns releases. Printables mirrors selected reviewed releases. Build once
and use the same ZIPs and SHA256SUMS on both platforms.

1. Update `VERSION`, the handshake in `tools/start_gcode.py`, `svzero_pack.cfg`
   and `source/presets.json`, plus numeric vendor versions in the generator.
   Update the changelog and validation record. Regenerate and run the suite.
2. Record current actual-slicer and physical test results. Describe limitations
   explicitly. Resolve the upstream licence questions in `NOTICE` before the
   first public upload; a passing software test is not a licensing decision.
3. Review the exact repository and downloadable contents for personal settings,
   credentials, private quotations, missing attribution and broken citations.
4. Commit the reviewed tree. Run `python tools/build-release.py`. The script
   requires a clean tracked tree and produces deterministic archives plus
   checksums in `dist/`. Review their file lists, not only the working tree.
5. Create the matching GitHub prerelease tag and attach the files. The workflow
   validates builds; it does **not** automatically publish a release or post an
   announcement. Obtain community feedback on the GitHub prerelease first.
6. When that version is selected for Printables, upload those exact archives,
   repeat the version and link the GitHub release. Do not rebuild or edit an
   archive for Printables. Record the mirrored version in the release notes.

Numeric vendor versions cannot express a prerelease suffix. This candidate maps
release `2.0.1-beta.1` to PS vendor `2.0.1` and Orca vendor `02.00.01.01`.
Advance vendor versions for each distributed update so loaders see the change.
The workflow has read-only repository permissions and no publishing credentials.
