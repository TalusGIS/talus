# Design note: the zooms a layer is drawn at

**In Talus since:** v1.1.0 (November 2026) · **Decided:** 23 and 24 November 2026, by the project lead

This is the public version of the decision behind the visible range of a layer. Section numbers match the working
document, so a comment that says `decision-layer-zoom-range.md §5` points at §5 below. Sections that hold only
working detail are left out, which is why the numbers skip. For how to use the feature, see "Set the zooms a layer is
drawn at" in [`guide-maps.md`](../guide-maps.md).

## §1 The problem

A large polygon layer, such as a county's parcels, is slow and costly to draw when the map is zoomed far out: every
tile holds thousands of shapes too small to read. Nothing in Talus let the layer's owner say "don't draw this until
the map is zoomed in".

## §3 Where the range lives

Three places were considered:

- **On the map only.** Each map sets its own range for each layer. Flexible, but every map that carries the parcels
  has to remember to set it, and one that forgets brings the slow tiles back.
- **On the layer only.** The owner sets it once and every map follows. Simple, but one range has to suit every map.
- **Both:** a default on the layer, and a map's own range over it.

The first proposal was the layer only, as the smallest change. The project lead chose both, because different maps at
different scales need to draw the same layer differently.

### §3.1 How the two combine

- **One range is in force for a layer on a map:** the map's own range if it has one, otherwise the layer's default,
  otherwise every zoom.
- **The map's range replaces the layer's as a pair.** A map doesn't take its lowest zoom from one place and its
  highest from the other, because a row in the layer list has to be explainable in one line.
- **The layer default stays live.** A map that uses the layer's range follows it when the owner changes it later.
  Copying the range into the map when the layer is added was turned down: fixing a slow layer once would then not fix
  the maps that already carry it.
- **A map may widen the range as well as narrow it.** When a map starts drawing a layer further out than its owner's
  default, the panel says that it can be slow, and doesn't prevent it.
- **Who sets which:** the default, the layer's owner or an administrator; a map's own range, anyone who can edit
  that map.
- **Every kind of layer on a map can have a map range,** including rasters and layers from other servers. Only hosted
  layers and views have a layer default, because only they have a Talus style to hold it.

A visible range is a drawing rule. It is not access control: it doesn't change who may read a layer's data.

## §4 Names, meaning and the control

### §4.1 Names and meaning

- The two values are `visibleMinZoom` and `visibleMaxZoom`, with the same names on the layer's style and on a map's
  entry for the layer. Absent means every zoom, so layers and maps saved before this draw as they always did.
- A layer is drawn when the map's zoom is at least the lowest value and below the highest.
- A point layer's circles, its icons and its labels each already had a zoom of their own. They are combined with the
  layer's range so that none of them can show when the layer doesn't.
- Changing the range changes the layers already on the map. Nothing is reloaded or re-tiled.

### §4.2 The control

Two controls were considered: a pair of zoom-number boxes, and a two-ended slider with named stops and the scale for
each. The slider was chosen. The people Talus is for think in scales and in "can I see parcels yet", not in zoom
numbers, and a slider of that kind is what they know from ArcGIS. What is stored is still the zoom. The scale is
shown beside it and never stored, because on a web map the scale for a given zoom changes with latitude.

### §4.3 Where the control sits

A **Visibility** tab in the layer inspector holds both halves: the range for this map, and, for the layer's owner or
an administrator, the layer's default. A **Visible range** item in the layer's menu opens that tab. Someone who can't
edit the map sees neither; they see the greyed row described in §5.

### §4.4 Rasters

A raster has no Talus style, so it has no layer default: only a map's own range applies to it. Raster rows were
given the same menu as other layers so that there is somewhere to set it.

## §5 How the layer list shows a layer that is out of range

The risk is a layer that looks broken: ticked, and nothing on the map.

- The row stays where it is and its tick box still works. Being out of range is not being switched off. The tick
  box is the reader's choice; the range belongs to the layer.
- The layer's name is greyed, and a short line under it says when it is drawn, such as "Shown from zoom 14".
  Clicking that line zooms the map into range.
- Story maps, dashboards and embedded maps draw through the same map component, so the range applies there too.

## §6 Other features when a layer is out of range

| Feature | What happens |
|---|---|
| Identify (popups) and the select tool | Nothing is found for that layer: both read what is drawn. The select tool says to zoom in, instead of coming back empty without a reason. |
| Attribute table | Unchanged. It reads every row at any zoom, because a visible range is about drawing, not about the data. |
| Zoom to a row, a selection or a layer | Lands inside the range in force, so what was zoomed to can be seen. |
| Labels | Hidden with the layer. |
| Data export | Unchanged: the whole layer or the filtered rows, whatever the zoom. |

## §7 Printed and GeoPDF output

A print is made from the same map style at the same zoom, so a layer out of range on screen is out of range on paper.
The printed legend leaves out layers that are out of range, because a legend describes what is drawn.

## §10 Does this fit what Talus is for?

Yes. It makes a large layer cheap to serve on a small server, which is about running cost, and it is one setting an
owner makes once. It isn't a step toward matching ArcGIS feature for feature.

## §11 Security

No new way of reaching data. The layer default is saved through the existing style route (the layer's owner or an
administrator), and a map's own range is saved with the map (whoever may edit that map). Because the range is not
access control, nothing that was readable becomes hidden from the server's point of view, and nothing hidden becomes
readable.
