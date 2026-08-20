#!/usr/bin/env python3
"""Goal-centric retrieval over the scheming mined-workflow library. Stdlib only.

Architecture (matches the 3-part design):
  1. Mine + index: entries carry goal/trigger; BM25 finds relevant GOALS.
  2. Competing solutions: goal_groups (built at storage from conflicts /
     corroboration / sibling / subworkflow families) hold the alternative
     workflows that target the same goal.
  3. Improve over time: within a matched goal-group, a two-objective bandit picks
     which solution to lead with. A Thompson sample of *success* is multiplied by
     a cost *efficiency* (cheaper-within-group -> closer to 1) — exploit the
     proven-and-cheap one, explore the rest.
BM25 orders goals (query-specific, no distortion). The bandit only chooses
AMONG interchangeable same-goal solutions (valid explore/exploit) and gates
(suppress broadly-bad members; abstain when nothing clears the bar).

Paths and IO come entirely from scheming_core (siblings in lib/, so `import scheming_core`
works when this script is launched by path). Nothing is hardcoded."""
import argparse, json, math, os, random, re, sys, time, uuid
from collections import Counter, namedtuple
import scheming_core

try:  # UTF-8 stdout so a cp1252 console can't crash a print on non-ASCII goals
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

INJECT_BAR = float(os.environ.get("SCHEMING_INJECT_BAR", "2.0"))
SUPPRESS_MIN_OBS = int(os.environ.get("SCHEMING_SUPPRESS_MIN_OBS", "5"))
SUPPRESS_MEAN = float(os.environ.get("SCHEMING_SUPPRESS_MEAN", "0.25"))
COST_MIN_N = int(os.environ.get("SCHEMING_COST_MIN_N", "2"))  # cost samples before an arm is priced

