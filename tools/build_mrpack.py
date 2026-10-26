"""Build a reproducible Modrinth .mrpack from modpack.toml, .index/ and the override folders.

Each mod's sha512 (from its .index entry) is looked up on the Modrinth API to get the
sha1 and file size the .mrpack format requires. A mod whose hash, URL or filename
doesn't match Modrinth fails the build.

Side mapping: Prism `client` -> client-only. Everything else (both, server, unset) is
required on both sides, because in single-player the client runs the server.
"""
import argparse
import json
import os
import pathlib
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile

ERROR = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "ERROR: "

API = "https://api.modrinth.com/v2/version_files"
USER_AGENT = "lizard-thief-fighter/espresso (pack build)"
EPOCH = (1980, 1, 1, 0, 0, 0)  # fixed timestamp so identical inputs give identical bytes


def read_index(root: pathlib.Path) -> list[dict]:
    mods = []
    for f in sorted((root / ".index").glob("*.pw.toml")):
        d = tomllib.loads(f.read_text())
        mods.append({
            "entry": f.name,
            "filename": d["filename"],
            "side": d.get("side", ""),
            "url": d["download"]["url"],
            "sha512": d["download"]["hash"],
        })
    return mods


def lookup(hashes: list[str]) -> dict:
    body = json.dumps({"hashes": hashes, "algorithm": "sha512"}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def resolve_files(mods: list[dict], versions: dict) -> tuple[list[dict], list[str]]:
    files, problems = [], []
    for m in mods:
        v = versions.get(m["sha512"])
        f = next((f for f in (v or {}).get("files", []) if f["hashes"].get("sha512") == m["sha512"]), None)
        if f is None:
            problems.append(f"{m['entry']}: sha512 not found on Modrinth")
            continue
        if f["url"] != m["url"]:
            problems.append(f"{m['entry']}: URL differs from Modrinth ({f['url']})")
        if f["filename"] != m["filename"]:
            problems.append(f"{m['entry']}: filename differs from Modrinth ({f['filename']})")
        client_only = m["side"] == "client"
        files.append({
            "path": f"mods/{m['filename']}",
            "hashes": {"sha1": f["hashes"]["sha1"], "sha512": m["sha512"]},
            "env": {"client": "required", "server": "unsupported" if client_only else "required"},
            "downloads": [m["url"]],
            "fileSize": f["size"],
        })
    return files, problems


def add_file(z: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(arcname, EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data, compresslevel=9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out-dir", default="dist")
    ap.add_argument("--lookup-cache", help="read Modrinth lookup results from this JSON file instead of the API")
    ap.add_argument("--save-lookup", help="also save the Modrinth lookup results to this JSON file")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()

    manifest = tomllib.loads((root / "modpack.toml").read_text())
    pack, ver = manifest["pack"], manifest["versions"]
    mods = read_index(root)

    if a.lookup_cache:
        versions = json.loads(pathlib.Path(a.lookup_cache).read_text())
    else:
        try:
            versions = lookup([m["sha512"] for m in mods])
        except urllib.error.HTTPError as e:
            print(f"{ERROR}Modrinth lookup failed: HTTP {e.code} {e.reason}: {e.read()[:300]!r}")
            return 1
        except urllib.error.URLError as e:
            print(f"{ERROR}Modrinth lookup failed: {e.reason}")
            return 1
    if a.save_lookup:
        pathlib.Path(a.save_lookup).write_text(json.dumps(versions, indent=1, sort_keys=True))

    files, problems = resolve_files(mods, versions)
    for p in problems:
        print(f"{ERROR}{p}")
    if problems:
        return 1

    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": pack["version"],
        "name": pack["name"],
        "summary": pack.get("summary", ""),
        "files": files,
        "dependencies": {"minecraft": ver["minecraft"], "neoforge": ver["neoforge"]},
    }

    out_dir = root / a.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"Espresso-{pack['version']}.mrpack"
    overrides = []
    for o in manifest["build"]["overrides"]:
        p = root / o
        overrides += [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
    with zipfile.ZipFile(out, "w") as z:
        add_file(z, "modrinth.index.json", (json.dumps(index, indent=2) + "\n").encode())
        for f in sorted(overrides, key=lambda f: f.relative_to(root).as_posix()):
            add_file(z, f"overrides/{f.relative_to(root).as_posix()}", f.read_bytes())

    client_only = sum(f["env"]["server"] == "unsupported" for f in files)
    print(f"Built {out}: {len(files)} mods ({client_only} client-only), "
          f"{len(overrides)} override files, {out.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
