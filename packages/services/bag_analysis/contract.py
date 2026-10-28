"""Bag analysis contract 1: validation of a manifest (analyzer.yaml) and of a result envelope (result.json).

Contract: docs/bag_analysis_contract_2026-10-28.md. The two JSON schemas are vendored in `schemas/`
(byte-equal to docs/bag_analysis/, pinned by a unit test; the single source moves to the navstack repo once
the robot team accepts the contract). The schemas cannot express everything, so `check_result` adds the
server-side rules: every metric is declared in the manifest, the envelope names the analyzer / profile / bag it
was asked for, artifact paths stay inside the analyzer's output directory (also after symlink resolution),
size caps, and no NUL character or NaN/Infinity (PostgreSQL jsonb refuses both).

Everything in a result is UNTRUSTED text (notes, table cells, names written by an analyzer reading robot
logs): it is stored as given and never interpreted, rendered as HTML or used to build a path or a command;
artifact paths are the only field that reaches the file system and they are checked here first.

A result that fails any check is not dropped: `error_envelope` turns the failure into a stored `error` result.
Needs the `jsonschema` package (the service's requirements; the other server images do not carry it).
"""
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
DEFAULT_MAX_ENVELOPE_BYTES = 256 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 200 * 1024 ** 2
MAX_CELL_CHARS = 2000
MAX_NAME_CHARS = 200
MAX_ERRORS_REPORTED = 8

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ContractError(Exception):
    """Raised for programming/config errors (missing jsonschema), never for a bad result."""


def _load_schema(name: str) -> Dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text())


_VALIDATORS: Dict[str, Any] = {}


def _validator(name: str) -> Any:
    if name not in _VALIDATORS:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as e:  # pragma: no cover - environment
            raise ContractError("the jsonschema package is required for bag analysis validation") from e
        schema = _load_schema(name)
        Draft202012Validator.check_schema(schema)
        _VALIDATORS[name] = Draft202012Validator(schema)
    return _VALIDATORS[name]


def _schema_errors(name: str, obj: Any) -> List[str]:
    errs = sorted(_validator(name).iter_errors(obj), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message[:200]}" for e in errs]


def validate_manifest(manifest: Any) -> List[str]:
    """Schema errors of a manifest (empty = valid), plus: a `default` profile must exist."""
    errors = _schema_errors("analyzer.schema.json", manifest)
    if not errors and "default" not in manifest["profiles"]:
        errors.append("profiles: a 'default' profile is required")
    return errors


def validate_envelope(envelope: Any) -> List[str]:
    """Schema errors of a result envelope (empty = valid)."""
    return _schema_errors("result.schema.json", envelope)


def _walk(obj: Any, depth: int = 0):
    if depth > 40:
        raise ValueError("nesting deeper than 40")
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)
    else:
        yield obj


def _no_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name} is not allowed")


def parse_envelope_bytes(raw: bytes, max_bytes: int = DEFAULT_MAX_ENVELOPE_BYTES) -> Tuple[Optional[Any], List[str]]:
    """(parsed json, errors). Size cap first; NaN/Infinity and invalid UTF-8 are errors."""
    if len(raw) > max_bytes:
        return None, [f"envelope is {len(raw)} bytes, over the {max_bytes} byte cap"]
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_no_constant), []
    except (ValueError, UnicodeDecodeError) as e:
        return None, [f"result.json is not valid JSON: {str(e)[:200]}"]


