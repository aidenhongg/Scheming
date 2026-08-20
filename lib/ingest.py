#!/usr/bin/env python3
"""scheming ingest (C2) — deterministic, ZERO-LLM ingestion of Claude Code
Workflow-tool artifacts into library.ingested_workflows. Stdlib only.

The Workflow tool persists, per run: a run-log ``wf_*.json`` and a script
(``*.js``/``*.mjs``). We scan ``claude_projects_dir()`` recursively for the run
logs, recover each workflow's definition, and merge one ``ingested_workflow``
record per unique workflow into the library (dedupe by ``id``; other collections
are never touched).

E15 correction we implement: the RUNTIME artifact persists no ``meta.description``
(0/55), so ``goal`` is recovered by regex from the embedded ``export const
meta = {...}`` literal in the script source (present 55/55). We therefore parse
the script text, not just the json.

Robust by contract: a missing/empty projects dir reports "0 found" and exits 0;
each wf_*.json is probed defensively (meta stored in json, script inline, or a
referenced/sibling script file) and an unparseable one is skipped + counted,
never fatal.

CLI: ``python lib/ingest.py [--dry-run]`` prints the funnel
``found -> parsed -> new -> skipped(unparseable)``. ``--dry-run`` computes but
does not write. ``--selftest`` runs an isolated end-to-end check.
"""
import glob, json, os, re, sys

import scheming_core

# ---- meta recovery from JS script source ------------------------------------
# The definition lives in an `export const meta = { name, description, phases }`
# object literal. We brace-match the object (string-aware) then pull fields out,
# rather than trusting one flat regex over nested/quoted content.

_META_START = re.compile(r"(?<![\w$])meta\s*=\s*\{")   # export const meta = {  /  const meta = {  /  meta = {


def _match(text, i, open_ch, close_ch):
    """text[i] is `open_ch`; return the balanced substring through its match,
    skipping delimiters that sit inside JS strings ('...', "...", `...`).
    None if unbalanced. This is why a `{` or `[` inside a description string
    cannot throw off the parse."""
    depth, k, n, quote = 0, i, len(text), None
    while k < n:
        c = text[k]
        if quote:
            if c == "\\":
                k += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[i:k + 1]
        k += 1
    return None


def _unescape(s):
    # one level of JS string escapes: \n \t \r become the char; \" \\ ` etc. drop the backslash
    return re.sub(r"\\(.)", lambda g: {"n": "\n", "t": "\t", "r": "\r"}.get(g.group(1), g.group(1)), s)


