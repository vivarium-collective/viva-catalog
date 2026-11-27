#!/usr/bin/env python3
"""Build ``viva_catalog/ecosystem-index.json`` from the repo registry.

For every repo in ``modules.json`` we **shallow-clone the repo and scan its
source** — no published dashboard required. What we extract:

- **composites** — ``@composite_generator(name=…, description=…)`` decorators
  (AST) plus any ``*.composite.yaml`` files.
- **processes / steps** — top-level classes whose base ends in ``Process`` /
  ``Step`` — plus the Step-derived reactive edges ``Emitter`` / ``Visualization``
  / ``Viewer`` / ``Analysis`` (each tagged with its ``kind``) — found via AST,
  with the description taken from a ``description`` class
  attribute or the class docstring.
- **studies** — ``**/studies/*/study.yaml`` (name + objective/title).
- **investigations** — ``**/investigations/*/investigation.yaml`` (name + title).

This gives complete coverage across the ecosystem regardless of whether a repo
publishes a workbench dashboard. Repos that can't be cloned are still listed
(empty artifacts, ``cloned: false``).

Usage:  python scripts/build_ecosystem_index.py [--out PATH] [--timeout N] [--jobs N]
Needs: git + PyYAML.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "viva_catalog"

# Edge base classes we index. Process/Step are the two process-bigraph edge kinds;
# Emitter/Visualization/Viewer/Analysis are Step-derived reactive edges that a
# browse-worthy library (e.g. viva-emitters) exposes but that a bare (Process|Step)
# base-name check would miss.
_EDGE_BASES = ("Process", "Step", "Emitter", "Visualization", "Viewer", "Analysis")
_PROC_BASE = re.compile(r"(%s)$" % "|".join(_EDGE_BASES))
# suffix -> artifact kind (longest/most-specific suffix checked first)
_EDGE_KIND = (("Process", "process"), ("Step", "step"), ("Emitter", "emitter"),
              ("Visualization", "visualization"), ("Viewer", "viewer"), ("Analysis", "analysis"))

# A repo "publishes to the marketplace" by adding this GitHub topic. Membership
# is discovered from the org (no hand-maintained list) — see discover_modules.
MARKETPLACE_TOPIC = "viva-marketplace"
ORG = "vivarium-collective"


def discover_modules(timeout: float = 60.0) -> list[dict]:
    """Discover the marketplace registry from GitHub: every PUBLIC, non-archived
    ``vivarium-collective`` repo carrying the ``viva-marketplace`` topic.

    This replaces the hand-maintained ``modules.json`` membership list — a repo
    joins the marketplace by adding the topic (``gh repo edit --add-topic
    viva-marketplace``), and the daily index build picks it up automatically.
    Returns registry entries in the same shape modules.json used
    (name/source/ref/package/homepage/description/tags), sorted by name.
    """
    import urllib.parse
    import urllib.request

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    q = f"org:{ORG} topic:{MARKETPLACE_TOPIC} archived:false"
    modules: list[dict] = []
    page = 1
    while True:
        url = ("https://api.github.com/search/repositories?q="
               + urllib.parse.quote(q)
               + f"&per_page=100&page={page}&sort=full_name&order=asc")
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "viva-marketplace-index"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        items = data.get("items", []) or []
        for it in items:
            modules.append({
                "name": it["name"],
                "source": it.get("clone_url") or f"{it['html_url']}.git",
                "ref": it.get("default_branch") or "main",
                "package": it["name"].replace("-", "_"),
                "homepage": it.get("html_url"),
                "description": it.get("description") or "",
                "tags": sorted(t for t in (it.get("topics") or [])
                               if t != MARKETPLACE_TOPIC),
            })
        total = int(data.get("total_count") or 0)
        if len(items) < 100 or len(modules) >= total:
            break
        page += 1
    modules.sort(key=lambda m: m["name"].lower())
    return modules


def _org_repo(source: str) -> tuple[str, str]:
    s = re.sub(r"\.git$", "", (source or "").strip())
    s = re.sub(r"^git@github\.com:", "https://github.com/", s)
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+)/?$", s)
    return (m.group(1), m.group(2)) if m else ("vivarium-collective", s.rsplit("/", 1)[-1])


def _clone(url: str, ref: str, dest: Path, timeout: float) -> bool:
    """Shallow-clone url@ref into dest. Falls back to the default branch if the
    ref doesn't exist. Returns True on success."""
    base = ["git", "clone", "--depth", "1", "--quiet"]
    for args in ([*base, "--branch", ref, url, str(dest)] if ref else None,
                 [*base, url, str(dest)]):
        if args is None:
            continue
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False


