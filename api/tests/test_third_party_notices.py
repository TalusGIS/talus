"""The third-party drift check: every arm passes the reviewed state and FAILS on the change it exists to catch.

Synthetic site-packages trees only (a hand-built ELF stands in for a shared object), so the suite runs on any
host — including macOS, where shipped tests run during the release sync.
"""
import hashlib
import json as _json
import struct
import tarfile

import pytest

from third_party_notices import __main__ as cli
from third_party_notices import drift, elf, notices, sources

# ── fixtures ────────────────────────────────────────────────────────────────────────────────────────────


def make_elf(path, defined=None, undefined=()):
    """A minimal ELF64 LE shared object: .dynstr, .dynsym, .data. `defined` maps symbol -> bytes it holds."""
    defined = defined or {}
    names = list(defined) + list(undefined)
    dynstr, offs = b"\0", {}
    for n in names:
        offs[n] = len(dynstr)
        dynstr += n.encode() + b"\0"
    data, at = b"", {}
    for n, v in defined.items():
        at[n] = len(data)
        data += v or b"\xc3"
    data_addr = 0x1000
    syms = b"\0" * 24
    for n in names:
        if n in defined:
            info = (1 << 4) | (1 if defined[n] else 2)  # GLOBAL OBJECT|FUNC
            syms += struct.pack("<IBBHQQ", offs[n], info, 0, 3, data_addr + at[n], len(defined[n] or b"\xc3"))
        else:
            syms += struct.pack("<IBBHQQ", offs[n], (1 << 4) | 2, 0, 0, 0, 0)  # undefined reference
    shstr = b"\0.dynstr\0.dynsym\0.data\0.shstrtab\0"
    body = [dynstr, syms, data, shstr]
    off, layout = 64, []
    for b in body:
        layout.append(off)
        off += len(b)
    shoff = (off + 7) & ~7
    out = bytearray(b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8)
    out += struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 5, 4)
    for b in body:
        out += b
    out += b"\0" * (shoff - len(out))
    sh = struct.Struct("<IIQQQQIIQQ")
    out += b"\0" * 64
    out += sh.pack(1, 3, 0, 0, layout[0], len(dynstr), 0, 0, 1, 0)
    out += sh.pack(9, 11, 0, 0, layout[1], len(syms), 1, 1, 8, 24)
    out += sh.pack(17, 1, 3, data_addr, layout[2], len(data), 0, 0, 8, 0)
    out += sh.pack(23, 3, 0, 0, layout[3], len(shstr), 0, 0, 1, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def add_dist(sp, name, licence="MIT", licence_text=None):
    di = sp / f"{name}-1.0.dist-info"
    di.mkdir(parents=True)
    meta = f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n"
    if licence:
        meta += f"License-Expression: {licence}\n"
    (di / "METADATA").write_text(meta)
    record = [f"{di.name}/METADATA,,"]
    if licence_text is not None:
        (di / "licenses").mkdir()
        (di / "licenses" / "LICENSE").write_text(licence_text)
        record.append(f"{di.name}/licenses/LICENSE,,")
    (di / "RECORD").write_text("\n".join(record) + "\n")


INV = {
    "audited": {"arch": "amd64"},
    "fingerprint": [{"symbol": "libiconv_open", "project": "GNU libiconv"}],
    "native": [
        {"name": "libgdal", "project": "GDAL", "license": "MIT", "license_text": "gdal"},
        {"name": "libgeos", "project": "GEOS", "license": "LGPL-2.1-or-later", "license_text": "geos",
         "copyleft": True, "version": "3.14.1", "libs_dir": "rasterio.libs", "hashes": ["3ec8fbdb"],
         "built_by": "x", "source": [{"url": "https://example.org/geos.tar.bz2"}]},
    ],
    "static": [],
    "licence_wording": [],
}


@pytest.fixture
def sp(tmp_path):
    site = tmp_path / "site-packages"
    add_dist(site, "goodpkg", licence_text="MIT License. Permission is hereby granted...")
    make_elf(site / "goodpkg" / "_speedups.so", defined={"PyInit__speedups": None})
    make_elf(site / "rasterio.libs" / "libgdal-61394b7a.so.38.3.12.2")
    make_elf(site / "rasterio.libs" / "libgeos-3ec8fbdb.so.3.14.1")
    return site


def run(site, inv=INV, arch="amd64", release=False):
    return drift.run(site, arch, release, inv=inv)


# ── the reviewed state passes ───────────────────────────────────────────────────────────────────────────


def test_reviewed_tree_passes(sp):
    violations, _ = run(sp)
    assert violations == []


def test_the_shipped_inventory_loads_and_pins_every_copyleft_component():
    inv = drift.load_inventory()
    copyleft = {r["name"] for r in inv["native"] if r.get("copyleft")}
    assert copyleft == {"libgeos", "libgeos_c", "libcrypt", "libkeyutils", "libquadmath", "libgfortran"}
    assert [r["project"] for r in inv["static"]] == ["GNU libiconv"]
    assert inv["audited"]["arch"] == "amd64"


# ── native arm ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_new_bundled_library_fails(sp):
    make_elf(sp / "rasterio.libs" / "libnewthing-0badf00d.so.1")
    violations, _ = run(sp)
    assert any("NEW bundled library" in v and "libnewthing" in v for v in violations)


def test_a_changed_copyleft_library_fails(sp):
    (sp / "rasterio.libs" / "libgeos-3ec8fbdb.so.3.14.1").rename(sp / "rasterio.libs" / "libgeos-deadbeef.so.3.14.2")
    violations, _ = run(sp)
    assert any("copyleft library CHANGED" in v and "GEOS" in v for v in violations)


def test_a_changed_permissive_library_passes(sp):
    (sp / "rasterio.libs" / "libgdal-61394b7a.so.38.3.12.2").rename(sp / "rasterio.libs" / "libgdal-12345678.so.38.3.13.0")
    violations, _ = run(sp)
    assert violations == []


def test_an_unaudited_arch_reports_in_dev_and_fails_a_release(sp):
    (sp / "rasterio.libs" / "libgeos-3ec8fbdb.so.3.14.1").rename(sp / "rasterio.libs" / "libgeos-deadbeef.so.3.14.1")
    violations, notes = run(sp, arch="arm64")
    assert violations == [] and any("not audited" in n for n in notes)
    violations, _ = run(sp, arch="arm64", release=True)
    assert any("not audited" in v for v in violations)


# ── static arm ──────────────────────────────────────────────────────────────────────────────────────────

STATIC_ROW = {"files": "lxml/*.so", "fingerprint": "libiconv_open", "project": "GNU libiconv", "version": "1.18",
              "version_symbol": "_libiconv_version", "version_value": 0x0112, "license": "LGPL-2.1-or-later",
              "license_text": "libiconv", "built_by": "x", "source": [{"url": "https://example.org/i.tar.gz"}]}


def test_unlisted_static_copyleft_fails(sp):
    make_elf(sp / "lxml" / "etree.so", defined={"libiconv_open": None, "_libiconv_version": (0x0112).to_bytes(4, "little")})
    violations, _ = run(sp)
    assert any("STATICALLY LINKED" in v and "lxml/etree.so" in v for v in violations)


def test_covered_static_copyleft_passes_at_its_version_and_fails_when_it_changes(sp):
    inv = dict(INV, static=[STATIC_ROW])
    make_elf(sp / "lxml" / "etree.so", defined={"libiconv_open": None, "_libiconv_version": (0x0112).to_bytes(4, "little")})
    assert run(sp, inv)[0] == []
    make_elf(sp / "lxml" / "etree.so", defined={"libiconv_open": None, "_libiconv_version": (0x0113).to_bytes(4, "little")})
    violations, _ = run(sp, inv)
    assert any("GNU libiconv CHANGED" in v and "0x113" in v for v in violations)


def test_an_undefined_reference_is_not_static_linking(sp):
    make_elf(sp / "otherpkg" / "_ext.so", undefined=["libiconv_open"])
    assert run(sp)[0] == []


def test_a_tree_with_no_readable_elf_fails_closed(tmp_path):
    site = tmp_path / "site-packages"
    add_dist(site, "goodpkg")
    (site / "goodpkg").mkdir()
    (site / "goodpkg" / "_ext.so").write_bytes(b"\xcf\xfa\xed\xfe not an ELF object")
    with pytest.raises(drift.InventoryError, match="vacuously"):
        run(site)


def test_elf_reader_reads_symbols_and_values(tmp_path):
    p = tmp_path / "x.so"
    make_elf(p, defined={"f": None, "v": b"\x12\x01\x00\x00"}, undefined=["u"])
    assert elf.defined_symbols(p) == {"f", "v"}
    assert elf.symbol_bytes(p, "v") == b"\x12\x01\x00\x00"
    assert elf.symbol_bytes(p, "u") is None


# ── wording arm ─────────────────────────────────────────────────────────────────────────────────────────


def test_new_gpl_wording_in_a_wheels_licence_file_fails_until_reviewed(sp):
    add_dist(sp, "newwheel", licence_text="This wheel bundles libfoo, under the GNU Lesser General Public License.")
    violations, _ = run(sp)
    assert any("wording NEW in newwheel" in v for v in violations)
    inv = dict(INV, licence_wording=[{"dist": "newwheel", "file": "licenses/LICENSE", "reason": "reviewed"}])
    assert run(sp, inv)[0] == []


# ── python arm ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("licence", ["GPL-3.0-only", "LGPL-2.1-or-later", "BUSL-1.1", None])
def test_a_copyleft_or_unknown_python_package_fails(sp, licence):
    add_dist(sp, "badpkg", licence=licence)
    violations, _ = run(sp)
    assert any("badpkg" in v for v in violations)


def test_the_named_lgpl_exception_passes(sp):
    add_dist(sp, "psycopg", licence="LGPL-3.0-only")
    violations, notes = run(sp)
    assert violations == [] and any("named exception: psycopg" in n for n in notes)


# ── the inventory itself fails closed ───────────────────────────────────────────────────────────────────


def test_a_copyleft_row_without_its_source_is_rejected(tmp_path):
    bad = tmp_path / "inventory.toml"
    bad.write_text('[audited]\narch = "amd64"\n[[native]]\nname = "libx"\nproject = "X"\nlicense = "LGPL-2.1"\n'
                   'license_text = "x"\ncopyleft = true\nversion = "1"\nlibs_dir = "a.libs"\nhashes = ["0badf00d"]\n'
                   'built_by = "y"\n')
    with pytest.raises(drift.InventoryError, match="lacks 'source'"):
        drift.load_inventory(bad)


# ── NOTICES ─────────────────────────────────────────────────────────────────────────────────────────────

DEBIAN = [{"package": "libc6", "version": "2.36-9+deb12u14", "source": "glibc", "source_version": "2.36-9+deb12u14"}]


def test_every_licence_text_the_inventory_names_exists():
    inv = drift.load_inventory()
    rows = sum((inv.get(t, []) for t in ("native", "static", "embedded", "binary", "asset")), [])
    sources = (notices.LICENSES / "SOURCES.toml").read_text()
    for r in rows:
        key = r["license_text"]
        if key.startswith(("dist:", "image:")):
            continue
        d = notices.LICENSES / key
        assert d.is_dir() and any(d.iterdir()), f"no licence text for {key}"
        assert f'["{key}"]' in sources, f"{key} has no provenance in SOURCES.toml"


def test_notices_lists_every_copyleft_component_with_its_source_and_reproduces_texts(sp, tmp_path):
    add_dist(sp, "psycopg", licence="LGPL-3.0-only", licence_text="GNU LESSER GENERAL PUBLIC LICENSE v3 ...")
    out = notices.generate(sp, tmp_path / "out", "amd64", "v9.9.9", inv=INV, debian=DEBIAN)
    text = out.read_text()
    assert "1. WHERE TO GET THE SOURCE" in text and "releases/tag/v9.9.9" in text
    two = text.split("2. GPL, LGPL AND MPL COMPONENTS")[1].split("3. PYTHON PACKAGES")[0]
    assert "* psycopg 1.0 — LGPL-3.0-only" in two and "* GEOS 3.14.1 — LGPL-2.1-or-later" in two
    assert "https://example.org/geos.tar.bz2" in two
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text.split("4. NATIVE LIBRARIES")[1]  # GEOS's COPYING
    assert "* libc6 (2.36-9+deb12u14) — glibc (2.36-9+deb12u14)" in text
    assert "Apache License" in text.split("7. LICENSE TEXTS")[1]
    data = __import__("json").loads((tmp_path / "out" / "NOTICES.json").read_text())
    assert {c["component"] for c in data["copyleft"]} == {"psycopg", "GEOS"} and data["audited"] is True


def test_notices_reproduce_an_identical_text_once(sp, tmp_path):
    add_dist(sp, "twinpkg", licence_text="MIT License. Permission is hereby granted...")
    text = notices.generate(sp, tmp_path / "out", "amd64", "v1", inv=INV, debian=DEBIAN).read_text()
    assert text.count("MIT License. Permission is hereby granted...") == 1
    assert "twinpkg licenses/LICENSE: identical to the text reproduced above for goodpkg" in text


def test_notices_fail_closed_when_a_licence_text_is_missing(sp, tmp_path):
    add_dist(sp, "nofilepkg", licence="MIT")
    with pytest.raises(notices.NoticesError, match="nofilepkg ships no licence file"):
        notices.generate(sp, tmp_path / "out", "amd64", "v1", inv=INV, debian=DEBIAN)


def test_an_unaudited_build_says_so_at_the_top(sp, tmp_path):
    text = notices.generate(sp, tmp_path / "out", "arm64", "v1", inv=INV, debian=DEBIAN).read_text()
    assert "NOT FOR DISTRIBUTION" in text.split("2. GPL, LGPL AND MPL")[0]


# ── binaries the Dockerfile adds ────────────────────────────────────────────────────────────────────────


def fake_image(tmp_path, go_modules=None, typst=b"typst-bytes", extra_elf=None):
    root = tmp_path / "image"
    bindir = root / "usr/local/bin"
    bindir.mkdir(parents=True)
    deps = "".join(f"dep\t{m}\t{v}\th1:x\n" for m, v in (go_modules or []))
    (bindir / "pmtiles").write_bytes(b"\x7fELF" + b"\0" * 60 + b"\nmod\tgithub.com/protomaps/go-pmtiles\n" + deps.encode())
    (bindir / "typst").write_bytes(b"\x7fELF" + typst)
    if extra_elf:
        (bindir / extra_elf).write_bytes(b"\x7fELF" + b"\0" * 60)
    (bindir / "uvicorn").write_text("#!/usr/local/bin/python3.12\n")  # console scripts are not executables we audit
    return root


def real_go_modules():
    return sorted((e["module"], e["version"]) for e in drift.manifest("go-modules").values())


BIN_INV = dict(INV, binary=[
    {"path": "/usr/local/bin/pmtiles", "project": "go-pmtiles", "license": "BSD-3-Clause",
     "license_text": "go-pmtiles", "go_modules": "go-modules"},
    {"path": "/usr/local/bin/typst", "project": "Typst", "license": "Apache-2.0", "license_text": "geos",
     "rust_crates": "typst-crates", "sha256": __import__("hashlib").sha256(b"\x7fELFtypst-bytes").hexdigest()},
])


def test_binaries_pass_when_their_compiled_in_dependencies_match(tmp_path):
    assert drift.check_binaries(fake_image(tmp_path, real_go_modules()), BIN_INV) == ([], [])


def test_a_changed_go_module_set_fails(tmp_path):
    mods = real_go_modules()[1:] + [("example.com/new", "v1.0.0")]
    violations, _ = drift.check_binaries(fake_image(tmp_path, mods), BIN_INV)
    assert any("Go modules CHANGED" in v and "example.com/new@v1.0.0" in v for v in violations)


def test_a_changed_typst_fails_on_the_audited_arch_only(tmp_path):
    root = fake_image(tmp_path, real_go_modules(), typst=b"a-newer-typst")
    assert any("Typst at /usr/local/bin/typst CHANGED" in v for v in drift.check_binaries(root, BIN_INV)[0])
    assert drift.check_binaries(root, BIN_INV, enforce_bytes=False)[0] == []


def test_a_new_executable_in_usr_local_bin_fails(tmp_path):
    violations, _ = drift.check_binaries(fake_image(tmp_path, real_go_modules(), extra_elf="mystery"), BIN_INV)
    assert violations == ["NEW executable /usr/local/bin/mystery: identify its project, licence and bundled code, "
                          "then add a [[binary]] row to inventory.toml"]


def test_the_shipped_dependency_manifests_have_a_text_for_every_file():
    for key in ("go-modules", "typst-crates"):
        for entry in drift.manifest(key).values():
            for f in entry["files"]:
                assert (notices.LICENSES / key / f["text"]).is_file(), f"{key}: {f['text']} missing"


@pytest.mark.parametrize("expr,copyleft", [("MPL-2.0", True), ("LGPL-2.1-or-later", True), ("MIT OR MPL-2.0", False),
                                           ("MIT/Apache-2.0", False), ("Apache-2.0 WITH LLVM-exception", False),
                                           ("", False)])
def test_copyleft_means_no_permissive_alternative(expr, copyleft):
    assert notices.expr_copyleft(expr) is copyleft


def test_dependencies_without_a_licence_file(tmp_path):
    assert "Apache License 2.0 applies" in notices.no_file_note("MIT OR Apache-2.0", [])
    assert "requires no notice" in notices.no_file_note("0BSD OR CC0-1.0", [])
    assert "copyright its authors (A. Author)" in notices.no_file_note("BSD-2-Clause", ["A. Author"])
    assert notices.no_file_note("MIT", ["A. Author"]) is None  # MIT needs the crate's own notice: fail closed


def test_a_copyleft_crate_compiled_into_a_binary_is_listed_with_its_source(sp, tmp_path):
    root = fake_image(tmp_path, real_go_modules())
    text = notices.generate(sp, tmp_path / "out", "amd64", "v1", inv=BIN_INV, debian=DEBIAN,
                            image_root=root).read_text()
    two = text.split("2. GPL, LGPL AND MPL COMPONENTS")[1].split("3. PYTHON PACKAGES")[0]
    assert "* option-ext (Rust crate, in Typst) 0.2.0 — MPL-2.0" in two
    assert "https://static.crates.io/crates/option-ext/option-ext-0.2.0.crate" in two
    assert "contains these 403 Rust crates" in text and "contains these 106 Go modules" in text


def test_packaging_tooling_is_outside_the_wording_check_but_inside_notices(sp, tmp_path):
    # CI's interpreter carries a newer pip than the image; its vendored distlib licence mentions the GPL in the PSF
    # history table. The check must not depend on which interpreter runs it — and pip still ships in NOTICES.
    add_dist(sp, "pip", licence_text="PSF LICENSE ... Python releases have also been GPL-compatible ...")
    assert run(sp)[0] == []
    text = notices.generate(sp, tmp_path / "out", "amd64", "v1", inv=INV, debian=DEBIAN).read_text()
    assert "* pip 1.0 — MIT" in text and "GPL-compatible" in text


# ── release sources (fake network: no test touches the internet) ────────────────────────────────────────


def fake_web(files):
    """A `get` that serves bytes (or JSON-able objects) by exact URL, and fails like the network otherwise."""
    def get(url):
        if url not in files:
            raise sources.SourceError(f"{url}: 404")
        v = files[url]
        return v if isinstance(v, bytes) else _json.dumps(v).encode()
    return get


def dsc_for(files):
    lines = "".join(f" {hashlib.sha256(b).hexdigest()} {len(b)} {n}\n" for n, b in files.items())
    return f"Format: 3.0 (quilt)\nSource: demo\nChecksums-Sha256:\n{lines}".encode()


def test_component_sources_verify_against_their_published_checksums(tmp_path):
    geos, sdist = b"geos source bytes", b"psycopg sdist bytes"
    web = fake_web({
        "https://example.org/geos-3.14.1.tar.bz2": geos,
        "https://pypi.org/pypi/psycopg/3.3.6/json": {"urls": [{"packagetype": "sdist", "url": "https://files.example/psycopg-3.3.6.tar.gz",
                                                               "digests": {"sha256": hashlib.sha256(sdist).hexdigest()}}]},
        "https://files.example/psycopg-3.3.6.tar.gz": sdist,
    })
    copyleft = [
        {"component": "GEOS", "version": "3.14.1", "source": [
            {"url": "https://example.org/geos-3.14.1.tar.bz2", "sha256": hashlib.sha256(geos).hexdigest()}]},
        {"component": "GEOS (C API)", "version": "3.14.1", "source": [
            {"url": "https://example.org/geos-3.14.1.tar.bz2", "sha256": hashlib.sha256(geos).hexdigest()}]},
        {"component": "psycopg", "version": "3.3.6", "source": [{"pypi_sdist": "psycopg==3.3.6", "url": "x"}]},
    ]
    manifest = sources.fetch_components(copyleft, tmp_path, web)
    assert (tmp_path / "GEOS-3.14.1" / "geos-3.14.1.tar.bz2").read_bytes() == geos
    assert any("same file as GEOS-3.14.1/geos-3.14.1.tar.bz2" in m[1] for m in manifest)  # downloaded once
    assert any(m[3].startswith("sha256 published by PyPI") for m in manifest)


def test_a_tampered_or_missing_component_source_fails(tmp_path):
    web = fake_web({"https://example.org/x.tar": b"tampered",
                    "https://pypi.org/pypi/psycopg-binary/3.3.6/json": {"urls": [{"packagetype": "bdist_wheel"}]}})
    with pytest.raises(sources.SourceError, match="sha256"):
        sources.fetch_components([{"component": "X", "version": "1", "source": [
            {"url": "https://example.org/x.tar", "sha256": "0" * 64}]}], tmp_path, web)
    with pytest.raises(sources.SourceError, match="no source distribution"):
        sources.fetch_components([{"component": "psycopg-binary", "version": "3.3.6", "source": [
            {"pypi_sdist": "psycopg-binary==3.3.6", "url": "x"}]}], tmp_path, web)


def debian_web(dsc_override=None):
    orig, deb = b"orig tarball", b"debian tarball"
    dsc = dsc_override or dsc_for({"demo_1.0.orig.tar.gz": orig, "demo_1.0-1.debian.tar.xz": deb})
    blobs = {"demo_1.0-1.dsc": dsc, "demo_1.0.orig.tar.gz": orig, "demo_1.0-1.debian.tar.xz": deb}
    web = {f"{sources.SNAPSHOT}/file/{hashlib.sha1(b).hexdigest()}": b for b in blobs.values()}
    web[f"{sources.SNAPSHOT}/mr/package/demo/1%3A1.0-1/srcfiles?fileinfo=1"] = {
        "fileinfo": {hashlib.sha1(b).hexdigest(): [{"name": n}] for n, b in blobs.items()}}
    return fake_web(web)


def test_a_debian_source_package_verifies_against_snapshot_and_its_dsc(tmp_path):
    manifest = sources.fetch_debian([{"source": "demo", "source_version": "1:1.0-1"}], tmp_path, debian_web())
    names = sorted(m[1] for m in manifest)
    assert names == ["demo_1_1.0-1/demo_1.0-1.debian.tar.xz", "demo_1_1.0-1/demo_1.0-1.dsc", "demo_1_1.0-1/demo_1.0.orig.tar.gz"]


def test_a_debian_file_that_disagrees_with_its_dsc_fails(tmp_path):
    lying = dsc_for({"demo_1.0.orig.tar.gz": b"something else", "demo_1.0-1.debian.tar.xz": b"debian tarball"})
    with pytest.raises(sources.SourceError, match="does not match the .dsc"):
        sources.fetch_debian([{"source": "demo", "source_version": "1:1.0-1"}], tmp_path, debian_web(lying))


def pgdg_web(dsc, orig, on_archive=False, index_dsc_sha=None, tamper_index=False, list_version=True):
    """A fake PGDG host pair: a signed-looking InRelease (the fake `verify` below returns its body untouched)
    naming a Sources index whose stanza carries the .dsc's sha256. The live suite is served empty of the
    package unless `on_archive` is False; the archive suite carries it when `on_archive` is True."""
    src, ver, codename = "postgresql-common", "293.pgdg12+1", "bookworm"
    dsc_name, orig_name = f"{src}_{ver}.dsc", f"{src}_{ver}.tar.xz"
    stanza = (f"Package: {src}\nFormat: 3.0 (native)\nVersion: {ver}\nDirectory: pool/main/p/{src}\n"
              f"Checksums-Sha256: \n {index_dsc_sha or hashlib.sha256(dsc).hexdigest()} {len(dsc)} {dsc_name}\n"
              f" {hashlib.sha256(orig).hexdigest()} {len(orig)} {orig_name}\n")
    empty = "Package: other\nVersion: 1\nDirectory: pool/main/o/other\nChecksums-Sha256: \n 00 1 other_1.dsc\n"
    files = {}
    for base, suite, carries in ((sources.PGDG[0], f"{codename}-pgdg", not on_archive), (sources.PGDG[1], f"{codename}-pgdg-archive", on_archive)):
        index = (stanza if (carries and list_version) else empty).encode()
        served = index + (b"tampered" if tamper_index and carries else b"")
        files[f"{base}/dists/{suite}/InRelease"] = (f"Suite: {suite}\nSHA256:\n {hashlib.sha256(index).hexdigest()} {len(index)} main/source/Sources\n").encode()
        files[f"{base}/dists/{suite}/main/source/Sources"] = served
        if carries:
            files[f"{base}/pool/main/p/{src}/{dsc_name}"] = dsc
            files[f"{base}/pool/main/p/{src}/{orig_name}"] = orig
    return fake_web(files)


def unsigned_ok(data):
    """The test stand-in for verify_inrelease: the body IS the message (no armor, no gpgv)."""
    return data.decode()


@pytest.fixture(autouse=True)
def _fresh_pgdg_index_cache():
    sources._INDEX_CACHE.clear()
    yield
    sources._INDEX_CACHE.clear()


def test_pgdg_packages_verify_against_the_signed_sources_index(tmp_path):
    orig = b"pg orig"
    dsc = dsc_for({"postgresql-common_293.pgdg12+1.tar.xz": orig})
    manifest = sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                                    pgdg_web(dsc, orig), verify=unsigned_ok)
    assert {m[1].split("/")[1] for m in manifest} == {"postgresql-common_293.pgdg12+1.dsc", "postgresql-common_293.pgdg12+1.tar.xz"}
    how = {m[1].split("/")[1]: m[3] for m in manifest}
    assert "signed Sources index of bookworm-pgdg" in how["postgresql-common_293.pgdg12+1.dsc"]
    assert "itself verified against the signed index" in how["postgresql-common_293.pgdg12+1.tar.xz"]


