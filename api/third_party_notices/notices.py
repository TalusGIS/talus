"""NOTICES for the talus-api image, generated from the image itself — never maintained by hand.

    python3 -m third_party_notices generate --out /usr/share/doc/talus

Writes NOTICES (for people) and NOTICES.json (for the release pipeline, which publishes the exact source of
every copyleft component listed in it). Every licence text is reproduced once; anything whose text cannot be
found fails the build rather than shipping a NOTICES with a hole in it.
"""
import hashlib
import json
import platform
import re
import subprocess
import textwrap
from importlib import metadata
from pathlib import Path

from . import drift, licensing

LICENSES = Path(__file__).with_name("licenses")
RULE = "=" * 100
SPDX = {"LGPL-2.1-or-later", "LGPL-3.0-only", "GPL-2.0-or-later", "GPL-3.0-or-later", "GCC-exception-3.1",
        "Apache-2.0", "MPL-2.0", "BSD-2-Clause"}
COPYLEFT_ID = re.compile(r"\b(A?L?GPL|MPL|EPL|CDDL|EUPL|OSL)", re.I)
COPYLEFT_TEXT = re.compile(r"Mozilla Public License|GNU (?:Lesser |Library )?General Public License", re.I)


def arms(expr):
    return [a.strip(" ()") for a in re.split(r"\s+OR\s+|/", expr or "") if a.strip(" ()")]


def expr_copyleft(expr):
    """True when every alternative of a licence expression is copyleft (no permissive choice exists)."""
    return bool(arms(expr)) and all(COPYLEFT_ID.search(a) for a in arms(expr))


def no_file_note(lic, authors):
    """How NOTICES covers a dependency that ships no licence file — or None when it cannot."""
    a = arms(lic)
    if any(x.startswith("Apache-2.0") for x in a):
        return "ships no licence file; the Apache License 2.0 applies and is reproduced in section 7"
    if any(x in ("0BSD", "CC0-1.0", "Unlicense") for x in a):
        return f"ships no licence file; {lic} requires no notice"
    if "BSD-2-Clause" in a:
        return (f"ships no licence file; BSD-2-Clause, copyright its authors ({', '.join(authors) or 'unnamed'}); "
                "the license is reproduced in section 7")
    return None
APACHE = {"apache-2.0"}
SOURCE_URL = "https://github.com/TalusGIS/talus/releases/tag/{version}"


class NoticesError(Exception):
    """A licence text NOTICES must carry could not be found."""


def wrap(text, width=100):
    return "\n".join(textwrap.fill(p, width) if p.strip() else "" for p in text.split("\n"))


