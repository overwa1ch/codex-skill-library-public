#!/usr/bin/env python3
"""Local skill registry, CLI and read-only stdio MCP adapter."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import uuid

import yaml


class LibraryError(Exception):
    pass


class _RegistryState(dict):
    """Mapping serialized as ordinary JSON; transaction guards stay in memory."""
    def __init__(self, value):
        super().__init__(value)
        self.write_checks = []


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_path(path):
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise LibraryError("Cannot resolve path: " + str(exc)) from exc


def skill_file(path):
    path = canonical_path(path)
    if path.name != "SKILL.md" or not path.is_file():
        raise LibraryError("Expected an existing SKILL.md: " + str(path))
    return path


def read_policy(skill_path):
    path = skill_path.parent / "agents" / "openai.yaml"
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        if path.is_symlink() or (path.parent.is_symlink() and not path.parent.exists()):
            raise LibraryError("Broken skill policy link: " + str(path)) from exc
        return {"allow_implicit_invocation": True, "policy_sha256": None, "display_name": None}
    except OSError as exc:
        raise LibraryError("Cannot read skill policy: " + str(path)) from exc
    try:
        meta = yaml.safe_load(data.decode("utf-8"))
        if not isinstance(meta, dict):
            raise LibraryError("Skill policy file must be a mapping: " + str(path))
        policy = meta.get("policy", {})
        if not isinstance(policy, dict):
            raise LibraryError("Skill policy must be a mapping: " + str(path))
        implicit = policy.get("allow_implicit_invocation", True)
        if type(implicit) is not bool:
            raise LibraryError("allow_implicit_invocation must be boolean: " + str(path))
        interface = meta.get("interface", {})
        display_name = interface.get("display_name") if isinstance(interface, dict) else None
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = None
        return {"allow_implicit_invocation": implicit, "policy_sha256": digest(data),
                "display_name": display_name}
    except (UnicodeError, yaml.YAMLError) as exc:
        raise LibraryError("Invalid skill policy: " + str(path)) from exc


def read_skill(path):
    path = skill_file(path)
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise LibraryError("Missing YAML frontmatter: " + str(path))
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        meta = yaml.safe_load("\n".join(lines[1:end]))
        if not isinstance(meta, dict):
            raise LibraryError("Frontmatter must be a mapping: " + str(path))
        for key in ("name", "description"):
            if not isinstance(meta.get(key), str) or not meta[key].strip():
                raise LibraryError("Missing non-empty " + key + ": " + str(path))
        return {"name": meta["name"], "description": meta["description"],
                "path": str(path), "skill_root": str(path.parent),
                "sha256": digest(data), "text": text, **read_policy(path)}
    except (OSError, UnicodeError, StopIteration, yaml.YAMLError) as exc:
        raise LibraryError("Cannot read skill " + str(path) + ": " + str(exc)) from exc


class Library:
    def __init__(self, registry):
        self.path = Path(registry).expanduser().absolute()

    def read(self):
        if not self.path.exists():
            raise LibraryError("Registry does not exist; run init: " + str(self.path))
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LibraryError("Unreadable registry: " + str(exc)) from exc
        self.validate(state)
        return state

    @staticmethod
    def validate(state):
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("skills"), list):
            raise LibraryError("Unsupported registry schema")
        ids, paths, sources = set(), set(), set()
        for item in state["skills"]:
            if not isinstance(item, dict):
                raise LibraryError("Invalid registry entry")
            sid = item.get("id")
            if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{32}", sid) or sid in ids:
                raise LibraryError("Invalid or duplicate skill ID")
            ids.add(sid)
            path = item.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise LibraryError("Skill path must be absolute")
            canonical = str(canonical_path(path))
            if canonical in paths:
                raise LibraryError("Duplicate canonical skill path")
            paths.add(canonical)
            if item.get("placement") not in ("global", "library") or type(item.get("enabled")) is not bool:
                raise LibraryError("Invalid placement or enabled state")
            if not isinstance(item.get("source"), str) or not item["source"]:
                raise LibraryError("Missing source identity")
            if item["source"] in sources:
                raise LibraryError("Duplicate logical source identity")
            sources.add(item["source"])
            if item.get("scope") not in ("system", "user", "repo", "admin", "plugin"):
                raise LibraryError("Invalid source scope")
            if not isinstance(item.get("tags"), list) or not all(isinstance(t, str) and t for t in item["tags"]):
                raise LibraryError("Invalid tags")
            availability = item.get("source_state")
            if "source_state" in item:
                if (not isinstance(availability, dict)
                        or availability.get("state") not in (
                            "current", "plugin_inactive", "removed_from_current_plugin", "unknown")
                        or not isinstance(availability.get("reason"), str) or not availability["reason"].strip()
                        or not isinstance(availability.get("evidence_sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", availability["evidence_sha256"])
                        or item["scope"] != "plugin"):
                    raise LibraryError("Invalid plugin source state")

    @staticmethod
    def source_current(entry):
        return entry.get("source_state", {}).get("state", "current") == "current"

    @staticmethod
    def source_inactive(entry):
        return entry.get("source_state", {}).get("state") in (
            "plugin_inactive", "removed_from_current_plugin")

    @staticmethod
    def require_current(entry):
        if not Library.source_current(entry):
            state = entry["source_state"]
            raise LibraryError("Skill source is " + state["state"] + ": " + state["reason"]
                               + "; saved enabled preference is unchanged (" + entry["id"] + ")")

    def apply_source_plan(self, plan):
        """Apply explicit source lifecycle changes from a complete reviewed inventory.

        The inventory is a local authority review, not an inference from missing files.
        It binds exact plugin identities, installation state and declared sources.
        """
        if (not isinstance(plan, dict) or plan.get("version") != 1
                or not isinstance(plan.get("changes"), list)
                or not isinstance(plan.get("evidence"), str) or not Path(plan["evidence"]).is_absolute()):
            raise LibraryError("Invalid source-state plan")
        raw = Path(plan["evidence"]).read_bytes()
        evidence_sha = digest(raw)
        if evidence_sha != plan.get("evidence_sha256"):
            raise LibraryError("Source inventory evidence changed")
        evidence = json.loads(raw)
        if (not isinstance(evidence, dict) or evidence.get("version") != 1
                or evidence.get("complete") is not True
                or evidence.get("errors") != [] or evidence.get("fallback") is not False
                or not isinstance(evidence.get("plugins"), list)):
            raise LibraryError("Source changes require a complete successful inventory without fallback")
        plugins = {}
        for plugin in evidence["plugins"]:
            if (not isinstance(plugin, dict) or not isinstance(plugin.get("id"), str)
                    or plugin["id"] in plugins or type(plugin.get("installed")) is not bool
                    or type(plugin.get("enabled")) is not bool):
                raise LibraryError("Invalid or duplicate inventory plugin")
            plugins[plugin["id"]] = plugin
        with self.transaction() as state:
            if digest(self.path.read_bytes()) != plan.get("registry_sha256"):
                raise LibraryError("Registry changed after source-state plan")
            def verify_evidence():
                if digest(Path(plan["evidence"]).read_bytes()) != evidence_sha:
                    raise LibraryError("Source inventory evidence changed during classification")
            state.write_checks.append(verify_evidence)
            seen = set()
            for change in plan["changes"]:
                if (not isinstance(change, dict) or not isinstance(change.get("id"), str)
                        or change["id"] in seen):
                    raise LibraryError("Invalid or duplicate source-state change")
                entry = self.entry(state, change.get("id"))
                seen.add(entry["id"])
                if entry["scope"] != "plugin" or entry.get("source_state") != change.get("before"):
                    raise LibraryError("Source state changed after plan")
                parts = entry["source"].removeprefix("plugin:").split("/")
                if not entry["source"].startswith("plugin:") or len(parts) < 4 or parts[2] != "skills":
                    raise LibraryError("Source state requires an exact plugin identity")
                plugin = plugins.get(parts[1] + "@" + parts[0])
                if plugin is None:
                    raise LibraryError("Inventory lacks explicit plugin installation state")
                after = change.get("after")
                if not isinstance(after, dict) or after.get("evidence_sha256") != evidence_sha:
                    raise LibraryError("Source change lacks evidence binding")
                target = after.get("state")
                if target == "plugin_inactive":
                    if plugin["installed"] and plugin["enabled"]:
                        raise LibraryError("Current plugin cannot be classified inactive")
                elif target in ("current", "removed_from_current_plugin"):
                    if (not plugin["installed"] or not plugin["enabled"]
                            or plugin.get("skills_complete") is not True
                            or not isinstance(plugin.get("sources"), list)
                            or not all(isinstance(s, str) and s.startswith(
                                "plugin:" + parts[0] + "/" + parts[1] + "/skills/")
                                       for s in plugin["sources"])
                            or len(set(plugin["sources"])) != len(plugin["sources"])
                            or not isinstance(plugin.get("version"), str) or not plugin["version"]
                            or not isinstance(plugin.get("manifest_sha256"), str)
                            or not re.fullmatch(r"[0-9a-f]{64}", plugin["manifest_sha256"])):
                        raise LibraryError("Source classification requires a complete current plugin manifest")
                    present = entry["source"] in plugin["sources"]
                    if present != (target == "current"):
                        raise LibraryError("Source classification contradicts the current plugin manifest")
                elif target != "unknown":
                    raise LibraryError("Unknown source-state target")
                entry["source_state"] = dict(after)
            # Validate the entire batch before the transaction writes any changes.
            self.validate(state)
        return {"changed": len(plan["changes"]), "evidence_sha256": evidence_sha}

    @contextlib.contextmanager
    def transaction(self, create=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            before = self.path.read_bytes() if self.path.exists() else None
            state = _RegistryState({"version": 1, "skills": []}
                                   if create and not self.path.exists() else self.read())
            yield state
            self.validate(state)
            for check in state.write_checks:
                check()
            if (self.path.read_bytes() if self.path.exists() else None) != before:
                raise LibraryError("Registry changed during transaction; refusing to overwrite")
            fd, tmp = tempfile.mkstemp(prefix=".registry-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                for check in state.write_checks:
                    check()
                if (self.path.read_bytes() if self.path.exists() else None) != before:
                    raise LibraryError("Registry changed while preparing transaction; refusing to overwrite")
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def init(self):
        with self.transaction(create=True):
            pass
        return {"registry": str(self.path)}

    @staticmethod
    def entry(state, sid):
        for item in state["skills"]:
            if item["id"] == sid:
                return item
        raise LibraryError("Unknown skill ID: " + sid)

    def register(self, path, source, scope="user", enabled=True, tags=()):
        return self.import_entries([{"path": str(path), "source": source, "scope": scope,
                                     "enabled": enabled, "tags": list(tags)}])[0]

    def import_entries(self, entries):
        """Import one reviewed inventory atomically; never infer a path rebind."""
        if not isinstance(entries, list):
            raise LibraryError("Inventory skills must be an array")
        prepared = []
        for item in entries:
            if not isinstance(item, dict) or type(item.get("enabled")) is not bool:
                raise LibraryError("Inventory entries require explicit enabled state")
            if not isinstance(item.get("path"), str):
                raise LibraryError("Inventory entries require a path")
            # Disabled tombstones may refer to removed versions. Never read their body.
            path = (read_skill(item["path"])["path"] if item["enabled"]
                    else str(canonical_path(item["path"])))
            if Path(path).name != "SKILL.md":
                raise LibraryError("Inventory path must end in SKILL.md")
            prepared.append({"id": uuid.uuid4().hex, "path": path,
                             "source": item.get("source"), "scope": item.get("scope"),
                             "placement": "global" if item.get("scope") == "system" else "library",
                             "enabled": item["enabled"], "tags": item.get("tags", [])})
        self.validate({"version": 1, "skills": prepared})
        imported = []
        with self.transaction() as state:
            for candidate in prepared:
                previous = next((s for s in state["skills"]
                                 if str(canonical_path(s["path"])) == candidate["path"]), None)
                if previous is not None:
                    if (previous["source"], previous["scope"]) != (candidate["source"], candidate["scope"]):
                        raise LibraryError("Source conflict for existing path; resolve explicitly")
                    # Repeated scans never reset manual status, placement or tags.
                    imported.append(dict(previous))
                    continue
                if any(s["source"] == candidate["source"] for s in state["skills"]):
                    raise LibraryError("Source already registered at another path; use edit --path to rebind")
                candidate["tags"] = list(dict.fromkeys(candidate["tags"]))
                state["skills"].append(candidate)
                imported.append(dict(candidate))
        return imported

    def edit(self, sid, enabled=None, placement=None, tags=None, path=None):
        if path is not None:
            path = read_skill(path)["path"]
        with self.transaction() as state:
            item = self.entry(state, sid)
            if enabled is not None:
                item["enabled"] = enabled
            if placement is not None:
                item["placement"] = placement
            if tags is not None:
                item["tags"] = list(dict.fromkeys(tags))
            if path is not None:
                item["path"] = path
            return dict(item)

    def listing(self, tag=None, include_disabled=False, tools_only=False, include_inactive=False):
        items = []
        for entry in self.read()["skills"]:
            if not entry["enabled"] and not include_disabled:
                continue
            if tools_only and entry["placement"] != "library":
                continue
            if tag is not None and tag not in entry["tags"]:
                continue
            if self.source_inactive(entry):
                if include_inactive:
                    items.append(dict(entry))
                continue
            if not entry["enabled"]:
                # Disabled missing paths remain manageable without reading their contents.
                items.append(dict(entry))
                continue
            self.require_current(entry)
            skill = read_skill(entry["path"])
            skill.pop("text")
            items.append({**entry, **skill})
        return items

    def load(self, sid, tool=False):
        entry = self.entry(self.read(), sid)
        if not entry["enabled"]:
            raise LibraryError("Skill is disabled: " + sid)
        self.require_current(entry)
        if tool and entry["placement"] != "library":
            raise LibraryError("Skill is not Library-managed: " + sid)
        skill = read_skill(entry["path"])
        if tool and not skill["allow_implicit_invocation"]:
            raise LibraryError("Skill requires an explicit request; use load_explicit_skill")
        return {**entry, **skill,
                "relative_path_base": skill["skill_root"],
                "execution_note": "Resolve skill-relative files against skill_root. Use absolute paths or an explicit cwd when running scripts. Loading does not execute the workflow."}

    def load_explicit(self, name, source=None):
        if (not isinstance(name, str) or not name.strip()
                or (source is not None and (not isinstance(source, str) or not source.strip()))):
            raise LibraryError("Explicit loading requires a non-empty name and optional source")
        if source is None:
            candidates, unavailable = self.catalog_status()
            if unavailable:
                readable = [s["source"] for s in candidates
                            if not s["allow_implicit_invocation"]
                            and name in (s["name"], s["display_name"])]
                raise LibraryError("Some sources are unavailable; provide an exact source before "
                                   "loading by name. Readable matching sources: "
                                   + json.dumps(readable, ensure_ascii=False))
        else:
            candidates = []
            for entry in self.read()["skills"]:
                if (entry["enabled"] and entry["placement"] == "library"
                        and entry["source"] == source):
                    self.require_current(entry)
                    candidates.append({**entry, **read_skill(entry["path"])})
        matches = [s for s in candidates
                   if not s["allow_implicit_invocation"] and name in (s["name"], s["display_name"])
                   and (source is None or s["source"] == source)]
        if not matches:
            raise LibraryError("No enabled explicit-only Library skill matches that name/source")
        if len(matches) != 1:
            raise LibraryError("Ambiguous skill name; specify source: "
                               + json.dumps([s["source"] for s in matches], ensure_ascii=False))
        selected = matches[0]
        loaded = self.load(selected["id"])
        if (loaded["placement"] != "library" or loaded["allow_implicit_invocation"]
                or (loaded["name"], loaded["source"]) != (selected["name"], selected["source"])
                or name not in (loaded["name"], loaded["display_name"])):
            raise LibraryError("Skill selection changed; discover it again")
        return loaded

    @staticmethod
    def _tools_for_skills(skills):
        result = [{
            "name": "load_" + s["id"],
            "description": "Load the local skill instructions for " + s["name"] + ". " + s["description"],
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False}
        } for s in skills if s["allow_implicit_invocation"]]
        if any(not s["allow_implicit_invocation"] for s in skills):
            result.append({
                "name": "load_explicit_skill",
                "description": "Load an explicit-only Library skill by exact skill name or display name only when the user names it. Never choose one from the task topic alone. Supply source to disambiguate equal names.",
                "inputSchema": {"type": "object", "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "source": {"type": "string", "minLength": 1}},
                    "required": ["name"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                "idempotentHint": True, "openWorldHint": False}
            })
        return result

    def tools(self):
        return self._tools_for_skills(self.listing(tools_only=True))

    def catalog_status(self):
        """Read live sources independently; never cache a failed source's metadata."""
        skills, errors = [], []
        for entry in self.read()["skills"]:
            if not entry["enabled"] or entry["placement"] != "library":
                continue
            if self.source_inactive(entry):
                continue
            try:
                self.require_current(entry)
                skill = read_skill(entry["path"])
                skill.pop("text")
                skills.append({**entry, **skill})
            except (LibraryError, OSError) as exc:
                errors.append({"id": entry["id"], "source": entry["source"],
                               "path": entry["path"], "error": str(exc)})
        return skills, errors

    def mcp_tools(self):
        skills, errors = self.catalog_status()
        inactive = any(self.source_inactive(e) for e in self.read()["skills"])
        if not errors and not inactive:
            return self._tools_for_skills(skills)
        if not skills and errors:
            raise LibraryError(errors[0]["error"])
        # UUID/source routes cannot collide with an unavailable skill. Unqualified
        # name selection stays fail-closed because unreadable aliases are unknown.
        tools = self._tools_for_skills(skills)
        tools.append({
            "name": "library_status",
            "description": "Inspect current source errors and historical plugin source states. Current workflows remain available. Explicit name loading requires an exact source while current sources have errors.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False}
        })
        return tools

    def status(self):
        skills, errors = self.catalog_status()
        return {"state": "degraded" if errors else "healthy", "unavailable": errors,
                "inactive_sources": [dict(e) for e in self.read()["skills"] if self.source_inactive(e)],
                "available_implicit": sum(s["allow_implicit_invocation"] for s in skills),
                "available_explicit": sum(not s["allow_implicit_invocation"] for s in skills),
                "explicit_loading_available": any(not s["allow_implicit_invocation"] for s in skills),
                "explicit_source_required": bool(errors)}