def _session():
    """The real session id — SCHEMING_SESSION_ID override, else Claude Code's exported
    CLAUDE_CODE_SESSION_ID (which is the transcript filename stem), else 'unknown'.
    This is what lets the feedback loop attribute a reaction to its session."""
    return (os.environ.get("SCHEMING_SESSION_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown")

def toks(s):
    return re.findall(r"[a-z0-9_.\-]+", str(s).lower())

def _flat(seq):
    """Join a list whose items are strings OR dicts into one searchable blob.
    Ingested `phases` preserve their runtime shape ({title,detail} or {id,do}),
    so a plain str-join would crash on the dicts (phases may be
    list[dict|str])."""
    out = []
    for x in seq or []:
        if isinstance(x, dict):
            out.extend(str(v) for v in x.values() if isinstance(v, (str, int, float)))
        elif x is not None:
            out.append(str(x))
    return " ".join(out)

def fields(item, kind):
    out = [(3, item.get("goal", "")), (3, item.get("trigger", ""))]
    out.append((2, _flat(item.get("invariants"))))
    steps = item.get("steps") or []
    out.append((1, " ".join(
        f"{s.get('do','')} {s.get('check','')} {s.get('on_fail','')}" for s in steps)))
    if kind == "ingested":
        out.append((2, _flat(item.get("phases"))))
        out.append((2, item.get("id", "")))
    return out

def load():
    lib = scheming_core.load_library(create=False)   # read-only: never writes an empty library on recall
    items = [("entry", str(e["idx"]), e) for e in lib.get("entries", [])]
    items += [("subworkflow", s["id"], s) for s in lib.get("subworkflows", [])]
    items += [("ingested", w["id"], w) for w in lib.get("ingested_workflows", [])]
    return items

def load_groups():
    lib = scheming_core.load_library(create=False)
    groups = {g["group_id"]: g for g in lib.get("goal_groups", [])}
    by_member = {}
    for g in groups.values():
        for m in g["members"]:
            by_member.setdefault(m, []).append(g["group_id"])
    return groups, by_member

# ---------------- bandit (per-member usefulness + cost) ----------------

# One scored candidate of the two-objective head — named, not a positional tuple,
# so adding a field never ripples through every unpack site.
Scored = namedtuple("Scored", "utility sample cost arm a b")

def prior(item, kind):
    return scheming_core.prior(item, kind)   # shared with feedback catch-up (single source of truth)

def load_state():
    return scheming_core.load_json(scheming_core.state_path(), {})

def save_state(s):
    scheming_core.save_json_atomic(scheming_core.state_path(), s)

def posterior(state, iid, item, kind):
    st = state.get(iid)
    if st and "a" in st:          # an arm may carry only cost fields before any success feedback
        return st["a"], st["b"]
    return prior(item, kind)

def suppressed(state, iid):
    st = state.get(iid)
    if not st or "a" not in st:
        return False
    a, b, n = st["a"], st["b"], st.get("n", 0)
    return n >= SUPPRESS_MIN_OBS and a / (a + b) < SUPPRESS_MEAN

def available(item):
    # held-* items are quarantined pending an owner decision — never surface them
    return not str(item.get("status", "active")).startswith("held")

def arm_cost(state, iid):
    """The arm's running cost_mean, or None until it has >= COST_MIN_N samples
    (cold-start: unpriced arms are treated as neutral, never penalized)."""
    st = state.get(iid) or {}
    if st.get("cost_n", 0) >= COST_MIN_N:
        cm = st.get("cost_mean")
        if cm is not None and cm > 0:
            return cm
    return None

def efficiency(cm, gmin):
    """Normalize a cost to (0,1] WITHIN the goal-group: cheapest member -> 1.0,
    dearer -> smaller. Neutral (1.0) when this arm is unpriced or no member of
    the group has cost data yet — so with no costs the head is success-only."""
    if cm is None or gmin is None or cm <= 0 or gmin <= 0:
        return 1.0
    return gmin / cm

def _add_cost(st, cost):
    scheming_core.add_cost(st, cost)   # shared running mean (same fold as feedback catch-up)
    # (cost is captured automatically from transcript usage in catch-up; there is
    #  no manual cost CLI — --feedback ID OUTCOME [COST] covers the manual path.)

# ---------------- retrieval ----------------

def bm25(query, k1=1.5, b=0.75):
    docs = []
    for kind, iid, item in load():
        tf = Counter()
        dl = 0
        for w, text in fields(item, kind):
            for t in toks(text):
                tf[t] += w
                dl += w
        docs.append((kind, iid, item, tf, dl))
    N = len(docs)
    avgdl = sum(d[4] for d in docs) / max(N, 1)
    q = set(toks(query))
    df = {t: sum(1 for d in docs if t in d[3]) for t in q}
    idf = {t: math.log((N - df[t] + 0.5) / (df[t] + 0.5) + 1) for t in q}
    hits = []
    for kind, iid, item, tf, dl in docs:
        score = sum(
            idf[t] * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * dl / avgdl))
            for t in q if tf[t])
        if score > 0:
            hits.append((score, kind, iid, item))
    hits.sort(key=lambda h: -h[0])
    return hits

def _record(state, iid, item, kind):
    a, b = posterior(state, iid, item, kind)
    return (a / (a + b), a, b, state.get(iid, {}).get("n", 0))