def _class_description(node: ast.ClassDef) -> str:
    # Prefer a `description = "..."` class attribute (pbg convention), else the
    # first line of the docstring.
    for stmt in node.body:
        if (isinstance(stmt, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "description" for t in stmt.targets)
                and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
            return stmt.value.value.strip().splitlines()[0]
    doc = ast.get_docstring(node) or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def _kw_str(call: ast.Call, name: str) -> str:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return ""


def _scan_python(root: Path) -> tuple[list, list, list]:
    composites, processes, steps = [], [], []
    seen_c, seen_p, seen_s = set(), set(), set()
    for py in root.rglob("*.py"):
        # Skip vendored / test / build noise.
        parts = set(py.parts)
        if parts & {".git", "tests", "test", "build", "dist", "node_modules", ".venv", "venv"}:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for b in node.bases:
                    bname = b.attr if isinstance(b, ast.Attribute) else (b.id if isinstance(b, ast.Name) else "")
                    if not _PROC_BASE.search(bname or "") or node.name in _EDGE_BASES:
                        continue
                    kind = next(v for k, v in _EDGE_KIND if bname.endswith(k))
                    # Process is its own bucket; every other edge kind is Step-derived.
                    bucket, seen = (processes, seen_p) if kind == "process" else (steps, seen_s)
                    if node.name not in seen:
                        seen.add(node.name)
                        bucket.append({"name": node.name, "kind": kind,
                                       "description": _class_description(node)})
                    break
            elif isinstance(node, ast.Call):
                fn = node.func
                fname = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if fname == "composite_generator":
                    nm = _kw_str(node, "name")
                    if nm and nm not in seen_c:
                        seen_c.add(nm)
                        composites.append({"name": nm, "description": _kw_str(node, "description")})
    # *.composite.yaml files
    for cy in root.rglob("*.composite.yaml"):
        if ".git" in cy.parts:
            continue
        nm = cy.name[: -len(".composite.yaml")]
        desc = ""
        if yaml:
            try:
                d = yaml.safe_load(cy.read_text(encoding="utf-8")) or {}
                nm = d.get("name") or nm
                desc = d.get("description") or ""
            except Exception:  # noqa: BLE001
                pass
        if nm not in seen_c:
            seen_c.add(nm)
            composites.append({"name": nm, "description": desc})
    return composites, processes, steps


def _scan_specs(root: Path, kind: str, spec_name: str, desc_keys) -> list:
    out, seen = [], set()
    for spec in root.rglob(f"{kind}/*/{spec_name}"):
        if ".git" in spec.parts:
            continue
        name, desc = spec.parent.name, ""
        if yaml:
            try:
                d = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
                name = d.get("name") or name
                for k in desc_keys:
                    v = d.get(k)
                    if isinstance(v, dict):
                        v = v.get("question") or v.get("objective")
                    if v:
                        desc = str(v); break
            except Exception:  # noqa: BLE001
                pass
        if name not in seen:
            seen.add(name)
            out.append({"name": name, "description": desc})
    return out


def derive_kind(counts: dict) -> str:
    """Classify a repo from its scanned artifact counts, so consumers can group
    the ledger without re-deriving it:

      - ``workspace``  — carries its own studies / investigations (a research
        program: a simulator, or a pinned one, plus the questions asked of it).
      - ``simulator``  — ships processes / steps / composites but no studies
        (a simulator wrapped as Processes, or a reusable composite / library).
      - ``other``      — neither (emitters, utilities, infrastructure).
    """
    studies = (counts.get("studies", 0) or 0) + (counts.get("investigations", 0) or 0)
    sim = ((counts.get("processes", 0) or 0) + (counts.get("steps", 0) or 0)
           + (counts.get("composites", 0) or 0))
    if studies > 0:
        return "workspace"
    if sim > 0:
        return "simulator"
    return "other"


_SKIP_PKG_DIRS = {
    "tests", "test", "docs", "doc", "examples", "example", "scripts", "build",
    "dist", "notebooks", "data", "assets", ".github", ".git", "venv", ".venv",
    "node_modules", "site", "__pycache__",
}


def _detect_package(root: Path, fallback: str) -> str:
    """Recover the real importable package a repo installs, rather than guessing
    it from the repo name.

    This matters for sim/studies splits: a repo like ``viva-genesis-sim`` keeps
    the un-suffixed package ``viva_genesis`` (so study composite refs resolve
    across the split), so ``name.replace('-', '_')`` would give the WRONG,
    un-installable name. We read it from ``pyproject.toml`` (hatch / setuptools
    explicit package lists), then fall back to the single top-level importable
    directory, then to the name-derived guess.
    """
    # 1) pyproject.toml explicit package declarations.
    try:
        import tomllib
    except ModuleNotFoundError:  # py<3.11
        tomllib = None
    pyproject = root / "pyproject.toml"
    if tomllib and pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        tool = data.get("tool", {}) if isinstance(data, dict) else {}
        # hatch:  [tool.hatch.build.targets.wheel] packages = ["viva_genesis"]
        wheel = (tool.get("hatch", {}).get("build", {})
                 .get("targets", {}).get("wheel", {}))
        pkgs = wheel.get("packages")
        if isinstance(pkgs, list) and pkgs and isinstance(pkgs[0], str):
            return Path(pkgs[0]).name
        # setuptools:  [tool.setuptools] packages = ["viva_foo", ...]
        st_pkgs = tool.get("setuptools", {}).get("packages")
        if isinstance(st_pkgs, list) and st_pkgs and isinstance(st_pkgs[0], str):
            # take the shortest top-level (not a sub-package "a.b")
            tops = sorted({p.split(".")[0] for p in st_pkgs if isinstance(p, str)}, key=len)
            if tops:
                return tops[0]

    # 2) single top-level importable directory.
    cands = []
    for child in sorted(root.iterdir() if root.is_dir() else []):
        if (child.is_dir() and child.name not in _SKIP_PKG_DIRS
                and not child.name.startswith(".")
                and (child / "__init__.py").is_file()):
            cands.append(child.name)
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        # prefer one matching the name-derived guess or a known package prefix
        preferred = [c for c in cands if c == fallback or c.startswith(("viva_", "pbg_"))]
        if len(preferred) == 1:
            return preferred[0]

    # 3) name-derived guess.
    return fallback


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _canon(name: str) -> str:
    """PEP 503 canonical distribution name, for matching deps/pins to repos.
    Collapses runs of - _ . to a single - and lowercases, so ``viva_genesis``,
    ``viva-genesis`` and ``Viva.Genesis`` all match."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def _parse_pyproject_deps(root: Path) -> set:
    """Distribution names this repo depends on, canonicalized: from
    ``[project.dependencies]`` + every ``[project.optional-dependencies]`` group
    (engines live in extras as often as in the core list) + ``[tool.uv.sources]``
    keys (git overrides that name catalog repos)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # py<3.11
        return set()
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return set()
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    proj = data.get("project", {}) if isinstance(data, dict) else {}
    reqs = list(proj.get("dependencies", []) or [])
    for grp in (proj.get("optional-dependencies", {}) or {}).values():
        reqs.extend(grp or [])
    names = set()
    for r in reqs:
        if isinstance(r, str):
            m = _REQ_NAME.match(r)
            if m:
                names.add(_canon(m.group(1)))
    uv_sources = (data.get("tool", {}) or {}).get("uv", {}).get("sources", {})
    if isinstance(uv_sources, dict):
        names.update(_canon(k) for k in uv_sources)
    return names


def _scan_study_sim_refs(root: Path) -> list:
    """The simulator each study pins — the ``simulator: {package, ref}`` block of
    every ``studies/*/study.yaml`` (``package`` is a distribution name). Unique by
    (package, ref)."""
    out, seen = [], set()
    if not yaml:
        return out
    for spec in root.rglob("studies/*/study.yaml"):
        if ".git" in spec.parts:
            continue
        try:
            d = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        sim = d.get("simulator") if isinstance(d, dict) else None
        if isinstance(sim, dict) and sim.get("package"):
            ref = "" if sim.get("ref") is None else str(sim.get("ref"))
            key = (_canon(str(sim["package"])), ref)
            if key not in seen:
                seen.add(key)
                out.append({"package": str(sim["package"]), "ref": ref})
    return out


def harvest_repo(module: dict, timeout: float) -> dict:
    name = module.get("name") or module.get("package") or ""
    source = module.get("source") or module.get("homepage") or ""
    ref = module.get("ref") or ""
    org, repo = _org_repo(source)
    # Name-derived guess; corrected from the source below when the repo clones.
    fallback_pkg = module.get("package") or name.replace("-", "_")
    entry = {
        "name": name, "repo": repo, "source": re.sub(r"\.git$", "", source),
        "homepage": module.get("homepage") or f"https://github.com/{org}/{repo}",
        "description": module.get("description") or "", "tags": module.get("tags") or [],
        "package": fallback_pkg,
        "cloned": False, "processes": [], "steps": [], "composites": [],
        "studies": [], "investigations": [],
        "_deps": [], "_sim_refs": [],  # resolved to reuses / simulator_refs in main()
    }
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / repo
        if _clone(re.sub(r"^git@github\.com:", "https://github.com/", source) or f"https://github.com/{org}/{repo}.git",
                  ref, dest, timeout):
            entry["cloned"] = True
            entry["package"] = _detect_package(dest, fallback_pkg)
            comps, procs, steps = _scan_python(dest)
            entry["composites"] = comps
            entry["processes"] = procs
            entry["steps"] = steps
            entry["studies"] = _scan_specs(dest, "studies", "study.yaml",
                                           ("objective", "purpose", "title", "description"))
            entry["investigations"] = _scan_specs(dest, "investigations", "investigation.yaml",
                                                  ("title", "description", "objective"))
            entry["_deps"] = sorted(_parse_pyproject_deps(dest))
            entry["_sim_refs"] = _scan_study_sim_refs(dest)
    entry["counts"] = {k: len(entry[k]) for k in
                       ("processes", "steps", "composites", "studies", "investigations")}
    entry["kind"] = derive_kind(entry["counts"])
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(PKG / "ecosystem-index.json"))
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--stamp", default=None)
    ap.add_argument("--only", default=None, help="comma-separated repo names to limit (debug)")
    ap.add_argument("--no-discover", action="store_true",
                    help="skip GitHub topic discovery; use the committed modules.json as-is "
                         "(offline / no token)")
    args = ap.parse_args(argv)

    # Discover the registry from the `viva-marketplace` GitHub topic and refresh
    # the committed modules.json (the generated cache). Fall back to the committed
    # list if discovery fails (offline / API error) so the build never breaks.
    if not args.no_discover:
        try:
            discovered = discover_modules(args.timeout)
            (PKG / "modules.json").write_text(
                json.dumps(discovered, indent=2) + "\n", encoding="utf-8")
            print(f"discovered {len(discovered)} repos via topic "
                  f"'{MARKETPLACE_TOPIC}' -> refreshed modules.json", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: topic discovery failed ({e}); using committed "
                  f"modules.json", file=sys.stderr)

    modules = json.loads((PKG / "modules.json").read_text(encoding="utf-8"))
    if isinstance(modules, dict):
        modules = modules.get("modules") or []
    only = set(args.only.split(",")) if args.only else None

    repos = []
    for m in modules:
        if not isinstance(m, dict):
            continue
        if only and m.get("name") not in only:
            continue
        entry = harvest_repo(m, args.timeout)
        c = entry["counts"]
        print(f"  {entry['name']:24} cloned={str(entry['cloned']):5} "
              f"proc={c['processes']} step={c['steps']} comp={c['composites']} "
              f"study={c['studies']} inv={c['investigations']}", file=sys.stderr)
        repos.append(entry)

    # Resolve cross-repo edges now that every repo is known. Both map a
    # DISTRIBUTION name (what deps and study pins reference — equal to the repo
    # name) to a repo; unresolved names (private/un-topic'd/external, e.g.
    # process-bigraph) simply aren't nodes, so they drop rather than dangle.
    #   reuses         — repo -> catalog repos it declares a dependency on
    #   simulator_refs — workspace repo -> the sim repo(s) its studies pin
    dist_to_repo = {_canon(r["name"]): r["name"] for r in repos}
    for r in repos:
        deps = r.pop("_deps", []) or []
        r["reuses"] = sorted({dist_to_repo[d] for d in deps
                              if d in dist_to_repo and dist_to_repo[d] != r["name"]})
        resolved, seen = [], set()
        for sr in (r.pop("_sim_refs", []) or []):
            repo = dist_to_repo.get(_canon(sr.get("package", "")))
            if repo and repo != r["name"] and (repo, sr.get("ref", "")) not in seen:
                seen.add((repo, sr.get("ref", "")))
                resolved.append({"repo": repo, "ref": sr.get("ref", "")})
        r["simulator_refs"] = resolved

    index = {
        "generated_at": args.stamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_repos": len(repos),
        "n_cloned": sum(1 for r in repos if r["cloned"]),
        "repos": repos,
    }
    Path(args.out).write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {index['n_repos']} repos, {index['n_cloned']} cloned", file=sys.stderr)

    # Back-patch modules.json with the real packages recovered from source, so the
    # install metadata is correct for sim/studies splits (e.g. viva-genesis-sim
    # installs viva_genesis, not viva_genesis_sim). Only for cloned repos; a full
    # discovery rebuild is idempotent (re-derives the guess, then re-corrects).
    if not only:
        detected = {r["name"]: r["package"] for r in repos if r.get("cloned")}
        changed = 0
        for m in modules:
            if isinstance(m, dict) and m.get("name") in detected:
                pkg = detected[m["name"]]
                if m.get("package") != pkg:
                    m["package"] = pkg
                    changed += 1
        if changed:
            (PKG / "modules.json").write_text(
                json.dumps(modules, indent=2) + "\n", encoding="utf-8")
            print(f"corrected {changed} package name(s) in modules.json from source",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
