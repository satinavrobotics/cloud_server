"""Branch viewer driver: export each git ref, play it over each map in its own process, and write one HTML page.

Outputs under tools/scene_graph_sim/out/ (not committed): branch_viewer.html (open in a browser), branch_viewer.json (the check
summary per map and ref), trees/<sha>/ (the exported code), run_<map>_<sha>.json (one run each).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SIM))
OUT = os.path.join(SIM, "out")
PAGE = os.path.join(HERE, "page")


def export_tree(ref):
    """the tree of a git ref, exported with git archive into out/trees/<short sha> (reused when already there)"""
    sha = subprocess.run(["git", "-C", REPO, "rev-parse", "--short=7", ref], capture_output=True, text=True, check=True).stdout.strip()
    dest = os.path.join(OUT, "trees", sha)
    if not os.path.isdir(os.path.join(dest, "packages")):
        os.makedirs(dest, exist_ok=True)
        archive = subprocess.run(["git", "-C", REPO, "archive", ref, "packages", "cloud_common"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", dest], input=archive, check=True)
    return sha, dest


def play(ref_sha, tree, map_name, data_dir, exponential):
    """run one tree over one map in a subprocess; returns the run's JSON"""
    out = os.path.join(OUT, "run_%s_%s.json" % (map_name, ref_sha))
    cmd = [sys.executable, os.path.join(HERE, "run.py"), "--worker", "--tree", tree,
           "--map-dir", os.path.join(data_dir, map_name), "--out", out]
    if exponential:
        cmd.append("--exponential")
    print("playing %s on %s" % (ref_sha, map_name), flush=True)
    subprocess.run(cmd, check=True)
    with open(out) as f:
        return json.load(f)


def summarise(run):
    pairs = routed = failing = 0
    reasons = {}
    for per_goal in run["checks"]:
        for c in per_goal:
            pairs += 1
            routed += c["length"] is not None
            failing += bool(c["fails"])
            for f in c["fails"]:
                reasons[f] = reasons.get(f, 0) + 1
    return dict(captures=pairs, with_route=routed, failing=failing, fail_reasons=reasons)


def build_page(runs):
    with open(os.path.join(PAGE, "viewer.html")) as f:
        html = f.read()
    with open(os.path.join(PAGE, "viewer.css")) as f:
        css = f.read()
    with open(os.path.join(PAGE, "viewer.js")) as f:
        js = f.read()
    data = json.dumps(runs, separators=(",", ":"))
    return html.replace("__CSS__", css).replace("__JS__", js).replace("__DATA__", data)


def drive(refs, maps, data_dir, exponential):
    os.makedirs(OUT, exist_ok=True)
    runs = {}
    for ref in refs:
        sha, tree = export_tree(ref)
        for m in maps:
            runs.setdefault(m, {})[ref] = play(sha, tree, m, data_dir, exponential)
    summary = {m: {ref: summarise(r) for ref, r in by.items()} for m, by in runs.items()}
    with open(os.path.join(OUT, "branch_viewer.json"), "w") as f:
        json.dump(summary, f, indent=1)
    with open(os.path.join(OUT, "branch_viewer.html"), "w") as f:
        f.write(build_page(runs))
    for m in summary:
        for ref, s in summary[m].items():
            print("%-10s %-10s pairs %4d  with route %4d  failing %4d  %s"
                  % (m, ref, s["captures"], s["with_route"], s["failing"], s["fail_reasons"]))
    print("wrote", os.path.join(OUT, "branch_viewer.html"))
