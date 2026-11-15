"""The drift check: the built tree against inventory.toml. Four arms, every one fails closed.

  python   every installed distribution's licence passes the allowlist or is a named exception
  native   every `*.libs` library is in the inventory; a copyleft one still has its pinned bytes
  static   copyleft code statically linked outside `*.libs` is covered by a [[static]] row, at its version
  wording  GPL/LGPL wording in a wheel's own licence files is in the reviewed set

Each arm returns (violations, notes). A violation fails the build; a note is printed and changes nothing.
"""
import fnmatch
import hashlib
import os
import re
import tomllib
from importlib import metadata
from pathlib import Path

from . import elf, licensing

INVENTORY = Path(__file__).with_name("inventory.toml")
WORDING = re.compile(r"\b(?:A?L?GPL|(?:Lesser|Library) General Public|GNU General Public)", re.I)
LICENCE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.I)
HEX8 = re.compile(r"-([0-9a-f]{8})(?=[.-])")


class InventoryError(Exception):
    """The inventory is unreadable or incomplete — the check cannot vouch for anything."""


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def load_inventory(path=INVENTORY):
    try:
        inv = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise InventoryError(f"cannot read {path}: {e}") from e
    if not isinstance(inv.get("audited", {}).get("arch"), str):
        raise InventoryError("[audited] arch is missing")
    names = set()
    for row in inv.get("native", []):
        for key in ("name", "project", "license", "license_text"):
            if not row.get(key):
                raise InventoryError(f"[[native]] row {row.get('name', '?')!r} lacks {key!r}")
        if row["name"] in names:
            raise InventoryError(f"[[native]] {row['name']!r} is listed twice")
        names.add(row["name"])
        if row.get("copyleft"):
            _require_copyleft_fields(row, "native", ("version", "libs_dir", "hashes", "source", "built_by"))
            if not all(re.fullmatch(r"[0-9a-f]{8}", h) for h in row["hashes"]):
                raise InventoryError(f"[[native]] {row['name']!r}: hashes must be 8 lowercase hex digits")
    for row in inv.get("static", []):
        _require_copyleft_fields(row, "static", ("files", "fingerprint", "project", "version", "license",
                                                 "license_text", "source", "built_by"))
    for row in inv.get("fingerprint", []):
        if not row.get("symbol") or not row.get("project"):
            raise InventoryError("[[fingerprint]] rows need symbol and project")
    for row in inv.get("python_source", []):
        if not row.get("name") or not row.get("version") or not row.get("source"):
            raise InventoryError("[[python_source]] rows need name, version and source")
        for src in row["source"]:
            if not str(src.get("url", "")).startswith("https://"):
                raise InventoryError(f"[[python_source]] {row['name']!r}: every source needs an https url")
    for row in inv.get("licence_wording", []):
        if not row.get("dist") or not row.get("file") or not row.get("reason"):
            raise InventoryError("[[licence_wording]] rows need dist, file and reason")
    return inv


def _require_copyleft_fields(row, table, keys):
    label = row.get("name") or row.get("project") or "?"
    for key in keys:
        if not row.get(key):
            raise InventoryError(f"[[{table}]] {label!r} is copyleft and lacks {key!r}")
    for src in row["source"]:
        if not str(src.get("url", "")).startswith("https://"):
            raise InventoryError(f"[[{table}]] {label!r}: every source needs an https url")


# ── python ───────────────────────────────────────────────────────────────────────────────────────────────
def check_python(dists, inv=None):
    rows = [(d.metadata.get("Name") or "<unnamed>", licensing.dist_licence(d)) for d in dists
            if (d.metadata.get("Name") or "").lower() not in licensing.SKIP_DISTS]
    if not rows:
        raise InventoryError("no Python distributions found — wrong site-packages?")
    violations, excepted = licensing.judge(rows)
    out = [f"Python package {n}: licence {lic!r} is outside the allowlist and not a named exception"
           for n, lic in violations]
    # A copyleft package whose source is not its own sdist ([[python_source]]) is described for ONE version:
    # another version needs its source re-traced, or NOTICES and the release would name the wrong source.
    installed = {canonical(d.metadata.get("Name") or ""): d.version for d in dists}
    for r in (inv or {}).get("python_source", []):
        have = installed.get(canonical(r["name"]))
        if have and have != r["version"]:
            out.append(f"{r['name']} is {have}, but its [[python_source]] row describes {r['version']}: re-trace "
                       "where this version's source comes from, then update the row")
    return out, [f"named exception: {n} ({lic}) — {why}" for n, lic, why in excepted]


