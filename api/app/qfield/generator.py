"""Compile a QField-ready .qgz from Talus data (decision-generate-field-project.md).

THE FENCE: this module EMITS a project from templates — it never parses, opens, or
round-trips one. The templates under templates/ are derived mechanically from the
field-proven scripts/qfield-project-template.qgs (B5c acceptance: 14/14 photos, the GNSS
integration matrix); the per-layer recipe is the same one scripts/make-qfield-project.py
ships for the CLI: FeatureServer datasource with the PAT in the URL userinfo (QGIS auth
Configurations don't travel to QField), GNSS default-value expressions (creation-only) for
whichever gnss_* fields the layer has, every constraint zeroed, the photo kit gated on
attachments, `QFieldSync/action = offline`, OSM XYZ basemap.

Inputs are plain dataclasses — the caller (routers/field_projects.py) resolves the web map
+ layer registry into LayerSpecs; nothing here touches the DB or the network, so the whole
compile is unit-testable offline.
"""
from __future__ import annotations

import io
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

_TEMPLATES = Path(__file__).parent / "templates"

# The bench-verified capture recipe (connect-desktop.md "Field collection" table).
# gnss_correction_age / gnss_station_id have no QField variable — manual-or-blank.
GNSS_EXPRESSIONS = {
    "gnss_h_accuracy": "@position_horizontal_accuracy",
    "gnss_v_accuracy": "@position_vertical_accuracy",
    "gnss_altitude": "z(@position_coordinate)",
    "gnss_fix_type": "@position_quality_description",
    "gnss_fix_time": "format_date(@position_timestamp, 'yyyy-MM-ddTHH:mm:ss') || 'Z'",
    "gnss_num_sats": "@position_number_of_used_satellites",
    "gnss_pdop": "@position_pdop",
    "gnss_hdop": "@position_hdop",
    "gnss_vdop": "@position_vdop",
    "gnss_receiver": "@position_source_name",
}

# The bench-verified ExternalResource widget (session-summary-2026-08-15 §3.2 lineage):
# ★ the property key is `storageUrl`, NOT `propertyStorageUrl` (a wrong key is silently
#   ignored and the literal StorageUrl wins).
# ★ StorageAuthConfigId stays EMPTY — auth Configurations don't travel; the PAT rides the URL.
# ★ the literal StorageUrl fallback deliberately omits the objectid so a dropped override
#   404s loudly instead of writing every photo to one hardcoded feature.
ATTACH_WIDGET = """
        <field configurationFlags="NoFlag" name="_talus_attach">
          <editWidget type="ExternalResource">
            <config>
              <Option type="Map">
                <Option name="DocumentViewer" value="1" type="int"/>
                <Option name="DocumentViewerHeight" value="0" type="int"/>
                <Option name="DocumentViewerWidth" value="0" type="int"/>
                <Option name="FileWidget" value="true" type="bool"/>
                <Option name="FileWidgetButton" value="true" type="bool"/>
                <Option name="FileWidgetFilter" value="" type="QString"/>
                <Option name="PropertyCollection" type="Map">
                  <Option name="name" value="" type="QString"/>
                  <Option name="properties" type="Map">
                    <Option name="storageUrl" type="Map">
                      <Option name="active" value="true" type="bool"/>
                      <Option name="expression" value="'{{CRED_ORIGIN}}/api/v1/esri-auth/layers/{{LAYER_ID}}/attachments/' || &quot;OBJECTID&quot; || '/'" type="QString"/>
                      <Option name="type" value="3" type="int"/>
                    </Option>
                  </Option>
                  <Option name="type" value="collection" type="QString"/>
                </Option>
                <Option name="RelativeStorage" value="0" type="int"/>
                <Option name="StorageAuthConfigId" value="" type="QString"/>
                <Option name="StorageMode" value="0" type="int"/>
                <Option name="StorageType" value="WebDAV" type="QString"/>
                <Option name="StorageUrl" value="{{CRED_ORIGIN}}/api/v1/esri-auth/layers/{{LAYER_ID}}/attachments/" type="QString"/>
              </Option>
            </config>
          </editWidget>
        </field>"""

# QGIS maplayer attrs + symbol family per Talus geom_type (V055 friendly enum). Multi
# variants match the PostGIS reality (a 'polygon' layer is physically MULTIPOLYGON);
# the provider's actual type wins at load either way.
_GEOM = {
    "point": ("Point", "Point", "marker"),
    "line": ("Line", "MultiLineString", "line"),
    "polygon": ("Polygon", "MultiPolygon", "fill"),
}

