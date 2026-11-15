"""Fetch the exact source of what an image's NOTICES.json lists, verify it, and pack it for the release.

    python3 -m third_party_notices sources --notices NOTICES.json --out DIR

Writes two archives, attached to the GitHub Release beside the image they describe:
  DIR/talus-api-<version>-third-party-source.tar   every component in NOTICES section 2 (GPL / LGPL / MPL),
                                                    with the build recipes NOTICES names
  DIR/talus-api-<version>-debian-source.tar        the Debian source package of every Debian package in the
                                                    image (including the PostgreSQL project's apt packages)
Each carries SOURCES.txt: every file, its sha256, where it came from and how it was verified. Verification:
PyPI sdists against PyPI's sha256; pinned tarballs against the inventory's sha256; Debian files against
snapshot.debian.org's recorded hash and then against the sha256 sums in the package's own .dsc; the PostgreSQL
project's apt packages (no snapshot entry) against the suite's SIGNED Sources index — InRelease verified by gpgv
against the PGDG key committed beside this module and pinned by fingerprint — and then against their .dsc.
Anything that cannot be fetched or verified FAILS — a release must never ship with a source it cannot vouch for.
"""
import base64
import concurrent.futures
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "talus-release-sources/1"}
SNAPSHOT = "https://snapshot.debian.org"
PGDG = ("https://apt.postgresql.org/pub/repos/apt", "https://apt-archive.postgresql.org/pub/repos/apt")
# The PostgreSQL project's apt signing key, committed here and pinned by fingerprint (the same key the image's
# apt trusts — api/Dockerfile checks that file against the same fingerprint at build). A key swapped on the
# way in is refused before it verifies anything.
PGDG_KEY = Path(__file__).parent / "keys" / "pgdg-ACCC4CF8.asc"
PGDG_KEY_FINGERPRINT = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"
# pgdg<NN> in a version names the Debian release it was built for; the suite is "<codename>-pgdg" on the live
# host and "<codename>-pgdg-archive" on the archive host (which is where a superseded version's index lives).
PGDG_CODENAMES = {"11": "bullseye", "12": "bookworm", "13": "trixie"}


class SourceError(Exception):
    """A source could not be fetched or did not verify."""


def http_get(url, retries=3, timeout=300):
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network hiccups retry; a 404 retries too and then fails loudly
            last = e
            time.sleep(2 ** attempt)
    raise SourceError(f"{url}: {last}")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def slug(text):
    return re.sub(r"[^A-Za-z0-9._@+-]+", "_", text).strip("_")


def file_name(url):
    """A readable file name for a source URL (GitHub archives are named by repository and ref)."""
    u = urllib.parse.urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    if u.netloc == "github.com" and "archive" in parts:
        i = parts.index("archive")
        return f"{parts[0]}-{parts[1]}-{'-'.join(parts[i + 1:]).replace('refs-tags-', '')}"
    return parts[-1]


# ── components (NOTICES section 2) ──────────────────────────────────────────────────────────────────────
def resolve(src, get_json):
    """(url, expected_sha256 or None, how it is verified) for one source entry."""
    if src.get("pypi_sdist"):
        name, version = src["pypi_sdist"].split("==")
        meta = get_json(f"https://pypi.org/pypi/{name}/{version}/json")
        sdists = [u for u in meta["urls"] if u["packagetype"] == "sdist"]
        if not sdists:
            raise SourceError(f"{src['pypi_sdist']} has no source distribution on PyPI")
        return sdists[0]["url"], sdists[0]["digests"]["sha256"], "sha256 published by PyPI"
    if src.get("sha256"):
        return src["url"], src["sha256"], "sha256 pinned in the image's inventory"
    return src["url"], None, "no published checksum — sha256 recorded at download"


