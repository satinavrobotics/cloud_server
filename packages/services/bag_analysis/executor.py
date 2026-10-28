"""How the worker runs the analysis image's runner (`run_analysis.py`, docs/bag_analysis_contract_2026-10-28.md section 3).

`Executor` is the seam: the worker only knows `list_analyzers()` and `run(...)`.

- `SubprocessExecutor`: runs `python3 run_analysis.py ...` as a child process of THIS container, which must
  therefore be built FROM the analysis image (ROS + the tools) plus this package. argv is always a list (no shell),
  the environment is scrubbed to an allow-list (no database, object-store or cloud credentials reach the runner),
  the child gets its own session (a timeout kills the whole process group), lower priority, no core dumps and
  optionally an address-space limit. The bag directory is passed as `--bag` and is read-only on disk (the worker
  strips the write bits); the runner writes only under `--out`.
- `FakeExecutor`: scripted results for tests; writes real files so the validation path runs unchanged.

There is deliberately NO executor that talks to the Docker socket: a service holding it is root on the host.
The trade-off is written up in docs/bag_analysis_server_2026-10-28.md.
"""
import json
import logging
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import resource
except ImportError:  # pragma: no cover - not on Windows
    resource = None  # type: ignore

from . import contract

logger = logging.getLogger("bag_analysis.executor")

LOG_TAIL_BYTES = 8000
LIST_MAX_BYTES = 4 * 1024 * 1024               # `--list` stdout cap (a catalog of a few dozen manifests is far smaller)
ENV_ALLOW_EXACT = ("PATH", "LANG", "TZ", "LD_LIBRARY_PATH", "PYTHONPATH", "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH",
                   "PYTHONHASHSEED", "SATI_ANALYSIS_IMAGE")
ENV_ALLOW_PREFIX = ("LC_", "ROS_", "AMENT_", "COLCON_", "RMW_", "RCUTILS_", "BAG_ANALYSIS_TOOLS")


@dataclass
class Catalog:
    """What an analysis image offers: its extractor version and its (validated) manifests."""
    extractor_version: int
    analyzers: List[Dict[str, Any]]
    analysis_image: Optional[str] = None
    tool_git_sha: Optional[str] = None             # the runner's `--list` reports it; stored with the declaration
    rejected: List[str] = field(default_factory=list)  # one line per manifest that failed validation

    def by_id(self) -> Dict[str, Dict[str, Any]]:
        return {m["id"]: m for m in self.analyzers}


@dataclass
class RunResult:
    exit_code: Optional[int]                       # None: timed out / cancelled before an exit code
    analyzer_dirs: Dict[Tuple[str, str], Path]     # (analyzer, profile) -> OUT/<analyzer>/<profile>/
    log_tail: str = ""
    timed_out: bool = False
    cancelled: bool = False
    extractor_version: Optional[int] = None


class Executor:
    """Protocol (duck-typed)."""

    def list_analyzers(self) -> Catalog:  # pragma: no cover
        raise NotImplementedError

    def run(self, bag_dir: Path, out_dir: Path, bag_id: str, only: Sequence[str],
            optional_inputs: Mapping[str, Path]) -> RunResult:  # pragma: no cover
        raise NotImplementedError


