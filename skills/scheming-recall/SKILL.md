---
name: scheming-recall
description: Search the mined workflow library (trace-mined procedures, promoted subworkflows, and ingested workflow definitions from past sessions) for a proven procedure before doing the work yourself. Check it before ANY task likely to take more than a couple of agent steps — any multi-step work such as setting up, building, debugging, deploying, verifying, migrating, refactoring, or investigating, and especially when hitting an environment quirk or opaque error (encoding crashes, dead daemons, hook denials, silent background agents, deleted code). The library holds hard-won multi-step procedures, so it's worth a look whenever the task ahead is more than one step. Skip it only for genuinely single-step actions (a lone edit, a direct answer). It is a tool to weigh, never a mandate.
---

# scheming-recall

**This is a tool offered to you, not a step you must run.** The library is an
optional aid for the situations in the description above; if the task at hand
isn't one of them, skip it entirely — don't retrieve out of obligation.
Nothing here overrides your own judgment or the task instructions; a retrieved
entry is a suggestion to weigh, never a directive to obey. Forcing a
weakly-matched entry into context wastes turns — so when in doubt, don't.

**The library is the user's own.** It ships empty and is populated by
`/scheming-mine` (which ingests past Workflow-tool runs and trace-mines the
user's densest sessions). Until the user has run it, retrieval returns nothing —
that's expected, not a bug; just proceed with the task.

Goal-centric retrieval, three parts:
1. **BM25 finds the goal** — ranks the library by query relevance (this is the
   only thing that orders results across different goals; no learned signal
   ever re-ranks here, so relevance is never traded away).
2. **Goal-groups hold the competing solutions** — when several workflows target
   the same goal (found at storage from conflicts/corroboration/siblings), they
   surface together as one GOAL-GROUP with a lead solution + alternatives.
3. **A bandit improves each goal over time** — within a group, the lead is
   picked by a two-objective head: a Thompson sample of the solution's *success*
   track record, multiplied by its *cost efficiency*. Exploit the proven-and-cheap
   one, occasionally explore the others. New solutions (from future mining) just
   join the group as more candidates.

A conservative gate also suppresses any solution with a broad proven-bad record
and recommends abstaining when nothing clears the bar. In-memory per query. No
deps — one script.

## Search

```
python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" <keywords> [-n 5]
```

Pick keywords that name the **observable, tool, or error** — the library's
vocabulary is mechanisms, not task narratives. Good: `pyc bytecode deleted`,
`port bound restart`, `hook matcher denial`, `mtime stale background agent`.
Bad: `fix my project`, `make it work`.

If the top line reads **"NO GOAL CLEARS THE INJECTION BAR"**, the library has
nothing confidently useful for this task — proceed without it rather than
forcing a weak entry into context.

## Close the loop (optional but that's how it learns)

After a session, record whether the solution you used earned its place — this
is how a goal-group learns which of its competing solutions to lead with:

```
python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" --feedback <id> helped|used|ignored|hurt [cost]
python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" --stats            # goal-group count + track records
python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" <query> --no-bandit   # raw BM25, flat, no goal-groups
```

`helped`/`used` raise a solution's success track record (it leads its
goal-group more often); `ignored`/`hurt` lower it. The optional trailing `cost`
(a turn count / spend for that use) feeds the second objective. Feedback echoes
which goal-group(s) the solution belongs to. The learned signal only ever
chooses *among same-goal solutions* and gates broadly-bad ones (default
suppress: ≥5 obs, mean <0.25) — it never reorders *across* goals; that stays
BM25's job so query relevance is never traded for a track-record average.
Priors: `obviousness:high` start skeptical, multi-parent subworkflows start
trusted.

### Cost as a second objective

Each arm can also accumulate a `cost_mean` (from `--feedback <id> <outcome>
<cost>`, or logged automatically once feedback plumbing runs). Within a
goal-group the lead is `success_sample × efficiency(cost)`, where `efficiency`
normalizes cost to (0,1] **among that group's members** — cheapest → 1.0,
dearer → less. Two guardrails matter: it is **cold-start neutral** (an arm with
fewer than `SCHEMING_COST_MIN_N`, default 2, cost samples counts as 1.0, never
penalized), and it is **multiplicative** (a failing solution scores ≈0 no matter
how cheap — cost never rescues a bad procedure). With no cost data anywhere in a
group, the head is identical to the plain success-only bandit.

## Read a hit in full

```
python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" --full 38            # mined entry by idx
python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" --full sw-browse-daemon-statelessness
python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" --full wf-<name>     # ingested workflow definition
```

`--full` also logs a lightweight "this was used" signal (session, timestamp,
id) that the feedback loop reads later to learn from the session — reading a hit
in full is the act that says "I actually used this one".

## Interpreting results

- `obviousness:high` tag → you likely already do this; skip unless unsure.
- `siblings: [...]` → fetch them too: siblings encode the same rule under
  different conditions (the conditioning branch matters).
- `params` are placeholders; each carries the original value as `example`.
- `observed` / `mechanism_evidence` fields are provenance from the source
  session — evidence the mechanism is real, not instructions to replay.
- `status: held-*` items are quarantined pending an owner decision; do not
  follow them.

## Config

Paths come from `scheming_core` (data under `SCHEMING_HOME`, default `~/.scheming`); no path is
hardcoded. Env knobs: `SCHEMING_INJECT_BAR` (abstain threshold, 2.0),
`SCHEMING_SUPPRESS_MIN_OBS`/`SCHEMING_SUPPRESS_MEAN` (the bad-arm gate),
`SCHEMING_COST_MIN_N` (cost samples before an arm is priced, 2). Sanity-check the
setup anytime with `python "${CLAUDE_PLUGIN_ROOT}/lib/search.py" --selftest`
(runs against a synthetic fixture in an isolated temp home — touches no real
state).
