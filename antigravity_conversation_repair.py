#!/usr/bin/env python3
"""Antigravity Conversation Repair — single-file, standard-library recovery helper.

Repairs the two Antigravity IDE conversation indices in state.vscdb:
  * chat.ChatSessionStore.index (JSON)
  * antigravityUnifiedStateSync.trajectorySummaries (Base64-wrapped Protobuf)

Safety design:
  * dry-run by default; writes require --apply
  * best-effort running-process checks on Linux, macOS and Windows; quit the IDE manually first
  * makes and validates a SQLite-consistent backup before the first write
  * uses one SQLite transaction and rechecks for concurrent database changes
  * preserves unrelated Protobuf entries byte-for-byte and never prunes JSON entries
  * only repairs workspace metadata that is absent, incomplete, or malformed

Python 3.10+; no third-party dependencies.
"""
from __future__ import annotations

import argparse
import base64
import collections
import csv
import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

VERSION = "1.2.0"
PB_KEY = "antigravityUnifiedStateSync.trajectorySummaries"
JSON_KEY = "chat.ChatSessionStore.index"
DB_FILENAME = "state.vscdb"
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PLACEHOLDER_RE = re.compile(r"^Conversation\s*\([^)]*\)\s+[0-9a-fA-F]{8}$")


class RepairError(RuntimeError):
    pass


def note(message: str = "") -> None:
    print(message)


# ---------------------------------------------------------------------------
# minnimal, strict Protobuf wire parser / encoder (wire types 0, 1, 2 and 5).
# ---------------------------------------------------------------------------