_NO_OP = '3x:0,0,0,0,0,0'


class GenerationError(Exception):
    """Compile failed in a way that is OUR bug (template drift, bad substitution) — the
    router maps it to a 500, never a 4xx: the publisher did nothing wrong."""


@dataclass
class LayerSpec:
    layer_id: str                     # tal_xxxxxxxx (the hosted layer id)
    name: str                         # display name (map title override wins)
    geom: str                         # 'point' | 'line' | 'polygon'
    fields: list[str]                 # field names as the ESRI door serves them
    editable: bool = False
    attachments: bool = False         # attachments_enabled AND _talus_attach present
    color: str = "#2563eb"            # '#rrggbb' (resolved renderer / display color)
    line_width: float | None = None   # renderer extras, used per-geometry when present
    line_dash: list[float] | None = None
    fill_opacity: float | None = None
    outline_color: str | None = None
    outline_width: float | None = None
    point_size: float | None = None   # marker diameter in POINT units (converted from px upstream)


@dataclass
class ProjectSpec:
    title: str
    origin: str                       # advertised base URL, e.g. https://talus.example.org
    pat: str                          # the embedded per-project token (plaintext)
    layers: list[LayerSpec] = field(default_factory=list)
    center: tuple[float, float] = (0.0, 0.0)   # lon, lat (web map initialView)
    zoom: float = 2.0


def _qgis_color(hex_color: str, alpha: int = 255) -> str:
    """'#2563eb' -> '37,99,235,255,rgb:0.1450980,0.3882353,0.9215686,1' (the template's
    color grammar)."""
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", hex_color or "")
    if not m:
        raise GenerationError(f"bad color {hex_color!r}")
    r, g, b = (int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))

    def unit(v: int) -> str:
        return "1" if v == 255 else "0" if v == 0 else f"{v / 255:.7f}"

    return f"{r},{g},{b},{alpha},rgb:{unit(r)},{unit(g)},{unit(b)},{unit(alpha)}"


def _opt(name: str, value: str) -> str:
    return f'                <Option type="QString" value={quoteattr(value)} name={quoteattr(name)}/>'


_DDP = """              <data_defined_properties>
                <Option type="Map">
                  <Option type="QString" value="" name="name"/>
                  <Option name="properties"/>
                  <Option type="QString" value="collection" name="type"/>
                </Option>
              </data_defined_properties>"""


def _symbol(sym_type: str, name: str, options: list[str], klass: str) -> str:
    opts = "\n".join(options)
    return f"""          <symbol is_animated="0" frame_rate="10" alpha="1" type="{sym_type}" force_rhr="0" clip_to_extent="1" name="{name}">
            <data_defined_properties>
              <Option type="Map">
                <Option type="QString" value="" name="name"/>
                <Option name="properties"/>
                <Option type="QString" value="collection" name="type"/>
              </Option>
            </data_defined_properties>
            <layer class="{klass}" id="{{{uuid.uuid4()}}}" locked="0" pass="0" enabled="1">
              <Option type="Map">
{opts}
              </Option>
{_DDP}
            </layer>
          </symbol>"""


def _marker_symbol(name: str, color: str, size: str = "8", outline: str = "255,255,255,255,rgb:1,1,1,1", outline_width: str = "1", unit: str = "Point") -> str:
    return _symbol("marker", name, [
        _opt("angle", "0"), _opt("cap_style", "square"), _opt("color", color),
        _opt("horizontal_anchor_point", "1"), _opt("joinstyle", "bevel"),
        _opt("name", "circle"), _opt("offset", "0,0"),
        _opt("offset_map_unit_scale", _NO_OP), _opt("offset_unit", unit),
        _opt("outline_color", outline), _opt("outline_style", "solid"),
        _opt("outline_width", outline_width), _opt("outline_width_map_unit_scale", _NO_OP),
        _opt("outline_width_unit", unit), _opt("scale_method", "area"),
        _opt("size", size), _opt("size_map_unit_scale", _NO_OP), _opt("size_unit", unit),
        _opt("vertical_anchor_point", "1"),
    ], "SimpleMarker")


