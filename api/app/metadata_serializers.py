"""Talus 33 c7 — metadata serializers: the ISO-19115-cored `item_metadata.doc` -> open standards.

Pure functions `doc -> XML string`, the open-source "your metadata isn't locked in" promise: a
documented dataset exports to **QGIS `.qmd`** (the desktop user's native sidecar) and **ISO 19139**
(the ISO 19115 XML encoding everyone harvests). No new dependency — built with stdlib ElementTree, so
user content (title/abstract/contact) is XML-escaped for free. The DCAT-US feed (c5) is the third
serializer off the same record; these are read-only views, never a second source of truth.

The `doc` is the stored JSON (shape per `metadata.MetadataDoc`): title, abstract, keywords[],
topicCategory, license, pointOfContact{name,organization,email,role}, language, spatialExtent{W,S,E,N}.
Optional fields are simply omitted from the output; a documented (Open Data-complete) item carries the
required core, so its export is full.
"""
from typing import Any
from xml.etree import ElementTree as ET

_ISO_CODELIST = (
    "http://standards.iso.org/ittf/PubliclyAvailableStandards/ISO_19139_Schemas/resources/codelist/"
    "gmxCodelists.xml"
)
_GMD = "http://www.isotc211.org/2005/gmd"
_GCO = "http://www.isotc211.org/2005/gco"

# ISO 639-2/B 3-letter for the few languages a municipal deployment realistically tags; default eng.
_ISO_LANG = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu", "pt": "por"}


def _lang3(value: Any) -> str:
    v = (value or "en").strip().lower()
    return _ISO_LANG.get(v, v if len(v) == 3 else "eng")


def _xml(root: ET.Element) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# --- QGIS .qmd ------------------------------------------------------------------


def to_qmd(doc: dict[str, Any], identifier: str) -> str:
    """Serialize to a QGIS layer-metadata `.qmd` document (QGIS reads this as a layer's metadata)."""
    qgis = ET.Element("qgis", version="3.34.0")
    ET.SubElement(qgis, "identifier").text = identifier
    ET.SubElement(qgis, "parentidentifier")
    ET.SubElement(qgis, "language").text = _lang3(doc.get("language")).upper()
    ET.SubElement(qgis, "type").text = "dataset"
    ET.SubElement(qgis, "title").text = doc.get("title") or identifier
    ET.SubElement(qgis, "abstract").text = doc.get("abstract") or ""

    if doc.get("topicCategory"):
        kw = ET.SubElement(qgis, "keywords", vocabulary="gmd:topicCategory")
        ET.SubElement(kw, "keyword").text = doc["topicCategory"]
    if isinstance(doc.get("keywords"), list) and doc["keywords"]:
        kw = ET.SubElement(qgis, "keywords", vocabulary="keywords")
        for k in doc["keywords"]:
            ET.SubElement(kw, "keyword").text = str(k)

    poc = doc.get("pointOfContact") if isinstance(doc.get("pointOfContact"), dict) else {}
    if poc.get("name") or poc.get("organization") or poc.get("email"):
        c = ET.SubElement(qgis, "contact")
        ET.SubElement(c, "name").text = poc.get("name") or ""
        ET.SubElement(c, "organization").text = poc.get("organization") or ""
        ET.SubElement(c, "role").text = poc.get("role") or "pointOfContact"
        ET.SubElement(c, "email").text = poc.get("email") or ""

    ET.SubElement(qgis, "links")
    ET.SubElement(qgis, "fees")
    if doc.get("license"):
        ET.SubElement(ET.SubElement(qgis, "rights"), "right").text = doc["license"]
        ET.SubElement(ET.SubElement(qgis, "license"), "license").text = doc["license"]
    ET.SubElement(qgis, "encoding")

    srs = ET.SubElement(ET.SubElement(qgis, "crs"), "spatialrefsys")
    ET.SubElement(srs, "authid").text = "EPSG:4326"

    bbox = doc.get("spatialExtent")
    if isinstance(bbox, dict) and all(k in bbox for k in ("west", "south", "east", "north")):
        ET.SubElement(
            ET.SubElement(qgis, "extent"), "spatial", dimensions="2", crs="EPSG:4326",
            minx=str(bbox["west"]), miny=str(bbox["south"]),
            maxx=str(bbox["east"]), maxy=str(bbox["north"]), minz="0", maxz="0",
        )
    return _xml(qgis)


# --- ISO 19139 (ISO 19115 XML encoding) -----------------------------------------


def _g(tag: str) -> str:
    return f"{{{_GMD}}}{tag}"


def _c(tag: str) -> str:
    return f"{{{_GCO}}}{tag}"


def _char(parent: ET.Element, gmd_tag: str, value: str) -> None:
    """<gmd:tag><gco:CharacterString>value</gco:CharacterString></gmd:tag>."""
    ET.SubElement(ET.SubElement(parent, _g(gmd_tag)), _c("CharacterString")).text = value


