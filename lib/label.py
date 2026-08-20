#!/usr/bin/env python3
"""scheming reaction labeler — classify a user's reaction to a served procedure.

Exposes ``classify(used_entry, following_turns) -> {"reaction", "correction"}``
where reaction is one of accepted | complaint | neutral. Used by the SessionStart
catch-up (``catchup.py``) to close the learning loop.

Two tiers, cheapest first:
  1. A FREE keyword prefilter (no model call) resolves the obviously-clear turns.
  2. Only genuinely ambiguous turns invoke a model, headless, through the user's
     existing Claude Code auth (NO API key):
         claude -p <prompt> --model haiku --permission-mode dontAsk \
                --output-format json --json-schema <schema>

DEFENSIVE BY CONSTRUCTION: classify() never raises. On ANY failure it returns the
safe no-op {"reaction":"neutral","correction":None} and records the reason under a
"_error" key (and logs it to stderr). A labeling failure must never crash the hook.

Reentrancy: a ``claude -p`` spawned from inside a SessionStart hook would itself
fire SessionStart -> infinite recursion. Every child we spawn carries
SCHEMING_LABELER_RUNNING=1 in its env; catchup.py exits immediately if it sees it.

Stdlib only.
"""
import argparse, json, os, subprocess, sys, time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console safety
except Exception:
    pass

# ---------------- prefilter wordlists ----------------------------------------
# Strong, unambiguous markers only. A turn that trips a COMPLAINT marker (and no
# ACCEPT marker) is a clear complaint; the reverse is a clear acceptance. Turns
# that trip both, or neither, are AMBIGUOUS and fall through to the model.
# Kept deliberately small/high-precision: false positives here skip the model.
COMPLAINT = (
    "that's wrong", "thats wrong", "that is wrong", "no,", "no.", "don't", "dont",
    "stop", "revert", "actually", "that broke", "broke", "undo", "not what",
    "doesn't work", "does not work", "didn't work", "did not work", "wrong",
)
ACCEPT = (
    "thanks", "thank you", "perfect", "great", "works", "worked", "ship it",
    "lgtm", "nice", "awesome",
)

SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "reaction": {"type": "string", "enum": ["accepted", "complaint", "neutral"]},
        "correction": {"type": ["string", "null"]},
    },
    "required": ["reaction"],
    "additionalProperties": False,
})

NEUTRAL = {"reaction": "neutral", "correction": None}


def _no_model():
    """Prefilter-only mode: set by tests and used as the graceful-degradation
    fallback when claude is unavailable."""
    return bool(os.environ.get("SCHEMING_LABEL_NO_MODEL"))


def _timeout():
    try:
        return int(os.environ.get("SCHEMING_LABEL_TIMEOUT", "60"))
    except ValueError:
        return 60


def _neutral(reason, code="unexpected"):
    """The safe no-op result, annotated + logged. Never raises. `code` is the
    mapped enum stored on reactions.jsonl (never raw stdout/stderr); `reason` is
    the free-text detail for stderr only."""
    try:
        print(f"[scheming-label] degraded to neutral: {reason}", file=sys.stderr)
    except Exception:
        pass
    return {"reaction": "neutral", "correction": None, "_error": reason,
            "error_reason": code}


# ---------------- tier 1: free keyword prefilter -----------------------------

def _prefilter(following_turns):
    """Return (reaction, correction) for a CLEAR case, or (None, None) if ambiguous."""
    joined = "\n".join(following_turns).lower()
    comp = any(m in joined for m in COMPLAINT)
    acc = any(m in joined for m in ACCEPT)
    if comp and not acc:
        # the correction is the first turn that voiced the complaint
        corr = next((t for t in following_turns
                     if any(m in t.lower() for m in COMPLAINT)), None)
        return "complaint", corr
    if acc and not comp:
        return "accepted", None
    return None, None  # both -> conflicting, or neither -> no signal: ambiguous


# ---------------- tier 2: model path -----------------------------------------

def _build_prompt(used_entry, following_turns):
    goal = (used_entry or {}).get("goal") or "(unknown)"
    turns = "\n".join(f"- {str(t)[:500]}" for t in following_turns[:8])
    return (
        "You are labeling how a user reacted to a suggested procedure in a coding "
        "session.\n"
        f"The assistant suggested a procedure whose goal was:\n  {goal}\n\n"
        "These are the user's messages that FOLLOWED the suggestion, in order:\n"
        f"{turns}\n\n"
        "Classify the user's REACTION to that suggestion as exactly one of:\n"
        "  accepted  - the user used it / was satisfied / moved on approvingly\n"
        "  complaint - the user pushed back, corrected, reverted, or called it wrong\n"
        "  neutral   - no clear signal either way\n"
        "If it was a complaint, put the correction the user wanted (what they asked "
        "for instead) in 'correction'; otherwise null.\n"
        "Respond ONLY with the JSON object, nothing else."
    )