def test_the_signed_index_check_says_so_on_success_and_only_then(tmp_path, capsys):
    """The one positive line the release record quotes (beta.18 passed silently): named suite and host, the pinned
    key's fingerprint, the index that matched, the stanza count — once per suite, and never for an index that
    did not match its InRelease."""
    orig = b"pg orig"
    dsc = dsc_for({"postgresql-common_293.pgdg12+1.tar.xz": orig})
    sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                         pgdg_web(dsc, orig), verify=unsigned_ok)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[third-party] InRelease verified")]
    assert len(lines) == 1, lines
    assert f"bookworm-pgdg on {sources.PGDG[0]}" in lines[0]
    assert sources.PGDG_KEY_FINGERPRINT in lines[0]
    assert "main/source/Sources matches it, 1 stanzas" in lines[0]
    sources._INDEX_CACHE.clear()
    with pytest.raises(sources.SourceError):
        sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                             pgdg_web(dsc, orig, tamper_index=True), verify=unsigned_ok)
    assert "InRelease verified" not in capsys.readouterr().out


def test_pgdg_packages_fall_back_to_the_archive_suite(tmp_path):
    orig = b"pg orig"
    dsc = dsc_for({"postgresql-common_293.pgdg12+1.tar.xz": orig})
    manifest = sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                                    pgdg_web(dsc, orig, on_archive=True), verify=unsigned_ok)
    assert any("bookworm-pgdg-archive" in m[3] for m in manifest)