def fetch_components(copyleft, root, get=http_get, get_json=None):
    get_json = get_json or (lambda u: json.loads(get(u)))
    done, manifest = {}, []
    for c in copyleft:
        folder = slug(f"{c['component']}-{c['version']}")
        for src in c["source"]:
            url, want, how = resolve(src, get_json)
            if url in done:
                manifest.append((done[url][0], f"{folder}: same file as {done[url][1]}", url, how))
                continue
            data = get(url)
            got = sha256(data)
            if want and got != want:
                raise SourceError(f"{c['component']}: {url} sha256 {got} != expected {want}")
            rel = f"{folder}/{file_name(url)}"
            (root / folder).mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
            done[url] = (got, rel)
            manifest.append((got, rel, url, how + (f"; {src['role']}" if src.get("role") else "")))
    return manifest


# ── Debian ──────────────────────────────────────────────────────────────────────────────────────────────
# What a Debian source file name looks like (package_version.dsc, .orig.tar.xz, .debian.tar.xz, .asc ...). Names
# arrive from snapshot.debian.org and from .dsc files; anything else — a "/", a "..", a leading dot — is refused
# before it can become a path under the output directory.
PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")


def plain_name(name, src, version):
    if not isinstance(name, str) or not PLAIN_NAME.match(name):
        raise SourceError(f"{src} {version}: refusing a file name that is not a plain file name: {name!r}")
    return name


def strip_epoch(version):
    return version.split(":", 1)[1] if ":" in version else version


def pool_prefix(src):
    return src[:4] if src.startswith("lib") else src[0]


def dsc_sha256(text):
    """{file name: sha256} from a .dsc's (or a Sources stanza's) Checksums-Sha256 field. The apt index writes
    the field name with a trailing space; a .dsc does not — both are accepted."""
    m = re.search(r"^Checksums-Sha256:[ \t]*\n((?:[ \t].*\n?)+)", text, re.M)
    if not m:
        raise SourceError("the .dsc has no Checksums-Sha256 field")
    return {name: digest for digest, _size, name in (line.split() for line in m.group(1).splitlines() if line.strip())}


# ── the PostgreSQL project's apt repository: a signed index, not TLS alone ──────────────────────────────
def openpgp_fingerprint(armored):
    """The v4 fingerprint of the first public-key packet in an ASCII-armored OpenPGP key — SHA-1 over
    0x99, the packet length (2 bytes) and the packet body (RFC 4880 §12.2). Pure Python, so the pin is
    checked without trusting the gpg that will later use the key."""
    text = armored.decode("utf-8", "replace")
    m = re.search(r"-----BEGIN PGP PUBLIC KEY BLOCK-----\n(.*?)\n-----END PGP PUBLIC KEY BLOCK-----", text, re.S)
    if not m:
        raise SourceError("not an ASCII-armored OpenPGP public key")
    b64 = "".join(l for l in m.group(1).splitlines() if l and ":" not in l and not l.startswith("="))
    try:
        return _first_v4_fingerprint(base64.b64decode(b64))
    except (IndexError, ValueError) as e:  # a truncated or malformed packet stream
        raise SourceError(f"malformed OpenPGP key: {e}")


def _first_v4_fingerprint(data):
    i = 0
    while i < len(data):
        c = data[i]
        i += 1
        if c & 0x40:  # new-format packet header
            tag, l = c & 0x3F, data[i]
            i += 1
            if l < 192:
                ln = l
            elif l < 224:
                ln = ((l - 192) << 8) + data[i] + 192
                i += 1
            else:
                ln = int.from_bytes(data[i:i + 4], "big")
                i += 4
        else:  # old-format
            tag, lt = (c >> 2) & 0x0F, c & 3
            width = {0: 1, 1: 2, 2: 4}.get(lt)
            if width is None:
                raise SourceError("an OpenPGP packet of indeterminate length")
            ln = int.from_bytes(data[i:i + width], "big")
            i += width
        if i + ln > len(data):
            raise ValueError("a packet runs past the end of the key (truncated)")
        body = data[i:i + ln]
        i += ln
        if tag == 6:  # public-key packet
            if body[:1] != b"\x04":
                raise SourceError("only v4 OpenPGP keys are pinned")
            return hashlib.sha1(b"\x99" + ln.to_bytes(2, "big") + body).hexdigest().upper()
    raise SourceError("no public-key packet in the key")