def safe_artifact_path(analyzer_dir: Path, rel: str) -> Tuple[Optional[Path], Optional[str]]:
    """(resolved file, None) when `rel` names a regular file inside `analyzer_dir`, else (None, reason).
    Rejects absolute paths, '..', NUL, backslashes and anything that resolves (symlinks included) outside."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        return None, "empty or malformed path"
    if rel.startswith("/") or os.path.isabs(rel):
        return None, "absolute path"
    if ".." in Path(rel).parts:
        return None, "'..' in path"
    base = analyzer_dir.resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None, "resolves outside the analyzer directory"
    if not target.is_file():
        return None, "not a regular file"
    return target, None


def _sha256_of(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_result(raw: bytes, manifest: Dict[str, Any], analyzer_dir: Path, *, bag_id: str, profile: str,
                 extractor_version: Optional[int] = None, max_envelope_bytes: int = DEFAULT_MAX_ENVELOPE_BYTES,
                 max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
                 verify_hashes: bool = True) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validate the bytes of a result.json. Returns (envelope, errors); with errors the envelope must NOT be
    stored as it is (use `error_envelope`). `analyzer_dir` is OUT/<analyzer>/<profile>/ on disk."""
    envelope, errors = parse_envelope_bytes(raw, max_envelope_bytes)
    if errors:
        return None, errors
    errors = validate_envelope(envelope)
    if errors:
        return envelope, errors
    errors = []
    try:
        for leaf in _walk(envelope):
            if isinstance(leaf, str) and "\x00" in leaf:
                errors.append("a string contains a NUL character")
                break
    except ValueError as e:
        errors.append(str(e))
    if envelope["analyzer"] != manifest["id"]:
        errors.append(f"analyzer is {envelope['analyzer']!r}, expected {manifest['id']!r}")
    if envelope["analyzer_version"] != manifest["version"]:
        errors.append(f"analyzer_version is {envelope['analyzer_version']}, manifest says {manifest['version']}")
    if envelope["profile"] != profile:
        errors.append(f"profile is {envelope['profile']!r}, expected {profile!r}")
    if envelope["bag_id"] != bag_id:
        errors.append(f"bag_id is {envelope['bag_id']!r}, expected {bag_id!r}")
    if extractor_version is not None and envelope["extractor_version"] != extractor_version:
        errors.append(f"extractor_version is {envelope['extractor_version']}, the runner reports {extractor_version}")
    declared = manifest.get("metrics", {})
    undeclared = sorted(set(envelope["metrics"]) - set(declared))
    if undeclared:
        errors.append("metrics not declared in the manifest: " + ", ".join(undeclared[:10]))
    for tname, table in (envelope.get("tables") or {}).items():
        ncol = len(table["columns"])
        for i, row in enumerate(table["rows"]):
            if len(row) != ncol:
                errors.append(f"tables/{tname}/rows/{i}: {len(row)} cells for {ncol} columns")
                break
            if any(isinstance(c, str) and len(c) > MAX_CELL_CHARS for c in row):
                errors.append(f"tables/{tname}/rows/{i}: a cell is longer than {MAX_CELL_CHARS} characters")
                break
    seen = set()
    for i, art in enumerate(envelope.get("artifacts") or []):
        path, why = safe_artifact_path(analyzer_dir, art["path"])
        if why:
            errors.append(f"artifacts/{i}/path {art['path']!r}: {why}")
            continue
        if art["path"] in seen:
            errors.append(f"artifacts/{i}/path {art['path']!r}: listed twice")
        seen.add(art["path"])
        if len(art["name"]) > MAX_NAME_CHARS:
            errors.append(f"artifacts/{i}/name: longer than {MAX_NAME_CHARS} characters")
        size = path.stat().st_size
        if size > max_artifact_bytes:
            errors.append(f"artifacts/{i}: {size} bytes, over the {max_artifact_bytes} byte cap")
        elif "size" in art and art["size"] != size:
            errors.append(f"artifacts/{i}: declared size {art['size']} but the file has {size} bytes")
        elif verify_hashes and "sha256" in art and _SHA256.match(art["sha256"]) and _sha256_of(path) != art["sha256"]:
            errors.append(f"artifacts/{i}: sha256 does not match the file")
    return envelope, errors


def error_envelope(manifest: Dict[str, Any], profile: str, extractor_version: int, bag_id: str, errors: List[str],
                   *, code: str = "validation.failed", producer: Optional[Dict[str, Any]] = None,
                   present: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """A schema-valid `error` result carrying the failure text (what is stored instead of an invalid result)."""
    notes = [{"code": code, "level": "error", "text": str(e)[:500]} for e in errors[:MAX_ERRORS_REPORTED]] or \
            [{"code": code, "level": "error", "text": "unspecified failure"}]
    prod = {"tool_git_sha": "unknown"}
    if isinstance(producer, dict) and isinstance(producer.get("tool_git_sha"), str):
        prod = {k: v for k, v in producer.items() if k in ("tool_git_sha", "analysis_image", "stack_build",
                                                           "started_at", "duration_s")
                and isinstance(v, (str, int, float, type(None)))}
    return {"schema": 1, "analyzer": manifest["id"], "analyzer_version": manifest["version"], "profile": profile,
            "extractor_version": extractor_version, "bag_id": bag_id, "status": "error", "notes": notes,
            "producer": prod, "inputs": {"bag_dir": "", "present": dict(present or {})}, "metrics": {}}