def _classify_failure(proc):
    """Map a nonzero claude run to a human reason by scanning its text (exit code
    alone does not distinguish auth vs quota vs overload)."""
    blob = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
    for needle, why, code in (
        ("authentication_failed", "auth failed", "auth_failed"),
        ("oauth", "auth/oauth issue", "auth_failed"),
        ("billing_error", "billing error", "billing_error"),
        ("rate_limit", "rate limited", "rate_limited"),
        ("overloaded", "server overloaded", "overloaded"),
        ("server_error", "server error", "server_error"),
        ("model_not_found", "model not found", "model_not_found"),
        ("command not found", "claude not on PATH", "not_on_path"),
    ):
        if needle in blob:
            return f"claude failed (rc={proc.returncode}): {why}", code
    return f"claude failed (rc={proc.returncode})", "nonzero_exit"


def _first_json_object(text):
    """Extract the first balanced {...} JSON object from arbitrary text (tier b:
    for a claude build that lacks --json-schema and just prints the object)."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _coerce(obj):
    """Force any parsed object into the strict {reaction, correction} shape."""
    r = str(obj.get("reaction", "neutral")).strip().lower()
    if r not in ("accepted", "complaint", "neutral"):
        r = "neutral"
    corr = obj.get("correction")
    if corr is not None and not isinstance(corr, str):
        corr = str(corr)
    if isinstance(corr, str) and not corr.strip():
        corr = None
    return {"reaction": r, "correction": corr}


def _parse_envelope(stdout):
    """3-tier parse of the claude --output-format json envelope:
       (a) structured_output if present; (b) else the first {...} in result/stdout;
       (c) else neutral."""
    stdout = (stdout or "").strip()
    if not stdout:
        return _neutral("empty stdout")
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        env = None
    if isinstance(env, dict):
        if env.get("subtype") == "error_max_structured_output_retries":
            return _neutral("schema validation retries exhausted", "schema_retries")
        so = env.get("structured_output")
        if isinstance(so, dict):                     # tier a
            return _coerce(so)
        result = env.get("result")                   # tier b source
        text = result if isinstance(result, str) else stdout
    else:
        text = stdout                                # not an envelope at all
    obj = _first_json_object(text)
    if obj is not None:                              # tier b
        return _coerce(obj)
    return _neutral("no parseable structured output", "no_parseable")  # tier c


def _envelope_cost(stdout):
    """total_cost_usd from the claude --output-format json envelope, or None. The
    labeler's own token spend, for telemetry."""
    try:
        env = json.loads((stdout or "").strip())
        c = env.get("total_cost_usd") if isinstance(env, dict) else None
        return float(c) if isinstance(c, (int, float)) else None
    except Exception:
        return None


def _classify_model(used_entry, following_turns):
    # No allowed tools: this is a pure text classification, so it needs none.
    # dontAsk denies any tool the model might reach for (e.g. a prompt-injected
    # transcript turn trying to make it Read a file), losing nothing.
    argv = ["claude", "-p", _build_prompt(used_entry, following_turns),
            "--model", "haiku", "--permission-mode", "dontAsk",
            "--output-format", "json", "--json-schema", SCHEMA]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_timeout(),
            env={**os.environ, "SCHEMING_LABELER_RUNNING": "1"})  # guard child recursion
    except FileNotFoundError:
        return _neutral("claude not on PATH", "not_on_path")
    except subprocess.TimeoutExpired:
        return _neutral("claude timed out", "timeout")
    except Exception as e:  # any other spawn failure
        return _neutral(f"spawn failed: {e!r}", "spawn_failed")
    dur_ms = round((time.monotonic() - t0) * 1000)
    if proc.returncode != 0:
        text, code = _classify_failure(proc)
        out = _neutral(text, code)
    else:
        out = _parse_envelope(proc.stdout)
    out["model_duration_ms"] = dur_ms                # labeler health telemetry
    out["model_cost_usd"] = _envelope_cost(proc.stdout)
    return out


# ---------------- public API -------------------------------------------------

def _annotate(result, decided_by, model_invoked):
    """Attach telemetry metadata (for reactions.jsonl) to a classify result:
    decided_by (prefilter|model|none), model_invoked, degraded (a reaction that
    fell back to neutral on an error), and the mapped error_reason enum. Cost and
    duration are set by _classify_model; default to None."""
    result.setdefault("error_reason", None)
    result["decided_by"] = decided_by
    result["model_invoked"] = model_invoked
    result["degraded"] = result.get("error_reason") is not None
    result.setdefault("model_cost_usd", None)
    result.setdefault("model_duration_ms", None)
    return result


