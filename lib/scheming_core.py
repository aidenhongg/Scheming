#!/usr/bin/env python3
"""scheming shared core — paths, library IO, atomic state IO. Stdlib only.

Every scheming script imports this module (they are siblings in lib/, so a plain
`import scheming_core` works when a script is launched by path). It is the single
place that knows *where* per-user data lives, so no other component hardcodes a
path. Nothing here is user-specific: a fresh install has an empty library and
this module creates it on demand.

Data layout (all under SCHEMING_HOME, default ~/.scheming). The .jsonl files are an
append-only, joinable telemetry trail of the whole lifecycle
(recall -> selection -> use -> reaction -> cost -> bandit update); bandit_state
is the live serving read-model, bandit_updates is its replay ledger:
    library.json          the user's own mined library (never ships populated)
    bandit_state.json     {arm_id: {a, b, n, cost_mean?, cost_n?}}  (serving cache)
    recalls.jsonl         one row per query: {query_id, session, ts, query, abstained, ...}
    selections.jsonl      one row per served group/single: candidates[] + excluded[] + winner
    retrieval_log.jsonl   one row per --full read: {session, ts, entry_id, kind}
    reactions.jsonl       one row per labeled reaction (all: accepted/complaint/neutral) + raw cost + labeler health
    bandit_updates.jsonl  one row per bandit_state mutation (replay ledger, all writers)
    catchup_runs.jsonl    catch-up heartbeat + per-session summary rows
    library_events.jsonl  ingest/groups/mine/setup events (library evolution)
    assessed_sessions.json  ["<session_id>", ...] idempotency marker for catch-up
Privacy: raw query text lives ONLY in recalls.jsonl; verbatim corrections ONLY on
reactions.jsonl complaint rows; every other row carries ids/counts/enums. All
local under SCHEMING_HOME, never exported.
"""
import contextlib, json, os, sys, tempfile, time

# ---------------- path resolution (the one place paths are decided) ----------

def home():
    """Per-user scheming data dir. Override with SCHEMING_HOME; default ~/.scheming."""
    return os.path.abspath(os.path.expanduser(os.environ.get("SCHEMING_HOME", "~/.scheming")))

def ensure_home():
    os.makedirs(home(), exist_ok=True)
    return home()

def _p(env, name):
    """A file under home(), overridable by a specific env var (back-compat)."""
    override = os.environ.get(env)
    return os.path.abspath(os.path.expanduser(override)) if override else os.path.join(home(), name)

def library_path():        return _p("SCHEMING_LIBRARY", "library.json")
def state_path():          return _p("SCHEMING_BANDIT_STATE", "bandit_state.json")
def retrieval_log_path():  return _p("SCHEMING_RETRIEVAL_LOG", "retrieval_log.jsonl")
def recalls_path():        return _p("SCHEMING_RECALLS", "recalls.jsonl")
def selections_path():     return _p("SCHEMING_SELECTIONS", "selections.jsonl")
def reactions_path():      return _p("SCHEMING_REACTIONS", "reactions.jsonl")
def bandit_updates_path(): return _p("SCHEMING_BANDIT_UPDATES", "bandit_updates.jsonl")
def catchup_runs_path():   return _p("SCHEMING_CATCHUP_RUNS", "catchup_runs.jsonl")
def library_events_path(): return _p("SCHEMING_LIBRARY_EVENTS", "library_events.jsonl")
def assessed_path():       return _p("SCHEMING_ASSESSED", "assessed_sessions.json")

# every log file's env override, for tests to clear so nothing escapes SCHEMING_HOME
LOG_ENV_OVERRIDES = ("SCHEMING_LIBRARY", "SCHEMING_BANDIT_STATE", "SCHEMING_RETRIEVAL_LOG",
                     "SCHEMING_RECALLS", "SCHEMING_SELECTIONS", "SCHEMING_REACTIONS",
                     "SCHEMING_BANDIT_UPDATES", "SCHEMING_CATCHUP_RUNS", "SCHEMING_LIBRARY_EVENTS",
                     "SCHEMING_ASSESSED")

@contextlib.contextmanager
def isolated_home(home, **extra_env):
    """Run with SCHEMING_HOME=home and every telemetry-file env override cleared, so all
    scheming reads/writes land under `home` and nothing escapes to the real SCHEMING_HOME.
    `extra_env` sets extra vars (a value of None clears one). Restores the prior
    environment on exit. This is the ONE place test isolation lives — the
    --selftests share it instead of hand-rolling (and drifting) their own env lists."""
    keys = ("SCHEMING_HOME",) + LOG_ENV_OVERRIDES + tuple(extra_env)
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["SCHEMING_HOME"] = home
        for k in LOG_ENV_OVERRIDES:
            os.environ.pop(k, None)
        for k, v in extra_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield home
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