def _file_stamp(path):
    """Cheap change detection only; requests still validate and read the source."""
    def one(follow):
        try:
            value = path.stat() if follow else path.lstat()
            return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns)
        except OSError as exc:
            return (type(exc).__name__, exc.errno)
    return (one(False), one(True))


class _CatalogWatchState:
    """Retain paths and stat signatures, never skill bodies or parsed metadata."""
    def __init__(self, lib):
        self.lib = lib
        self.registry_stamp = None
        self.paths = ()

    def stamp(self):
        registry_stamp = _file_stamp(self.lib.path)
        if registry_stamp != self.registry_stamp:
            try:
                self.paths = tuple(Path(entry['path']) for entry in self.lib.read()['skills']
                                   if entry['enabled'] and entry['placement'] == 'library'
                                   and not self.lib.source_inactive(entry))
            except (LibraryError, OSError):
                self.paths = ()
                # A referenced path can become unresolvable (for example a
                # symlink loop) while registry bytes remain valid and unchanged.
                # Retry failed discovery so repairing that source can notify.
                self.registry_stamp = None
            else:
                self.registry_stamp = registry_stamp
        sources = []
        for path in self.paths:
            body_stamp = _file_stamp(path)
            # A final SKILL.md symlink can move its policy root outside the
            # registered parent. Directory symlinks are covered by parent stamps.
            try:
                root = path.resolve().parent if stat.S_ISLNK(path.lstat().st_mode) else path.parent
            except (OSError, RuntimeError):
                root = path.parent
            policy = root / 'agents' / 'openai.yaml'
            sources.append((str(path), body_stamp, _file_stamp(path.parent),
                            str(policy), _file_stamp(policy.parent), _file_stamp(policy)))
        return (registry_stamp, tuple(sources))