# ── native (*.libs) ──────────────────────────────────────────────────────────────────────────────────────
def bundled_libraries(site_packages):
    """Yields (libs_dir, name, hashes, filename) for every file in a `*.libs` directory."""
    for libdir in sorted(Path(site_packages).glob("*.libs")):
        for f in sorted(libdir.iterdir()):
            hashes = HEX8.findall(f.name)
            name = HEX8.sub("", f.name).split(".so")[0]
            yield libdir.name, name, hashes, f.name


def check_native(site_packages, inv, enforce_bytes):
    rows = {r["name"]: r for r in inv.get("native", [])}
    violations, notes, seen = [], [], set()
    for libs_dir, name, hashes, filename in bundled_libraries(site_packages):
        seen.add(name)
        row = rows.get(name)
        if row is None:
            violations.append(f"NEW bundled library {libs_dir}/{filename}: identify its project and licence, "
                              "then add it to inventory.toml")
            continue
        if row.get("copyleft") and enforce_bytes:
            shipped = row.get("sha256")
            if shipped and libs_dir == row["libs_dir"] and hashes == row["hashes"]:
                actual = hashlib.sha256((Path(site_packages) / libs_dir / filename).read_bytes()).hexdigest()
                if actual != shipped:
                    violations.append(f"copyleft library CHANGED IN PLACE: {libs_dir}/{filename} ({row['project']}) "
                                      "has the audited name but not the audited bytes. Re-trace it, then update the row")
            if libs_dir != row["libs_dir"] or hashes != row["hashes"]:
                violations.append(
                    f"copyleft library CHANGED: {libs_dir}/{filename} ({row['project']}) — pinned "
                    f"{row['libs_dir']} {'-'.join(row['hashes'])}, found {libs_dir} {'-'.join(hashes) or 'no hash'}. "
                    "Re-trace its version and exact source, then update the row")
    for name in sorted(set(rows) - seen):
        notes.append(f"inventoried library no longer bundled: {name} (prune the row when convenient)")
    return violations, notes


# ── static (fingerprints outside *.libs) ─────────────────────────────────────────────────────────────────
def shared_objects(site_packages):
    for root, dirs, files in os.walk(site_packages):
        dirs[:] = [d for d in dirs if not d.endswith(".libs") and d != "__pycache__"]
        for f in files:
            if ".so" in f:
                yield Path(root) / f


def check_static(site_packages, inv, enforce_bytes):
    fingerprints = {r["symbol"]: r["project"] for r in inv.get("fingerprint", [])}
    statics = inv.get("static", [])
    violations, notes, parsed = [], [], 0
    for path in shared_objects(site_packages):
        syms = elf.defined_symbols(path)
        if syms is None:
            continue
        parsed += 1
        rel = path.relative_to(site_packages).as_posix()
        for sym in sorted(syms & set(fingerprints)):
            row = next((r for r in statics if r["fingerprint"] == sym and fnmatch.fnmatch(rel, r["files"])), None)
            if row is None:
                violations.append(f"copyleft code STATICALLY LINKED: {rel} defines {sym} ({fingerprints[sym]}) "
                                  "and no [[static]] row covers it")
            elif enforce_bytes and row.get("file_sha256") and (
                    rel not in row["file_sha256"]
                    or hashlib.sha256(path.read_bytes()).hexdigest() != row["file_sha256"][rel]):
                violations.append(f"{rel} carries statically linked {row['project']} and is not the audited file "
                                  "(new or rebuilt). Re-trace what it links, then update the row's file_sha256")
            elif enforce_bytes and row.get("version_symbol"):
                raw = elf.symbol_bytes(path, row["version_symbol"])
                value = int.from_bytes(raw[:4], "little") if raw else None
                if value != row["version_value"]:
                    violations.append(
                        f"statically linked {row['project']} CHANGED in {rel}: {row['version_symbol']} = "
                        f"{hex(value) if value is not None else 'absent'}, pinned {hex(row['version_value'])} "
                        f"({row['version']}). Re-trace its source, then update the row")
    if parsed == 0 and any(True for _ in shared_objects(site_packages)):
        raise InventoryError("no ELF shared object could be parsed — the static arm would pass vacuously")
    return violations, notes


