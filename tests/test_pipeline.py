#!/usr/bin/env python3
"""End-to-end pipeline integration test — the cross-component seams the isolated
per-component selftests can't cover. Runs each component as its REAL CLI against
one shared, isolated SCHEMING_HOME:

  setup -> seed a mined library -> groups -> search (goal-group surfaces)
        -> --full read logs the "used" signal -> catchup labels the reaction
        (prefilter only, no real `claude`) -> the bandit learns -> idempotent.

No real user state, no real `claude` (SCHEMING_LABEL_NO_MODEL=1). Stdlib only.
"""
import json, os, shutil, subprocess, sys, tempfile

LIB = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
sys.path.insert(0, LIB)
import scheming_core          # for LOG_ENV_OVERRIDES (single source of truth)


def main():
    tmp = tempfile.mkdtemp(prefix="scheming_pipeline_")
    projects = os.path.join(tmp, "projects", "C--proj")
    os.makedirs(projects)
    home = os.path.join(tmp, "home")
    settings = os.path.join(tmp, "settings.json")
    env = {**os.environ, "SCHEMING_HOME": home,
           "CLAUDE_PROJECTS_DIR": os.path.join(tmp, "projects"),
           "SCHEMING_LABEL_NO_MODEL": "1"}
    for k in scheming_core.LOG_ENV_OVERRIDES:      # keep every log inside the temp home
        env.pop(k, None)

    def run(script, *args, session=None):
        e = dict(env)
        if session:
            e["SCHEMING_SESSION_ID"] = session
        p = subprocess.run([sys.executable, os.path.join(LIB, script), *args],
                           capture_output=True, text=True, env=e)
        assert p.returncode == 0, f"{script} {args} failed: {p.stderr}"
        return p

    libpath = os.path.join(home, "library.json")
    try:
        # 1. setup (consented): creates home + empty library, raises retention
        run("setup.py", "--yes", "--settings", settings)
        assert json.load(open(settings))["cleanupPeriodDays"] >= 3650
        assert os.path.exists(libpath), "setup did not create the library"

        # 2. seed a mined library (what /scheming-mine appends): two disagreeing siblings
        #    + a subworkflow promoted from them
        os.environ.update(env)          # so the in-process scheming_core resolves this home
        lib = scheming_core.load_library()
        lib["entries"] = [
            {"idx": 100, "goal": "free a port bound by a stale dev server",
             "trigger": "EADDRINUSE port already in use", "cross_ref": ["101"],
             "steps": [{"do": "kill the pid holding the port"}], "status": "active"},
            {"idx": 101, "goal": "restart the dev server on a fresh port",
             "trigger": "port already in use choose a new port", "cross_ref": ["100"],
             "steps": [{"do": "start on an alternate port"}], "status": "active"},
        ]
        lib["subworkflows"] = [
            {"id": "sw-port", "goal": "free the port then restart the server",
             "composes_parents": ["100", "101"],
             "steps": [{"do": "kill then restart"}], "status": "active"}]
        scheming_core.save_library(lib)

        # 3. groups: builds goal_groups from the links, deterministically
        run("groups.py")
        gl = json.load(open(libpath, encoding="utf-8"))
        assert gl["goal_groups"], "groups.py produced no goal_groups"

        # 4. search: the query should surface a GOAL-GROUP (competing solutions).
        #    Same session as the later --full (in production both are one live session)
        #    so the selection and the use share a session for the reaction join.
        session = "pipesess"
        p = run("search.py", "port", "already", "in", "use", session=session)
        assert "GOAL-GROUP" in p.stdout, f"expected a goal-group:\n{p.stdout}"

        # 5. a --full read logs the "used" signal for a specific session.
        #    The reaction must POST-DATE the serve (search logs ts=time.time());
        #    a far-future timestamp guarantees it lands after "now" on the same
        #    epoch clock catchup compares against.
        session = "pipesess"
        with open(os.path.join(projects, session + ".jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z",
                    "message": {"role": "assistant", "usage": {"output_tokens": 500}}}) + "\n")
            f.write(json.dumps({"type": "user", "timestamp": "2099-01-01T00:01:00.000Z",
                    "message": {"role": "user", "content": "no, that's wrong, revert it"}}) + "\n")
        run("search.py", "--full", "100", session=session)
        def rows(name):
            p = os.path.join(home, name)
            return [json.loads(l) for l in open(p, encoding="utf-8")] if os.path.exists(p) else []
        rlog = rows("retrieval_log.jsonl")
        assert any(r["entry_id"] == "100" and r["session"] == session for r in rlog), "retrieval not logged"

        # telemetry: the recall (query) and the head selection were both logged and join
        rc = rows("recalls.jsonl")
        assert any(r["query"] == "port already in use" and not r["abstained"] for r in rc), rc
        qids = {r["query_id"] for r in rows("selections.jsonl")} & {r["query_id"] for r in rc}
        assert qids, "selections do not join to any recall via query_id"

        # 6. catchup: labels the complaint (prefilter, b += 1) AND captures cost from
        #    the assistant usage turn (500 output tokens) — both objectives flow.
        run("catchup.py")
        state = json.load(open(os.path.join(home, "bandit_state.json"), encoding="utf-8"))
        assert "100" in state, f"arm 100 not updated: {state}"
        assert state["100"] == {"a": 2.0, "b": 3.0, "n": 1, "cost_mean": 500.0, "cost_n": 1}, \
            state["100"]   # neutral prior + complaint (b+1) + cost captured from usage
        # reactions.jsonl (replaces complaints): the reaction row carries the raw cost
        # AND resolves the query_id/group_id from the selection that produced the lead
        rx = [r for r in rows("reactions.jsonl") if r["entry_id"] == "100"]
        assert rx and rx[0]["reaction"] == "complaint" and rx[0]["cost"] == 500, rx
        # end-to-end join resolved: the reaction points back to the recall + group that served it
        assert rx[0]["query_id"] in qids and rx[0]["group_id"], rx
        # bandit_updates replay ledger recorded the catch-up mutation
        assert any(u["arm_id"] == "100" and u["source"] == "catchup" for u in rows("bandit_updates.jsonl"))

        # 7. idempotent: a second catch-up changes nothing
        run("catchup.py")
        state2 = json.load(open(os.path.join(home, "bandit_state.json"), encoding="utf-8"))
        assert state2 == state, f"catchup not idempotent: {state2}"

        print("pipeline integration ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