def serve(lib):
    """One JSON-RPC object per line, as required by MCP stdio."""
    write_lock = threading.Lock()
    ready = threading.Event()
    stopped = threading.Event()
    catalog_lock = threading.Lock()
    previous_catalog = None
    previous_stamp = None
    watch_state = _CatalogWatchState(lib)

    def emit(value):
        with write_lock:
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def catalog_digest():
        try:
            return digest(json.dumps(lib.mcp_tools(), sort_keys=True).encode())
        except (LibraryError, OSError):
            return "invalid"

    def watch():
        nonlocal previous_catalog, previous_stamp
        while not stopped.wait(1):
            if not ready.is_set():
                continue
            with catalog_lock:
                stamp = watch_state.stamp()
                if stamp == previous_stamp:
                    continue
                current = catalog_digest()
                if previous_catalog != current:
                    emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                previous_catalog = current
                previous_stamp = stamp

    threading.Thread(target=watch, daemon=True).start()
    try:
        for line in sys.stdin:
            rid = None
            req = {}
            try:
                req = json.loads(line)
                if (not isinstance(req, dict) or req.get("jsonrpc") != "2.0"
                        or not isinstance(req.get("method"), str)
                        or ("id" in req and type(req["id"]) not in (int, str))):
                    emit({"jsonrpc": "2.0", "id": None,
                          "error": {"code": -32600, "message": "Invalid request"}})
                    continue
                rid = req.get("id")
                method = req.get("method")
                if rid is None:
                    if method == "notifications/initialized":
                        with catalog_lock:
                            previous_stamp = watch_state.stamp()
                            previous_catalog = catalog_digest()
                        ready.set()
                    continue
                params = req.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                if method == "initialize":
                    version = params.get("protocolVersion")
                    supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
                    result = {
                        "protocolVersion": version if version in supported else "2025-06-18",
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "skill-library", "version": "0.1.0"},
                        "instructions": "Always discover and load relevant workflows here BEFORE research, web, notes, writing, code, data or media work, even for short replies. If tool_search fails, inspect ALL_TOOLS metadata. Skip only arithmetic, translation and punctuation-only edits."
                    }
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    with catalog_lock:
                        stamp = watch_state.stamp()
                        catalog = lib.mcp_tools()
                        previous_catalog = digest(json.dumps(catalog, sort_keys=True).encode())
                        previous_stamp = stamp
                    result = {"tools": catalog}
                elif method == "tools/call":
                    name = params.get("name", "")
                    args = params.get("arguments", {})
                    if name == "library_status":
                        if args != {}:
                            raise LibraryError("Library status accepts no arguments")
                        skill = lib.status()
                    elif name == "load_explicit_skill":
                        if (not isinstance(args, dict) or "name" not in args
                                or set(args) - {"name", "source"}
                                or ("source" in args and args["source"] is None)):
                            raise LibraryError("Explicit loading accepts name and optional source")
                        skill = lib.load_explicit(args["name"], args.get("source"))
                    elif not isinstance(name, str) or not re.fullmatch(r"load_[0-9a-f]{32}", name):
                        raise LibraryError("Unknown loading tool")
                    elif args != {}:
                        raise LibraryError("Loading tools accept no arguments")
                    else:
                        skill = lib.load(name[5:], tool=True)
                    result = {"content": [{"type": "text", "text": json.dumps(skill, ensure_ascii=False)}],
                              "isError": False}
                else:
                    emit({"jsonrpc": "2.0", "id": rid,
                          "error": {"code": -32601, "message": "Method not found"}})
                    continue
                emit({"jsonrpc": "2.0", "id": rid, "result": result})
            except (LibraryError, OSError) as exc:
                if req.get("method") == "tools/call":
                    emit({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": str(exc)}], "isError": True}})
                else:
                    emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(exc)}})
            except json.JSONDecodeError as exc:
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
            except (ValueError, TypeError) as exc:
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(exc)}})
    finally:
        stopped.set()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    add = sub.add_parser("register")
    add.add_argument("path")
    add.add_argument("--source", required=True, help="Unique, stable logical identity for this skill, excluding plugin version")
    add.add_argument("--scope", choices=["system", "user", "repo", "admin", "plugin"], default="user")
    add.add_argument("--disabled", action="store_true")
    add.add_argument("--tag", action="append", default=[])
    imp = sub.add_parser("import-manifest", help="Import a reviewed explicit inventory atomically")
    imp.add_argument("manifest", help="JSON object containing a skills array with path/source/scope/enabled")
    ls = sub.add_parser("list")
    ls.add_argument("--tag")
    ls.add_argument("--include-disabled", action="store_true")
    ls.add_argument("--include-inactive", action="store_true")
    source = sub.add_parser("apply-source-plan", help="Apply a reviewed, evidence-bound source lifecycle plan")
    source.add_argument("plan")
    sub.add_parser("tools")
    sub.add_parser("status", help="Report live source availability without changing enabled state")
    sub.add_parser("serve")
    load = sub.add_parser("load")
    load.add_argument("id")
    edit = sub.add_parser("edit")
    edit.add_argument("id")
    edit.add_argument("--enabled", choices=["true", "false"])
    edit.add_argument("--placement", choices=["global", "library"])
    edit.add_argument("--tags", nargs="*")
    edit.add_argument("--path", help="Explicitly rebind an existing identity to an updated source path")
    args = p.parse_args()
    lib = Library(args.registry)
    try:
        if args.command == "serve":
            serve(lib)
            return 0
        if args.command == "init":
            result = lib.init()
        elif args.command == "register":
            result = lib.register(args.path, args.source, args.scope, not args.disabled, args.tag)
        elif args.command == "import-manifest":
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise LibraryError("Manifest must be an object")
            result = lib.import_entries(manifest.get("skills"))
        elif args.command == "list":
            result = lib.listing(args.tag, args.include_disabled, include_inactive=args.include_inactive)
        elif args.command == "apply-source-plan":
            result = lib.apply_source_plan(json.loads(Path(args.plan).read_text(encoding="utf-8")))
        elif args.command == "load":
            result = lib.load(args.id)
        elif args.command == "tools":
            result = lib.tools()
        elif args.command == "status":
            result = lib.status()
        elif args.command == "edit":
            result = lib.edit(args.id, None if args.enabled is None else args.enabled == "true",
                              args.placement, args.tags, args.path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LibraryError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