# ── wording (wheels' own licence files) ──────────────────────────────────────────────────────────────────
def check_wording(dists, inv):
    reviewed = {(canonical(r["dist"]), r["file"]) for r in inv.get("licence_wording", [])}
    violations, found = [], set()
    for d in dists:
        name = canonical(d.metadata.get("Name") or "")
        # Packaging tooling (pip & co.) differs between the image and any CI interpreter and vendors only
        # permissive code; its wording would make the check depend on which interpreter runs it. Its licence
        # texts are still reproduced in NOTICES whenever it ships in the image.
        if name in licensing.SKIP_DISTS:
            continue
        for f in d.files or []:
            s = str(f)
            if ".dist-info/" not in s or not LICENCE_FILE.search(os.path.basename(s)):
                continue
            try:
                text = d.locate_file(f).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                violations.append(f"unreadable licence file {s}: {e}")
                continue
            if WORDING.search(text):
                key = (name, s.split(".dist-info/", 1)[1])
                found.add(key)
                if key not in reviewed:
                    violations.append(f"GPL/LGPL wording NEW in {name}'s {key[1]}: read it — it may declare "
                                      "bundled copyleft code — then record it in [[licence_wording]]")
    notes = [f"reviewed licence wording no longer present: {d} {f}" for d, f in sorted(reviewed - found)]
    return violations, notes


# ── binary (what the Dockerfile adds outside Python and Debian; checked only inside the image) ───────────
def go_build_modules(binary):
    """{(module, version)} a Go binary's build info lists as compiled in."""
    data = Path(binary).read_bytes()
    return {(m.decode(), v.decode()) for m, v in re.findall(rb"(?m)^dep\t([^\t\n]+)\t([^\t\n]+)", data)}


def manifest(key):
    """The manifest of a binary's dependency licence texts (licenses/<key>/*.toml)."""
    files = sorted((INVENTORY.parent / "licenses" / key).glob("*.toml"))
    if len(files) != 1:
        raise InventoryError(f"licenses/{key} must hold exactly one manifest")
    return tomllib.loads(files[0].read_text(encoding="utf-8"))


def check_binaries(image_root, inv, enforce_bytes=True):
    listed = {r["path"] for r in inv.get("binary", [])}
    bindir = Path(image_root) / "usr/local/bin"
    violations, notes = [], []
    for f in sorted(bindir.iterdir()) if bindir.is_dir() else []:
        if f.is_symlink() or not f.is_file():
            continue
        with open(f, "rb") as fh:
            if fh.read(4) != b"\x7fELF":
                continue
        path = "/" + f.relative_to(image_root).as_posix()
        if path not in listed:
            violations.append(f"NEW executable {path}: identify its project, licence and bundled code, then add "
                              "a [[binary]] row to inventory.toml")
    for r in inv.get("binary", []) + inv.get("asset", []):
        path = Path(image_root) / r["path"].lstrip("/")
        if not path.exists():
            notes.append(f"inventoried {r['path']} is not in the image (prune the row when convenient)")
            continue
        if r.get("go_modules"):
            have = {(e["module"], e["version"]) for e in manifest(r["go_modules"]).values()}
            built = go_build_modules(path)
            if built != have:
                diff = sorted(f"{m}@{v}" for m, v in built ^ have)[:6]
                violations.append(f"{r['project']}'s compiled-in Go modules CHANGED ({len(built ^ have)} "
                                  f"differ, e.g. {', '.join(diff)}): re-fetch their licence texts")
        if r.get("sha256") and enforce_bytes:
            if hashlib.sha256(path.read_bytes()).hexdigest() != r["sha256"]:
                violations.append(f"{r['project']} at {r['path']} CHANGED (sha256 no longer matches): re-fetch "
                                  "the licence texts of what it compiles in, then update the row")
    return violations, notes


def run(site_packages, arch, release, inv=None, dists=None, image_root=None):
    """Runs every arm. Returns (violations, notes). enforce_bytes only on the audited architecture: hashes
    pin that architecture's bytes; elsewhere a release build fails and a dev build reports. image_root is
    given only inside the image, where /usr/local/bin is the image's own."""
    inv = inv if inv is not None else load_inventory()
    dists = list(dists if dists is not None else metadata.distributions(path=[str(site_packages)]))
    audited = arch == inv["audited"]["arch"]
    violations, notes = [], []
    if not audited:
        msg = (f"architecture {arch} is not audited (inventory pins {inv['audited']['arch']}): copyleft bytes "
               "are not verified and this image must not be distributed")
        (violations if release else notes).append(msg)
    for v, n in (check_python(dists, inv),
                 check_native(site_packages, inv, audited),
                 check_static(site_packages, inv, audited),
                 check_wording(dists, inv),
                 check_binaries(image_root, inv, audited) if image_root else ([], [])):
        violations += v
        notes += n
    return violations, notes