def _line_symbol(name: str, color: str, width: float | None, dash: list[float] | None) -> str:
    custom = bool(dash)
    dash_str = ";".join(f"{d:g}" for d in dash) if custom else "5;2"
    return _symbol("line", name, [
        _opt("align_dash_pattern", "0"), _opt("capstyle", "square"),
        _opt("customdash", dash_str), _opt("customdash_map_unit_scale", _NO_OP),
        _opt("customdash_unit", "MM"), _opt("dash_pattern_offset", "0"),
        _opt("dash_pattern_offset_map_unit_scale", _NO_OP), _opt("dash_pattern_offset_unit", "MM"),
        _opt("draw_inside_polygon", "0"), _opt("joinstyle", "bevel"),
        _opt("line_color", color), _opt("line_style", "solid"),
        _opt("line_width", f"{width if width is not None else 0.8:g}"),
        _opt("line_width_unit", "MM"), _opt("offset", "0"),
        _opt("offset_map_unit_scale", _NO_OP), _opt("offset_unit", "MM"),
        _opt("ring_filter", "0"), _opt("trim_distance_end", "0"),
        _opt("trim_distance_end_map_unit_scale", _NO_OP), _opt("trim_distance_end_unit", "MM"),
        _opt("trim_distance_start", "0"), _opt("trim_distance_start_map_unit_scale", _NO_OP),
        _opt("trim_distance_start_unit", "MM"), _opt("tweak_dash_pattern_on_corners", "0"),
        _opt("use_custom_dash", "1" if custom else "0"),
        _opt("width_map_unit_scale", _NO_OP),
    ], "SimpleLine")


def _fill_symbol(name: str, color_hex: str, opacity: float | None, outline_hex: str | None, outline_width: float | None) -> str:
    alpha = round((opacity if opacity is not None else 0.35) * 255)
    return _symbol("fill", name, [
        _opt("border_width_map_unit_scale", _NO_OP),
        _opt("color", _qgis_color(color_hex, alpha=max(0, min(255, alpha)))),
        _opt("joinstyle", "bevel"), _opt("offset", "0,0"),
        _opt("offset_map_unit_scale", _NO_OP), _opt("offset_unit", "MM"),
        _opt("outline_color", _qgis_color(outline_hex or color_hex)),
        _opt("outline_style", "solid"),
        _opt("outline_width", f"{outline_width if outline_width is not None else 0.4:g}"),
        _opt("outline_width_unit", "MM"), _opt("style", "solid"),
    ], "SimpleFill")


def _layer_symbol(spec: LayerSpec, name: str, selection: bool = False) -> str:
    sym_type = _GEOM[spec.geom][2]
    if selection:
        red = "255,0,0,255,rgb:1,0,0,1"
        if sym_type == "marker":
            return _marker_symbol(name, red, size="2", outline="35,35,35,255,rgb:0.1372549,0.1372549,0.1372549,1", outline_width="0", unit="MM")
        if sym_type == "line":
            return _line_symbol(name, red, 1.0, None)
        return _fill_symbol(name, "#ff0000", 0.5, "#232323", 0.4)
    if sym_type == "marker":
        if spec.point_size is not None:
            return _marker_symbol(name, _qgis_color(spec.color), size=f"{spec.point_size:g}")
        return _marker_symbol(name, _qgis_color(spec.color))
    if sym_type == "line":
        return _line_symbol(name, _qgis_color(spec.color), spec.line_width, spec.line_dash)
    return _fill_symbol(name, spec.color, spec.fill_opacity, spec.outline_color, spec.outline_width)


def _renderer_v2(spec: LayerSpec) -> str:
    return f"""      <renderer-v2 referencescale="-1" forceraster="0" type="singleSymbol" enableorderby="0" symbollevels="0">
        <symbols>
{_layer_symbol(spec, "0")}
        </symbols>
        <rotation/>
        <sizescale/>
        <data-defined-properties>
          <Option type="Map">
            <Option type="QString" value="" name="name"/>
            <Option name="properties"/>
            <Option type="QString" value="collection" name="type"/>
          </Option>
        </data-defined-properties>
      </renderer-v2>"""


def _selection_block(spec: LayerSpec) -> str:
    return f"""      <selection mode="Default">
        <selectionColor invalid="1"/>
        <selectionSymbol>
{_layer_symbol(spec, "", selection=True)}
        </selectionSymbol>
      </selection>"""