def search(query, n, use_bandit=True):
    """Return a list of result dicts. Goals are ordered by BM25 relevance;
    within a goal-group the lead solution is chosen by the two-objective head
    (success sample x cost efficiency)."""
    query_id = uuid.uuid4().hex                  # the join root for this recall's telemetry
    hits = bm25(query)
    if not use_bandit:
        # raw BM25, flat, no goal-groups/bandit. The learned suppress gate is
        # bandit machinery so it stays off here, but held-* quarantine is an
        # absolute invariant ("held items never surface in retrieval") — filter it.
        out, filtered = [], []
        for s, k, i, it in hits:
            if not available(it):
                filtered.append({"arm_id": i, "reason": "held"})   # gate decision, not dark
                continue
            out.append({"type": "single", "relevance": round(s, 1), "kind": k,
                        "iid": i, "item": it, "rec": None})
            if len(out) >= n:
                break
        _log_recall(query_id, query, n, False, len(hits), out, filtered)
        return out
    score_of = {iid: s for s, k, iid, it in hits}
    meta_of = {iid: (k, it) for s, k, iid, it in hits}
    groups, by_member = load_groups()
    state = load_state()

    def group_relevance(gid):  # goal relevance = total BM25 mass of its members
        return sum(score_of.get(m, 0) for m in groups[gid]["members"])

    def entry(m):
        k2, it2 = meta_of[m]
        return {"iid": m, "kind": k2, "item": it2, "rec": _record(state, m, it2, k2),
                "relevance": round(score_of.get(m, 0), 1), "cost": arm_cost(state, m)}

    # Assign each hit item to ONE goal key: the highest-relevance-mass group it
    # belongs to, else a singleton. An item therefore renders under exactly one
    # goal, so overlapping goal-groups never double-emit a shared member.
    assigned = {}
    for s, kind, iid, item in hits:
        gids = by_member.get(iid, [])            # every such group has relevance>0 (iid hit)
        assigned[iid] = max(gids, key=group_relevance) if gids else "single:" + iid

    # Build one entry per goal key, keeping only VALID candidates (available +
    # not suppressed); drop a key with none. cand is restricted to members
    # ASSIGNED to this key, which is what dedups overlapping groups. Members
    # dropped by the gate are captured in `excluded` for the selections audit.
    seen, staged, filtered = set(), [], []       # staged: (relevance, key, cand|None, excluded)
    for s, kind, iid, item in hits:
        key = assigned[iid]
        if key in seen:
            continue
        seen.add(key)
        if key.startswith("single:"):
            if suppressed(state, iid):           # gate decisions on singletons are not dark
                filtered.append({"arm_id": iid, "reason": "suppressed"})
                continue
            if not available(item):
                filtered.append({"arm_id": iid, "reason": "held"})
                continue
            staged.append((score_of[iid], key, None, []))
        else:
            cand, excluded = [], []
            for m in groups[key]["members"]:
                if assigned.get(m) != key or m not in meta_of:
                    continue                     # not this query's hit, or assigned elsewhere
                it2 = meta_of[m][1]
                if suppressed(state, m):
                    a, b = posterior(state, m, it2, meta_of[m][0])
                    excluded.append({"arm_id": m, "reason": "suppressed", "a": a, "b": b,
                                     "n": (state.get(m) or {}).get("n", 0),
                                     "mean": round(a / (a + b), 3),
                                     "relevance": round(score_of.get(m, 0), 1)})
                elif not available(it2):
                    excluded.append({"arm_id": m, "reason": "held",
                                     "relevance": round(score_of.get(m, 0), 1)})
                else:
                    cand.append(m)
            if cand:
                staged.append((group_relevance(key), key, cand, excluded))

    # BM25 orders GOALS: rank keys by relevance mass, take the top n. No learned
    # signal ever reorders across goals — the bandit only picks WITHIN a group.
    staged.sort(key=lambda x: -x[0])
    results = []
    for relevance, key, cand, excluded in staged[:n]:
        if cand is None:                         # singleton solution
            iid = key[len("single:"):]
            k2, it2 = meta_of[iid]
            a, b = posterior(state, iid, it2, k2)
            scheming_core.emit("selections", {  # a single serve is no longer dark
                "query_id": query_id, "session": _session(),
                "role": "single", "group_id": None, "group_source": None,
                "group_relevance": round(relevance, 1), "winner": iid,
                "candidates": [{"arm_id": iid, "kind": k2, "a": a, "b": b,
                                "n": (state.get(iid) or {}).get("n", 0),
                                "cost_mean": (state.get(iid) or {}).get("cost_mean"),
                                "cost_n": (state.get(iid) or {}).get("cost_n", 0),
                                "priced": arm_cost(state, iid) is not None,
                                "relevance": round(relevance, 1)}], "excluded": []})
            results.append({"type": "single", "relevance": round(relevance, 1),
                            "kind": k2, "iid": iid, "item": it2,
                            "rec": _record(state, iid, it2, k2)})
            continue
        g = groups[key]
        # two-objective head: utility = success_sample x efficiency(cost), normalized
        # within THIS group; a failing arm (success~0) scores ~0 regardless of cost.
        gmin = min([c for c in (arm_cost(state, m) for m in cand) if c is not None] or [None])
        scored = []
        for m in cand:
            k2, it2 = meta_of[m]
            a, b = posterior(state, m, it2, k2)
            sample = random.betavariate(a, b)
            cm = arm_cost(state, m)
            scored.append(Scored(sample * efficiency(cm, gmin), sample, cm, m, a, b))
        scored.sort(key=lambda s: -s.utility)
        # selections.jsonl: the full replayable head decision — every candidate with
        # the (a,b,n)+cost behind its draw, gmin/efficiency/utility, plus the members
        # the gate excluded. One row per group actually served (offline head re-fit).
        cand_rows = [{"arm_id": s.arm, "kind": meta_of[s.arm][0], "a": s.a, "b": s.b,
                      "n": (state.get(s.arm) or {}).get("n", 0),
                      "success_sample": round(s.sample, 4),
                      "cost_mean": (state.get(s.arm) or {}).get("cost_mean"),
                      "cost_n": (state.get(s.arm) or {}).get("cost_n", 0),
                      "priced": s.cost is not None, "gmin": gmin,
                      "efficiency": round(efficiency(s.cost, gmin), 4), "utility": round(s.utility, 4),
                      "relevance": round(score_of.get(s.arm, 0), 1)}
                     for s in scored]
        scheming_core.emit("selections", {
            "query_id": query_id, "session": _session(),
            "role": "group", "group_id": g["group_id"], "group_source": g["source"],
            "group_relevance": round(relevance, 1), "winner": scored[0].arm,
            "candidates": cand_rows, "excluded": excluded})
        results.append({"type": "group", "relevance": round(relevance, 1),
                        "group": {"id": g["group_id"], "label": g["label"], "source": g["source"]},
                        "lead": entry(scored[0].arm), "alts": [entry(s.arm) for s in scored[1:]]})
    _log_recall(query_id, query, n, True, len(hits), results, filtered)
    return results