def scrub_env(source: Mapping[str, str], extra_allow: Sequence[str] = (), home: Optional[str] = None) -> Dict[str, str]:
    """The environment handed to the runner: an allow-list, never the service's own secrets."""
    env = {k: v for k, v in source.items()
           if k in ENV_ALLOW_EXACT or k in extra_allow or any(k.startswith(p) for p in ENV_ALLOW_PREFIX)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if home:
        env["HOME"] = home
    return env


def parse_catalog(text: str) -> Catalog:
    """`run_analysis.py --list` output: {"extractor_version": N, "analysis_image": str|null, "analyzers": [manifest, ...]}.
    The contract text only says `--list` prints the manifests; the extractor version is part of every result key,
    so the server asks for it here (a contract question for the robot team, see the report). A bare list is refused."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("extractor_version"), int) or data["extractor_version"] < 1 \
            or not isinstance(data.get("analyzers"), list):
        raise ValueError("--list must print {'extractor_version': int>=1, 'analyzers': [manifest, ...]}")
    good, rejected, seen = [], [], set()
    for m in data["analyzers"]:
        errors = contract.validate_manifest(m)
        mid = m.get("id") if isinstance(m, dict) else None
        if errors:
            rejected.append(f"{mid!r}: {errors[0]}")
        elif m["id"] in seen:
            rejected.append(f"{mid!r}: duplicate id")
        else:
            seen.add(m["id"])
            good.append(m)
    sha = data.get("tool_git_sha")
    return Catalog(data["extractor_version"], good, data.get("analysis_image"),
                   sha if isinstance(sha, str) else None, rejected)


def discover_dirs(out_dir: Path, only: Sequence[str] = ()) -> Dict[Tuple[str, str], Path]:
    """OUT/<analyzer>/<profile>/ directories the runner produced (the shared `x/` extraction is not one)."""
    wanted = {tuple(o.split(":", 1)) if ":" in o else (o, "default") for o in only}
    found: Dict[Tuple[str, str], Path] = {}
    if not out_dir.is_dir():
        return found
    for a in sorted(out_dir.iterdir()):
        if a.name == "x" or a.is_symlink() or not a.is_dir():
            continue
        for p in sorted(a.iterdir()):
            if p.is_dir() and not p.is_symlink() and (not wanted or (a.name, p.name) in wanted):
                found[(a.name, p.name)] = p
    return found


def _tail(path: Path, n: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


class SubprocessExecutor(Executor):
    def __init__(self, run_analysis: str, python: str = "python3", timeout_s: float = 7200.0, memory_mb: int = 0,
                 list_timeout_s: float = 120.0, env_source: Optional[Mapping[str, str]] = None,
                 extra_env_allow: Sequence[str] = (), should_stop: Callable[[], bool] = lambda: False,
                 nice: int = 10, max_file_bytes: int = 0, list_max_bytes: int = LIST_MAX_BYTES):
        self.max_file_bytes, self.list_max_bytes = max_file_bytes, list_max_bytes
        self.run_analysis, self.python = run_analysis, python
        self.timeout_s, self.memory_mb, self.list_timeout_s = timeout_s, memory_mb, list_timeout_s
        self.env_source = os.environ if env_source is None else env_source
        self.extra_env_allow, self.should_stop, self.nice = tuple(extra_env_allow), should_stop, nice

    def _env(self, home: Optional[str]) -> Dict[str, str]:
        return scrub_env(self.env_source, self.extra_env_allow, home)

    def _preexec(self, fsize: Optional[int] = None) -> Optional[Callable[[], None]]:
        if resource is None:
            return None
        memory_bytes, nice = self.memory_mb * 1024 * 1024, self.nice
        fsize = self.max_file_bytes if fsize is None else fsize

        def limits() -> None:  # runs in the child between fork and exec
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if memory_bytes:
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            if fsize:  # no single file the runner writes may exceed this (SIGXFSZ / EFBIG)
                resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
            if nice:
                os.nice(nice)
        return limits

    def argv_list(self) -> List[str]:
        return [self.python, self.run_analysis, "--list"]

    def list_analyzers(self) -> Catalog:
        # stdout/stderr go to files in the throwaway cwd and are read with a cap, so a runner that prints without
        # end can neither fill this process's memory nor pass an oversized catalog on.
        with tempfile.TemporaryDirectory(prefix="ba-list-") as cwd:
            out_path, err_path = Path(cwd) / "stdout", Path(cwd) / "stderr"
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                proc = subprocess.run(self.argv_list(), stdout=out, stderr=err, stdin=subprocess.DEVNULL, cwd=cwd,
                                      env=self._env(cwd), timeout=self.list_timeout_s,
                                      preexec_fn=self._preexec(self.list_max_bytes + 1), start_new_session=True)
            if proc.returncode != 0:
                raise RuntimeError(f"run_analysis.py --list exited {proc.returncode}: {_tail(err_path, 500)}")
            raw = contract.read_capped(out_path, self.list_max_bytes)
        if len(raw) > self.list_max_bytes:
            raise ValueError(f"--list printed more than {self.list_max_bytes} bytes")
        return parse_catalog(raw.decode("utf-8"))

    def run_argv(self, bag_dir: Path, out_dir: Path, bag_id: str, only: Sequence[str],
                 optional_inputs: Mapping[str, Path]) -> List[str]:
        argv = [self.python, self.run_analysis, "--bag", str(bag_dir), "--out", str(out_dir), f"--bag-id={bag_id}"]  # `=` form: an id starting with '-' is a value, not a flag
        if only:
            argv += ["--only", ",".join(only)]
        flags = {"params": "--params", "episodes": "--episodes", "vda_log": "--vda-log"}
        for name, path in sorted(optional_inputs.items()):
            if name not in flags:
                raise ValueError(f"unknown optional input {name!r}")
            argv += [flags[name], str(path)]
        return argv

    def run(self, bag_dir: Path, out_dir: Path, bag_id: str, only: Sequence[str],
            optional_inputs: Mapping[str, Path]) -> RunResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        argv = self.run_argv(bag_dir, out_dir, bag_id, only, optional_inputs)
        log_path = out_dir.parent / "runner.stdout.log"
        timed_out = cancelled = False
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=str(out_dir), env=self._env(str(out_dir)), preexec_fn=self._preexec(),
                                    start_new_session=True)
            import time
            deadline = time.monotonic() + self.timeout_s
            code: Optional[int] = None
            while code is None:
                try:
                    code = proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    if time.monotonic() > deadline:
                        timed_out = True
                    elif self.should_stop():
                        cancelled = True
                    if timed_out or cancelled:
                        self._kill_group(proc)
                        code = None
                        break
        return RunResult(None if (timed_out or cancelled) else code, discover_dirs(out_dir, only),
                         _tail(log_path), timed_out=timed_out, cancelled=cancelled)

    @staticmethod
    def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
        for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue


class FakeExecutor(Executor):
    """Test double. `results[(analyzer, profile)]` is an envelope dict (written as result.json), raw bytes, a
    callable(analyzer_dir) -> None that writes what it likes, or None (no result.json at all)."""

    def __init__(self, catalog: Catalog, results: Optional[Dict[Tuple[str, str], Any]] = None, exit_code: int = 0,
                 log: str = "fake runner log", timed_out: bool = False, cancelled: bool = False,
                 raise_on_run: Optional[BaseException] = None):
        self.catalog, self.results, self.exit_code, self.log = catalog, results or {}, exit_code, log
        self.timed_out, self.cancelled, self.raise_on_run = timed_out, cancelled, raise_on_run
        self.calls: List[Dict[str, Any]] = []

    def list_analyzers(self) -> Catalog:
        return self.catalog

    def run(self, bag_dir, out_dir, bag_id, only, optional_inputs) -> RunResult:
        assert Path(bag_dir).is_dir(), "the bag directory must exist when the runner starts"
        self.calls.append({"bag_dir": Path(bag_dir), "out_dir": Path(out_dir), "bag_id": bag_id, "only": list(only),
                           "optional_inputs": dict(optional_inputs),
                           "bag_files": sorted(p.name for p in Path(bag_dir).iterdir())})
        if self.raise_on_run:
            raise self.raise_on_run
        out_dir.mkdir(parents=True, exist_ok=True)
        if not (self.timed_out or self.cancelled):
            for entry in only:
                key = tuple(entry.split(":", 1)) if ":" in entry else (entry, "default")
                spec = self.results.get(key)
                adir = out_dir / key[0] / key[1]
                adir.mkdir(parents=True, exist_ok=True)
                if spec is None:
                    continue
                if callable(spec):
                    spec(adir)
                elif isinstance(spec, (bytes, bytearray)):
                    (adir / "result.json").write_bytes(bytes(spec))
                else:
                    (adir / "result.json").write_text(json.dumps(spec))
        return RunResult(None if (self.timed_out or self.cancelled) else self.exit_code,
                         discover_dirs(out_dir, only), self.log, timed_out=self.timed_out, cancelled=self.cancelled,
                         extractor_version=self.catalog.extractor_version)
