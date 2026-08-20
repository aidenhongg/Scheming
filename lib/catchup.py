#!/usr/bin/env python3
"""scheming feedback catch-up — the SessionStart orchestrator that closes the loop.

Runs at the start of every session (registered by hooks/hooks.json). It reads the
retrieval log, finds sessions not yet assessed, locates each session's persisted
transcript, extracts the user turns that FOLLOWED each served entry, labels the
reaction (label.py), and applies the consumers in one atomic bandit_state write:
  (i)   success update (accepted -> a+=1, complaint -> b+=1, neutral -> no move) +
        cost fold from the transcript usage (independent of the reaction), logged
        to the bandit_updates.jsonl replay ledger
  (ii)  a reactions.jsonl row for EVERY reaction (not just complaints) carrying the
        raw cost + labeler health; correction is verbatim on complaint rows only
        (correction-mining filters reaction==complaint)
  (iii) catchup_runs.jsonl heartbeat + per-session summary
then marks the session assessed.

Design guarantees:
  * Reentrancy-safe: exits immediately if SCHEMING_LABELER_RUNNING is set (a claude -p
    spawned by the labeler would otherwise fire SessionStart -> infinite recursion).
  * Idempotent: the assessed-sessions ledger means a re-run does nothing new.
  * Crash-safe: assessed is written per-session AFTER its consumers, so a messy
    exit loses at most the one in-flight session (contract-accepted).
  * Never fatal: one bad session cannot abort the rest, and the process ALWAYS
    exits 0 — a feedback failure must never surface as a hook error.

Stdlib only.
"""
import argparse, datetime, glob, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # make lib/ importable
import scheming_core          # noqa: E402  paths + atomic IO + shared bandit prior
import label             # noqa: E402  the reaction labeler


# ---------------- transcript reading -----------------------------------------

def _epoch(ts_iso):
    """ISO-8601 (e.g. '2026-07-28T23:47:06.305Z') -> epoch seconds, or None.
    A naive (offset-less) timestamp is read as UTC — never local time — so it
    stays comparable with the retrieval-log's UTC epoch `ts`."""
    try:
        dt = datetime.datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _human_turn(rec):
    """(epoch, text) for a genuine human turn, else None. Tool-result 'user'
    records (content is a list of tool_result blocks) are NOT human turns."""
    if rec.get("type") != "user":
        return None
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None  # tool output injected as a user message, not the person
        text = " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    else:
        return None
    if not text:
        return None
    return _epoch(rec.get("timestamp")), text


def _find_transcript(session):
    """Locate a session's transcript under claude_projects_dir() by filename
    (Claude Code names each transcript '<session_id>.jsonl'). None if absent."""
    base = scheming_core.claude_projects_dir()
    hits = glob.glob(os.path.join(base, "**", session + ".jsonl"), recursive=True)
    return hits[0] if hits else None


def _human_turns(path):
    """All human turns in a transcript, in file order: [(epoch|None, text), ...]."""
    out = []
    for rec in scheming_core.read_jsonl(path):
        t = _human_turn(rec)
        if t:
            out.append(t)
    return out


def _following(turns, since):
    """Texts of the human turns at/after epoch `since` (the moment the entry was
    served). If either timestamp is unknown, fall back to keeping the turn — a
    superset is safe (the labeler tolerates extra context)."""
    return [text for ep, text in turns
            if since is None or ep is None or ep >= since]


def _assistant_usage(path):
    """[(epoch, output_tokens)] for assistant turns that report token usage — the
    deterministic cost signal (captured from the session usage, no model call).
    Missing/odd usage is simply skipped."""
    out = []
    for rec in scheming_core.read_jsonl(path):
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        u = (rec.get("message") or {}).get("usage") or {}
        ot = u.get("output_tokens")
        if isinstance(ot, (int, float)) and not isinstance(ot, bool):
            out.append((_epoch(rec.get("timestamp")), ot))
    return out


def _cost_after(usage, since, until):
    """Output tokens of assistant turns in [since, until) — a deterministic proxy
    for the token work spent applying a served solution. None if no usage falls in
    range (the arm stays unpriced -> efficiency neutral, cold-start).
    # ponytail: output-token-in-window proxy; refine the window if a sharper
    # per-solution attribution signal turns out to matter."""
    total, seen = 0, False
    for ep, ot in usage:
        if ep is None:                 # untimed turn: attributable to at most one
            continue                   # window, so skip rather than leak into all
        if since is not None and ep < since:
            continue
        if until is not None and ep >= until:
            continue
        total += ot
        seen = True
    return total if seen else None