def _decimal(parent: ET.Element, gmd_tag: str, value: Any) -> None:
    ET.SubElement(ET.SubElement(parent, _g(gmd_tag)), _c("Decimal")).text = str(value)


def _responsible_party(parent: ET.Element, gmd_tag: str, poc: dict[str, Any]) -> None:
    rp = ET.SubElement(ET.SubElement(parent, _g(gmd_tag)), _g("CI_ResponsibleParty"))
    if poc.get("name"):
        _char(rp, "individualName", poc["name"])
    if poc.get("organization"):
        _char(rp, "organisationName", poc["organization"])
    if poc.get("email"):
        addr = ET.SubElement(
            ET.SubElement(ET.SubElement(rp, _g("contactInfo")), _g("CI_Contact")), _g("address")
        )
        _char(ET.SubElement(addr, _g("CI_Address")), "electronicMailAddress", poc["email"])
    role_val = poc.get("role") or "pointOfContact"
    ET.SubElement(
        ET.SubElement(rp, _g("role")), _g("CI_RoleCode"),
        codeList=f"{_ISO_CODELIST}#CI_RoleCode", codeListValue=role_val,
    ).text = role_val


def to_iso19139(doc: dict[str, Any], identifier: str, date_stamp: str) -> str:
    """Serialize to ISO 19139 `gmd:MD_Metadata` (the XML encoding of ISO 19115). Element ORDER follows
    the ISO schema sequence so the output validates; optional elements are omitted when absent."""
    ET.register_namespace("gmd", _GMD)
    ET.register_namespace("gco", _GCO)
    root = ET.Element(_g("MD_Metadata"))

    _char(root, "fileIdentifier", identifier)
    _char(root, "language", _lang3(doc.get("language")))
    ET.SubElement(
        ET.SubElement(root, _g("hierarchyLevel")), _g("MD_ScopeCode"),
        codeList=f"{_ISO_CODELIST}#MD_ScopeCode", codeListValue="dataset",
    ).text = "dataset"

    poc = doc.get("pointOfContact") if isinstance(doc.get("pointOfContact"), dict) else {}
    _responsible_party(root, "contact", poc or {"role": "pointOfContact"})

    ET.SubElement(ET.SubElement(root, _g("dateStamp")), _c("Date")).text = date_stamp
    _char(root, "metadataStandardName", "ISO 19115")
    _char(root, "metadataStandardVersion", "2003/Cor.1:2006")

    di = ET.SubElement(ET.SubElement(root, _g("identificationInfo")), _g("MD_DataIdentification"))
    cit = ET.SubElement(ET.SubElement(di, _g("citation")), _g("CI_Citation"))
    _char(cit, "title", doc.get("title") or identifier)
    # CI_Citation requires a date; use the metadata datestamp as the (revision) date.
    cdate = ET.SubElement(ET.SubElement(cit, _g("date")), _g("CI_Date"))
    ET.SubElement(ET.SubElement(cdate, _g("date")), _c("Date")).text = date_stamp
    ET.SubElement(
        ET.SubElement(cdate, _g("dateType")), _g("CI_DateTypeCode"),
        codeList=f"{_ISO_CODELIST}#CI_DateTypeCode", codeListValue="revision",
    ).text = "revision"

    _char(di, "abstract", doc.get("abstract") or "")
    if poc:
        _responsible_party(di, "pointOfContact", poc)
    if isinstance(doc.get("keywords"), list) and doc["keywords"]:
        mk = ET.SubElement(ET.SubElement(di, _g("descriptiveKeywords")), _g("MD_Keywords"))
        for k in doc["keywords"]:
            _char(mk, "keyword", str(k))
    if doc.get("license"):
        lc = ET.SubElement(ET.SubElement(di, _g("resourceConstraints")), _g("MD_LegalConstraints"))
        _char(lc, "useLimitation", doc["license"])
    _char(di, "language", _lang3(doc.get("language")))
    if doc.get("topicCategory"):
        ET.SubElement(
            ET.SubElement(di, _g("topicCategory")), _g("MD_TopicCategoryCode")
        ).text = doc["topicCategory"]

    bbox = doc.get("spatialExtent")
    if isinstance(bbox, dict) and all(k in bbox for k in ("west", "south", "east", "north")):
        gbb = ET.SubElement(
            ET.SubElement(ET.SubElement(di, _g("extent")), _g("EX_Extent")), _g("geographicElement")
        )
        box = ET.SubElement(gbb, _g("EX_GeographicBoundingBox"))
        _decimal(box, "westBoundLongitude", bbox["west"])
        _decimal(box, "eastBoundLongitude", bbox["east"])
        _decimal(box, "southBoundLatitude", bbox["south"])
        _decimal(box, "northBoundLatitude", bbox["north"])
    return _xml(root)