def debian_packages(run=subprocess.run):
    fmt = "${Package}\t${Version}\t${source:Package}\t${source:Version}\t${db:Status-Abbrev}\n"
    try:
        out = run(["dpkg-query", "-W", f"-f={fmt}"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise NoticesError(f"cannot list Debian packages: {e}") from e
    rows = []
    for line in out.splitlines():
        pkg, ver, src, srcver, status = (line.split("\t") + [""] * 5)[:5]
        if status.startswith("ii"):
            rows.append({"package": pkg, "version": ver, "source": src or pkg.split(":")[0],
                         "source_version": srcver or ver})
    if not rows:
        raise NoticesError("dpkg reports no installed packages")
    return rows


def licence_files(dist):
    out = []
    for f in dist.files or []:
        s = str(f)
        if ".dist-info/" in s and drift.LICENCE_FILE.search(Path(s).name):
            out.append((s.split(".dist-info/", 1)[1], dist.locate_file(f).read_text(encoding="utf-8", errors="replace")))
    return sorted(out)


class Texts:
    """Reproduces each distinct text once; later identical texts point back to the first."""

    def __init__(self):
        self.seen = {}

    def emit(self, label, text):
        h = hashlib.sha256(text.strip().encode()).hexdigest()
        if h in self.seen:
            return f"--- {label}: identical to the text reproduced above for {self.seen[h]}\n"
        self.seen[h] = label
        return f"--- {label}\n\n{text.rstrip()}\n"


def key_texts(key, image_root="/"):
    """[(name, text)] for an inventory license_text key."""
    if key.startswith("image:"):
        p = Path(image_root) / key[6:].lstrip("/")
        files = sorted(p.iterdir()) if p.is_dir() else [p]
        found = [(f.name, f.read_text(encoding="utf-8", errors="replace")) for f in files if f.is_file()]
    else:
        d = LICENSES / key
        found = [(f.name, f.read_text(encoding="utf-8")) for f in sorted(d.iterdir())] if d.is_dir() else []
    if not found:
        raise NoticesError(f"no licence text for {key!r}")
    return found


def generate(site_packages, out_dir, arch, version, inv=None, dists=None, debian=None, image_root="/", modified=None):
    """modified: the drift check's differences when an operator built with TALUS_THIRD_PARTY_CHECK=report —
    NOTICES then says so first, and a library the inventory does not know is listed as unaudited."""
    inv = inv if inv is not None else drift.load_inventory()
    dists = sorted(dists if dists is not None else metadata.distributions(path=[str(site_packages)]),
                   key=lambda d: drift.canonical(d.metadata["Name"]))
    debian = debian if debian is not None else debian_packages()
    audited = arch == inv["audited"]["arch"]
    texts = Texts()
    rows = {r["name"]: r for r in inv.get("native", [])}
    src_url = SOURCE_URL.format(version=version)
    copyleft, out = [], []

    # ── 1. where the source is ──
    out += [RULE, "OPEN-SOURCE NOTICES — the talus-api image of Talus GIS " + version, RULE, "",
            "1. WHERE TO GET THE SOURCE", ""]
    if not audited:
        out += [f"NOT FOR DISTRIBUTION: architecture {arch} is not audited (the inventory pins "
                f"{inv['audited']['arch']}); its copyleft components were not verified.", ""]
    if modified:
        out += [wrap("MODIFIED BUILD: this image was rebuilt with third-party components that differ from the ones "
                     "Talus GIS released and audited. Where they differ, the versions, licenses and sources below "
                     "describe the released components, not these. The differences:")]
        out += [f"  - {m}" for m in modified] + [""]
    out.append(wrap(
        "This image includes software written by others and distributed under their own licenses. Some of it is "
        "licensed under the GNU General Public License (GPL), the GNU Lesser General Public License (LGPL) or the "
        "Mozilla Public License (MPL). For "
        "each such component, the complete corresponding source code for the exact version in this image is "
        f"published with this release, free of charge, at:\n\n    {src_url}\n\n    "
        f"talus-api-{version}-third-party-source.tar  — the components listed in section 2\n    "
        f"talus-api-{version}-debian-source.tar       — the Debian packages listed in section 5\n\nInstructions for replacing the LGPL components and "
        "rebuilding the image: docs/replacing-lgpl-components.md in the same release.\n\nThis file is generated "
        "from the image when it is built. Section 2 lists the GPL, LGPL and MPL components; sections 3 to 6 list "
        "everything else; section 7 reproduces the standard license texts once each."))

    # ── collect copyleft ──
    overrides = {(drift.canonical(r["name"]), r["version"]): r["source"] for r in inv.get("python_source", [])}
    for d in dists:
        name = d.metadata["Name"]
        if name.lower() in licensing.EXCEPTIONS:
            source = overrides.get((drift.canonical(name), d.version)) or [
                {"pypi_sdist": f"{name}=={d.version}", "url": f"https://pypi.org/project/{name}/{d.version}/#files",
                 "role": "its source distribution on PyPI"}]
            copyleft.append({"component": name, "version": d.version, "license": licensing.dist_licence(d),
                             "how": "Python package", "where": "site-packages", "source": source})
    present = list(drift.bundled_libraries(site_packages))
    for libs_dir, name, hashes, filename in present:
        r = rows.get(name)
        if r and r.get("copyleft"):
            copyleft.append({"component": r["project"], "version": r["version"], "license": r["license"],
                             "how": f"shared library bundled in a wheel, built by: {r['built_by']}",
                             "where": f"site-packages/{libs_dir}/{filename}", "source": r["source"]})
    for r in inv.get("static", []):
        copyleft.append({"component": r["project"], "version": r["version"], "license": r["license"],
                         "how": f"statically linked into {r['files']}, built by: {r['built_by']}",
                         "where": f"site-packages/{r['files']}", "source": r["source"]})
    deps = {}  # binary path -> [(label, licence, entry)] of the code compiled into it
    for r in inv.get("binary", []):
        path = Path(image_root) / r["path"].lstrip("/")
        if r.get("go_modules"):
            entries = {f"{e['module']}@{e['version']}": e for e in drift.manifest(r["go_modules"]).values()}
            rows_ = []
            for m, v in sorted(drift.go_build_modules(path)):
                e = entries.get(f"{m}@{v}")
                if e is None:
                    raise NoticesError(f"{r['project']} compiles in {m}@{v}, which has no licence texts")
                rows_.append((f"{m} {v}", None, e, r["go_modules"]))
                body = "".join((LICENSES / r["go_modules"] / f["text"]).read_text(encoding="utf-8", errors="replace")
                               for f in e["files"])
                if COPYLEFT_TEXT.search(body):
                    copyleft.append({"component": f"{m} (Go module, in {r['project']})", "version": v,
                                     "license": "see its licence text, section 6", "how": f"compiled into {r['path']}",
                                     "where": r["path"], "source": [{"url": f"{e['repo']}/tree/{e['commit']}",
                                                                     "role": "repository at the exact commit"}]})
            deps[r["path"]] = ("Go modules", rows_)
        if r.get("rust_crates"):
            rows_ = []
            for e in drift.manifest(r["rust_crates"]).values():
                rows_.append((f"{e['name']} {e['version']}", e["license"], e, r["rust_crates"]))
                if expr_copyleft(e["license"]):
                    copyleft.append({"component": f"{e['name']} (Rust crate, in {r['project']})",
                                     "version": e["version"], "license": e["license"],
                                     "how": f"compiled into {r['path']}", "where": r["path"],
                                     "source": [{"url": f"https://static.crates.io/crates/{e['name']}/"
                                                       f"{e['name']}-{e['version']}.crate",
                                                 "sha256": e["crate_sha256"], "role": "the crate's source"}]})
            deps[r["path"]] = ("Rust crates", rows_)

    out += ["", RULE, "2. GPL, LGPL AND MPL COMPONENTS", RULE, ""]
    for c in copyleft:
        out += [f"* {c['component']} {c['version']} — {c['license']}", f"  {c['how']}", f"  in this image: {c['where']}",
                "  source: " + "; ".join(s["url"] + (f" ({s['role']})" if s.get("role") else "") for s in c["source"]), ""]
    out.append(wrap("The Debian packages in section 5 include software licensed under the GPL and LGPL (the shell, "
                    "core utilities, the C library and others). The Debian source package of every one of them, at "
                    "the exact version in this image, is published with this release at the address in section 1."))

    # ── 3. python ──
    out += ["", RULE, "3. PYTHON PACKAGES", RULE, ""]
    for d in dists:
        # Every package in the image, packaging tooling included: pip ships in it, so its notices do too.
        name, lic = d.metadata["Name"], licensing.dist_licence(d)
        out.append(f"* {name} {d.version} — {lic}")
        files = licence_files(d)
        if files:
            out += [texts.emit(f"{name} {rel}", text) for rel, text in files]
        elif lic and any(licensing.norm(p) in APACHE for p in re.split(r";|\s+OR\s+", lic)):
            out.append("  (ships no licence file; the Apache License 2.0 is reproduced in section 7)\n")
        else:
            raise NoticesError(f"Python package {name} ships no licence file and is not Apache-2.0")

    # ── 4. native ──
    out += ["", RULE, "4. NATIVE LIBRARIES BUNDLED IN PYTHON WHEELS", RULE, ""]
    keys = {}
    for libs_dir, name, hashes, filename in present:
        r = rows.get(name)
        if r is None:
            if modified is None:
                raise NoticesError(f"{libs_dir}/{filename} is not in the inventory — run the check first")
            out.append(f"* {libs_dir}/{filename} — NOT IN THE AUDITED INVENTORY (modified build)")
            continue
        out.append(f"* {libs_dir}/{filename} — {r['project']} — {r['license']}")
        keys.setdefault(r["license_text"], []).append(r["project"])
    for r in inv.get("static", []) + inv.get("embedded", []):
        where = r.get("files") or f"the {r['dist']} wheel's extension modules"
        out.append(f"* {r['project']} — {r['license']} — statically linked into {where}")
        keys.setdefault(r["license_text"], []).append(r["project"])
    out.append("")
    for key, projects in keys.items():
        project = ", ".join(dict.fromkeys(projects))
        if key.startswith("dist:"):
            out.append(f"--- {project}: full text in the {key[5:]} package's licence files, section 3\n")
            continue
        out += [texts.emit(f"{project} — {n}", t) for n, t in key_texts(key, image_root)]

    # ── 5. debian ──
    out += ["", RULE, "5. DEBIAN PACKAGES", RULE, "",
            wrap("The copyright and license text of each package is in this image at "
                 "/usr/share/doc/<package>/copyright. Package (version) — Debian source package (version):"), ""]
    out += [f"* {p['package']} ({p['version']}) — {p['source']} ({p['source_version']})" for p in debian]

    # ── 6. other ──
    out += ["", RULE, "6. OTHER SOFTWARE IN THIS IMAGE", RULE, ""]
    for r in inv.get("binary", []) + inv.get("asset", []):
        ver = r.get("version") or (platform.python_version() if "CPython" in r["project"] else "")
        out.append(f"* {r['path']} — {r['project']} {ver} — {r['license']}".replace("  ", " "))
        out += [texts.emit(f"{r['project']} — {n}", t) for n, t in key_texts(r["license_text"], image_root)]
        if r["path"] in deps:
            kind, rows_ = deps[r["path"]]
            out.append(wrap(f"{r['project']} is statically linked and also contains these {len(rows_)} {kind}, "
                            "each with its own license text:") + "\n")
            for label, lic, e, key in rows_:
                out.append(f"  * {label}" + (f" — {lic}" if lic else ""))
                if e["files"]:
                    out += [texts.emit(f"{label} {f['file']}",
                                       (LICENSES / key / f["text"]).read_text(encoding="utf-8", errors="replace"))
                            for f in e["files"]]
                else:
                    note = no_file_note(lic, e.get("authors", []))
                    if note is None:
                        raise NoticesError(f"{label} ships no licence file and {lic!r} needs one")
                    out.append(f"    ({note})\n")

    # ── 7. standard texts ──
    out += ["", RULE, "7. LICENSE TEXTS", RULE, ""]
    out += [texts.emit(n[:-4], t) for n, t in key_texts("spdx") if n[:-4] in SPDX]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "NOTICES").write_text("\n".join(out) + "\n", encoding="utf-8")
    (out_dir / "NOTICES.json").write_text(json.dumps({
        "version": version, "arch": arch, "audited": audited and not modified, "modified": modified or [],
        "copyleft": copyleft,
        "debian": debian, "python": [{"name": d.metadata["Name"], "version": d.version,
                                      "license": licensing.dist_licence(d)} for d in dists],
    }, indent=1) + "\n", encoding="utf-8")
    return out_dir / "NOTICES"