# ---------------- per-session processing -------------------------------------

def _entry_index():
    """arm_id -> (item, kind), across every collection. Used both for goal context
    (labeler) and the arm's Beta prior (scheming_core.prior needs the kind). Empty on
    any failure (goal context is optional; the labeler defaults to '(unknown)')."""
    try:
        lib = scheming_core.load_library(create=False)
    except Exception:
        return {}
    idx = {}
    for e in lib.get("entries", []):
        idx[str(e.get("idx"))] = (e, "entry")
    for s in lib.get("subworkflows", []):
        idx[str(s.get("id"))] = (s, "subworkflow")
    for w in lib.get("ingested_workflows", []):
        idx[str(w.get("id"))] = (w, "ingested")
    return idx


def _turns_in_window(usage, since, until):
    """Count assistant turns whose usage was summed into a cost window (telemetry)."""
    c = 0
    for ep, _ in usage:
        if ep is None:
            continue
        if since is not None and ep < since:
            continue
        if until is not None and ep >= until:
            continue
        c += 1
    return c


def _selection_index():
    """{session: [(ts, query_id, group_id, winner, {cand arm_ids})]} sorted by ts,
    from selections.jsonl — lets a reaction be joined back OFFLINE to the query and
    group that produced it. Best-effort: empty if selections is missing/unreadable."""
    idx = {}
    try:
        for r in scheming_core.read_jsonl(scheming_core.selections_path()):
            if not isinstance(r, dict):
                continue
            sess, ts = r.get("session"), r.get("ts")
            if not sess or not isinstance(ts, (int, float)):
                continue
            cands = {str(c.get("arm_id")) for c in (r.get("candidates") or [])
                     if isinstance(c, dict)}
            idx.setdefault(sess, []).append(
                (float(ts), r.get("query_id"), r.get("group_id"), str(r.get("winner")), cands))
    except Exception:
        return {}
    for rows in idx.values():
        rows.sort(key=lambda x: x[0])
    return idx


def _resolve(sel_index, session, entry_id, serve_ts):
    """(query_id, group_id, was_lead) for a use — the latest selection in the same
    session at ts<=serve_ts whose candidate set includes entry_id. (None,None,None)
    if unresolved."""
    best = None
    for ts, qid, gid, winner, cands in sel_index.get(session, []):
        if serve_ts is not None and ts > serve_ts:
            break                         # rows are ts-sorted; nothing later can match
        if entry_id in cands:
            best = (qid, gid, winner)
    if best is None:
        return None, None, None
    qid, gid, winner = best
    return qid, gid, (winner == entry_id)