def _replace_block(src: str, tag: str, new_inner: str) -> str:
    """Replace the single <tag>…</tag> block's contents (each fragment instance carries
    exactly one — the script's proven approach, applied per-fragment)."""
    pattern = re.compile(rf"<{tag}>.*?</{tag}>", re.DOTALL)
    hits = pattern.findall(src)
    if len(hits) != 1:
        raise GenerationError(f"expected exactly one <{tag}> block in the fragment, found {len(hits)}")
    return pattern.sub(lambda _: f"<{tag}>{new_inner}</{tag}>", src)


def _feature_server_url(cred_origin: str, layer_id: str) -> str:
    return f"{cred_origin}/api/v1/esri-auth/rest/services/{layer_id}/FeatureServer/0"


def _compile_layer(spec: LayerSpec, cred_origin: str, warnings: list[str]) -> tuple[str, str, str]:
    """Returns (maplayer_xml, qgis_layer_id, naming_note). The per-layer port of the
    script's main()."""
    if spec.geom not in _GEOM:
        raise GenerationError(f"unsupported geometry {spec.geom!r} for {spec.layer_id}")
    geometry, wkb, _ = _GEOM[spec.geom]

    gnss_present = [k for k in GNSS_EXPRESSIONS if k in spec.fields]
    defaults = "\n".join(
        f'      <default field={quoteattr(k)} applyOnUpdate="0" expression={quoteattr(GNSS_EXPRESSIONS[k])}/>'
        for k in gnss_present
    )
    constraints = "\n".join(
        f'      <constraint unique_strength="0" notnull_strength="0" constraints="0" exp_strength="0" field={quoteattr(n)}/>'
        for n in spec.fields
    )
    constraint_exprs = "\n".join(
        f'      <constraint exp="" field={quoteattr(n)} desc=""/>' for n in spec.fields
    )

    attach_on = spec.attachments and "_talus_attach" in spec.fields
    if attach_on:
        slug = re.sub(r"[^a-z0-9]+", "_", spec.name.lower()).strip("_") or spec.layer_id
        if "gnss_fix_time" in spec.fields:
            naming_expr = (f"'DCIM/{slug}_' || regexp_replace(coalesce(\"gnss_fix_time\","
                           "'NOFIX'), '[:]', '') || '.{extension}'")
        else:
            naming_expr = (f"'DCIM/{slug}_' || format_date(now(),'yyyyMMddhhmmsszzz') "
                           "|| '.{extension}'")
            if "photo_naming_wall_clock" not in warnings:
                warnings.append("photo_naming_wall_clock")
        widget_block = ATTACH_WIDGET
        naming_attr = escape(
            __import__("json").dumps({"_talus_attach": naming_expr}), {'"': "&quot;"}
        )
    else:
        widget_block = ""
        naming_attr = "{}"

    frag = (_TEMPLATES / "vector-layer.qgs.tmpl").read_text()
    frag = _replace_block(frag, "defaults", "\n" + defaults + "\n    ")
    frag = _replace_block(frag, "constraints", "\n" + constraints + "\n    ")
    frag = _replace_block(frag, "constraintExpressions", "\n" + constraint_exprs + "\n    ")

    qgis_layer_id = re.sub(r"[^A-Za-z0-9]", "_", spec.name) + "_" + str(uuid.uuid4()).replace("-", "_")
    # Widget/naming first: the widget carries {{CRED_ORIGIN}}/{{LAYER_ID}} tokens of its
    # own that the second pass resolves (the script's ordering).
    frag = (frag
            .replace("{{ATTACH_WIDGET}}", widget_block)
            .replace("{{QFS_NAMING}}", naming_attr)
            .replace("{{RENDERER_V2}}", _renderer_v2(spec))
            .replace("{{SELECTION}}", _selection_block(spec))
            .replace("{{GEOMETRY}}", geometry)
            .replace("{{WKB_TYPE}}", wkb)
            .replace("{{CRED_ORIGIN}}", cred_origin)
            .replace("{{LAYER_ID}}", spec.layer_id)
            .replace("{{QGIS_LAYER_ID}}", qgis_layer_id)
            .replace("{{LAYER_NAME}}", escape(spec.name)))
    return frag, qgis_layer_id, naming_attr


