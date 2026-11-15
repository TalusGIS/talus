# Point symbols — provenance

The bundled marker set for point symbology. **224 icons**, curated
from two upstream projects, both **CC0-1.0 (public domain)** — no attribution obligation, no licence
incompatibility with AGPLv3, no third-party redistribution terms to pass on to deployers.

| Source | Repository | Pinned commit | Upstream size | Taken |
|---|---|---|---|---|
| Maki | `mapbox/maki` | `28e2a3602e4bde033a1dd388e86e9c117b425e34` | 215 | 100 |
| Temaki | `ideditor/temaki` | `6d9ac860d1d615d4449aab049f630d25d036c19e` | 557 | 124 |

Both licences were read from the repositories' own `LICENSE.md`. Note that **GitHub mis-detects
Temaki's licence as `NOASSERTION`** because the file opens with a CC0 badge image; the file body is
the full CC0 1.0 Universal text.

## Sets considered and rejected

* **Esri `@esri/calcite-ui-icons`** — licensed under the **Esri Master License Agreement**, all
  rights reserved (`LICENSE.md`; npm reports `SEE LICENSE.md`, GitHub `NOASSERTION`). Incompatible
  with AGPLv3 redistribution, would require deployers to hold an Esri licence, and is Esri IP inside
  an Esri replacement. Not usable at any scope.
* **opensymbols.org** — an AAC (assistive-communication) pictogram aggregator, not a map set. Its
  two largest libraries are ARASAAC (CC BY-**NC**-SA) and Sclera (CC BY-**NC**); non-commercial
  terms are incompatible with both a commercial product and free-software distribution.

## What is checked in

`manifest.ts` (generated) is the shipped artifact — it carries each icon's name, category, viewBox
and raw `<path>` data, so **no network access is needed at build or run time**. `curated.txt` is the
source list: `category|source|name`, one per line.

The raw upstream SVGs are deliberately **not** vendored — `manifest.ts` already contains everything
the renderer needs, and the pins above make the extraction reproducible.

## Regenerating

The generator is a development tool and is not part of this tree. It fetches from the pinned
commits above, or reads a local `DIR/{maki,temaki}/<name>.svg` tree.

The generator writes **both** `manifest.ts` and `api/app/point_symbols.py` (the server-side
allowlist) from `curated.txt`, so the picker and the validator cannot drift.

It also **fails the build** rather than the map if an upstream bump breaks the rasterizer's
assumptions: any non-`<path>` element, any `fill-rule`, a missing viewBox, or a duplicate name
across the two sources is a hard error. Adding or removing an icon is a `curated.txt` edit plus a
regeneration — no code change.

## Known non-uniformity

218 icons use a `0 0 15 15` viewBox. **Six Temaki icons use `0 0 50 50`** — `blind`, `boat_ramp`,
`pin`, `fishing_pier`, `compass`, `courthouse`. The rasterizer scales by each icon's own viewBox; a
hardcoded 15 would render those six at roughly a third size.