def test_a_pgdg_dsc_that_disagrees_with_the_signed_index_fails(tmp_path):
    orig = b"pg orig"
    dsc = dsc_for({"postgresql-common_293.pgdg12+1.tar.xz": orig})
    with pytest.raises(sources.SourceError, match="does not match the signed Sources index"):
        sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                             pgdg_web(dsc, orig, index_dsc_sha="0" * 64), verify=unsigned_ok)


def test_a_pgdg_index_that_disagrees_with_inrelease_fails(tmp_path):
    orig = b"pg orig"
    dsc = dsc_for({"postgresql-common_293.pgdg12+1.tar.xz": orig})
    # an index that ARRIVED and does not match its signed InRelease stops the run — it is never skipped over in
    # favour of the next host (that would let a tampered live suite hide behind a clean archive)
    with pytest.raises(sources.SourceError, match="does not match the signed InRelease"):
        sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                             pgdg_web(dsc, orig, tamper_index=True), verify=unsigned_ok)


def test_a_suite_missing_from_one_host_is_skipped_but_a_bad_signature_is_not(tmp_path):
    orig = b"pg orig"
    dsc = dsc_for({"postgresql-common_293.pgdg12+1.tar.xz": orig})
    seen = []

    def refusing_verify(data):  # the live host's InRelease fails signature; the archive's would pass
        seen.append(data)
        raise sources.SourceError("InRelease did not verify against the pinned PGDG key")
    with pytest.raises(sources.SourceError, match="did not verify"):
        sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                             pgdg_web(dsc, orig, on_archive=True), verify=refusing_verify)
    assert len(seen) == 1  # it stopped at the first bad signature; it did not go on to try the archive