def claude_projects_dir():
    """Where Claude Code persists session transcripts. Override CLAUDE_PROJECTS_DIR."""
    d = os.environ.get("CLAUDE_PROJECTS_DIR")
    return os.path.abspath(os.path.expanduser(d)) if d else \
        os.path.join(os.path.expanduser("~"), ".claude", "projects")

def claude_settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

# ---------------- library IO -------------------------------------------------

def empty_library():
    """The skeleton a fresh install starts from. No entries — the user mines
    their own. Counts/meta mirror the library's documented shape."""
    return {
        "meta": {"schema": "scheming/1", "source": "empty (fresh install)",
                 "counts": {"entries": 0, "subworkflows": 0,
                            "ingested_workflows": 0, "goal_groups": 0},
                 "refinement_passes": []},
        "entries": [], "subworkflows": [], "ingested_workflows": [], "goal_groups": [],
    }

def load_library(create=True):
    """Load the library dict; create an empty one on disk if missing (create=True).
    Always returns a dict with the four collection keys present."""
    path = library_path()
    if not os.path.exists(path):
        if not create:
            return empty_library()
        ensure_home()
        save_json_atomic(path, empty_library())
    lib = _read_json(path)
    for k in ("entries", "subworkflows", "ingested_workflows", "goal_groups"):
        lib.setdefault(k, [])
    lib.setdefault("meta", {})
    return lib

def save_library(lib):
    """Persist the library, refreshing meta.counts. Single writer (mine/refine time)."""
    lib.setdefault("meta", {})["counts"] = {
        "entries": len(lib.get("entries", [])),
        "subworkflows": len(lib.get("subworkflows", [])),
        "ingested_workflows": len(lib.get("ingested_workflows", [])),
        "goal_groups": len(lib.get("goal_groups", [])),
    }
    ensure_home()
    save_json_atomic(library_path(), lib)

# ---------------- bandit prior (shared by retrieval + feedback) --------------

def prior(item, kind):
    """Beta(a, b) seed for a bandit arm. This is a property of the *library item*,
    not of any one component, so retrieval and the feedback catch-up both call it
    — a first-touched arm then gets the same prior no matter which path writes it
    first (catch-up is usually first, being the automatic path)."""
    tags = item.get("tags") or {}
    if tags.get("obviousness") == "high":
        return [1.0, 4.0]                                     # skeptical
    if kind == "subworkflow":
        return [1.0 + len(item.get("composes_parents") or []), 2.0]   # trusted
    return [2.0, 2.0]                                         # neutral


def add_cost(st, cost):
    """Fold one observed cost into an arm-state dict's running mean, in place.
    Shared by retrieval's manual --feedback and the feedback catch-up so the
    second (efficiency) objective accumulates identically on either path."""
    cn, cm = st.get("cost_n", 0), st.get("cost_mean", 0.0)
    st["cost_n"] = cn + 1
    st["cost_mean"] = (cm * cn + float(cost)) / (cn + 1)
    return st


def log_bandit_update(arm_id, before, after, source, session=None,
                      reaction=None, cost_sample=None, prior=None):
    """Append one bandit_updates.jsonl row snapshotting an arm's a/b/n/cost_mean/
    cost_n before -> after. This is the append-only REPLAY LEDGER for the
    destructive bandit_state aggregate — call it from EVERY state mutation site
    (catch-up + the manual --feedback CLI) so the posterior trajectory, double-counts,
    out-of-order folds, and prior mis-seeding stay reconstructable from zero.
    Non-sensitive (numbers + ids only)."""
    def g(d, k):
        return (d or {}).get(k)
    emit("bandit_updates", {   # telemetry sink: routed + best-effort
        "arm_id": arm_id, "session": session, "source": source,
        "reaction": reaction, "prior_seeded": prior is not None, "prior": prior,
        "cost_sample": cost_sample,
        "a_before": g(before, "a"), "b_before": g(before, "b"), "n_before": g(before, "n"),
        "cost_mean_before": g(before, "cost_mean"), "cost_n_before": g(before, "cost_n"),
        "a_after": g(after, "a"), "b_after": g(after, "b"), "n_after": g(after, "n"),
        "cost_mean_after": g(after, "cost_mean"), "cost_n_after": g(after, "cost_n"),
    })


# ---------------- generic JSON / JSONL IO ------------------------------------