def _process_session(session, uses, entries, sel_index, dry_run):
    """Label every entry used in `session`, apply the consumers, and emit the
    reactions.jsonl telemetry. `uses` maps entry_id -> earliest serve-epoch.
    Returns a summary dict, or None if the transcript is gone."""
    path = _find_transcript(session)
    if path is None:
        # Transcript gone (pruned, or never persisted). Nothing to label; caller
        # marks assessed so a vanished session isn't retried forever.
        return None
    turns = _human_turns(path)
    usage = _assistant_usage(path)
    serves = sorted({t for t in uses.values() if t is not None})   # for cost windows

    def _until(since):   # next distinct serve after `since` bounds this arm's cost window
        return next((t for t in serves if since is not None and t > since), None)

    report = []
    counts = {"accepted": 0, "complaint": 0, "neutral": 0, "cost_captured": 0}
    for entry_id, since in uses.items():
        item, kind = entries.get(entry_id, ({}, "entry"))
        following = _following(turns, since)
        result = label.classify(item, following)
        reaction = result.get("reaction", "neutral")
        correction = result.get("correction")
        until = _until(since)
        cost = _cost_after(usage, since, until)
        query_id, group_id, was_lead = _resolve(sel_index, session, entry_id, since)
        report.append((entry_id, reaction, len(following)))
        counts[reaction] = counts.get(reaction, 0) + 1
        if cost is not None:
            counts["cost_captured"] += 1
        if dry_run:
            continue
        # Two objectives, ONE atomic whole-state write, logged to the replay ledger.
        # SUCCESS moves only on accepted/complaint (neutral = no signal); COST is
        # recorded whenever usage says what the solution cost, independent of the
        # reaction. A first-touched arm seeds from the item's real prior via prior().
        # One entry's write failure must NOT abort the batch: else the session is
        # left unassessed and RETRIED, re-folding entries already committed before
        # the failure -> double-counted posterior + duplicate telemetry. Isolating
        # per entry converts that into "skip the one bad entry" (the documented
        # in-flight-loss contract). save_json_atomic is the only raiser here (e.g.
        # Windows PermissionError if a concurrent recall has the file open).
        try:
            if reaction in ("accepted", "complaint") or cost is not None:
                state = scheming_core.load_json(scheming_core.state_path(), {})
                before = dict(state.get(entry_id) or {})
                st = dict(before)
                seeded = None
                if reaction in ("accepted", "complaint"):
                    if "a" in st and "b" in st:
                        a, b = st["a"], st["b"]
                    else:
                        a, b = scheming_core.prior(item, kind)
                        seeded = [a, b]
                    st["a"], st["b"] = (a + 1, b) if reaction == "accepted" else (a, b + 1)
                    st["n"] = st.get("n", 0) + 1
                if cost is not None:
                    scheming_core.add_cost(st, cost)           # running cost_mean/cost_n
                state[entry_id] = st
                scheming_core.save_json_atomic(scheming_core.state_path(), state)
                scheming_core.log_bandit_update(entry_id, before, st, source="catchup",
                                           session=session, reaction=reaction,
                                           cost_sample=cost, prior=seeded)  # best-effort
            # reactions.jsonl — ONE durable row per processed entry, EVERY reaction
            # (accepted/complaint/neutral) + raw cost (incl cost=null) + labeler health.
            # correction is verbatim on complaint rows only (single home). Best-effort.
            scheming_core.emit("reactions", {
                "session": session, "entry_id": entry_id, "kind": kind,
                "query_id": query_id, "group_id": group_id, "was_lead": was_lead,
                "reaction": reaction,
                "correction": correction if reaction == "complaint" else None,
                "decided_by": result.get("decided_by"), "model_invoked": result.get("model_invoked"),
                "degraded": result.get("degraded"), "error_reason": result.get("error_reason"),
                "model_cost_usd": result.get("model_cost_usd"),
                "model_duration_ms": result.get("model_duration_ms"),
                "n_following_turns": len(following), "serve_ts": since, "until_ts": until,
                "cost": cost, "n_assistant_turns_in_window": _turns_in_window(usage, since, until),
                "priced": cost is not None,
            })
        except Exception as e:   # a single entry's state write failed — skip it, keep going
            print(f"[scheming-catchup] {session}: entry {entry_id} write failed, skipped: {e!r}",
                  file=sys.stderr)
    return {"found": True, "report": report, "counts": counts}


def _group_uses(records):
    """retrieval_log records -> {session: {entry_id: earliest_serve_epoch}}.
    One observation per (session, entry_id): first use, and we read all turns
    after it (dedupe avoids double-counting an entry served twice in a session)."""
    sessions = {}
    for r in records:
        if not isinstance(r, dict):     # a non-dict JSON line (e.g. `5`) is not a record
            continue
        session, entry_id = r.get("session"), r.get("entry_id")
        if not session or entry_id is None:
            continue
        entry_id = str(entry_id)
        ts = r.get("ts")
        ts = float(ts) if isinstance(ts, (int, float)) else None
        uses = sessions.setdefault(session, {})
        if entry_id not in uses or (ts is not None and
                                    (uses[entry_id] is None or ts < uses[entry_id])):
            uses[entry_id] = ts
    return sessions


# ---------------- orchestration ----------------------------------------------

def _log_catchup(dry_run, row):
    """Emit a catchup_runs telemetry row (no-op under --dry-run). The sink is
    already best-effort + ts-stamping."""
    if not dry_run:
        scheming_core.emit("catchup_runs", row)