def _log_recall(query_id, query, n, use_bandit, num_hits, results, filtered_singletons=None):
    """One recalls.jsonl row per recall — including abstains and --no-bandit runs.
    The decision denominator (serve/abstain rate, coverage gaps) and the SINGLE home
    for the raw query text; every downstream row carries only query_id."""
    top = results[0]["relevance"] if results else 0.0
    if not results:
        abstained, reason = True, ("no_match" if num_hits == 0 else "all_filtered")
    elif top < INJECT_BAR:
        abstained, reason = True, "below_bar"
    else:
        abstained, reason = False, "none"
    served = [(r["lead"]["iid"] if r["type"] == "group" else r["iid"]) for r in results]
    scheming_core.emit("recalls", {
        "query_id": query_id, "session": _session(),
        "query": query, "n_requested": n, "use_bandit": use_bandit,
        "num_hits": num_hits, "top_relevance": round(top, 1),
        "abstained": abstained, "abstain_reason": reason,
        "n_groups_served": sum(1 for r in results if r["type"] == "group"),
        "n_singles_served": sum(1 for r in results if r["type"] == "single"),
        "n_results": len(results), "served_ids": served,
        "filtered_singletons": filtered_singletons or []})   # gated singles (suppressed/held)

# ---------------- rendering ----------------

def flags(item):
    tag = (item.get("tags") or {}).get("obviousness")
    return [item.get("status", "active")] + ([f"obviousness:{tag}"] if tag else [])

def one_line(iid, kind, item, rec, rel, cost=None):
    head = f"    {kind} {iid} ({', '.join(flags(item))})"
    if rec:
        head += f"  track-record~{rec[0]:.2f} (n={rec[3]})"
    if cost is not None:
        head += f"  cost~{cost:.2g}"
    if rel is not None:
        head += f"  [rel {rel}]"
    line = head + f"\n      goal: {item.get('goal','')}"
    if item.get("trigger"):
        line += f"\n      trigger: {item['trigger'][:160]}"
    return line

