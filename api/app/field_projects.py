"""Field-project (.qgz) upload validation + metadata sniff (V085, decision-field-projects.md F5/F6).

THE FENCE: Talus never opens or edits a .qgz — the artifact is stored and served as opaque
bytes. Everything in this module is the upload LIP (is it a zip, does it hold a .qgs, is it a
sane size) plus a strictly read-only, best-effort catalog sniff (project title / layer count).
A malformed project still stores fine — the sniff yields blanks and a warning, never a reject.

Hard floor (raises NotAFieldProject -> 400; the size cap is enforced upstream while spooling):
  * zip magic bytes (PK\\x03\\x04) + parses as a zip
  * contains at least one real .qgs member
  * zip-bomb guard: member-count cap + declared-decompressed-sum ceiling. Declared sizes can
    lie, but nothing here trusts them for a read — the ONLY decompression ever performed is the
    sniffed .qgs member, and that read is byte-capped regardless of what the entry declares
    (the GeoParquet ceiling pattern, layer_features.py).

Warnings (stored on the row, surfaced in the catalog/Manage UI — never blocking, F5):
  * multiple_qgs        — more than one .qgs-ish member (.qgs / .qgs~). The .qgs~ is QGIS's
                          PRE-CONVERSION backup that still edits the server — a live B5c field
                          hazard (session-summary-2026-08-15 §3.4); QField mis-opens neighbors.
  * os_junk             — __MACOSX/ or AppleDouble ._* members (Finder-compressed zips).
  * foreign_datasources — no datasource in the project references this portal's advertised
                          origin (best-effort; skipped when no origin is configured).
  * unreadable_metadata — the sniff failed (bad XML, DTD present, oversized member). The
                          artifact stores regardless; the card just shows blanks.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

# Bomb-guard bounds (F5; build-tunable constants, the guard itself is not optional).
MEMBER_COUNT_CAP = 1000
DECLARED_SUM_CAP = 512 * 1024 * 1024   # sum of declared uncompressed sizes
QGS_SNIFF_CAP = 64 * 1024 * 1024       # actual decompressed bytes read from the sniffed .qgs

_ZIP_MAGIC = b"PK\x03\x04"
_URL_HOST_RE = re.compile(r"https?://([^/\s'\"@]+@)?([^/\s:'\"]+)", re.IGNORECASE)


class NotAFieldProject(Exception):
    """Upload fails the hard floor — the message is the 400 detail."""


@dataclass
class QgzInfo:
    qgs_title: str | None = None
    layer_count: int | None = None
    warnings: list[str] = field(default_factory=list)


def sha256_file(path: str) -> str:
    """Streamed sha256 of the spooled upload — the row's content identity and the serving
    door's strong ETag. BLOCKING — call via a threadpool."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_junk(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return name.startswith("__MACOSX/") or base.startswith("._")


def inspect_qgz(path: str, expected_origin_host: str | None) -> QgzInfo:
    """Validate the hard floor and run the best-effort sniff. BLOCKING — call via a threadpool.
    Raises NotAFieldProject on floor failures; every sniff-side failure degrades to a warning."""
    with open(path, "rb") as f:
        if f.read(4) != _ZIP_MAGIC:
            raise NotAFieldProject("not a .qgz — the file is not a zip archive")

    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise NotAFieldProject("not a .qgz — the zip archive is unreadable") from exc

    with zf:
        entries = zf.infolist()
        if len(entries) > MEMBER_COUNT_CAP:
            raise NotAFieldProject(
                f"archive holds {len(entries)} entries (cap {MEMBER_COUNT_CAP}) — not a QGIS project"
            )
        declared = sum(max(e.file_size, 0) for e in entries)
        if declared > DECLARED_SUM_CAP:
            raise NotAFieldProject(
                "archive declares more than "
                f"{DECLARED_SUM_CAP // (1024 * 1024)} MB uncompressed — refused as a zip-bomb guard"
            )

        names = [e.filename for e in entries if not e.is_dir()]
        qgs_members = sorted(
            (n for n in names if n.lower().endswith(".qgs") and not _is_junk(n)),
            key=lambda n: (n.count("/"), n),
        )
        if not qgs_members:
            raise NotAFieldProject("not a .qgz — no .qgs project file inside the archive")

        info = QgzInfo()
        qgs_ish = [n for n in names if n.lower().endswith((".qgs", ".qgs~")) and not _is_junk(n)]
        if len(qgs_ish) > 1:
            info.warnings.append("multiple_qgs")
        if any(_is_junk(n) for n in names):
            info.warnings.append("os_junk")

        _sniff(zf, qgs_members[0], expected_origin_host, info)
        return info


def _sniff(zf: zipfile.ZipFile, member: str, expected_origin_host: str | None, info: QgzInfo) -> None:
    """Read-only catalog sniff of the project XML. Any failure -> unreadable_metadata + blanks."""
    try:
        with zf.open(member) as f:
            data = f.read(QGS_SNIFF_CAP + 1)
        if len(data) > QGS_SNIFF_CAP:
            info.warnings.append("unreadable_metadata")
            return
        # Every real QGIS project opens with `<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd'
        # 'SYSTEM'>` — a bare DOCTYPE is normal and harmless (expat resolves no external DTDs).
        # The entity-expansion attack vehicle is an entity DECLARATION, so that is what we refuse,
        # scanning the WHOLE (already size-capped) buffer — a prefix-only scan is paddable.
        # (First guard shipped refused any DOCTYPE and broke the sniff on every legitimate upload —
        # caught by the acceptance fixture, the generator's own output.)
        if b"<!ENTITY" in data:
            info.warnings.append("unreadable_metadata")
            return
        root = ET.fromstring(data)
    except Exception:
        info.warnings.append("unreadable_metadata")
        return

    title = (root.get("projectname") or "").strip()
    if not title:
        title = (root.findtext("title") or "").strip()
    info.qgs_title = title or None

    layers = root.findall(".//projectlayers/maplayer") or root.findall(".//maplayer")
    info.layer_count = len(layers)

    if expected_origin_host:
        hosts: set[str] = set()
        for ds in root.iter("datasource"):
            for m in _URL_HOST_RE.finditer(ds.text or ""):
                hosts.add(m.group(2).lower())
        if expected_origin_host.lower() not in hosts:
            info.warnings.append("foreign_datasources")