def run(dry_run=False, once=False):
    t0 = time.monotonic()
    try:
        records = list(scheming_core.read_jsonl(scheming_core.retrieval_log_path()))
    except Exception as e:
        print(f"[scheming-catchup] cannot read retrieval log: {e!r}", file=sys.stderr)
        records = []
    sessions = _group_uses(records)
    assessed = scheming_core.load_json(scheming_core.assessed_path(), [])
    if not isinstance(assessed, list):
        assessed = []
    assessed_set = set(assessed)
    entries = _entry_index()
    sel_index = _selection_index()                 # for the reaction->query/group join
    processed = errored = missing = already = 0
    for session, uses in sessions.items():
        if session in assessed_set:
            already += 1
            continue
        try:
            summary = _process_session(session, uses, entries, sel_index, dry_run)
        except Exception as e:
            # One bad session must not abort the rest. Leave it UNassessed so a
            # later run retries (usually transient: a locked/partial file).
            print(f"[scheming-catchup] {session}: error, skipping: {e!r}", file=sys.stderr)
            errored += 1
            _log_catchup(dry_run, {"event": "session", "session": session,
                                   "transcript_found": None, "error": repr(e)})
            continue
        if summary is None:
            print(f"[scheming-catchup] {session}: transcript not found; marking assessed",
                  file=sys.stderr)
            missing += 1
            _log_catchup(dry_run, {"event": "session", "session": session,
                                   "transcript_found": False, "error": None})
        else:
            for entry_id, reaction, n in summary["report"]:
                print(f"[scheming-catchup] {session}: entry {entry_id} -> {reaction} "
                      f"({n} following turn(s)){' [dry-run]' if dry_run else ''}",
                      file=sys.stderr)
            c = summary["counts"]
            _log_catchup(dry_run, {"event": "session", "session": session,
                                   "transcript_found": True, "error": None,
                                   "n_entries_processed": len(summary["report"]),
                                   "n_accepted": c["accepted"], "n_complaint": c["complaint"],
                                   "n_neutral": c["neutral"], "n_cost_captured": c["cost_captured"]})
        if not dry_run:
            # Mark assessed AFTER the consumers (crash before this -> retry this one
            # session; contract-accepted "in-flight" loss). Persist per-session.
            assessed.append(session)
            assessed_set.add(session)
            scheming_core.save_json_atomic(scheming_core.assessed_path(), assessed)
        processed += 1
        if once:
            break
    print(f"[scheming-catchup] assessed {processed} session(s)"
          f"{' (dry-run)' if dry_run else ''}", file=sys.stderr)
    # heartbeat: loop liveness (a frozen loop is otherwise identical to a converged one)
    _log_catchup(dry_run, {"event": "run", "duration_ms": round((time.monotonic() - t0) * 1000),
                           "dry_run": dry_run, "reentrancy_skipped": False,
                           "sessions_in_log": len(sessions), "sessions_already_assessed": already,
                           "sessions_processed": processed, "sessions_errored": errored,
                           "sessions_transcript_missing": missing})


