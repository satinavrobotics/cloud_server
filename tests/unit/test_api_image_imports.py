"""packages/api/Dockerfile copies selected directories only: every `packages.*` module the API
imports (at module level or lazily) must be inside one of them, else the import fails in the
container but not in a full source tree (the 503 on POST /maps with robot_name was exactly that)."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "packages" / "api"


def _copied():
    """Destination paths (relative to /app) of the Dockerfile's COPY lines, as dotted prefixes."""
    out = set()
    for line in (API / "Dockerfile").read_text().splitlines():
        m = re.match(r"\s*COPY\s+(?:--\S+\s+)*(.+?)\s+(\S+)\s*$", line)
        if not m:
            continue
        sources, dest = m.group(1).split(), m.group(2)
        dest = dest.removeprefix("/app/").strip("/")
        for src in sources:
            if dest.endswith(".py") or not dest or dest == ".":
                target = dest
            elif src.endswith("/") or "." not in Path(src).name:
                target = dest
            else:
                target = f"{dest}/{Path(src).name}"
            out.add(target.removesuffix(".py").replace("/", "."))
    return out


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            for a in node.names:
                yield f"{node.module}.{a.name}"


def test_every_packages_import_of_the_api_is_copied_into_the_image():
    copied = _copied()
    missing = {}
    for f in sorted(API.glob("*.py")):
        for mod in _imports(f):
            if not mod.startswith("packages."):
                continue
            parts = mod.split(".")
            # covered when some prefix of the import is a copied dir/module; a `from pkg import name`
            # candidate that is only an attribute is covered by its parent prefix.
            if not any(".".join(parts[:i]) in copied for i in range(2, len(parts) + 1)):
                missing.setdefault(f.name, set()).add(mod)
    assert not missing, f"imported by packages/api but not COPYed by its Dockerfile: {missing}"