def clearsigned_body(text):
    """The signed text of a clearsigned message (dash-escaping undone) — read only AFTER gpgv said yes."""
    m = re.search(r"-----BEGIN PGP SIGNED MESSAGE-----\n(?:[^\n]+\n)*\n(.*?)\n-----BEGIN PGP SIGNATURE-----", text, re.S)
    if not m:
        raise SourceError("InRelease is not a clearsigned message")
    return re.sub(r"^- ", "", m.group(1), flags=re.M)


def verify_inrelease(data):
    """Verify an InRelease's signature against the committed, fingerprint-pinned PGDG key and return its
    signed body. Needs gpg (to de-armor the key) and gpgv (to verify) on the PATH — the release pipeline's
    runner has both; without them this FAILS, it never falls back to trusting TLS."""
    fpr = openpgp_fingerprint(PGDG_KEY.read_bytes())
    if fpr != PGDG_KEY_FINGERPRINT:
        raise SourceError(f"the committed PGDG key has fingerprint {fpr}, not the pinned {PGDG_KEY_FINGERPRINT}")
    gpg, gpgv = shutil.which("gpg"), shutil.which("gpgv")
    if not gpg or not gpgv:
        raise SourceError("gpg and gpgv are required to verify the PostgreSQL apt repository's signed index (install gnupg)")
    with tempfile.TemporaryDirectory() as d:
        ring, inrel = Path(d) / "pgdg.gpg", Path(d) / "InRelease"
        subprocess.run([gpg, "--batch", "--yes", "--dearmor", "-o", str(ring), str(PGDG_KEY)], check=True, capture_output=True)
        inrel.write_bytes(data)
        r = subprocess.run([gpgv, "--keyring", str(ring), "--status-fd", "1", str(inrel)], capture_output=True, text=True)
        # VALIDSIG <signing-key fpr> <date> <timestamp> <expiry> <ver> <reserved> <pk-algo> <hash> <class> <primary fpr>:
        # the pinned key may sign directly (first field) or through a subkey (last field) — both are its signature.
        valid = any(line.split()[2:3] == [PGDG_KEY_FINGERPRINT] or line.split()[-1] == PGDG_KEY_FINGERPRINT
                    for line in r.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG "))
        if r.returncode != 0 or not valid:
            raise SourceError(f"InRelease did not verify against the pinned PGDG key: {r.stderr.strip()[:300]}")
    return clearsigned_body(data.decode("utf-8", "replace"))


def inrelease_sha256s(body):
    """{path: (sha256, size)} from the SHA256 section of a Release/InRelease body."""
    m = re.search(r"^SHA256:[ \t]*\n((?:[ \t].*\n?)+)", body, re.M)
    if not m:
        raise SourceError("InRelease has no SHA256 section")
    out = {}
    for line in m.group(1).splitlines():
        parts = line.split()
        if len(parts) == 3:
            if not re.fullmatch(r"[0-9a-f]{64}", parts[0]) or not parts[1].isdigit():
                raise SourceError(f"InRelease has a malformed SHA256 line: {line.strip()[:80]!r}")
            out[parts[2]] = (parts[0], int(parts[1]))
    return out


class IndexUnavailable(SourceError):
    """The suite is not published on this host (its InRelease cannot be fetched) — try the next host. Anything
    that DID arrive and failed to verify is a plain SourceError and stops the run: it is a signal, not an absence."""


def pgdg_suites(version):
    """[(base url, suite)] to try for a pgdg version — the live host first, then the archive host."""
    m = re.search(r"pgdg(\d+)", version)
    if not m:
        raise SourceError(f"{version}: not a PostgreSQL apt (pgdg) version")
    digits = m.group(1)
    codename = PGDG_CODENAMES.get(digits[:2] if len(digits) >= 3 else digits)
    if not codename:
        raise SourceError(f"{version}: no Debian codename known for pgdg{digits} — add it to PGDG_CODENAMES")
    return [(PGDG[0], f"{codename}-pgdg"), (PGDG[1], f"{codename}-pgdg-archive")]


_INDEX_CACHE, _INDEX_LOCK = {}, threading.Lock()


def pgdg_sources_index(base, suite, get=http_get, verify=verify_inrelease):
    """The suite's Sources index as text, trusted only after: InRelease verified by signature → the index's
    sha256 (and size) read from the signed body → the fetched index matches. Cached per suite for the run."""
    key = (base, suite)
    with _INDEX_LOCK:
        if key in _INDEX_CACHE:
            return _INDEX_CACHE[key]
        try:
            data = get(f"{base}/dists/{suite}/InRelease")
        except SourceError as e:
            raise IndexUnavailable(f"{suite}: no InRelease on {base}: {e}")
        body = verify(data)
        sums = inrelease_sha256s(body)
        for name in ("main/source/Sources.gz", "main/source/Sources"):
            if name in sums:
                break
        else:
            raise SourceError(f"{suite}: InRelease lists no main/source/Sources index")
        raw = get(f"{base}/dists/{suite}/{name}")
        want, size = sums[name]
        if len(raw) != size or sha256(raw) != want:
            raise SourceError(f"{suite}: {name} does not match the signed InRelease")
        text = (gzip.decompress(raw) if name.endswith(".gz") else raw).decode("utf-8", "replace")
        stanzas = sum(1 for s in re.split(r"\n\n+", text.strip()) if s.strip())
        # The one positive sentence of the signed-index check, for the release record: before this line the
        # success path was silent, and a cut's log could only say the step had not failed (beta.18, 2026-10-22).
        print(f"[third-party] InRelease verified: {suite} on {base} (signed by the pinned PGDG key "
              f"{PGDG_KEY_FINGERPRINT}), {name} matches it, {stanzas} stanzas", flush=True)
        _INDEX_CACHE[key] = text
        return text


def index_stanza(index_text, src, version):
    """The Sources stanza for exactly this source package and version, or None."""
    for stanza in re.split(r"\n\n+", index_text):
        if re.search(rf"^Package: {re.escape(src)}$", stanza, re.M) and re.search(rf"^Version: {re.escape(version)}$", stanza, re.M):
            return stanza
    return None


POOL_DIR = re.compile(r"^pool/[A-Za-z0-9][A-Za-z0-9._+/-]*$")


def fetch_debian_package(src, version, root, get=http_get, get_json=None, verify=verify_inrelease):
    get_json = get_json or (lambda u: json.loads(get(u)))
    folder = slug(f"{src}_{version}")
    (root / folder).mkdir(parents=True, exist_ok=True)
    got, manifest = {}, []
    if "pgdg" in version:
        # The PostgreSQL project's apt repository has no snapshot.debian.org entry, so the .dsc is trusted only
        # through the suite's SIGNED Sources index (live host, then the archive host for superseded versions):
        # the stanza's sha256 for the .dsc must match what the pool serves; everything else hangs off the .dsc.
        dsc_name = plain_name(f"{src}_{strip_epoch(version)}.dsc", src, version)
        for base, suite in pgdg_suites(version):
            try:
                index = pgdg_sources_index(base, suite, get, verify)
            except IndexUnavailable:
                continue  # the suite is not on this host (the archive has no live suite, and vice versa)
            # a verification failure propagates: an index that arrived and did not verify is never skipped over
            stanza = index_stanza(index, src, version)
            if stanza is None:
                continue
            listed = dsc_sha256(stanza)
            d = re.search(r"^Directory: (.*)$", stanza, re.M)
            directory = d.group(1).strip() if d else f"pool/main/{pool_prefix(src)}/{src}"
            if not POOL_DIR.match(directory) or ".." in directory:
                raise SourceError(f"{src} {version}: the index names a pool directory that is not a plain path: {directory!r}")
            if dsc_name not in listed:
                raise SourceError(f"{src} {version}: the signed index of {suite} does not list {dsc_name}")
            pool = f"{base}/{directory}"
            dsc = get(f"{pool}/{dsc_name}")
            if sha256(dsc) != listed[dsc_name]:
                raise SourceError(f"{src} {version}: {dsc_name} does not match the signed Sources index of {suite}")
            got[dsc_name] = (dsc, f"{pool}/{dsc_name}",
                             f"sha256 from the signed Sources index of {suite} (InRelease verified against the pinned PGDG key …{PGDG_KEY_FINGERPRINT[-8:]})")
            for name in dsc_sha256(dsc.decode("utf-8", "replace")):
                plain_name(name, src, version)
                got[name] = (get(f"{pool}/{name}"), f"{pool}/{name}", "sha256 from the package's .dsc, itself verified against the signed index")
            break
        else:
            raise SourceError(f"{src} {version}: not in the signed Sources index of the PostgreSQL apt repository or of its archive")
    else:
        q = f"{SNAPSHOT}/mr/package/{urllib.parse.quote(src)}/{urllib.parse.quote(version)}/srcfiles?fileinfo=1"
        info = get_json(q)
        for sha1, entries in info["fileinfo"].items():
            name = plain_name(entries[0]["name"], src, version)
            data = get(f"{SNAPSHOT}/file/{sha1}")
            if hashlib.sha1(data).hexdigest() != sha1:
                raise SourceError(f"{src} {version}: {name} does not match snapshot.debian.org's hash")
            got[name] = (data, f"{SNAPSHOT}/file/{sha1}", "sha1 recorded by snapshot.debian.org; sha256 from the .dsc")
    dscs = [n for n in got if n.endswith(".dsc")]
    if len(dscs) != 1:
        raise SourceError(f"{src} {version}: expected one .dsc, found {dscs}")
    listed = dsc_sha256(got[dscs[0]][0].decode("utf-8", "replace"))
    for name, want in listed.items():
        plain_name(name, src, version)
        if name not in got or sha256(got[name][0]) != want:
            raise SourceError(f"{src} {version}: {name} is missing or does not match the .dsc")
    extra = sorted(set(got) - set(listed) - {dscs[0]})
    if extra:  # the archive carries exactly the package the .dsc describes — nothing it does not vouch for
        raise SourceError(f"{src} {version}: the archive returned files the .dsc does not list: {extra}")
    for name, (data, url, how) in sorted(got.items()):
        (root / folder / name).write_bytes(data)
        manifest.append((sha256(data), f"{folder}/{name}", url, how))
    return manifest


def fetch_debian(packages, root, get=http_get, workers=4, verify=verify_inrelease):
    srcs = sorted({(p["source"], p["source_version"]) for p in packages})
    manifest, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_debian_package, s, v, root, get, None, verify): (s, v) for s, v in srcs}
        for f in concurrent.futures.as_completed(futs):
            try:
                manifest += f.result()
            except Exception as e:
                failed.append(f"{' '.join(futs[f])}: {e}")
    if failed:
        raise SourceError("Debian sources failed:\n  " + "\n  ".join(sorted(failed)))
    return sorted(manifest, key=lambda m: m[1])