def test_a_truncated_key_is_a_source_error_not_a_crash():
    good = sources.PGDG_KEY.read_text()
    head, rest = good.split("\n\n", 1)
    cut = head + "\n\n" + rest[:60] + "\n-----END PGP PUBLIC KEY BLOCK-----\n"
    with pytest.raises(sources.SourceError, match="OpenPGP"):
        sources.openpgp_fingerprint(cut.encode())


def test_a_malformed_inrelease_size_is_a_source_error():
    with pytest.raises(sources.SourceError, match="malformed SHA256 line"):
        sources.inrelease_sha256s("SHA256:\n " + "a" * 64 + " notanumber main/source/Sources\n")


def test_a_pgdg_version_in_neither_index_fails(tmp_path):
    orig = b"pg orig"
    dsc = dsc_for({"postgresql-common_293.pgdg12+1.tar.xz": orig})
    with pytest.raises(sources.SourceError, match="not in the signed Sources index"):
        sources.fetch_debian([{"source": "postgresql-common", "source_version": "293.pgdg12+1"}], tmp_path,
                             pgdg_web(dsc, orig, list_version=False), verify=unsigned_ok)


def test_the_committed_pgdg_key_has_the_pinned_fingerprint():
    assert sources.openpgp_fingerprint(sources.PGDG_KEY.read_bytes()) == sources.PGDG_KEY_FINGERPRINT