def render(r):
    if r["type"] == "single":
        out = f"[{r['relevance']:>5}] {r['kind']} {r['iid']} ({', '.join(flags(r['item']))})"
        if r["rec"]:
            out += f"  track-record~{r['rec'][0]:.2f} (n={r['rec'][3]})"
        out += f"\n  goal: {r['item'].get('goal','')}"
        if r["item"].get("trigger"):
            out += f"\n  trigger: {r['item']['trigger'][:180]}"
        return out
    g = r["group"]
    ld = r["lead"]
    n_sol = 1 + len(r["alts"])
    out = f"[{r['relevance']:>5}] GOAL-GROUP {g['id']} — {n_sol} competing solutions ({g['source']}: {g['label']})"
    out += "\n  LEAD (bandit-chosen):\n" + one_line(
        ld["iid"], ld["kind"], ld["item"], ld["rec"], ld["relevance"], ld.get("cost"))
    if r["alts"]:
        out += "\n  alternatives (--full <id> to read; --feedback to promote):"
        for a in r["alts"]:
            tr = f"track-record~{a['rec'][0]:.2f}/n{a['rec'][3]}" if a["rec"] else ""
            ct = f" cost~{a['cost']:.2g}" if a.get("cost") is not None else ""
            out += f"\n    {a['kind']} {a['iid']} [rel {a['relevance']}] {tr}{ct}"
    return out

def full(iid):
    sid = str(iid)
    for kind, i, item in load():
        if i == sid:
            # "this was used" signal for the feedback loop — minimal + additive.
            # Stamp the real session (_session) so catch-up can find this session's
            # transcript (<id>.jsonl); without a real id the loop can't attribute
            # the reaction, so it never learns. Give the user the item first, THEN
            # best-effort-log the used signal (a log failure must not deny the read).
            print(json.dumps(item, indent=2, ensure_ascii=False))
            scheming_core.emit("retrieval_log",
                          {"session": _session(), "entry_id": sid, "kind": kind})
            return
    print(f"no item with id {iid}", file=sys.stderr)
    sys.exit(1)

def feedback(iid, outcome, cost=None):
    items = {i: (k, it) for k, i, it in load()}
    if iid not in items:
        print(f"no item with id {iid}", file=sys.stderr)
        sys.exit(1)
    state = load_state()
    kind, item = items[iid]
    a, b = posterior(state, iid, item, kind)
    if outcome in ("helped", "used"):
        a += 1
    elif outcome in ("ignored", "hurt"):
        b += 1
    else:
        print("outcome must be one of: helped, used, ignored, hurt", file=sys.stderr)
        sys.exit(1)
    before = dict(state.get(iid, {}))        # preserve any cost fields already observed
    st = dict(before)
    st["a"], st["b"], st["n"] = a, b, st.get("n", 0) + 1
    if cost is not None:
        _add_cost(st, float(cost))
    state[iid] = st
    save_state(state)
    scheming_core.log_bandit_update(iid, before, st, source="feedback-cli", session=_session(),
                               reaction=outcome, cost_sample=cost)   # audit manual feedback
    _, by_member = load_groups()
    ctx = f" (goal-groups {by_member.get(iid)})" if by_member.get(iid) else ""
    cnote = f", cost~{st['cost_mean']:.2g}/n{st['cost_n']}" if "cost_mean" in st else ""
    print(f"{iid}: Beta({a:g}, {b:g}) after '{outcome}', {st['n']} obs{cnote}{ctx}")