def _js_str(obj, key):
    """Value of a string field `key: "..."` in an object-literal text (any of the
    three JS quote styles, escape-aware). None if absent."""
    m = re.search(r"(?<![\w$])" + re.escape(key) + r"""\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", obj, re.S)
    return _unescape(m.group(2)) if m else None


def _js_phases(obj):
    """Recover `phases: [...]` from an object-literal text. Handles an array of
    objects ({id, do/name/step/...}) and a bare array of strings. Best-effort:
    returns [] if none — an empty phase list is not a skip reason."""
    m = re.search(r"(?<![\w$])phases\s*:\s*\[", obj)
    if not m:
        return []
    arr = _match(obj, m.end() - 1, "[", "]")
    if not arr:
        return []
    phases, k = [], 0
    while True:
        b = arr.find("{", k)
        if b < 0:
            break
        blk = _match(arr, b, "{", "}")
        if not blk:
            break
        do = _js_str(blk, "do") or _js_str(blk, "name") or _js_str(blk, "step") or _js_str(blk, "description")
        pid = _js_str(blk, "id")
        p = {}
        if pid:
            p["id"] = pid
        if do:
            p["do"] = do
        if p:
            phases.append(p)
        k = b + len(blk)
    if phases:
        return phases
    for sm in re.finditer(r"""(["'`])((?:\\.|(?!\1).)*)\1""", arr):  # bare string array fallback
        phases.append({"do": _unescape(sm.group(2))})
    return phases


def _meta_from_script(script):
    if not script:
        return {}
    m = _META_START.search(script)
    if not m:
        return {}
    obj = _match(script, m.end() - 1, "{", "}")
    if not obj:
        return {}
    return {"name": _js_str(obj, "name"),
            "description": _js_str(obj, "description"),
            "trigger": _js_str(obj, "trigger"),
            "phases": _js_phases(obj)}


# ---- locating the script for a wf_*.json ------------------------------------

def _walk_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _walk_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_strings(v)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _find_script(data, wf_path):
    """Return (script_text, script_path). Probes, in order: script embedded
    inline in the run log; a referenced *.js/*.mjs path; a sibling script in the
    same dir. (None, None) if nothing carries the meta literal."""
    d = os.path.dirname(wf_path)
    # 1. inline — some run log field holds the JS source verbatim
    for v in _walk_strings(data):
        if "export const meta" in v or _META_START.search(v):
            return v, wf_path
    # 2. referenced — a field is a path ending in .js/.mjs
    for v in _walk_strings(data):
        if v.strip().lower().endswith((".js", ".mjs")):
            cand = v if os.path.isabs(v) else os.path.join(d, v)
            if os.path.isfile(cand):
                t = _read(cand)
                if t:
                    return t, cand
    # 3. sibling — any script alongside the run log that carries a meta literal
    for cand in sorted(glob.glob(os.path.join(d, "*.js")) + glob.glob(os.path.join(d, "*.mjs"))):
        t = _read(cand)
        if t and _META_START.search(t):
            return t, cand
    return None, None


# ---- transform --------------------------------------------------------------

def _slug(name):
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", name.strip().lower())).strip("-")


def build_record(wf_path):
    """Transform one wf_*.json into an ingested_workflow record, or None if it is
    unparseable / lacks a name or goal (the caller counts the skip). Never raises."""
    try:
        with open(wf_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    script, script_path = _find_script(data, wf_path)
    smeta = _meta_from_script(script)

    name = meta.get("name") or smeta.get("name") or data.get("name") or data.get("workflow") or data.get("slug")
    # goal := description; E15: absent from the runtime json, recovered from script source
    goal = meta.get("description") or smeta.get("description") or data.get("goal") or data.get("description")
    # phases: the runtime json carries them (100%); else recover from the script meta
    phases = data.get("phases") or meta.get("phases") or smeta.get("phases") or []

    if not name or not goal:
        return None
    slug = _slug(str(name))
    if not slug:
        return None

    rec = {
        "id": "wf-" + slug,
        "goal": str(goal).strip(),
        "name": str(name),
        "phases": phases if isinstance(phases, list) else [],
        "source": "workflow-tool",       # channel tag (origin filter)
        "script_path": script_path or wf_path,
        "run_log": wf_path,              # exact-artifact provenance (the wf_*.json run log)
    }
    trigger = data.get("trigger") or meta.get("trigger") or smeta.get("trigger")
    if trigger:
        rec["trigger"] = str(trigger)
    if isinstance(data.get("runs"), int):
        rec["runs"] = data["runs"]
    if data.get("session"):
        rec["session"] = data["session"]
    return rec


def ingest(dry_run=False):
    """Scan, transform, merge. Returns the funnel counts. Writes the library only
    when there is something new and not dry_run."""
    base = scheming_core.claude_projects_dir()
    found = (sorted(glob.glob(os.path.join(base, "**", "wf_*.json"), recursive=True))
             if os.path.isdir(base) else [])

    lib = scheming_core.load_library(create=False)   # create=False: don't write anything on a dry run
    existing = {w.get("id") for w in lib.get("ingested_workflows", [])}

    parsed = skipped = 0
    new_recs = {}
    for path in found:
        rec = build_record(path)
        if rec is None:
            skipped += 1
            print("warn: skipped unparseable %s" % path, file=sys.stderr)
            continue
        parsed += 1                       # counts every run log that yielded a definition
        if rec["id"] in existing or rec["id"] in new_recs:
            continue                      # dedupe by id: multiple runs collapse to one record
        new_recs[rec["id"]] = rec

    if new_recs and not dry_run:
        lib["ingested_workflows"].extend(new_recs.values())
        scheming_core.save_library(lib)
    if not dry_run:                       # library-evolution telemetry: arm provenance
        scheming_core.emit("library_events", {
            "event": "ingest", "files_found": len(found),
            "parsed": parsed, "dupes_skipped": parsed - len(new_recs),
            "new_ids": sorted(new_recs)})
    return {"found": len(found), "parsed": parsed, "new": len(new_recs),
            "skipped": skipped, "base": base}


# ---- CLI --------------------------------------------------------------------

def _print_funnel(f, dry_run):
    if f["found"] == 0:
        print("ingest: 0 found (no Claude Workflow artifacts under %s)" % f["base"])
        return
    print("ingest: %d found -> %d parsed -> %d new -> %d skipped(unparseable)%s"
          % (f["found"], f["parsed"], f["new"], f["skipped"],
             "  [dry-run, not written]" if dry_run else ""))


def main(argv):
    try:  # UTF-8 stdout so a cp1252 console can't crash a print on non-ASCII paths
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in argv:
        return _selftest()
    dry = "--dry-run" in argv
    _print_funnel(ingest(dry_run=dry), dry)
    return 0


def _selftest():
    """Isolated end-to-end check: synthetic projects dir with 3 fake wf artifacts
    (meta-in-json, meta-only-in-script, malformed). Touches no real user state."""
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="scheming_ingest_test_")
    home = os.path.join(tmp, "home")
    proj = os.path.join(tmp, "projects")
    wfdir = os.path.join(proj, "C--Users-x-proj", "workflows")
    os.makedirs(wfdir)
    iso = scheming_core.isolated_home(home, CLAUDE_PROJECTS_DIR=proj); iso.__enter__()

    def w(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    try:
        # 1. meta stored directly in the run log json
        w(os.path.join(wfdir, "wf_deploy.json"), json.dumps({
            "meta": {"name": "deploy-canary",
                     "description": "Ship a canary and verify metrics before promoting",
                     "phases": [{"id": "ship", "do": "deploy 1%"},
                                {"id": "watch", "do": "watch error rate"}]},
            "runs": 3, "session": "s-aaa"}))
        # 2. meta ONLY in the sibling script source (E15 case); runtime json has no description/phases
        w(os.path.join(wfdir, "wf_review.json"), json.dumps({
            "name": "search-fix-selfreview", "script": "review.mjs",
            "runs": 1, "session": "s-bbb"}))
        w(os.path.join(wfdir, "review.mjs"),
          'export const meta = {\n'
          '  name: "search-fix-selfreview",\n'
          '  description: "Adversarial multi-lens review of the search fixes, verified before reporting",\n'
          '  phases: [\n'
          '    { id: "review", do: "six independent lenses over the diff" },\n'
          '    { id: "verify", do: "three skeptics per finding, majority must fail to refute" },\n'
          '  ],\n'
          '};\n'
          'export default async function () {}\n')
        # 3. malformed — invalid json; must be skipped, not fatal
        w(os.path.join(wfdir, "wf_broken.json"), "{ this is not json ]]")

        f1 = ingest(dry_run=False)
        assert f1["found"] == 3, f1
        assert f1["parsed"] == 2, f1
        assert f1["skipped"] == 1, f1
        assert f1["new"] == 2, f1

        lib = scheming_core.load_library(create=False)
        ids = sorted(x["id"] for x in lib["ingested_workflows"])
        assert ids == ["wf-deploy-canary", "wf-search-fix-selfreview"], ids
        rev = next(x for x in lib["ingested_workflows"] if x["id"] == "wf-search-fix-selfreview")
        assert rev["goal"].startswith("Adversarial multi-lens review"), rev
        assert rev["script_path"].endswith("review.mjs"), rev
        assert len(rev["phases"]) == 2, rev
        assert rev["phases"][0].get("id") == "review", rev
        dep = next(x for x in lib["ingested_workflows"] if x["id"] == "wf-deploy-canary")
        assert dep["goal"].startswith("Ship a canary"), dep
        assert len(dep["phases"]) == 2, dep

        # dedupe on a second run: nothing new, no duplicates, malformed still just counted
        f2 = ingest(dry_run=False)
        assert f2["new"] == 0 and f2["parsed"] == 2 and f2["skipped"] == 1, f2
        assert len(scheming_core.load_library(create=False)["ingested_workflows"]) == 2

        # dry-run writes nothing: add a 4th good artifact, dry-run, assert library unchanged on disk
        w(os.path.join(wfdir, "wf_third.json"), json.dumps({
            "meta": {"name": "rotate-keys", "description": "Rotate service keys with zero downtime"}}))
        f3 = ingest(dry_run=True)
        assert f3["found"] == 4 and f3["new"] == 1, f3
        assert len(scheming_core.load_library(create=False)["ingested_workflows"]) == 2, "dry-run must not write"

        print("ingest selftest ok:", f1, "| dedupe:", f2, "| dry-run:", f3)
        return 0
    finally:
        iso.__exit__(None, None, None)     # restore env
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