def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_json(path, default=None):
    """Read a JSON file, or return default if it does not exist."""
    if not os.path.exists(path):
        return {} if default is None else default
    return _read_json(path)

def save_json_atomic(path, obj):
    """Atomic write: temp file in the same dir + os.replace (atomic on POSIX and
    Windows). A concurrent reader never sees a half-written file; a crash
    mid-write leaves the prior file intact. This is the ONLY safe way to write a
    mutable state file (e.g. bandit_state.json)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

def append_jsonl(path, record):
    """Append one JSON record as a line. Append-only logs collide harmlessly at
    scheming's volume (no locking needed at this scale)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl_safe(path, record):
    """append_jsonl that NEVER raises — for pure-telemetry writes on a path that
    must degrade gracefully rather than crash: the read-only recall hot path and
    the SessionStart hook. A full disk / unwritable SCHEMING_HOME then costs a log line,
    not the user's result. (Telemetry must not break the loop.)"""
    try:
        append_jsonl(path, record)
    except Exception as e:
        try:
            print(f"[scheming] telemetry write skipped ({os.path.basename(path)}): {e!r}", file=sys.stderr)
        except Exception:
            pass

# The one telemetry sink: every event name maps to its log file here — call sites
# name the EVENT, never a path.
_TELEMETRY_LOGS = {
    "recalls": recalls_path, "selections": selections_path, "reactions": reactions_path,
    "bandit_updates": bandit_updates_path, "catchup_runs": catchup_runs_path,
    "library_events": library_events_path, "retrieval_log": retrieval_log_path,
}

def emit(log, record):
    """The single telemetry sink. Route `record` to the named append-only log,
    stamp `ts` if absent, append best-effort (never raises). This is the ONE place
    telemetry routing + timestamping + crash-safety live, so instrumentation stays
    a one-liner and can't accidentally skip the best-effort guard. `log` is a key
    of `_TELEMETRY_LOGS`."""
    record.setdefault("ts", time.time())
    append_jsonl_safe(_TELEMETRY_LOGS[log](), record)

def read_jsonl(path):
    """Yield records from a JSONL file (empty if missing). Skips blank lines AND
    malformed ones. Append-only logs are lock-free,
    so a crash or interleaved append can leave one corrupt/partial line — a reader
    must skip it, not abort. Without this, a single bad line in retrieval_log
    would silently and permanently kill the feedback loop."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue   # corrupt/partial line — skip, keep the rest


def _selftest():
    # exercise every path helper + atomic IO in an isolated SCHEMING_HOME
    import shutil
    tmp = tempfile.mkdtemp(prefix="scheming_core_test_")
    try:
        with isolated_home(tmp):
            assert home() == os.path.abspath(tmp)
            lib = load_library()                  # creates empty on disk
            assert os.path.exists(library_path())
            assert lib["entries"] == [] and lib["goal_groups"] == []
            lib["entries"].append({"idx": 1, "goal": "x"})
            save_library(lib)
            assert load_library()["meta"]["counts"]["entries"] == 1
            save_json_atomic(state_path(), {"1": {"a": 2, "b": 2, "n": 0}})
            assert load_json(state_path())["1"]["a"] == 2
            append_jsonl(retrieval_log_path(), {"session": "s", "entry_id": "1"})
            append_jsonl(retrieval_log_path(), {"session": "s2", "entry_id": "1"})
            assert len(list(read_jsonl(retrieval_log_path()))) == 2
            # a corrupt/partial line must be skipped, not abort the read
            with open(retrieval_log_path(), "a", encoding="utf-8") as f:
                f.write("{ this is not valid json\n")
            append_jsonl(retrieval_log_path(), {"session": "s3", "entry_id": "1"})
            assert len(list(read_jsonl(retrieval_log_path()))) == 3, "corrupt line not skipped"
            assert list(read_jsonl(reactions_path())) == []   # missing → empty
            # emit() sink routes + stamps ts + is best-effort
            emit("reactions", {"entry_id": "1", "reaction": "neutral"})
            rx = list(read_jsonl(reactions_path()))
            assert len(rx) == 1 and rx[0]["entry_id"] == "1" and "ts" in rx[0], rx
            # bandit_updates replay ledger row snapshots before -> after (via emit)
            log_bandit_update("7", {"a": 2, "b": 2, "n": 0}, {"a": 3, "b": 2, "n": 1},
                              source="test", session="s", reaction="accepted")
            upd = list(read_jsonl(bandit_updates_path()))
            assert len(upd) == 1 and upd[0]["a_before"] == 2 and upd[0]["a_after"] == 3, upd
            print("scheming_core selftest ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    _selftest()