def stats():
    state = load_state()
    groups, by_member = load_groups()
    rows = []
    for kind, iid, item in load():
        a, b = posterior(state, iid, item, kind)
        rows.append((a / (a + b), state.get(iid, {}).get("n", 0), arm_cost(state, iid), iid, kind))
    rows.sort(key=lambda r: -r[0])
    priced = sum(1 for r in rows if r[2] is not None)
    print(f"{len(groups)} goal-groups; observations: {sum(r[1] for r in rows)} "
          f"across {sum(1 for r in rows if r[1])} items; {priced} priced")
    print("top track records:")
    for m, n, cm, iid, kind in rows[:6]:
        ct = f" cost~{cm:.2g}" if cm is not None else ""
        print(f"  {m:.2f} (n={n}){ct} {kind} {iid}")

# ---------------- self-test (self-contained: synthetic library, isolated SCHEMING_HOME) ----------------

def _fixture_library():
    """A tiny synthetic library: singles + two multi-member goal-groups + one
    ingested workflow. No dependency on any shipped/real library."""
    return {
        "meta": {},
        "entries": [
            {"idx": 1, "goal": "restart a bound TCP port by killing the holder",
             "trigger": "port already in use bound restart address",
             "steps": [{"do": "find and kill the pid holding the port"}]},
            {"idx": 2, "goal": "free a bound TCP port via lsof then kill",
             "trigger": "port already in use bound restart lsof pid",
             "steps": [{"do": "lsof -i:PORT then kill the pid"}]},
            {"idx": 3, "goal": "recover deleted pyc bytecode from the marshal cache",
             "trigger": "pyc bytecode deleted marshal recover cache",
             "steps": [{"do": "un-marshal the cached code object"}]},
            {"idx": 4, "goal": "revive a hung background agent by checking mtime",
             "trigger": "background agent hung mtime liveness stalled",
             "steps": [{"do": "compare log mtime to now"}]},
            {"idx": 5, "goal": "revive a hung background agent by restarting the daemon",
             "trigger": "background agent hung restart daemon stalled",
             "steps": [{"do": "restart the daemon"}]},
        ],
        "subworkflows": [],
        "ingested_workflows": [
            {"id": "wf-canary-deploy", "goal": "deploy a canary and watch the error rate",
             "trigger": "canary deploy rollout error rate",
             "phases": ["build", "canary", "watch", "promote"],
             "source": "fixture", "script_path": "n/a"},
        ],
        "goal_groups": [
            {"group_id": "grp-port", "label": "restart a bound port",
             "source": "corroboration", "members": ["1", "2"]},
            {"group_id": "grp-agent", "label": "revive a hung background agent",
             "source": "sibling-conflict", "members": ["4", "5"]},
        ],
    }

