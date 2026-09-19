# Third-party validators

Nothing in this directory is committed. `tools/fetch-vendor.sh` downloads what
the pipeline can use and checks the SHA-256; `validate-bundle.py` runs whatever
is present and says so plainly when something is not.

**Fetched, never redistributed — and that is a licensing decision, not a size
one.** `SoftFever/Orca_tools` ships **no LICENSE file at all**. An 11 MB binary
from an unlicensed repository was briefly committed here on 2026-09-07, inside
the directory that ships to Printables, in a pack licensed GPLv3. Fetching at
use time means we run it and never hand it on.

## `OrcaSlicer_profile_validator`

OrcaSlicer's own vendor-profile validator — the binary its CI runs on every pull
request touching `resources/profiles`.

```
url     https://github.com/SoftFever/Orca_tools/releases/download/1/OrcaSlicer_profile_validator
sha256  4e52df93460f867422c5143155856016966b9654c5a0526e708ad9f1f2ebbace
usage   OrcaSlicer_profile_validator -p <profiles dir> -v SVZero -l 2
```

**What it buys.** It answers the one question our own checks structurally
cannot: *would Orca load this?* Everything in `validate-bundle.py` is our
reading of Orca's rules; this is the implementation of them. Confirmed in both
directions on 2026-09-07 — it passed our vendor bundle unmodified, and rejected
a copy with one `inherits` name broken.

**What its silence is worth, and it is less than it looks.** The release is
tagged `1` and `SoftFever/Orca_tools` was last pushed **2024-02-28**, two and a
half years before we started using it, while OrcaSlicer itself is pushed daily
and we target 2.4.2. A PASS therefore means *"valid against Orca's 2024
loader"*. It cannot know about options added since, so it will not catch a
profile that is wrong in a way only current Orca cares about. Useful, and not a
substitute for the slice matrix, which runs the actual installed 2.4.2.

If the checksum ever fails, the release asset has been replaced under a fixed
tag. Do not update the expected hash to make it pass — verify by hand first.