def classify(used_entry, following_turns):
    """Classify a user's reaction to a served entry. NEVER raises; always returns
    {"reaction": accepted|complaint|neutral, "correction": str|None, plus the
    telemetry keys decided_by/model_invoked/degraded/error_reason/model_cost_usd/
    model_duration_ms}. The first two are the stable contract; the rest are
    additive labeler-health telemetry."""
    try:
        following_turns = [t for t in (following_turns or []) if str(t).strip()]
        if not following_turns:
            return _annotate(dict(NEUTRAL), "none", False)   # nothing followed -> no signal
        reaction, corr = _prefilter(following_turns)
        if reaction is not None:
            return _annotate({"reaction": reaction, "correction": corr}, "prefilter", False)
        if _no_model():                                       # ambiguous, model disabled
            nd = dict(NEUTRAL); nd["error_reason"] = "model_disabled"
            return _annotate(nd, "none", False)
        return _annotate(_classify_model(used_entry, following_turns), "model", True)
    except Exception as e:  # absolute backstop — classify must never throw
        return _annotate(_neutral(f"unexpected: {e!r}", "unexpected"), "none", False)


# ---------------- selftest ---------------------------------------------------

def _selftest():
    ok = 0
    rc = lambda r: {"reaction": r["reaction"], "correction": r["correction"]}
    # 1. prefilter: clear acceptance (+ telemetry keys present & correct)
    r = classify({"goal": "restore deleted bytecode"}, ["thanks, that works perfectly"])
    assert rc(r) == {"reaction": "accepted", "correction": None}, r
    assert r["decided_by"] == "prefilter" and not r["degraded"] and not r["model_invoked"], r
    ok += 1
    # 2. prefilter: clear complaint, correction captured
    r = classify({"goal": "restart daemon"}, ["no, that's wrong, revert it"])
    assert r["reaction"] == "complaint" and r["correction"], r
    ok += 1
    # 3. empty turns -> neutral (no model)
    assert classify({"goal": "x"}, [])["reaction"] == "neutral"
    assert classify({"goal": "x"}, ["   "])["reaction"] == "neutral"  # whitespace-only
    ok += 1
    # 4. ambiguous under no-model -> neutral (no claude invoked); degraded flag set
    os.environ["SCHEMING_LABEL_NO_MODEL"] = "1"
    try:
        r = classify({"goal": "x"}, ["let's move on to the next file"])
        assert rc(r) == {"reaction": "neutral", "correction": None}, r
        assert r["degraded"] and r["error_reason"] == "model_disabled", r
    finally:
        os.environ.pop("SCHEMING_LABEL_NO_MODEL", None)
    ok += 1
    # 5. STUBBED subprocess: canned envelope parses via structured_output (no real claude)
    import types
    real = subprocess.run
    try:
        env = json.dumps({"session_id": "s", "subtype": "success",
                          "structured_output": {"reaction": "complaint",
                                                "correction": "use os.replace"},
                          "total_cost_usd": 0.0})
        subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=env, stderr="")
        r = classify({"goal": "atomic write"}, ["hmm, wrong,, but also thanks"])  # both -> ambiguous
        assert rc(r) == {"reaction": "complaint", "correction": "use os.replace"}, r
        assert r["decided_by"] == "model" and r["model_invoked"] and not r["degraded"], r
        ok += 1
        # 5b. tier-b fallback: no structured_output, object embedded in result text
        env2 = json.dumps({"result": 'sure: {"reaction":"accepted"} done', "subtype": "success"})
        subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=env2, stderr="")
        r = classify({"goal": "x"}, ["ok maybe, unclear, thanks or not"])
        assert rc(r) == {"reaction": "accepted", "correction": None}, r
        ok += 1
        # 5c. nonzero returncode with auth text -> neutral degradation, mapped enum
        subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="Error: authentication_failed")
        r = classify({"goal": "x"}, ["ambiguous-ish, unclear signal here"])
        assert r["reaction"] == "neutral" and r["degraded"] and r["error_reason"] == "auth_failed", r
        ok += 1
        # 5d. schema-retries-exhausted subtype -> neutral, mapped enum
        subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"subtype": "error_max_structured_output_retries"}),
            stderr="")
        r = classify({"goal": "x"}, ["unclear ambiguous turn"])
        assert r["reaction"] == "neutral" and r["error_reason"] == "schema_retries", r
        ok += 1
    finally:
        subprocess.run = real
    print(f"label selftest ok ({ok} checks)")


# ---------------- CLI --------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="classify a user's reaction to a served entry")
    ap.add_argument("--entry-goal", default="", help="the served entry's goal (context)")
    ap.add_argument("--turn", action="append", default=[], metavar="TEXT",
                    help="a following user turn (repeatable)")
    ap.add_argument("--no-model", action="store_true",
                    help="prefilter only; never invoke claude")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        if a.no_model:
            os.environ["SCHEMING_LABEL_NO_MODEL"] = "1"
        print(json.dumps(classify({"goal": a.entry_goal}, a.turn), ensure_ascii=False))