def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while pos < len(data) and shift <= 70:
        byte = data[pos]
        value |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            return value, pos
        shift += 7
    raise RepairError("Malformed Protobuf varint")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("Protobuf varint must be non-negative")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field_bytes(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def field_string(number: int, value: str) -> bytes:
    return field_bytes(number, value.encode("utf-8"))


def field_varint(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | 0) + encode_varint(value)


def timestamp_field(number: int, epoch: int) -> bytes:
    stamp = field_varint(1, max(0, int(epoch))) + field_varint(2, 0)
    return field_bytes(number, stamp)


@dataclass(frozen=True)
class ProtoField:
    number: int
    wire: int
    value: Any
    raw: bytes
    start: int
    end: int


def parse_fields(data: bytes) -> list[ProtoField]:
    fields: list[ProtoField] = []
    pos = 0
    while pos < len(data):
        start = pos
        tag, pos = decode_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise RepairError("Invalid Protobuf field number 0")
        if wire == 0:
            value, pos = decode_varint(data, pos)
        elif wire == 1:
            end = pos + 8
            if end > len(data):
                raise RepairError("Truncated fixed64 Protobuf field")
            value, pos = data[pos:end], end
        elif wire == 2:
            length, pos = decode_varint(data, pos)
            end = pos + length
            if end > len(data):
                raise RepairError("Truncated length-delimited Protobuf field")
            value, pos = data[pos:end], end
        elif wire == 5:
            end = pos + 4
            if end > len(data):
                raise RepairError("Truncated fixed32 Protobuf field")
            value, pos = data[pos:end], end
        else:
            raise RepairError(f"Unsupported Protobuf wire type {wire}; refusing to rewrite this payload")
        fields.append(ProtoField(number, wire, value, data[start:pos], start, pos))
    return fields


def field_set(data: bytes) -> set[int]:
    return {f.number for f in parse_fields(data)}


def strip_fields(data: bytes, numbers: set[int]) -> bytes:
    return b"".join(f.raw for f in parse_fields(data) if f.number not in numbers)


def first_bytes_field(data: bytes, number: int) -> bytes | None:
    for f in parse_fields(data):
        if f.number == number and f.wire == 2:
            return f.value
    return None


def first_string_field(data: bytes, number: int) -> str | None:
    value = first_bytes_field(data, number)
    if value is None:
        return None
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def inner_title(inner: bytes) -> str:
    return first_string_field(inner, 1) or ""


def is_placeholder_title(title: Any) -> bool:
    if not isinstance(title, str) or not title.strip():
        return True
    title = title.strip()
    return bool(_PLACEHOLDER_RE.match(title)) or title in {"Recovered", "(Untitled)", "Conversation"}


def canonicalize_uri(uri: str) -> str:
    """Fix the Linux file:////path mistake; leave ordinary URIs unchanged."""
    if uri.startswith("file:////"):
        rest = uri[5:]
        # Local absolute path URI (empty authority) with too many slashes.
        stripped = rest.lstrip("/")
        if stripped and not stripped.startswith("/"):
            # file: + three slashes + path. For '/media/x', lstrip -> media/x.
            return "file:///" + stripped
    return uri


def uri_to_path(uri: str) -> str:
    uri = canonicalize_uri(uri)
    if not uri.startswith("file:///"):
        return ""
    raw = urllib.parse.unquote(uri[len("file:///"):])
    if re.match(r"^[A-Za-z]:/", raw):
        return raw.replace("/", os.sep) if os.name == "nt" else raw
    return "/" + raw.lstrip("/")


def workspace_uri_fields(inner: bytes) -> tuple[str, str]:
    """Return (WorkspaceInfo field 9.1 URI, SessionWorkspace field 17.7 URI)."""
    uri9 = ""
    uri17 = ""
    try:
        for outer in parse_fields(inner):
            if outer.wire != 2 or outer.number not in (9, 17):
                continue
            for nested in parse_fields(outer.value):
                if nested.wire != 2:
                    continue
                if outer.number == 17 and nested.number == 7:
                    uri17 = nested.value.decode("utf-8", "strict")
                elif outer.number == 9 and nested.number == 1:
                    uri9 = nested.value.decode("utf-8", "strict")
    except (RepairError, UnicodeDecodeError):
        return uri9, uri17
    return uri9, uri17


def extract_workspace_uri(inner: bytes) -> str:
    """Read workspace only from known Protobuf fields 17.7 or 9.1."""
    uri9, uri17 = workspace_uri_fields(inner)
    # Field 17 is the IDE's session-context URI and takes precedence when both
    # are present. Field 9 is retained as a compatibility fallback.
    return uri17 or uri9


def _git_dir_for_workspace(path: str) -> Path | None:
    git_entry = Path(path) / ".git"
    if git_entry.is_dir():
        return git_entry
    if git_entry.is_file():
        try:
            first_line = git_entry.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if not first_line.lower().startswith("gitdir:"):
                return None
            git_dir = Path(first_line.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (git_entry.parent / git_dir).resolve()
            return git_dir
        except (OSError, IndexError):
            return None
    return None


def _read_git_origin(path: str) -> str:
    """Read an origin URL from a local .git/config without invoking Git."""
    git_dir = _git_dir_for_workspace(path)
    if git_dir is None:
        return ""
    try:
        lines = (git_dir / "config").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_origin = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            in_origin = section in ('remote "origin"', "remote.origin")
        elif in_origin and "=" in stripped and not stripped.startswith(("#", ";")):
            key, value = stripped.split("=", 1)
            if key.strip().lower() == "url":
                return value.strip().strip('"')
    return ""


def _read_git_branch(path: str) -> str:
    """Read the checked-out branch name, or blank when the folder isn't on a branch."""
    git_dir = _git_dir_for_workspace(path)
    if git_dir is None:
        return ""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    prefix = "ref: refs/heads/"
    return head[len(prefix):].strip() if head.startswith(prefix) else ""


def build_workspace_dict(path_or_uri: str) -> dict[str, str]:
    if path_or_uri.startswith("file:///"):
        path = uri_to_path(path_or_uri)
    else:
        path = path_or_uri
    path = os.path.abspath(os.path.expanduser(path)).replace("\\", "/").rstrip("/")
    if not path:
        raise RepairError("Cannot build a workspace URI from an empty path")
    folder_name = os.path.basename(path) or "RecoveredProject"
    encoded_path = urllib.parse.quote(path, safe="/:" if os.name == "nt" else "/")
    if path.startswith("/"):
        uri_encoded = "file://" + encoded_path  # /x -> file:///x (exactly three slashes)
    else:
        # Windows drive paths look like c:/folder.
        uri_encoded = "file:///" + encoded_path
    return {
        "uri_encoded": uri_encoded,
        # Keep this a valid URI as well; spaces must be percent-encoded.
        "uri_plain": uri_encoded,
        "corpus": f"local/{folder_name}",
        # Don't invent a fake GitHub remote. Leave it blank for non-Git folders.
        "git_remote": _read_git_origin(path),
        "branch": _read_git_branch(path),
    }


def workspace_field9(ws: dict[str, str]) -> bytes:
    git = field_string(1, ws["corpus"]) + field_string(2, ws["git_remote"])
    content = (field_string(1, ws["uri_encoded"]) + field_string(2, ws["uri_encoded"])
               + field_bytes(3, git) + field_string(4, ws["branch"]))
    return field_bytes(9, content)


def workspace_field17(ws: dict[str, str], session_id: str, epoch: int) -> bytes:
    workspace_pair = field_string(1, ws["uri_plain"]) + field_string(2, ws["uri_plain"])
    stamp = field_varint(1, max(0, int(epoch))) + field_varint(2, 0)
    content = (field_bytes(1, workspace_pair) + field_bytes(2, stamp)
               + field_string(3, session_id) + field_string(7, ws["uri_encoded"]))
    return field_bytes(17, content)


def make_inner(title: str, session_id: str, created: int, modified: int,
               existing: bytes | None, workspace: dict[str, str] | None) -> bytes:
    if existing:
        parts = [field_string(1, title)]
        parts.extend(f.raw for f in parse_fields(existing) if f.number != 1)
        inner = b"".join(parts)
        if workspace is not None:
            inner = strip_fields(inner, {9, 17})
            inner += workspace_field9(workspace) + workspace_field17(workspace, session_id, modified)
        # Correctly fill timestamps individually, without replacing existing ones.
        existing_numbers = field_set(inner)
        add = b""
        if 3 not in existing_numbers:
            add += timestamp_field(3, created)
        if 7 not in existing_numbers:
            add += timestamp_field(7, modified)
        if 10 not in existing_numbers:
            add += timestamp_field(10, modified)
        return inner + add

    inner = (field_string(1, title) + field_varint(2, 1) + timestamp_field(3, created)
             + field_string(4, session_id) + field_varint(5, 1) + timestamp_field(7, modified))
    if workspace is not None:
        inner += workspace_field9(workspace)
    inner += timestamp_field(10, modified)
    if workspace is not None:
        inner += workspace_field17(workspace, session_id, modified)
    return inner


def wrap_trajectory_entry(session_id: str, inner: bytes) -> bytes:
    inner_b64 = base64.b64encode(inner).decode("ascii")
    wrapper = field_string(1, inner_b64)
    entry = field_string(1, session_id) + field_bytes(2, wrapper)
    return field_bytes(1, entry)


@dataclass
class StoredEntry:
    session_id: str | None
    raw_outer: bytes
    inner: bytes | None


def parse_outer_entry(raw_outer: bytes) -> StoredEntry:
    try:
        outer_fields = parse_fields(raw_outer)
        entry_data = raw_outer
        # Standard format is one outer field-1 wrapper around the trajectory entry.
        if len(outer_fields) == 1 and outer_fields[0].number == 1 and outer_fields[0].wire == 2:
            candidate = outer_fields[0].value
            candidate_fields = parse_fields(candidate)
            if candidate_fields and candidate_fields[0].number == 1 and candidate_fields[0].wire == 2:
                entry_data = candidate
        entry_fields = parse_fields(entry_data)
        session_id = None
        wrapper = None
        for f in entry_fields:
            if f.number == 1 and f.wire == 2 and session_id is None:
                try:
                    value = f.value.decode("utf-8", "strict")
                    if _UUID_RE.match(value):
                        session_id = value.lower()
                except UnicodeDecodeError:
                    pass
            elif f.number == 2 and f.wire == 2 and wrapper is None:
                wrapper = f.value
        inner = None
        if wrapper is not None:
            b64_text = first_string_field(wrapper, 1)
            if b64_text:
                inner = base64.b64decode(b64_text, validate=True)
                # Full parse is an important guard before any edit.
                parse_fields(inner)
        return StoredEntry(session_id, raw_outer, inner)
    except Exception as exc:
        raise RepairError(f"Cannot safely parse a trajectory entry: {exc}") from exc


def parse_trajectory_payload(decoded: bytes) -> list[StoredEntry]:
    entries: list[StoredEntry] = []
    for f in parse_fields(decoded):
        if f.number != 1 or f.wire != 2:
            raise RepairError("Unexpected top-level trajectorySummaries format; refusing to rewrite it")
        entries.append(parse_outer_entry(f.raw))
    return entries


def replace_inner_payload(raw_outer: bytes, session_id: str, new_inner: bytes) -> bytes:
    """Replace only a session's Base64 inner payload, preserving unknown wrapper fields.

    The current format is an outer field 1 containing an entry with UUID field 1
    and wrapper field 2; wrapper field 1 holds the Base64-encoded inner message.
    All sibling fields are kept byte-for-byte unchanged.
    """
    outer_fields = parse_fields(raw_outer)
    rebuilt_outer: list[bytes] = []
    matches = 0

    for outer_field in outer_fields:
        if outer_field.wire != 2:
            rebuilt_outer.append(outer_field.raw)
            continue

        try:
            entry_fields = parse_fields(outer_field.value)
        except RepairError:
            rebuilt_outer.append(outer_field.raw)
            continue

        entry_id = None
        for field in entry_fields:
            if field.number == 1 and field.wire == 2:
                try:
                    candidate = field.value.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    continue
                if _UUID_RE.match(candidate):
                    entry_id = candidate.lower()
                    break

        if entry_id != session_id.lower():
            rebuilt_outer.append(outer_field.raw)
            continue

        rebuilt_entry: list[bytes] = []
        wrapper_replaced = False
        for entry_field in entry_fields:
            if entry_field.number != 2 or entry_field.wire != 2 or wrapper_replaced:
                rebuilt_entry.append(entry_field.raw)
                continue

            wrapper_fields = parse_fields(entry_field.value)
            rebuilt_wrapper: list[bytes] = []
            inner_replaced = False
            inner_b64 = base64.b64encode(new_inner).decode("ascii")
            for wrapper_field in wrapper_fields:
                if wrapper_field.number == 1 and wrapper_field.wire == 2 and not inner_replaced:
                    rebuilt_wrapper.append(field_string(1, inner_b64))
                    inner_replaced = True
                else:
                    rebuilt_wrapper.append(wrapper_field.raw)
            if not inner_replaced:
                raise RepairError(f"Session {session_id} has no inner payload field to replace")
            rebuilt_entry.append(field_bytes(entry_field.number, b"".join(rebuilt_wrapper)))
            wrapper_replaced = True

        if not wrapper_replaced:
            raise RepairError(f"Session {session_id} has no wrapper field to replace")
        rebuilt_outer.append(field_bytes(outer_field.number, b"".join(rebuilt_entry)))
        matches += 1

    if matches != 1:
        raise RepairError(f"Expected exactly one outer entry for {session_id}; found {matches}")

    result = b"".join(rebuilt_outer)
    verified = parse_outer_entry(result)
    if verified.session_id != session_id.lower() or verified.inner != new_inner:
        raise RepairError(f"Surgical payload replacement failed validation for {session_id}")
    return result


# ---------------------------------------------------------------------------
# disk discovery and conservative workspace/title inference.
# ---------------------------------------------------------------------------

def get_app_roots() -> list[Path]:
    """Return common Antigravity storage roots on Windows, macOS and Linux."""
    home = Path.home()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = home / ".config"

    # Different builds/versions have used both names. Keep the names distinct
    # on case-sensitive filesystems, and prefer the newer IDE-named folder.
    names = ("Antigravity IDE", "Antigravity", "antigravity")
    return list(dict.fromkeys(base / name for name in names))


def candidate_db_paths() -> list[Path]:
    candidates: list[Path] = []
    for root in get_app_roots():
        candidates.append(root / "User" / "globalStorage" / DB_FILENAME)
    return candidates


def choose_db(custom: str | None) -> Path:
    if custom:
        path = Path(custom).expanduser().resolve()
        if not path.is_file():
            raise RepairError(f"Database not found: {path}")
        return path
    paths = candidate_db_paths()
    existing = [p for p in paths if p.is_file()]
    if not existing:
        raise RepairError("Could not find state.vscdb. Use --db /full/path/to/state.vscdb")
    # Prefer the current 'Antigravity IDE' installation over the legacy one.
    return existing[0]


def source_roots() -> tuple[list[Path], list[Path]]:
    home = Path.home()
    appdata_roots = [home / ".gemini" / "antigravity-ide", home / ".gemini" / "antigravity"]
    conversations: list[Path] = []
    brains: list[Path] = []
    for root in appdata_roots:
        conv = root / "conversations"
        brain = root / "brain"
        if conv.is_dir():
            conversations.append(conv)
        if brain.is_dir():
            brains.append(brain)
    # Preserve priority order and remove duplicate paths.
    return list(dict.fromkeys(conversations)), list(dict.fromkeys(brains))


def find_conversation_files(conversation_dirs: list[Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in conversation_dirs:
        try:
            items = list(directory.iterdir())
        except OSError:
            continue
        for path in items:
            if not path.is_file() or path.suffix.lower() not in (".db", ".pb"):
                continue
            session_id = path.stem.lower()
            if not _UUID_RE.match(session_id):
                continue
            old = found.get(session_id)
            try:
                if old is None or path.stat().st_mtime > old.stat().st_mtime:
                    found[session_id] = path
            except OSError:
                if old is None:
                    found[session_id] = path
    return found


def valid_workspace_path(value: str) -> str | None:
    if not value:
        return None
    value = value.strip().strip('"\'')
    if value.startswith("file:///"):
        value = uri_to_path(value)
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        return None
    value = os.path.realpath(value)
    if os.path.isdir(value) and os.access(value, os.R_OK):
        return value
    return None


def _collect_workspace_values(node: Any, parent_key: str = "") -> Iterable[str]:
    interesting = {
        "workspacepaths", "workspacepath", "workspaceuri", "workspacefolderabsoluteuri",
        "workspacefolder", "projectpath", "projectroot", "rootpath",
    }
    if isinstance(node, dict):
        for key, value in node.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if normalized in interesting:
                if isinstance(value, str):
                    yield value
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            yield item
                elif isinstance(value, dict):
                    yield from _collect_workspace_values(value, normalized)
            elif isinstance(value, (dict, list)):
                yield from _collect_workspace_values(value, normalized)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                yield from _collect_workspace_values(item, parent_key)


def infer_workspace_from_transcript(session_id: str, brain_dirs: list[Path]) -> str | None:
    for brain_root in brain_dirs:
        transcript = brain_root / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript.is_file():
            continue
        try:
            with transcript.open("r", encoding="utf-8", errors="replace") as stream:
                # Workspace metadata, when present, is normally near the start.
                for index, line in enumerate(stream):
                    if index >= 3000:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for candidate in _collect_workspace_values(record):
                        path = valid_workspace_path(candidate)
                        if path:
                            return path
        except OSError:
            pass
    return None


def artifact_title(session_id: str, brain_dirs: list[Path]) -> str | None:
    names = ("task.md", "walkthrough.md", "implementation_plan.md", "conversation.md", "title.md")
    for brain_root in brain_dirs:
        directory = brain_root / session_id
        for name in names:
            path = directory / name
            if not path.is_file():
                # Some artifacts are stored one level beneath artifacts/.
                path = directory / "artifacts" / name
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    for _ in range(80):
                        line = stream.readline()
                        if not line:
                            break
                        stripped = line.strip()
                        if stripped.startswith("#"):
                            title = stripped.lstrip("#").strip().strip("`*_ ")
                            if title and not is_placeholder_title(title):
                                return title[:300]
            except OSError:
                continue
    return None


def choose_title(session_id: str, file_path: Path, json_entry: dict[str, Any] | None,
                 existing_inner: bytes | None, brain_dirs: list[Path]) -> str:
    json_title = json_entry.get("title") if isinstance(json_entry, dict) else None
    pb_title = inner_title(existing_inner) if existing_inner else ""
    if isinstance(json_title, str) and not is_placeholder_title(json_title):
        return json_title.strip()
    if not is_placeholder_title(pb_title):
        return pb_title.strip()
    title_from_artifact = artifact_title(session_id, brain_dirs)
    if title_from_artifact:
        return title_from_artifact
    # Keep a real old placeholder if it's already there; otherwise create one.
    for old in (json_title, pb_title):
        if isinstance(old, str) and old.strip():
            return old.strip()
    try:
        modified = file_path.stat().st_mtime
    except OSError:
        modified = time.time()
    stamp = time.strftime("%b %d", time.localtime(modified))
    return f"Conversation ({stamp}) {session_id[:8]}"


# ---------------------------------------------------------------------------
# Database detection, analysis, backup and transaction.
# ---------------------------------------------------------------------------

def process_looks_running() -> list[str]:
    """Best-effort Antigravity process check on Linux, macOS and Windows.

    This is only a safety hint: process names and packaging can vary, so users
    should still quit Antigravity before applying any database changes.
    """
    my_pid = os.getpid()
    hits: list[str] = []

    if sys.platform.startswith("win"):
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                check=False, capture_output=True, text=True, timeout=8,
                errors="replace",
            )
            if result.returncode != 0:
                return []
            for row in csv.reader(result.stdout.splitlines()):
                if not row:
                    continue
                image_name = row[0].strip()
                if "antigravity" in image_name.lower():
                    pid = row[1].strip() if len(row) > 1 else "unknown PID"
                    hits.append(f"{image_name} (PID {pid})")
        except (OSError, subprocess.SubprocessError, csv.Error):
            return []
        return hits

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,comm="],
                check=False, capture_output=True, text=True, timeout=8,
                errors="replace",
            )
            if result.returncode != 0:
                return []
            for line in result.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) == my_pid:
                    continue
                command = parts[1].strip()
                if "antigravity" in command.lower():
                    hits.append(f"PID {parts[0]}: {command}")
        except (OSError, subprocess.SubprocessError):
            return []
        return hits

    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == my_pid:
            continue
        try:
            comm = (proc / "comm").read_text(errors="ignore").strip().lower()
            raw = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
            args = raw.lower()
        except (OSError, PermissionError):
            continue
        if ("antigravity_conversation_repair.py" in args
                or "antigravity_sidebar_repair.py" in args
                or "antigravity-database-manager" in args):
            continue
        executable = ""
        try:
            executable = os.path.basename(os.readlink(str(proc / "exe"))).lower()
        except OSError:
            pass
        if ("antigravity" in comm or "antigravity" in executable
                or (args.strip().startswith("/") and "/antigravity" in args.split()[0])):
            hits.append(f"PID {proc.name}: {comm or executable or args[:120]}")
    return hits




def sqlite_uri(path: Path | str, mode: str | None = None) -> str:
    """Build a properly escaped SQLite file URI on Windows, macOS and Linux."""
    uri = Path(path).expanduser().resolve().as_uri()
    if mode:
        uri += "?" + urllib.parse.urlencode({"mode": mode})
    return uri


def read_item_values(db_path: Path) -> tuple[str | None, str | None]:
    uri = sqlite_uri(db_path, mode="ro")
    con = sqlite3.connect(uri, uri=True, timeout=8)
    try:
        con.execute("PRAGMA query_only=ON")
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ItemTable'").fetchone()
        if not table:
            raise RepairError("SQLite database has no ItemTable")
        rows = dict(con.execute("SELECT key, value FROM ItemTable WHERE key IN (?, ?)", (PB_KEY, JSON_KEY)).fetchall())
        pb = _value_as_text(rows.get(PB_KEY))
        js = _value_as_text(rows.get(JSON_KEY))
        return pb, js
    except sqlite3.Error as exc:
        raise RepairError(f"Unable to read database: {exc}") from exc
    finally:
        con.close()


def _value_as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepairError("Database index value is not UTF-8 text") from exc
    return str(value)


def parse_json_index(text: str | None) -> dict[str, Any]:
    if not text:
        return {"version": 1, "entries": {}}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RepairError(f"Session JSON index is invalid JSON ({exc}); refusing to overwrite it") from exc
    if not isinstance(obj, dict):
        raise RepairError("Session JSON index is not an object; refusing to overwrite it")
    entries = obj.get("entries")
    if entries is None:
        obj["entries"] = {}
    elif not isinstance(entries, dict):
        raise RepairError("Session JSON index has a non-object 'entries' value")
    if "version" not in obj:
        obj["version"] = 1
    return obj


def make_backup(db_path: Path) -> Path:
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = Path(str(db_path) + f".before_conversation_repair_{timestamp}")
    backup = base
    suffix = 1
    while backup.exists():
        backup = Path(str(base) + f"_{suffix}")
        suffix += 1
    src = sqlite3.connect(sqlite_uri(db_path, mode="ro"), uri=True, timeout=10)
    dst = sqlite3.connect(str(backup), timeout=10)
    try:
        src.backup(dst)
        dst.commit()
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RepairError(f"Backup failed integrity_check: {result[0] if result else 'no result'}")
    except Exception:
        try:
            dst.close()
        finally:
            src.close()
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    else:
        dst.close()
        src.close()
    return backup


def restore_database_from_backup(backup_path: Path, db_path: Path) -> None:
    """Restore a logical SQLite backup without copying a live DB file over sidecars."""
    src_uri = sqlite_uri(backup_path, mode="ro")
    src = sqlite3.connect(src_uri, uri=True, timeout=15)
    dst = sqlite3.connect(str(db_path), timeout=15)
    try:
        src.backup(dst)
        dst.commit()
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RepairError(f"Restored database failed integrity_check: {result[0] if result else 'no result'}")
    finally:
        dst.close()
        src.close()


def infer_dominant_workspace(entries: list[StoredEntry], source_ids: set[str]) -> tuple[str | None, int, int]:
    counts: collections.Counter[str] = collections.Counter()
    for entry in entries:
        if not entry.session_id or entry.session_id not in source_ids or not entry.inner:
            continue
        uri = extract_workspace_uri(entry.inner)
        if not uri:
            continue
        uri = canonicalize_uri(uri)
        path = uri_to_path(uri)
        if path and os.path.isdir(path) and os.access(path, os.R_OK):
            counts[os.path.realpath(path)] += 1
    if not counts:
        return None, 0, 0
    path, count = counts.most_common(1)[0]
    total = sum(counts.values())
    # Only auto-assign when the data has a clear dominant project. Otherwise
    # preserve workspace-less sessions and explain how to provide --workspace.
    if count >= 3 and count / total >= 0.60:
        return path, count, total
    return None, count, total


def build_plan(db_path: Path, pb_text: str | None, json_text: str | None,
               explicit_workspace: str | None) -> dict[str, Any]:
    json_index = parse_json_index(json_text)
    json_entries: dict[str, Any] = json_index["entries"]
    source_dirs, brain_dirs = source_roots()
    source_files = find_conversation_files(source_dirs)
    if not source_dirs:
        raise RepairError("No conversation directory found under ~/.gemini/antigravity[-ide]/conversations")
    if not source_files:
        raise RepairError("No conversation .db or .pb files were found; nothing safe to recover")

    decoded = b""
    if pb_text:
        try:
            decoded = base64.b64decode(pb_text, validate=True)
        except Exception as exc:
            raise RepairError(f"trajectorySummaries is not valid Base64: {exc}") from exc
    stored_entries = parse_trajectory_payload(decoded) if decoded else []
    by_id: dict[str, StoredEntry] = {}
    duplicate_ids: set[str] = set()
    for entry in stored_entries:
        if not entry.session_id:
            continue
        if entry.session_id in by_id:
            duplicate_ids.add(entry.session_id)
        else:
            by_id[entry.session_id] = entry
    if duplicate_ids:
        raise RepairError("Duplicate Protobuf entries for UUID(s): " + ", ".join(sorted(duplicate_ids)))

    explicit_path = None
    if explicit_workspace:
        explicit_path = valid_workspace_path(explicit_workspace)
        if explicit_path is None:
            raise RepairError(f"--workspace must point to an existing readable directory: {explicit_workspace}")

    dominant_path, dominant_count, dominant_total = infer_dominant_workspace(stored_entries, set(source_files))
    if explicit_path:
        fallback_path = explicit_path
        fallback_reason = "explicit --workspace"
    elif dominant_path:
        fallback_path = dominant_path
        fallback_reason = f"dominant existing workspace ({dominant_count}/{dominant_total} mapped sessions)"
    else:
        fallback_path = None
        fallback_reason = "no unambiguous dominant workspace; missing workspace fields will be left untouched"

    pb_replacements: dict[str, bytes] = {}
    pb_new: list[tuple[float, str, bytes]] = []
    json_changed = False
    json_new = 0
    json_fixed = 0
    workspace_fixed = 0
    timestamp_fixed = 0
    new_pb_count = 0
    unresolved_workspace: list[str] = []
    planned_titles: dict[str, str] = {}
    planned_workspaces: dict[str, str] = {}

    # Process sessions backed by local .db/.pb files. Existing unrelated outer
    # Protobuf entries remain byte-for-byte identical.
    for session_id, file_path in sorted(source_files.items(), key=lambda kv: _safe_mtime(kv[1])):
        current = by_id.get(session_id)
        old_inner = current.inner if current else None
        json_entry = json_entries.get(session_id)
        if json_entry is not None and not isinstance(json_entry, dict):
            # Don't replace an unusual/unknown entry structure automatically.
            raise RepairError(f"JSON index entry for {session_id} is not an object")
        title = choose_title(session_id, file_path, json_entry, old_inner, brain_dirs)
        planned_titles[session_id] = title
        file_mtime = int(_safe_mtime(file_path))
        created = file_mtime or int(time.time())
        modified = file_mtime or created

        # Resolve desired workspace for this specific conversation first.
        inferred_session_path = infer_workspace_from_transcript(session_id, brain_dirs)
        desired_path = explicit_path or inferred_session_path or fallback_path
        current_uri = extract_workspace_uri(old_inner) if old_inner else ""
        normalized_uri = canonicalize_uri(current_uri) if current_uri else ""
        existing_fields = field_set(old_inner) if old_inner else set()
        uri9, uri17 = workspace_uri_fields(old_inner) if old_inner else ("", "")
        normalized9 = canonicalize_uri(uri9) if uri9 else ""
        normalized17 = canonicalize_uri(uri17) if uri17 else ""
        workspace_mismatch = bool(normalized9 and normalized17 and normalized9 != normalized17)
        workspace_uri_needs_canonicalization = bool(
            (uri9 and normalized9 != uri9) or (uri17 and normalized17 != uri17)
        )
        ws_dict: dict[str, str] | None = None
        needs_workspace_patch = False

        if old_inner and current_uri:
            # Canonicalize both nested URI fields and ensure both contain a URI.
            # A disagreement is resolved in favor of field 17, which is the
            # session-context URI read by the IDE. Other inner fields are kept.
            if (workspace_uri_needs_canonicalization or workspace_mismatch
                    or not uri9 or not uri17):
                ws_dict = build_workspace_dict(normalized_uri)
                needs_workspace_patch = True
                planned_workspaces[session_id] = ws_dict["uri_encoded"]
        elif desired_path:
            # Missing/unreadable workspace metadata: infer from this session or
            # the dominant project, but never overwrite a different valid URI.
            ws_dict = build_workspace_dict(desired_path)
            needs_workspace_patch = True
            planned_workspaces[session_id] = ws_dict["uri_encoded"]
        else:
            unresolved_workspace.append(session_id)

        existing_title = inner_title(old_inner) if old_inner else ""
        title_needs_patch = bool(old_inner and is_placeholder_title(existing_title) and not is_placeholder_title(title))
        fields = existing_fields
        timestamps_missing = bool(old_inner and not {3, 7, 10}.issubset(fields))
        should_rebuild_existing = bool(old_inner and (needs_workspace_patch or title_needs_patch or timestamps_missing))

        if current is None:
            inner = make_inner(title, session_id, created, modified, None, ws_dict)
            raw_outer = wrap_trajectory_entry(session_id, inner)
            pb_new.append((_safe_mtime(file_path), session_id, raw_outer))
            new_pb_count += 1
            if ws_dict:
                workspace_fixed += 1
            planned_workspaces.setdefault(session_id, ws_dict["uri_encoded"] if ws_dict else "")
        elif should_rebuild_existing:
            inner = make_inner(title if title_needs_patch else (existing_title or title), session_id,
                               created, modified, old_inner, ws_dict)
            pb_replacements[session_id] = replace_inner_payload(current.raw_outer, session_id, inner)
            if needs_workspace_patch:
                workspace_fixed += 1
            if timestamps_missing:
                timestamp_fixed += 1

        # JSON index is additive: never delete stale/orphan entries.
        if json_entry is None:
            json_entries[session_id] = {
                "sessionId": session_id,
                "title": title,
                "lastModified": modified * 1000,
                "isStale": False,
            }
            json_new += 1
            json_changed = True
        else:
            new_entry = dict(json_entry)
            old_title = new_entry.get("title")
            if is_placeholder_title(old_title) and not is_placeholder_title(title):
                new_entry["title"] = title
            elif not isinstance(old_title, str) or not old_title.strip():
                new_entry["title"] = title
            if not new_entry.get("sessionId"):
                new_entry["sessionId"] = session_id
            try:
                old_mtime = int(new_entry.get("lastModified", 0) or 0)
            except (ValueError, TypeError):
                old_mtime = 0
            new_entry["lastModified"] = max(old_mtime, modified * 1000)
            # A local conversation file means it is not stale on disk.
            if new_entry.get("isStale") is True:
                new_entry["isStale"] = False
            if new_entry != json_entry:
                json_entries[session_id] = new_entry
                json_fixed += 1
                json_changed = True

    # Rebuild the whole top-level byte stream from preserved raw entries,
    # replacing only sessions that really need changes; append missing entries.
    final_records: list[bytes] = []
    for entry in stored_entries:
        if entry.session_id and entry.session_id in pb_replacements:
            final_records.append(pb_replacements[entry.session_id])
        else:
            final_records.append(entry.raw_outer)
    # Append chronologically so latest additions are near the end of the stream.
    final_records.extend(raw for _mtime, _cid, raw in sorted(pb_new, key=lambda item: (item[0], item[1])))
    final_pb = b"".join(final_records)
    if not pb_replacements and not pb_new:
        # Keep the exact original textual representation if the payload did not
        # need repair (e.g. even if its Base64 formatting were unusual).
        final_pb_text = pb_text
    else:
        final_pb_text = base64.b64encode(final_pb).decode("ascii") if final_pb else None
    final_json_text = json.dumps(json_index, ensure_ascii=False, separators=(",", ":"))
    json_changed = json_changed or (json_text is None and bool(json_entries))
    pb_changed = final_pb_text != pb_text

    return {
        "db_path": db_path,
        "original_pb_text": pb_text,
        "original_json_text": json_text,
        "final_pb_text": final_pb_text,
        "final_json_text": final_json_text,
        "stored_entries": stored_entries,
        "source_files": source_files,
        "source_dirs": source_dirs,
        "brain_dirs": brain_dirs,
        "planned_titles": planned_titles,
        "planned_workspaces": planned_workspaces,
        "unresolved_workspace": unresolved_workspace,
        "fallback_path": fallback_path,
        "fallback_reason": fallback_reason,
        "pb_new_count": new_pb_count,
        "pb_replaced_count": len(pb_replacements),
        "json_new_count": json_new,
        "json_fixed_count": json_fixed,
        "workspace_fixed_count": workspace_fixed,
        "timestamp_fixed_count": timestamp_fixed,
        "pb_changed": pb_changed,
        "json_changed": json_changed,
        "changed": pb_changed or json_changed,
    }


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def print_report(plan: dict[str, Any], dry_run: bool) -> None:
    note(f"Antigravity Conversation Repair {VERSION}")
    note(f"Database: {plan['db_path']}")
    note(f"Conversation directories: {len(plan['source_dirs'])}")
    note(f"Conversation files discovered: {len(plan['source_files'])}")
    note(f"Existing Protobuf summaries: {len(plan['stored_entries'])}")
    note(f"Workspace fallback: {plan['fallback_path'] or 'none'}")
    note(f"Fallback basis: {plan['fallback_reason']}")
    note("")
    note("Planned changes:")
    note(f"  New Protobuf entries:          {plan['pb_new_count']}")
    note(f"  Existing summaries to patch:   {plan['pb_replaced_count']}")
    note(f"  JSON entries to add:            {plan['json_new_count']}")
    note(f"  JSON entries to update:         {plan['json_fixed_count']}")
    note(f"  Workspace metadata repairs:     {plan['workspace_fixed_count']}")
    note(f"  Timestamp repairs:              {plan['timestamp_fixed_count']}")
    note(f"  Database changes needed:        {'yes' if plan['changed'] else 'no'}")
    if plan["planned_workspaces"]:
        note("")
        note("Workspace assignments / URI repairs:")
        for session_id, uri in sorted(plan["planned_workspaces"].items()):
            if uri:
                note(f"  {session_id} -> {uri}")
    if plan["unresolved_workspace"]:
        note("")
        note(f"Sessions with no safe workspace inference: {len(plan['unresolved_workspace'])}")
        for session_id in plan["unresolved_workspace"][:10]:
            note(f"  {session_id}")
        if len(plan["unresolved_workspace"]) > 10:
            note(f"  ... and {len(plan['unresolved_workspace']) - 10} more")
        note('  To assign these to a project, rerun with --workspace "/path/to/project".')
    note("")
    if dry_run:
        note("DRY RUN ONLY — no database changes were written.")
        note("To apply this plan, close Antigravity and rerun with --apply.")
    elif not plan["changed"]:
        note("Everything already looks synchronized. No backup or write was needed.")


def apply_plan(plan: dict[str, Any]) -> Path | None:
    db_path: Path = plan["db_path"]
    if not plan["changed"]:
        return None

    running = process_looks_running()
    if running:
        raise RepairError(
            "Antigravity appears to be running. Close all Antigravity windows/processes before applying.\n"
            + "\n".join(running)
        )

    backup_path = make_backup(db_path)
    committed = False
    try:
        con = sqlite3.connect(str(db_path), timeout=15)
        try:
            con.execute("BEGIN IMMEDIATE")
            check = con.execute("SELECT key, value FROM ItemTable WHERE key IN (?, ?)", (PB_KEY, JSON_KEY)).fetchall()
            current = {key: _value_as_text(value) for key, value in check}
            if current.get(PB_KEY) != plan["original_pb_text"] or current.get(JSON_KEY) != plan["original_json_text"]:
                raise RepairError("Database changed after the plan was created. Nothing was committed; rerun the script.")

            if plan["pb_changed"]:
                if current.get(PB_KEY) is None:
                    con.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (PB_KEY, plan["final_pb_text"] or ""))
                else:
                    con.execute("UPDATE ItemTable SET value=? WHERE key=?", (plan["final_pb_text"] or "", PB_KEY))
            if plan["json_changed"]:
                if current.get(JSON_KEY) is None:
                    con.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (JSON_KEY, plan["final_json_text"]))
                else:
                    con.execute("UPDATE ItemTable SET value=? WHERE key=?", (plan["final_json_text"], JSON_KEY))

            # Validate values as seen from inside the uncommitted transaction.
            if plan["pb_changed"]:
                test_text = _value_as_text(con.execute("SELECT value FROM ItemTable WHERE key=?", (PB_KEY,)).fetchone()[0])
                test_entries = parse_trajectory_payload(base64.b64decode(test_text, validate=True)) if test_text else []
                test_ids = {e.session_id for e in test_entries if e.session_id}
                expected_ids = set(plan["source_files"])
                missing_pb = expected_ids - test_ids
                if missing_pb:
                    raise RepairError("Pre-commit validation failed; missing Protobuf sessions: " + ", ".join(sorted(missing_pb)[:10]))
                for session_id, expected_uri in plan["planned_workspaces"].items():
                    if not expected_uri:
                        continue
                    entry = next((e for e in test_entries if e.session_id == session_id), None)
                    actual = extract_workspace_uri(entry.inner) if entry and entry.inner else ""
                    if canonicalize_uri(actual) != canonicalize_uri(expected_uri):
                        raise RepairError(f"Pre-commit workspace validation failed for {session_id}: {actual!r}")
            if plan["json_changed"]:
                test_json = parse_json_index(_value_as_text(con.execute("SELECT value FROM ItemTable WHERE key=?", (JSON_KEY,)).fetchone()[0]))
                missing_json = set(plan["source_files"]) - set(test_json["entries"])
                if missing_json:
                    raise RepairError("Pre-commit JSON validation failed; missing sessions: " + ", ".join(sorted(missing_json)[:10]))
            con.commit()
            committed = True
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

        # A final filesystem read verifies the committed state. If this fails,
        # restore the known-good database backup rather than leave a partial fix.
        verify_pb, verify_json = read_item_values(db_path)
        verified_entries = parse_trajectory_payload(base64.b64decode(verify_pb, validate=True)) if verify_pb else []
        verified_ids = {e.session_id for e in verified_entries if e.session_id}
        if set(plan["source_files"]) - verified_ids:
            raise RepairError("Post-commit verification failed: not all local sessions appear in the Protobuf index")
        verified_json_obj = parse_json_index(verify_json)
        if set(plan["source_files"]) - set(verified_json_obj["entries"]):
            raise RepairError("Post-commit verification failed: not all local sessions appear in the JSON index")
        for session_id, expected_uri in plan["planned_workspaces"].items():
            if not expected_uri:
                continue
            entry = next((e for e in verified_entries if e.session_id == session_id), None)
            actual = extract_workspace_uri(entry.inner) if entry and entry.inner else ""
            if canonicalize_uri(actual) != canonicalize_uri(expected_uri):
                raise RepairError(f"Post-commit workspace verification failed for {session_id}")
        return backup_path
    except Exception as exc:
        # A pre-commit failure was rolled back by SQLite, so don't touch the DB.
        # Restore only when the write was committed but filesystem verification failed.
        if not committed:
            raise
        try:
            restore_database_from_backup(backup_path, db_path)
        except Exception as restore_exc:
            raise RepairError(
                f"Repair failed after commit: {exc}. Automatic restoration also failed: {restore_exc}. "
                f"Keep the backup at {backup_path} and do not reopen Antigravity until the DB is checked."
            ) from exc
        raise RepairError(
            f"Repair failed after commit and the database was restored from backup: {exc}. Backup: {backup_path}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely repair missing Antigravity IDE sidebar sessions and workspace metadata."
    )
    parser.add_argument("--db", help="Path to state.vscdb (auto-detected by default)")
    parser.add_argument("--workspace", help="Existing project directory to use only for missing/incomplete workspace assignments")
    parser.add_argument("--apply", action="store_true", help="Apply changes (without this, the script is read-only)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    try:
        db_path = choose_db(args.db)
        if args.apply:
            running = process_looks_running()
            if running:
                raise RepairError(
                    "Antigravity appears to be running. Close it completely before --apply.\n"
                    + "\n".join(running)
                )
        elif process_looks_running():
            note("WARNING: Antigravity may be running; this is a read-only dry run. Close it before --apply.\n")

        pb_text, json_text = read_item_values(db_path)
        plan = build_plan(db_path, pb_text, json_text, args.workspace)
        print_report(plan, dry_run=not args.apply)
        if args.apply and plan["changed"]:
            backup = apply_plan(plan)
            note("")
            note("SUCCESS: Repair applied and verified.")
            if backup:
                note(f"Safety backup: {backup}")
            note("Reopen Antigravity and check the conversation sidebar.")
        return 0
    except RepairError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"SQLITE ERROR: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
