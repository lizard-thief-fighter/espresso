"""Static checks for the Espresso! pack. Exits non-zero on any error.

Checks:
  - modpack.toml is complete; a release tag (if given) matches its version.
  - Every .index/*.pw.toml is a Modrinth URL entry with a sha512 hash and a valid side.
  - Every JSON / .mcmeta file in the datapacks and unzipped resource packs parses strictly.
  - Every espresso: reference in datapacks resolves to a file (tags to tag files).
  - Every function referenced by a function tag or `function` command exists.
  - Every file/ resource pack enabled in options.txt exists.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import tomllib

NS = "espresso"
SIDES = {"", "both", "client", "server"}
errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def check_manifest(root: pathlib.Path, tag: str | None) -> dict:
    m = tomllib.loads((root / "modpack.toml").read_text())
    for key in ("name", "version"):
        if not m.get("pack", {}).get(key):
            err(f"modpack.toml: [pack].{key} missing")
    for key in ("minecraft", "neoforge"):
        if not m.get("versions", {}).get(key):
            err(f"modpack.toml: [versions].{key} missing")
    if not re.fullmatch(r"\d+\.\d+\.\d+", m.get("pack", {}).get("version", "")):
        err("modpack.toml: version must be MAJOR.MINOR.PATCH")
    if tag and tag != f"v{m['pack']['version']}":
        err(f"tag {tag} does not match modpack.toml version {m['pack']['version']}")
    for o in m.get("build", {}).get("overrides", []):
        if not (root / o).exists():
            err(f"modpack.toml: override path {o} does not exist")
    return m


def check_index(root: pathlib.Path) -> None:
    files = sorted((root / ".index").glob("*.pw.toml"))
    if not files:
        err(".index: no mod entries")
    names = set()
    for f in files:
        try:
            d = tomllib.loads(f.read_text())
        except tomllib.TOMLDecodeError as e:
            err(f"{f.name}: invalid TOML ({e})")
            continue
        dl = d.get("download", {})
        if dl.get("mode") != "url" or not str(dl.get("url", "")).startswith("https://cdn.modrinth.com/"):
            err(f"{f.name}: download must be a cdn.modrinth.com URL")
        if dl.get("hash-format") != "sha512" or not re.fullmatch(r"[0-9a-f]{128}", dl.get("hash", "")):
            err(f"{f.name}: needs a sha512 hash")
        if d.get("side", "") not in SIDES:
            err(f"{f.name}: unknown side {d.get('side')!r}")
        fn = d.get("filename", "")
        if not fn.endswith(".jar"):
            err(f"{f.name}: filename must be a .jar")
        if fn in names:
            err(f"{f.name}: duplicate filename {fn}")
        names.add(fn)


# Folders whose JSON Minecraft itself loads strictly. Mod configs are excluded:
# several mods accept comments (JSON5) in their config files.
STRICT_JSON = ["moonlight-global-datapacks", "resourcepacks"]


def load_json_files(root: pathlib.Path) -> dict[pathlib.Path, object]:
    parsed = {}
    for o in STRICT_JSON:
        base = root / o
        if base.is_file():
            continue
        for p in base.rglob("*"):
            if p.suffix in (".json", ".mcmeta") and p.is_file():
                try:
                    parsed[p] = json.loads(p.read_text(encoding="utf-8-sig"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    err(f"{p.relative_to(root)}: invalid JSON ({e})")
    return parsed


def defined_ids(dp_root: pathlib.Path) -> tuple[set[str], set[str], set[str]]:
    """Return (any resource paths, tag paths, function ids) defined in namespace NS."""
    anything, tags, functions = set(), set(), set()
    for p in dp_root.glob(f"*/data/{NS}/**/*"):
        if not p.is_file():
            continue
        parts = p.relative_to(dp_root).parts[3:]  # drop <pack>/data/<NS>
        stem = "/".join(parts).rsplit(".", 1)[0].split("/")
        for k in range(1, len(stem)):
            anything.add("/".join(stem[k:]))
        if stem[0] == "tags":
            # Registry folders may nest (tags/worldgen/biome/...); accept any suffix.
            for k in range(2, len(stem)):
                tags.add("/".join(stem[k:]))
        if stem[0] == "function":
            functions.add("/".join(stem[1:]))
    return anything, tags, functions


REF = re.compile(rf"^(#?){NS}:([a-z0-9_./-]+)$")


def walk(node, path, parent, out):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, path + [k], node, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, path, parent, out)
    elif isinstance(node, str):
        out.append((node, path, parent))


def check_references(root: pathlib.Path, parsed: dict) -> None:
    dp_root = root / "moonlight-global-datapacks"
    anything, tags, functions = defined_ids(dp_root)
    for p, data in parsed.items():
        if dp_root not in p.parents and "overrides" not in p.parts:
            continue
        strings = []
        walk(data, [], None, strings)
        for s, path, parent in strings:
            m = REF.match(s)
            if not m:
                continue
            is_tag, rid = m.group(1) == "#", m.group(2)
            # Attribute-modifier ids are free-form names, not resources.
            if path and path[-1] == "id" and isinstance(parent, dict) and "attribute" in parent:
                continue
            if "tags" in p.parts and p.parts[p.parts.index("tags") + 1] == "function":
                ok = rid in functions
            else:
                ok = rid in tags if is_tag else rid in anything
            if not ok:
                err(f"{p.relative_to(root)}: unresolved reference {s}")
    for p in dp_root.glob("*/data/*/function/**/*.mcfunction"):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            for fid in re.findall(rf"\bfunction {NS}:([a-z0-9_./-]+)", line):
                if fid not in functions:
                    err(f"{p.relative_to(root)}:{n}: unknown function {NS}:{fid}")


def check_options(root: pathlib.Path) -> None:
    opts = root / "options.txt"
    if not opts.exists():
        return
    for line in opts.read_text().splitlines():
        if line.startswith("resourcePacks:"):
            try:
                packs = json.loads(line.split(":", 1)[1])
            except json.JSONDecodeError:
                err("options.txt: resourcePacks is not valid JSON")
                return
            for pk in packs:
                if pk.startswith("file/") and not (root / "resourcepacks" / pk[5:]).exists():
                    err(f"options.txt: enabled resource pack {pk} not found in resourcepacks/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--tag", help="release tag to validate against modpack.toml (e.g. v0.0.4)")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()
    m = check_manifest(root, a.tag)
    check_index(root)
    parsed = load_json_files(root)
    check_references(root, parsed)
    check_options(root)
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "ERROR: "
    for e in errors:
        print(f"{prefix}{e}")
    print(f"{len(parsed)} JSON files parsed; {len(list((root / '.index').glob('*.pw.toml')))} mods; {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
