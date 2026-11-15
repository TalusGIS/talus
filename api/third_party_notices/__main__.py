"""python3 -m third_party_notices check    [--site-packages DIR] [--arch ARCH]
python3 -m third_party_notices generate --out DIR [--site-packages DIR] [--arch ARCH]
python3 -m third_party_notices sources  --notices NOTICES.json --out DIR [--only components|debian]

check and generate run inside the image (api/Dockerfile); sources runs in the release pipeline, against
the NOTICES.json of the image being released. Exit 0 = pass · 1 = violation (the build must stop) · 2 = the
check or the generator cannot run (fails closed too). TALUS_RELEASE=1 marks a release build: an unaudited
architecture then fails instead of reporting. TALUS_VERSION names the release in NOTICES.

TALUS_THIRD_PARTY_CHECK=report is for an operator rebuilding the image with a component they replaced (the
LGPL's right to modify and relink — docs/replacing-lgpl-components.md): the check then reports instead of
failing, and NOTICES opens with MODIFIED BUILD and the differences. A release build (TALUS_RELEASE=1) ignores it.
"""
import argparse
import os
import platform
import site
import sys

from . import drift, notices, sources

ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m third_party_notices")
    p.add_argument("command", choices=["check", "generate", "sources"])
    p.add_argument("--out")
    p.add_argument("--notices")
    p.add_argument("--only", choices=["components", "debian"])
    p.add_argument("--site-packages", default=site.getsitepackages()[0])
    p.add_argument("--arch", default=ARCH.get(platform.machine().lower(), platform.machine().lower()))
    args = p.parse_args(argv)
    release = os.environ.get("TALUS_RELEASE") == "1"
    report = os.environ.get("TALUS_THIRD_PARTY_CHECK", "enforce") == "report" and not release
    if os.environ.get("TALUS_THIRD_PARTY_CHECK", "enforce") not in ("enforce", "report"):
        p.error("TALUS_THIRD_PARTY_CHECK must be 'enforce' or 'report'")
    if args.command == "sources":
        if not args.notices or not args.out:
            p.error("sources needs --notices NOTICES.json and --out DIR")
        try:
            for tar in sources.build(args.notices, args.out, args.only):
                print(f"[third-party] wrote {tar} ({tar.stat().st_size / 1e6:.0f} MB)")
        except (sources.SourceError, OSError, KeyError, ValueError) as e:
            print(f"[third-party] SOURCES FAILED — {e}", file=sys.stderr)
            return 2
        return 0
    if args.command == "generate":
        if not args.out:
            p.error("generate needs --out DIR")
        try:
            modified = drift.run(args.site_packages, args.arch, release, image_root="/")[0] if report else None
            path = notices.generate(args.site_packages, args.out, args.arch, os.environ.get("TALUS_VERSION", "dev"),
                                    modified=modified)
        except (drift.InventoryError, notices.NoticesError, OSError) as e:
            print(f"[third-party] CANNOT GENERATE NOTICES — {e}", file=sys.stderr)
            return 2
        print(f"[third-party] wrote {path} ({path.stat().st_size} bytes) and NOTICES.json")
        return 0
    try:
        violations, notes = drift.run(args.site_packages, args.arch, release, image_root="/")
    except drift.InventoryError as e:
        print(f"[third-party] CANNOT CHECK — {e}", file=sys.stderr)
        return 2
    print(f"[third-party] {args.site_packages} · arch {args.arch}{' · release build' if release else ''}")
    if os.environ.get("TALUS_THIRD_PARTY_CHECK") == "report" and release:
        print("  note: TALUS_THIRD_PARTY_CHECK=report is ignored on a release build")
    for n in notes:
        print(f"  note: {n}")
    if violations and report:
        print(f"\nMODIFIED BUILD (TALUS_THIRD_PARTY_CHECK=report) — {len(violations)} difference(s) from the audited "
              "inventory, reported, not failed; NOTICES will say so:")
        for v in violations:
            print(f"  ~ {v}")
        return 0
    if violations:
        print(f"\nFAIL — {len(violations)} third-party change(s) need a human:", file=sys.stderr)
        for v in violations:
            print(f"  ✗ {v}", file=sys.stderr)
        print("\nEach is a licence event, not a build detail: identify the component, its licence and its exact "
              "source, then update api/third_party_notices/inventory.toml.", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
