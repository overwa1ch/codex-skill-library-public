#!/usr/bin/env python3
"""Convert a standalone app-server snapshot into a reviewable local manifest.

This does not establish Desktop inventory completeness or change host configuration.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from configure import atomic, parse, skill_entries
from library import LibraryError, canonical_path


def source_identity(path, scope):
    parts = path.parts
    # Cache/<marketplace>/<plugin>/<version>/skills/.../SKILL.md
    if scope == "plugin":
        for i in range(len(parts) - 5):
            if parts[i:i+2] == ("plugins", "cache"):
                tail = parts[i+2:]
                if (len(tail) < 6 or tail[3] != "skills" or tail[-1] != "SKILL.md"
                        or any(part in (".", "..") for part in tail)
                        or not re.fullmatch(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?", tail[2])):
                    raise LibraryError("Unknown plugin layout; supply an explicit stable source identity")
                return "plugin:" + "/".join(tail[:2] + tail[3:])
        raise LibraryError("Unknown plugin layout; supply an explicit stable source identity")
    return scope + ":" + str(path)


def convert(snapshot, config, input_sha):
    if not isinstance(snapshot, dict) or snapshot.get("surface") != "standalone-app-server-not-desktop":
        raise LibraryError("Expected an explicitly labelled standalone app-server snapshot")
    inventory = snapshot.get("skills")
    if not isinstance(inventory, dict):
        raise LibraryError("Missing host inventory")
    rows = inventory.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise LibraryError("Expected a snapshot for exactly one cwd")
    if rows[0].get("errors"):
        raise LibraryError("Host reported inventory errors; resolve before importing")
    skills = rows[0].get("skills")
    if not isinstance(skills, list):
        raise LibraryError("Missing skills inventory")
    disabled = {str(canonical_path(s["path"]))
                for s in skill_entries(config) if s.get("enabled") is False}
    result, seen = [], set()
    for item in skills:
        if not isinstance(item, dict) or type(item.get("enabled")) is not bool:
            raise LibraryError("Host entry lacks explicit enabled state")
        if not isinstance(item.get("path"), str) or not isinstance(item.get("scope"), str):
            raise LibraryError("Host entry requires path and scope")
        path = canonical_path(item["path"])
        scope = item["scope"].lower()
        if scope not in ("system", "user", "repo", "admin", "plugin"):
            raise LibraryError("Unrecognized host scope: " + scope)
        if str(path) in seen:
            raise LibraryError("Duplicate host path")
        seen.add(str(path))
        result.append({"path": str(path), "source": source_identity(path, scope),
                       "scope": scope, "enabled": item["enabled"] and str(path) not in disabled,
                       "tags": []})
    # Preserve explicit disabled paths even if the host does not discover them.
    # Their scope/source must be reviewed; do not guess a plugin identity.
    absent = sorted(disabled - seen)
    return {"surface": snapshot["surface"], "input_sha256": input_sha,
            "desktop_completeness": "not-established",
            "unresolved_disabled_paths": absent, "skills": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        raw = Path(args.snapshot).read_bytes()
        doc = parse(Path(args.config).read_bytes())
        manifest = convert(json.loads(raw), doc, hashlib.sha256(raw).hexdigest())
        atomic(Path(args.output), (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
        print(json.dumps({"output": args.output, "skills": len(manifest["skills"]),
                          "unresolved_disabled_paths": len(manifest["unresolved_disabled_paths"]),
                          "desktop_completeness": manifest["desktop_completeness"]}))
        return 0
    except (LibraryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