# ── packing ─────────────────────────────────────────────────────────────────────────────────────────────
def pack(root, manifest, out_tar, title):
    lines = [f"# {title}", "# sha256  path-in-archive  source-url  (verification)", ""]
    lines += [f"{digest}  {rel}  {url}  ({how})" for digest, rel, url, how in manifest]
    (root / "SOURCES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with tarfile.open(out_tar, "w") as tar:
        for f in sorted(p for p in root.rglob("*") if p.is_file()):
            info = tar.gettarinfo(f, arcname=f"{root.name}/{f.relative_to(root).as_posix()}")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            with open(f, "rb") as fh:
                tar.addfile(info, fh)
    return out_tar


def build(notices_json, out_dir, only=None, get=http_get):
    data = json.loads(Path(notices_json).read_text(encoding="utf-8"))
    if not data.get("audited"):
        raise SourceError("NOTICES.json is from an unaudited architecture — not a release image")
    version, out = data["version"], Path(out_dir)
    made = []
    if only in (None, "components"):
        root = out / f"talus-api-{version}-third-party-source"
        manifest = fetch_components(data["copyleft"], root, get)
        made.append(pack(root, manifest, out / f"{root.name}.tar",
                         f"Talus GIS {version}: source of the GPL, LGPL and MPL components in the talus-api image"))
    if only in (None, "debian"):
        root = out / f"talus-api-{version}-debian-source"
        manifest = fetch_debian(data["debian"], root, get)
        made.append(pack(root, manifest, out / f"{root.name}.tar",
                         f"Talus GIS {version}: the Debian source packages of the talus-api image"))
    return made