def test_a_pgdg_key_with_another_fingerprint_is_refused_before_gpg_runs(monkeypatch):
    monkeypatch.setattr(sources, "PGDG_KEY_FINGERPRINT", "0" * 40)
    with pytest.raises(sources.SourceError, match="not the pinned"):
        sources.verify_inrelease(b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nx\n-----BEGIN PGP SIGNATURE-----\n")


def test_verify_never_falls_back_to_tls_without_gpg(monkeypatch):
    monkeypatch.setattr(sources.shutil, "which", lambda _name: None)
    with pytest.raises(sources.SourceError, match="gpg and gpgv are required"):
        sources.verify_inrelease(b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nx\n-----BEGIN PGP SIGNATURE-----\n")


@pytest.mark.skipif(not (sources.shutil.which("gpg") and sources.shutil.which("gpgv")), reason="gnupg not installed")
def test_an_unsigned_or_forged_inrelease_is_refused_by_gpgv():
    forged = b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSuite: x\n-----BEGIN PGP SIGNATURE-----\n\nnotasig\n-----END PGP SIGNATURE-----\n"
    with pytest.raises(sources.SourceError, match="did not verify"):
        sources.verify_inrelease(forged)


def test_clearsigned_body_undoes_dash_escaping():
    msg = "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSuite: s\n- -- dashed\n-----BEGIN PGP SIGNATURE-----\nx\n-----END PGP SIGNATURE-----\n"
    assert sources.clearsigned_body(msg) == "Suite: s\n-- dashed"


@pytest.mark.parametrize("version,suites", [
    ("293.pgdg12+1", ["bookworm-pgdg", "bookworm-pgdg-archive"]),
    ("16~rc1-1.pgdg120+1", ["bookworm-pgdg", "bookworm-pgdg-archive"]),
    ("1.0-1.pgdg11+1", ["bullseye-pgdg", "bullseye-pgdg-archive"]),
])
def test_pgdg_suites_follow_the_version(version, suites):
    assert [s for _b, s in sources.pgdg_suites(version)] == suites


def test_an_unknown_pgdg_release_is_refused():
    with pytest.raises(sources.SourceError, match="no Debian codename"):
        sources.pgdg_suites("1.0-1.pgdg99+1")


def test_the_archive_carries_a_manifest_and_refuses_an_unaudited_image(tmp_path):
    root = tmp_path / "talus-api-v1-third-party-source"
    (root / "GEOS-3.14.1").mkdir(parents=True)
    (root / "GEOS-3.14.1" / "geos.tar.bz2").write_bytes(b"x")
    tar = sources.pack(root, [("ab" * 32, "GEOS-3.14.1/geos.tar.bz2", "https://example.org/geos", "pinned")],
                       tmp_path / "out.tar", "demo")
    with tarfile.open(tar) as t:
        assert sorted(t.getnames()) == ["talus-api-v1-third-party-source/GEOS-3.14.1/geos.tar.bz2",
                                        "talus-api-v1-third-party-source/SOURCES.txt"]
    nj = tmp_path / "NOTICES.json"
    nj.write_text(_json.dumps({"version": "v1", "audited": False, "copyleft": [], "debian": []}))
    with pytest.raises(sources.SourceError, match="unaudited"):
        sources.build(nj, tmp_path / "o")


def test_github_archive_names_say_which_repository_and_ref():
    assert sources.file_name("https://github.com/lxml/lxml/archive/f9e4999e.tar.gz") == "lxml-lxml-f9e4999e.tar.gz"
    assert sources.file_name("https://github.com/rasterio/rasterio/archive/refs/tags/1.5.2.tar.gz") == "rasterio-rasterio-1.5.2.tar.gz"


# ── the operator's switch (rebuilding with a replaced component) ────────────────────────────────────────


def test_report_mode_passes_a_modified_tree_and_a_release_build_ignores_it(sp, monkeypatch, capsys):
    make_elf(sp / "rasterio.libs" / "libnewthing-0badf00d.so.1")
    monkeypatch.setattr(drift, "load_inventory", lambda path=None: INV)
    monkeypatch.setenv("TALUS_THIRD_PARTY_CHECK", "report")
    assert cli.main(["check", "--site-packages", str(sp), "--arch", "amd64"]) == 0
    assert "MODIFIED BUILD" in capsys.readouterr().out
    monkeypatch.setenv("TALUS_RELEASE", "1")
    assert cli.main(["check", "--site-packages", str(sp), "--arch", "amd64"]) == 1


def test_a_modified_build_says_so_and_lists_unknown_libraries(sp, tmp_path):
    make_elf(sp / "rasterio.libs" / "libnewthing-0badf00d.so.1")
    out = notices.generate(sp, tmp_path / "out", "amd64", "v1", inv=INV, debian=DEBIAN,
                           modified=["NEW bundled library rasterio.libs/libnewthing-0badf00d.so.1"])
    text = out.read_text()
    assert text.split("2. GPL")[0].count("MODIFIED BUILD") == 1
    assert "* rasterio.libs/libnewthing-0badf00d.so.1 — NOT IN THE AUDITED INVENTORY (modified build)" in text
    data = _json.loads((tmp_path / "out" / "NOTICES.json").read_text())
    assert data["audited"] is False and data["modified"]  # the release source fetcher refuses it


def test_a_copyleft_library_changed_in_place_under_its_audited_name_fails(sp):
    lib = sp / "rasterio.libs" / "libgeos-3ec8fbdb.so.3.14.1"
    geos = dict(INV["native"][1], sha256=hashlib.sha256(lib.read_bytes()).hexdigest())
    inv = dict(INV, native=[INV["native"][0], geos])
    assert run(sp, inv)[0] == []
    make_elf(lib, defined={"GEOS_rebuilt": None})  # same name, different bytes
    assert any("CHANGED IN PLACE" in v for v in run(sp, inv)[0])


def test_static_copyleft_in_a_file_not_pinned_by_bytes_fails(sp):
    make_elf(sp / "lxml" / "etree.so", defined={"libiconv_open": None, "_libiconv_version": (0x0112).to_bytes(4, "little")})
    pinned = dict(STATIC_ROW, file_sha256={"lxml/etree.so": hashlib.sha256((sp / "lxml" / "etree.so").read_bytes()).hexdigest()})
    assert run(sp, dict(INV, static=[pinned]))[0] == []
    make_elf(sp / "lxml" / "objectify.so", defined={"libiconv_open": None, "_libiconv_version": (0x0112).to_bytes(4, "little")})
    assert any("lxml/objectify.so carries statically linked GNU libiconv and is not the audited file" in v
               for v in run(sp, dict(INV, static=[pinned]))[0])


def test_a_copyleft_package_whose_source_row_describes_another_version_fails(sp):
    add_dist(sp, "psycopg-binary", licence="LGPL-3.0-only")  # version 1.0
    row = {"name": "psycopg-binary", "version": "3.3.6", "source": [{"url": "https://example.org/x.tar.gz"}]}
    violations, _ = run(sp, dict(INV, python_source=[row]))
    assert any("psycopg-binary is 1.0, but its [[python_source]] row describes 3.3.6" in v for v in violations)


@pytest.mark.parametrize("name", ["../escape.dsc", "sub/dir.tar.xz", ".hidden", "..", ""])
def test_a_debian_file_name_that_is_not_a_plain_name_is_refused(tmp_path, name):
    orig = b"orig tarball"
    dsc = dsc_for({"demo_1.0.orig.tar.gz": orig})
    web = {f"{sources.SNAPSHOT}/file/{hashlib.sha1(b).hexdigest()}": b for b in (dsc, orig)}
    web[f"{sources.SNAPSHOT}/mr/package/demo/1.0-1/srcfiles?fileinfo=1"] = {"fileinfo": {
        hashlib.sha1(dsc).hexdigest(): [{"name": "demo_1.0-1.dsc"}], hashlib.sha1(orig).hexdigest(): [{"name": name}]}}
    with pytest.raises(sources.SourceError, match="not a plain file name"):
        sources.fetch_debian([{"source": "demo", "source_version": "1.0-1"}], tmp_path, fake_web(web))
    assert not list(tmp_path.rglob("escape*"))


def test_a_file_the_dsc_does_not_list_is_refused(tmp_path):
    orig, stray = b"orig tarball", b"something the .dsc never mentions"
    dsc = dsc_for({"demo_1.0.orig.tar.gz": orig})
    blobs = {"demo_1.0-1.dsc": dsc, "demo_1.0.orig.tar.gz": orig, "demo_1.0-1.extra.tar.gz": stray}
    web = {f"{sources.SNAPSHOT}/file/{hashlib.sha1(b).hexdigest()}": b for b in blobs.values()}
    web[f"{sources.SNAPSHOT}/mr/package/demo/1.0-1/srcfiles?fileinfo=1"] = {
        "fileinfo": {hashlib.sha1(b).hexdigest(): [{"name": n}] for n, b in blobs.items()}}
    with pytest.raises(sources.SourceError, match="does not list"):
        sources.fetch_debian([{"source": "demo", "source_version": "1.0-1"}], tmp_path, fake_web(web))
