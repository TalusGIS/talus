"""Licence allowlist for Python distributions — an ALLOWLIST, not a denylist.

A denylist tuned to "block GPL" sails past BSL, SSPL, Elastic and Commons Clause, so anything not on the
allowlist fails until a human adds a NAMED exception with a reason.

Expression semantics, chosen conservative: " OR " passes if ANY branch passes (the licensee's choice);
" AND ", ";" and "," require ALL parts to pass (";" joins multiple Trove classifiers whose dual-licence
intent is ambiguous — AND fails safe).
"""
import re

# Licence families accepted without review. MPL-2.0 is file-level copyleft, fine for unmodified use —
# listed in NOTICES. OFL-1.1 = fonts. CC-BY-4.0 = data attribution. Strong and weak copyleft (GPL, AGPL,
# LGPL) and every source-available licence are absent ON PURPOSE: named exceptions only.
ALLOWED = {
    "mit", "mit-0", "mit-cmu", "expat",
    "isc",
    "bsd", "bsd-2-clause", "bsd-3-clause", "0bsd",
    "apache-2.0",
    "psf-2.0", "python-2.0",
    "zlib",
    "cc0-1.0", "public-domain", "unlicense",
    "blueoak-1.0.0",
    "mpl-2.0",
    "ofl-1.1",
    "cc-by-4.0",
}

# Package-level exceptions: name (lowercase) -> reason. The ONLY way past the allowlist. Each one is
# copyleft, so each one's source is published with every release and its replacement is documented
# (docs/replacing-lgpl-components.md).
EXCEPTIONS = {
    "psycopg": "a replaceable Python import; source published with each release",
    "psycopg-binary": "a replaceable Python import; source published with each release",
    "psycopg-pool": "a replaceable Python import; source published with each release",
}

# Packaging tooling present in an interpreter, not part of the product tree.
SKIP_DISTS = {"pip", "setuptools", "wheel", "pkg_resources", "pkg-resources"}

ALIASES = {
    "apache software license": "apache-2.0",
    "apache license 2.0": "apache-2.0",
    "apache 2.0": "apache-2.0",
    "apache": "apache-2.0",
    "bsd license": "bsd",
    "mozilla public license 2.0 (mpl 2.0)": "mpl-2.0",
    "python software foundation license": "psf-2.0",
    "psf": "psf-2.0",
    "mit license": "mit",
    "mit no attribution license": "mit-0",
    "the unlicense (unlicense)": "unlicense",
    "public domain": "public-domain",
    "zlib/libpng license": "zlib",
    "isc license (iscl)": "isc",
    "gnu lesser general public license v3 (lgplv3)": "lgpl-3.0-only",
    "gnu lesser general public license v3 or later (lgplv3+)": "lgpl-3.0-or-later",
}


def _norm(token: str) -> str:
    t = token.strip().lower()
    # Unwrap only a FULLY surrounding paren pair — "(mit or cc0-1.0)" arms arrive wrapped, but
    # "mozilla public license 2.0 (mpl 2.0)" must keep its embedded parens for the alias.
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    t = ALIASES.get(t, t)
    t = re.sub(r"\s+licen[cs]e$", "", t)
    return ALIASES.get(t, t)


norm = _norm  # public name for the generator


def _unwrap(s: str) -> str:
    # Unwrap fully-surrounding paren pairs BEFORE splitting — "(MIT OR Apache-2.0)" must lose its wrapper
    # pre-split, while "...(MPL 2.0)" (embedded, not surrounding) keeps its parens for the alias table.
    s = s.strip()
    while s.startswith("(") and s.endswith(")") and s.count("(") == s.count(")"):
        inner = s[1:-1].strip()
        if ")" in inner and inner.index(")") < inner.find("("):
            break  # e.g. "(a) or (b)" — the outer parens are not one wrapper
        s = inner
    return s


def expr_ok(expr: str) -> bool:
    """True when the licence expression is acceptable under the allowlist."""
    for branch in re.split(r"\s+OR\s+|\s+or\s+", _unwrap(expr)):
        parts = re.split(r"\s+AND\s+|\s+and\s+|;|,", _unwrap(branch))
        parts = [p for p in (x.strip() for x in parts) if p]
        if parts and all(_norm(p) in ALLOWED for p in parts):
            return True
    return False


def dist_licence(dist):
    """The licence a distribution declares, or None. Preference order mirrors how the ecosystem declares
    licences: License-Expression (PEP 639) > Trove classifiers > the bare License field (often the entire
    licence TEXT in older packages — usable only when short)."""
    lic = dist.metadata.get("License-Expression")
    if not lic:
        classifiers = [
            c.split("::")[-1].strip()
            for c in dist.metadata.get_all("Classifier") or []
            if c.startswith("License ::") and c.strip() != "License :: OSI Approved"
        ]
        if classifiers:
            lic = "; ".join(classifiers)
    if not lic:
        raw = dist.metadata.get("License")
        lic = raw.strip() if raw and len(raw.strip()) < 100 else None
    return lic


def judge(rows):
    """rows: iterable of (name, licence-or-None). Returns (violations, excepted):
    violations = [(name, licence)], excepted = [(name, licence, reason)]."""
    violations, excepted = [], []
    for name, lic in sorted(rows):
        key = name.lower()
        if key in EXCEPTIONS:
            excepted.append((name, lic, EXCEPTIONS[key]))
        elif not lic or not expr_ok(lic):
            violations.append((name, lic or "<no licence metadata>"))
    return violations, excepted