def compile_project(spec: ProjectSpec) -> tuple[bytes, list[str]]:
    """Compile the whole project. Returns (qgz bytes, generation warning codes). Raises
    GenerationError on template drift / bad specs — never emits a half-substituted doc."""
    if not spec.layers:
        raise GenerationError("no layers to compile")
    origin = spec.origin.rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise GenerationError(f"bad origin {spec.origin!r}")
    cred_origin = f"{parsed.scheme}://talus:{spec.pat}@{parsed.netloc}"

    warnings: list[str] = []
    maplayers, tree, legend, snapping, custom_order, layer_order = [], [], [], [], [], []
    gps_dest: tuple[str, str, str] | None = None   # (qgis_id, name, url)
    for layer in spec.layers:
        frag, qgis_id, _ = _compile_layer(layer, cred_origin, warnings)
        url = _feature_server_url(cred_origin, layer.layer_id)
        maplayers.append(frag)
        tree.append(
            f'    <layer-tree-layer expanded="1" id="{qgis_id}" legend_exp="" providerKey="arcgisfeatureserver" '
            f'checked="Qt::Checked" patch_size="-1,-1" source={quoteattr(" url=" + chr(39) + url + chr(39))} '
            f'legend_split_behavior="0" name={quoteattr(layer.name)}>\n'
            "      <customproperties>\n        <Option/>\n      </customproperties>\n"
            "    </layer-tree-layer>"
        )
        legend.append(
            f'    <legendlayer drawingOrder="-1" open="true" showFeatureCount="0" checked="Qt::Checked" name={quoteattr(layer.name)}>\n'
            '      <filegroup open="true" hidden="false">\n'
            f'        <legendlayerfile isInOverview="0" visible="1" layerid="{qgis_id}"/>\n'
            "      </filegroup>\n    </legendlayer>"
        )
        snapping.append(
            f'      <layer-setting tolerance="12" id="{qgis_id}" minScale="0" maxScale="0" type="1" enabled="0" units="1"/>'
        )
        custom_order.append(f"      <item>{qgis_id}</item>")
        layer_order.append(f'    <layer id="{qgis_id}"/>')
        if gps_dest is None and layer.editable and layer.geom == "point":
            gps_dest = (qgis_id, layer.name, url)
    if gps_dest is None:
        first = spec.layers[0]
        gps_dest = (custom_order[0].strip()[6:-7], first.name, _feature_server_url(cred_origin, first.layer_id))

    lon, lat = spec.center
    lat = max(-85.0, min(85.0, lat))
    half_lon = 360.0 / (2 ** max(1.0, min(24.0, spec.zoom)))
    half_lat = half_lon / 2
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    shell = (_TEMPLATES / "shell.qgs.tmpl").read_text()
    doc = (shell
           .replace("{{PROJECT_TITLE_ATTR}}", quoteattr(spec.title))
           .replace("{{PROJECT_TITLE}}", escape(spec.title))
           .replace("{{SAVE_DATETIME}}", stamp)
           .replace("{{LAYER_TREE_LAYERS}}", "\n".join(tree))
           .replace("{{CUSTOM_ORDER_ITEMS}}", "\n".join(custom_order))
           .replace("{{SNAPPING_SETTINGS}}", "\n".join(snapping))
           .replace("{{LEGEND_LAYERS}}", "\n".join(legend))
           .replace("{{MAPLAYERS}}", "\n".join(maplayers))
           # layerorder is bottom-to-top (the template's OSM-first shape); the tree is
           # top-first = the web map's draw order (front = top).
           .replace("{{LAYER_ORDER_ITEMS}}", "\n".join(reversed(layer_order)))
           .replace("{{XMIN}}", f"{lon - half_lon:.14f}")
           .replace("{{YMIN}}", f"{lat - half_lat:.14f}")
           .replace("{{XMAX}}", f"{lon + half_lon:.14f}")
           .replace("{{YMAX}}", f"{lat + half_lat:.14f}")
           .replace("{{GPS_DEST_NAME_ATTR}}", quoteattr(gps_dest[1]))
           .replace("{{GPS_DEST_ID}}", gps_dest[0])
           .replace("{{GPS_DEST_URL}}", escape(gps_dest[2], {'"': "&quot;"})))

    if "{{" in doc:
        leftover = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", doc)))
        raise GenerationError(f"unsubstituted placeholders {leftover} — template drift?")
    try:
        ET.fromstring(doc)  # well-formedness gate before anything is stored
    except ET.ParseError as exc:
        raise GenerationError(f"generated project is not well-formed XML: {exc}") from exc

    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", spec.title) or "field_project"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{safe}.qgs", doc)
    return buf.getvalue(), warnings