def main():
    # Reentrancy guard FIRST — before touching anything.
    if os.environ.get("SCHEMING_LABELER_RUNNING"):
        scheming_core.emit("catchup_runs", {"event": "run", "reentrancy_skipped": True})  # best-effort
        sys.exit(0)
    ap = argparse.ArgumentParser(description="scheming SessionStart feedback catch-up")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify + report but write nothing")
    ap.add_argument("--once", action="store_true",
                    help="process at most one unassessed session (testing)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    try:
        run(dry_run=a.dry_run, once=a.once)
    except Exception as e:  # a feedback failure must never surface as a hook error
        print(f"[scheming-catchup] fatal (ignored): {e!r}", file=sys.stderr)
    sys.exit(0)  # ALWAYS succeed


# ---------------- selftest ---------------------------------------------------

def _selftest():
    import json, shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="scheming_catchup_test_")
    projects = os.path.join(tmp, "projects")
    proj = os.path.join(projects, "C--proj")
    os.makedirs(proj)
    # isolate ALL state + force prefilter-only (no real claude ever runs)
    iso = scheming_core.isolated_home(tmp, CLAUDE_PROJECTS_DIR=projects,
                                 SCHEMING_LABEL_NO_MODEL="1", SCHEMING_LABELER_RUNNING=None)
    iso.__enter__()

    def write_transcript(session, turns):
        # turns: [(iso_ts, text)]; write minimal Claude-Code-shaped JSONL
        p = os.path.join(proj, session + ".jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for ts, text in turns:
                f.write(json.dumps({"type": "user", "timestamp": ts,
                                    "message": {"role": "user", "content": text}}) + "\n")

    try:
        # --- fixtures -------------------------------------------------------
        # a library so _entry_index resolves items + kinds (prior-aware seeding).
        # entries 7/9 are plain (neutral (2,2) prior); 12 is obviousness:high, so
        # its first-touched arm MUST seed skeptical (1,4), not a flat default.
        lib = scheming_core.load_library()
        lib["entries"] = [
            {"idx": 7, "goal": "set up the daemon", "status": "active"},
            {"idx": 9, "goal": "do the thing", "status": "active"},
            {"idx": 12, "goal": "an obvious step", "status": "active",
             "tags": {"obviousness": "high"}},
            {"idx": 30, "goal": "a priced step", "status": "active"},
        ]
        scheming_core.save_library(lib)
        # session A: entry "7" served at t=100; a clear complaint follows at t=200
        write_transcript("sessA", [
            ("2026-01-01T00:00:00.000Z", "please set up the daemon"),      # before serve
            ("2026-01-01T00:03:20.000Z", "no, that's wrong, revert it"),   # after serve
        ])
        serve_a = _epoch("2026-01-01T00:01:40.000Z")  # t=100 between the two turns
        # session B: entry "9" served, a clear acceptance follows
        write_transcript("sessB", [
            ("2026-01-02T00:05:00.000Z", "thanks, that works perfectly"),
        ])
        # session C: obviousness:high entry "12" served, a complaint follows
        write_transcript("sessC", [
            ("2026-01-03T00:05:00.000Z", "no, don't do that, it broke the build"),
        ])
        # session Cost: entry "30" served at t; an assistant turn reports 500 output
        # tokens, then a NEUTRAL human turn. Cost must be captured (from usage) even
        # though the reaction moves no success posterior.
        serve_cost = _epoch("2026-01-05T00:01:00.000Z")
        with open(os.path.join(proj, "sessCost.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-01-05T00:00:00.000Z",
                    "message": {"role": "user", "content": "please set it up"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-01-05T00:02:00.000Z",
                    "message": {"role": "assistant", "usage": {"output_tokens": 500}}}) + "\n")
            f.write(json.dumps({"type": "user", "timestamp": "2026-01-05T00:03:00.000Z",
                    "message": {"role": "user", "content": "ok, i will look at that"}}) + "\n")
        # session BAD: a malformed transcript line — read_jsonl skips it, so this
        # processes to a neutral no-op (0 turns) and never aborts the batch
        with open(os.path.join(proj, "sessBAD.jsonl"), "w", encoding="utf-8") as f:
            f.write("{ this is not valid json\n")

        rlog = scheming_core.retrieval_log_path()
        scheming_core.append_jsonl(rlog, {"session": "sessA", "ts": serve_a, "entry_id": "7"})
        scheming_core.append_jsonl(rlog, {"session": "sessBAD", "ts": 1.0, "entry_id": "1"})
        scheming_core.append_jsonl(rlog, {"session": "sessB", "ts": 0.0, "entry_id": "9"})
        scheming_core.append_jsonl(rlog, {"session": "sessC", "ts": 0.0, "entry_id": "12"})
        scheming_core.append_jsonl(rlog, {"session": "sessCost", "ts": serve_cost, "entry_id": "30"})

        # _cost_after: an untimed turn must NOT leak into a window (it would otherwise
        # be summed into every entry's window in a multi-entry session)
        assert _cost_after([(None, 99), (150.0, 10)], 100.0, 200.0) == 10, "untimed turn leaked"
        assert _cost_after([(None, 99)], 100.0, None) is None, "untimed-only should stay unpriced"

        # --- run 1 ----------------------------------------------------------
        run()
        state = scheming_core.load_json(scheming_core.state_path(), {})
        # complaint -> b bumped from the (2,2) neutral prior; n=1
        assert state.get("7") == {"a": 2.0, "b": 3.0, "n": 1}, state.get("7")
        # acceptance -> a bumped; n=1
        assert state.get("9") == {"a": 3.0, "b": 2.0, "n": 1}, state.get("9")
        # obviousness:high complaint -> seeds SKEPTICAL prior (1,4), b bumped -> (1,5)
        assert state.get("12") == {"a": 1.0, "b": 5.0, "n": 1}, state.get("12")
        # cost captured from usage even on a NEUTRAL reaction (no success move)
        assert state.get("30") == {"cost_mean": 500.0, "cost_n": 1}, state.get("30")
        # reactions.jsonl: ONE row per processed entry — EVERY reaction, not just
        # complaints — with cost + labeler health. Entries: 7(complaint), 9(accepted),
        # 12(complaint), 1(neutral, bad transcript), 30(neutral + cost captured).
        rx = {r["entry_id"]: r for r in scheming_core.read_jsonl(scheming_core.reactions_path())}
        assert set(rx) == {"7", "9", "12", "1", "30"}, sorted(rx)
        assert rx["9"]["reaction"] == "accepted" and rx["9"]["correction"] is None
        assert rx["7"]["reaction"] == "complaint" and rx["7"]["correction"]   # verbatim, complaints only
        assert rx["30"]["reaction"] == "neutral" and rx["30"]["cost"] == 500 and rx["30"]["priced"]
        assert all(k in r for r in rx.values() for k in ("decided_by", "degraded", "error_reason"))
        # bandit_updates.jsonl: the replay ledger got a row per state mutation
        upd = list(scheming_core.read_jsonl(scheming_core.bandit_updates_path()))
        assert {u["arm_id"] for u in upd} == {"7", "9", "12", "30"}, sorted({u["arm_id"] for u in upd})
        assert any(u["arm_id"] == "12" and u["prior_seeded"] and u["prior"] == [1.0, 4.0] for u in upd), upd
        assert any(u["arm_id"] == "30" and u["cost_sample"] == 500 for u in upd), upd
        # catchup_runs.jsonl: a heartbeat + per-session summary rows
        runs = list(scheming_core.read_jsonl(scheming_core.catchup_runs_path()))
        assert any(r.get("event") == "run" and not r.get("reentrancy_skipped") for r in runs), runs
        assert any(r.get("event") == "session" and r.get("session") == "sessB"
                   and r.get("n_accepted") == 1 for r in runs), runs
        # all sessions assessed — a malformed transcript LINE is skipped by
        # read_jsonl, so sessBAD processes to a neutral no-op and is marked assessed
        # (not retried forever), while still never aborting the batch
        assessed = set(scheming_core.load_json(scheming_core.assessed_path(), []))
        assert {"sessA", "sessB", "sessC", "sessBAD", "sessCost"} <= assessed, assessed

        # --- run 2: idempotent (no new writes) ------------------------------
        run()
        state2 = scheming_core.load_json(scheming_core.state_path(), {})
        assert state2 == state, f"second run changed state: {state2}"
        rx2 = list(scheming_core.read_jsonl(scheming_core.reactions_path()))
        assert len(rx2) == len(rx), f"second run re-logged reactions: {rx2}"

        # --- per-entry write failure is ISOLATED: the session still completes and
        #     is marked assessed (no infinite retry -> no double-count of the
        #     entries that DID commit) -------------------------------------------
        write_transcript("sessFail", [("2027-01-01T00:00:00.000Z", "thanks, works")])
        scheming_core.append_jsonl(rlog, {"session": "sessFail",
                                     "ts": _epoch("2027-01-01T00:00:00.000Z") - 100, "entry_id": "9"})
        real_save = scheming_core.save_json_atomic
        def _failing_save(path, obj):        # fail only the state write, not the ledger
            if path == scheming_core.state_path():
                raise OSError("simulated bandit_state lock")
            return real_save(path, obj)
        scheming_core.save_json_atomic = _failing_save
        try:
            run()
        finally:
            scheming_core.save_json_atomic = real_save
        assert "sessFail" in set(scheming_core.load_json(scheming_core.assessed_path(), [])), \
            "a per-entry write failure must not pin the session in retry"
        after = scheming_core.load_json(scheming_core.state_path(), {})
        assert after["9"] == state["9"], "the failed entry must not have been folded"  # no double-count

        # --- reentrancy guard: exits 0 immediately when the flag is set -----
        import subprocess
        child = {**os.environ, "SCHEMING_LABELER_RUNNING": "1"}
        rc = subprocess.run([sys.executable, os.path.abspath(__file__)],
                            env=child, capture_output=True, text=True)
        assert rc.returncode == 0 and not rc.stdout.strip(), (rc.returncode, rc.stdout)

        # --- transcript-not-found marks assessed (no infinite retry) --------
        scheming_core.append_jsonl(rlog, {"session": "ghost", "ts": 1.0, "entry_id": "3"})
        run()
        assert "ghost" in set(scheming_core.load_json(scheming_core.assessed_path(), [])), \
            "vanished-transcript session should be marked assessed"

        print("catchup selftest ok")
    finally:
        iso.__exit__(None, None, None)     # restore env
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