def selftest():
    import shutil, tempfile, contextlib, io
    tmp = tempfile.mkdtemp(prefix="scheming_search_test_")
    AGENT = "background agent hung stalled"
    iso = scheming_core.isolated_home(tmp, SCHEMING_SESSION_ID="selftest-sess"); iso.__enter__()
    try:
        scheming_core.save_library(_fixture_library())

        # (a) BM25 surfaces the relevant single for a keyword query
        top = [r["iid"] for r in search("pyc bytecode marshal deleted", 5, use_bandit=False)]
        assert "3" in top, f"bytecode query missed: {top}"

        # (b) a multi-member goal surfaces as a GROUP with alternatives
        res = search("port already in use bound restart", 6)
        grp = [r for r in res if r["type"] == "group"]
        assert grp and grp[0]["alts"], f"expected a goal-group with alts, got {[r['type'] for r in res]}"

        # (c) exploit: hammer one arm's Beta up and it leads (arm 1 kept live, n<5)
        save_state({"2": {"a": 60.0, "b": 1.0, "n": 61}, "1": {"a": 2.0, "b": 8.0, "n": 4}})
        leads = Counter()
        for _ in range(15):
            g2 = [r for r in search("port already in use bound restart", 6) if r["type"] == "group"]
            if g2:
                leads[g2[0]["lead"]["iid"]] += 1
        assert leads.most_common(1)[0][0] == "2", f"exploit failed: {leads}"

        # (d) a cost-CHEAP but losing arm (5) still never leads — multiplicative gate.
        #     arm 5 is the cheapest yet hopeless; n<5 so it is NOT suppressed, just outscored.
        save_state({"4": {"a": 50.0, "b": 1.0, "n": 51, "cost_mean": 2.0, "cost_n": 2},
                    "5": {"a": 1.0, "b": 200.0, "n": 4, "cost_mean": 0.5, "cost_n": 2}})
        leads = Counter()
        for _ in range(30):
            g2 = [r for r in search(AGENT, 6) if r["type"] == "group"]
            if g2:
                leads[g2[0]["lead"]["iid"]] += 1
        assert leads.get("4", 0) == 30 and "5" not in leads, f"cheap loser must never lead: {leads}"

        # (e) efficiency neutral under COST_MIN_N samples, then shifts the winner among
        #     equal-success arms once cost data exists.
        save_state({"4": {"a": 50.0, "b": 1.0, "n": 51},
                    "5": {"a": 50.0, "b": 1.0, "n": 51, "cost_mean": 10.0, "cost_n": 1}})  # 1 < MIN_N
        assert arm_cost(load_state(), "5") is None, "arm with <MIN_N cost samples must read unpriced"
        wins = Counter()
        for _ in range(40):
            g2 = [r for r in search(AGENT, 6) if r["type"] == "group"]
            if g2:
                wins[g2[0]["lead"]["iid"]] += 1
        assert wins["4"] and wins["5"], f"unpriced equal-success arms should each lead: {wins}"
        save_state({"4": {"a": 50.0, "b": 1.0, "n": 51, "cost_mean": 1.0, "cost_n": 2},   # cheaper
                    "5": {"a": 50.0, "b": 1.0, "n": 51, "cost_mean": 10.0, "cost_n": 2}})  # dearer
        wins = Counter()
        for _ in range(30):
            g2 = [r for r in search(AGENT, 6) if r["type"] == "group"]
            if g2:
                wins[g2[0]["lead"]["iid"]] += 1
        assert wins.get("4", 0) == 30 and "5" not in wins, f"cheaper equal-success arm should win once priced: {wins}"

        # (f) abstain: nonsense query returns nothing
        assert not search("qwzx zzzq nonexistent", 3), "should abstain on no match"

        # (g) --full appends a retrieval_log line
        with contextlib.redirect_stdout(io.StringIO()):
            full("3")
        logged = list(scheming_core.read_jsonl(scheming_core.retrieval_log_path()))
        assert any(r.get("entry_id") == "3" and r.get("session") == "selftest-sess" for r in logged), \
            f"retrieval_log missing --full record: {logged}"

        # (h) --no-bandit still filters held-* (absolute invariant); suppression, a
        #     learned gate, legitimately stays a bandit-only concern and isn't tested here
        held = _fixture_library()
        held["entries"][2]["status"] = "held-review"          # idx 3
        scheming_core.save_library(held)
        nb = [r["iid"] for r in search("pyc bytecode marshal deleted", 5, use_bandit=False)]
        assert "3" not in nb, f"--no-bandit surfaced a held item: {nb}"

        # (i) a higher-MASS group outranks a higher-scoring single and survives
        #     n-truncation. Single 10 carries the term twice (higher individual
        #     score) but group grp-one's two members sum to a larger mass.
        crafted = {
            "meta": {}, "subworkflows": [], "ingested_workflows": [],
            "entries": [
                {"idx": 10, "goal": "solo option", "trigger": "qterm qterm"},
                {"idx": 11, "goal": "grp one a", "trigger": "qterm"},
                {"idx": 12, "goal": "grp one b", "trigger": "qterm"},
                {"idx": 20, "goal": "shared solution", "trigger": "sterm sterm sterm"},
                {"idx": 21, "goal": "grp two b", "trigger": "sterm"},
                {"idx": 22, "goal": "grp three b", "trigger": "sterm"},
            ],
            "goal_groups": [
                {"group_id": "grp-one", "label": "one", "source": "corroboration", "members": ["11", "12"]},
                {"group_id": "grp-two", "label": "two", "source": "corroboration", "members": ["20", "21"]},
                {"group_id": "grp-three", "label": "three", "source": "sibling-conflict", "members": ["20", "22"]},
            ],
        }
        scheming_core.save_library(crafted)
        res = search("qterm", 6)
        rels = [r["relevance"] for r in res]
        assert rels == sorted(rels, reverse=True), f"results not sorted by relevance mass: {rels}"
        top1 = search("qterm", 1)
        assert top1 and top1[0]["type"] == "group" and top1[0]["group"]["id"] == "grp-one", \
            f"n=1 dropped the highest-mass goal (would emit the single): {top1}"

        # (j) member 20 is in grp-two AND grp-three (overlapping groups); it must
        #     render under exactly ONE goal, never double-emitted.
        res = search("sterm", 6)
        appears = sum(
            1 for r in res if r["type"] == "group" and
            (r["lead"]["iid"] == "20" or any(a["iid"] == "20" for a in r["alts"])))
        assert appears == 1, f"overlapping member 20 rendered {appears} times (want 1)"

        # (k) query-side telemetry: recalls.jsonl logs EVERY recall incl abstains;
        #     selections.jsonl carries a joinable, replayable head decision.
        for f in (scheming_core.recalls_path(), scheming_core.selections_path()):
            open(f, "w").close()                              # reset for a clean count
        r = search("qterm", 6)                                # one group (grp-one) + single 10
        rc = list(scheming_core.read_jsonl(scheming_core.recalls_path()))
        assert len(rc) == 1 and rc[0]["query"] == "qterm" and not rc[0]["abstained"], rc
        assert rc[0]["session"] == "selftest-sess", rc
        sel = list(scheming_core.read_jsonl(scheming_core.selections_path()))
        qid = rc[0]["query_id"]
        assert all(s["query_id"] == qid for s in sel), "selections must join to the recall"
        grp = [s for s in sel if s["role"] == "group"]
        assert grp and all("a" in c and "utility" in c for c in grp[0]["candidates"]), grp
        assert any(s["role"] == "single" for s in sel), "singleton serve must not be dark"
        # abstain on nonsense STILL logs a recall row (coverage-gap telemetry)
        open(scheming_core.recalls_path(), "w").close()
        search("qwzx zzzq nonexistent", 3)
        ab = list(scheming_core.read_jsonl(scheming_core.recalls_path()))
        assert len(ab) == 1 and ab[0]["abstained"] and ab[0]["abstain_reason"] == "no_match", ab

        print("selftest ok")
    finally:
        iso.__exit__(None, None, None)     # restore env
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*")
    ap.add_argument("-n", type=int, default=5)
    ap.add_argument("--full", help="print one item in full by id (logs a retrieval_log 'used' signal)")
    ap.add_argument("--dump", action="store_true", help="recall backstop: every item's id + goal")
    ap.add_argument("--no-bandit", action="store_true", help="raw BM25, flat, no goal-groups")
    ap.add_argument("--feedback", nargs="+", metavar="ARG",
                    help="ID OUTCOME [COST]: record helped|used|ignored|hurt, optional turn cost")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.feedback:
        fb = a.feedback
        if len(fb) not in (2, 3):
            print("--feedback takes: ID OUTCOME [COST]", file=sys.stderr)
            sys.exit(1)
        feedback(fb[0], fb[1], fb[2] if len(fb) == 3 else None)
    elif a.stats:
        stats()
    elif a.full:
        full(a.full)
    elif a.dump:
        for kind, iid, item in load():
            print(f"{kind} {iid}: {item.get('goal', '')}")
    elif a.query:
        results = search(" ".join(a.query), a.n, use_bandit=not a.no_bandit)
        if not a.no_bandit and (not results or results[0]["relevance"] < INJECT_BAR):
            print(f"NO GOAL CLEARS THE INJECTION BAR ({INJECT_BAR}) — recommend proceeding without the library. Closest anyway:")
        for r in results:
            print(render(r) + "\n")
    else:
        ap.print_help()
