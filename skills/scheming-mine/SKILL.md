---
name: scheming-mine
description: Build or refresh your personal scheming library from your own history. In one pass it pulls in your past Claude Code Workflow runs (free, deterministic) and mines your densest raw sessions for hard-won, evidence-backed procedures. Run it after setup and periodically as your history grows. Triggers on "mine my sessions", "refresh the scheming library", "scheming-mine", "grow my workflow library".
---

# scheming-mine — build your library from your own history

Mining has two parts you run as **one operation**, in order:
1. a **free, deterministic** pass that pulls in what you already automated (past
   Workflow-tool runs), then
2. the **LLM** pass where you (Claude) read raw tool-call traces and extract
   transferable, evidence-backed procedures.

The two inversions you must honor in part 2:

1. **Recurrence is refinement fuel, not an admission gate.** Admit an entry from
   a single session on content alone. Recurrence shows up *afterward* as
   corroboration / conflict / promotable subworkflows.
2. **One gate at emission, everything else is a tag.** The only rejection is
   **no mechanism**. Obviousness and every other quality judgment are
   environment-relative → they become `tags`, filtered at retrieval, never
   deletes.

All paths and IO go through `scheming_core` — never hardcode a path. `${CLAUDE_PLUGIN_ROOT}`
is the plugin root at runtime.

---

## 1. Ingest past Workflow runs (free, deterministic)

Start here — it's zero-cost and instant. It merges the user's existing
Workflow-tool artifacts (`wf_*.json` run logs + their scripts) into the library:

```
python "${CLAUDE_PLUGIN_ROOT}/lib/ingest.py"
```

Relay the funnel it prints — `found -> parsed -> new -> skipped(unparseable)`:
**found** = run logs discovered; **parsed** = those that yielded a definition;
**new** = unique workflows merged (re-runs of one workflow collapse to one record);
**skipped** = unparseable, counted, never fatal. `0 found` just means no Workflow
runs yet — fine, move on. `--dry-run` previews without writing.

## 2. Triage — find the densest un-mined sessions (deterministic)

```
python "${CLAUDE_PLUGIN_ROOT}/lib/triage.py" --top 20
```

This ranks every session transcript by tool-call density and prints the top
slice (id, path, tool-call count, size, first-prompt snippet). **Why density:**
the median session yields nothing; a handful of long, dense, failure-rich
sessions carry most of the yield (7 sessions produced 76% of one mining run's
output). Read the top decile in full, not the corpus flat.

To skip sessions you have already mined, check existing `mechanism_evidence`
values — each carries its `[sid=<session>]`, so a session already represented can
be deprioritized. `--all` lists everything; default is the top decile (min 1).

## Model discipline (cost) — Sonnet is the ceiling for the whole pipeline

Steps 1, 2, 7 are deterministic scripts (ingest / triage / groups): **no model,
free.** A mine run's token cost is dominated by the trace-reading + extraction of
steps 3–4, which fan out one subagent per top session. **Run those extractor
subagents on Sonnet — never let them inherit an Opus session.** Reading a trace and
filling the entry schema is a Sonnet-class job; Opus here costs ~5× for no
measurable gain in yield. Concretely:

- Spawn each extractor with the Agent tool and pass **`model: "sonnet"`** explicitly
  (do not omit it — omitting inherits the parent, which may be Opus). If you delegate
  via `claude -p` instead, pass **`--model sonnet`**. Use **Haiku** for any purely
  mechanical pass (reformatting, dedup).
- **Sonnet is the ceiling for every model call in this pipeline** — do not spawn
  Opus workers, and prefer running the orchestration itself on a Sonnet session.
- Provenance stays intact because verification is deterministic (grep the quote
  byte-exact against the source), not model-judged — so a cheaper extractor costs
  nothing in correctness.

(A past run mined 7 dense sessions on Opus subagents at ~1.4M tokens; the same work
on Sonnet is the intended cost.)

For a transcript too large to read raw (they reach tens of MB), first reduce it to a
signal-only **digest** — assistant decision text, tool-call name+command previews,
and error-flagged tool results, kept byte-exact — then hand the digest to the Sonnet
extractor so quotes stay verbatim for `mechanism_evidence`.

## 3. Read the traces — not the prompts

For each top session, **read the full tool-call trace** (the JSONL transcript at
the printed path), not the prompt or a summary. The mechanism is not in the
one-line request — it is in the thousands of tool calls that follow it.
Prompt-only extraction has a ~3.2% qualifying ceiling; trace extraction yields
~20× more per session.

Look especially for: opaque errors and how they were resolved, environment
quirks, dead daemons, encoding/permission traps, multi-step procedures that
recurred, and any decision rule stated in your own assistant text.

## 4. Extract entries — do/observed split + the one gate

Emit entries in this schema. Per entry:

- **`goal`, `trigger`, `meta_level`** — what it accomplishes, when it fires,
  object- vs meta-level.
- **`mechanism_evidence` — THE ADMISSION GATE (required).** Format exactly
  `"[sid=<session>] <quote>"`. The quote is a verbatim citation from the trace
  proving the mechanism — a tool-call pattern **or** a quoted decision rule from
  your assistant text. **No mechanism → do not emit.** This is the *only*
  rejection (it catches too-thin and goal-restating entries — ~94% of
  prompt-mined candidates fail exactly here).
- **`steps: [{do, observed, check?, on_fail?}]`** — the do/observed split:
  - `do` = the transferable action. It **must survive deletion of `observed`**
    (read it with `observed` removed — if it still teaches the action, good).
  - `observed` = the instance detail, **byte-exact provenance**. Never edit,
    never abstract, never "fix" a character inside it. Same for `observed_value`.
- **`inputs: [{name, what_it_is, observed_value}]`**, **`invariants: [...]`**.
- **`tags` — judgments, never gates.** Obviousness in particular:
  `tags.obviousness: "high"|...` + `obviousness_note` + `obviousness_source`.
  What is default behavior in this environment is hard-won elsewhere — rank it
  down, never delete it. Storage is free; obviousness is a ranking problem.
- **`params` — parameterization.** In *transferable* fields (`do`, `goal`,
  `trigger`, `invariants`) replace instance literals with `<typed-placeholders>`;
  keep every replaced literal as a labeled `example` in `params`
  (`[{name, example, ...}]`). Leave `observed` and `mechanism_evidence`
  untouched. (Concepts transfer ~99%, but only ~57% portable as written — this
  closes the gap.)
- **`status: "active"`** on admission (every admitted entry is active).

Optionally self-score at emission (`qualifies`, `as_written`,
`concept_transfers`, `failure_mode`, `teaches`). The extractor self-score runs
6–9 points optimistic — calibrated enough that no full adversarial sweep per
batch is needed; spot-audit adversarially only after schema/prompt changes.

## 5. Establish relationships as EXPLICIT links (so groups.py can mechanize them)

Do **not** merge entries — ever. Siblings stay separate; shared structure is
expressed through links that `groups.py` turns into `goal_groups`
deterministically:

- **Siblings that disagree → mutual `cross_ref`.** Add each other's `arm_id`
  (`str(idx)`) to both entries' `cross_ref: [...]`, and fold the *conditioning
  variable* (what makes each correct) into both as an explicit branch. Sibling
  disagreement is the accuracy signal, not noise. Hard conflicts are preserved,
  not resolved (they are owner policy).
- **Independent entries reaching the same rule → mutual `corroborates`.** Add
  each other's `arm_id` to both entries' `corroborates: [...]` (the link field
  the miner writes; groups.py builds corroboration groups from it).
- **A recurring multi-step fragment across ≥2 active parents → a
  `subworkflow`.** Emit it with the entry schema minus `observed`, plus `id`
  (must be a stable slug; `arm_id = id`), `composes_parents: [parent arm_ids]`,
  `observed_instances`, and `gate`. Parents gain `composed_by` backpointers.
  New subworkflow *text* is the one place strictness is kept — double-check it.
  A single shared rule does **not** promote; it already lives in the entries.

## 6. Append to the library via scheming_core

Assign a stable `idx` (max existing `idx` + 1, incrementing) so `arm_id =
str(idx)` stays unique. Then append and save. Run it as a heredoc with the
plugin's `lib/` on `PYTHONPATH` — that is what makes `import scheming_core` resolve
from any working directory (`${CLAUDE_PLUGIN_ROOT}` is a *shell* variable, so it
must be set in the shell, not inside the Python):

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/lib" python - <<'PY'
import scheming_core
lib = scheming_core.load_library()
next_idx = max([e.get("idx", 0) for e in lib["entries"]], default=0) + 1

entry = {
    "idx": next_idx,
    "goal": "...",
    "trigger": "...",
    "meta_level": "...",
    "mechanism_evidence": "[sid=<session>] <verbatim quote>",   # required gate
    "inputs":  [{"name": "...", "what_it_is": "...", "observed_value": "..."}],
    "steps":   [{"do": "...", "observed": "...(byte-exact)..."}],
    "invariants": ["..."],
    "params":  [{"name": "<placeholder>", "example": "..."}],
    "tags":    {"obviousness": "high", "obviousness_note": "...", "obviousness_source": "..."},
    "status":  "active",
    "cross_ref": [], "corroborates": [],   # fill from step 5
}
lib["entries"].append(entry)

# a promoted subworkflow (entry shape minus `observed`):
# lib["subworkflows"].append({"id": "sw-...", "goal": "...", "composes_parents": ["12","19"],
#                             "observed_instances": [...], "gate": "...", "status": "active", ...})

scheming_core.save_library(lib)   # atomic; refreshes meta.counts. NEVER write library.json by hand.
PY
```

Append incrementally and save as you go — a messy exit then loses nothing.
`save_library` is the only safe writer; never touch `library.json` directly.

## 7. Rebuild goal_groups (deterministic)

After appending, rebuild the derived grouping so retrieval can pick between
competing solutions within a goal:

```
python "${CLAUDE_PLUGIN_ROOT}/lib/groups.py"
```

It recomputes `goal_groups` from scratch (idempotent) out of the explicit links
you wrote in step 5: `subworkflow-family` (subworkflow + its parents),
`sibling-conflict` (cross_ref components), `corroboration` (corroborates
components). Groups may overlap; singletons are not emitted. `--dry-run` shows
the result without writing. (`groups.py` also records a `groups` library-event
for you — no action needed.)

## 8. Record the mine run (telemetry)

Mining is agent-driven, so log the run yourself so library growth stays
separable from accuracy changes over time (counts only — never log rejected
mechanism-evidence quote text):

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/lib" python - <<'PY'
import scheming_core
scheming_core.emit("library_events", {    # the one telemetry sink; stamps ts, best-effort
    "event": "mine",
    "sessions_read": 0,           # fill with how many transcripts you read in full
    "entries_added": 0,           # new entries you appended this run
    "subworkflows_added": 0,      # new subworkflows promoted
    "admission_rejections": 0})   # candidates dropped for missing mechanism_evidence
PY
```

---

## Honest limits (state them; do not paper over them)

- **Step 1 is deterministic; steps 2–8 are the LLM step.** triage.py and groups.py
  are deterministic helpers; the extraction judgment is yours and carries the
  error. Keep the gate strict and the provenance byte-exact.
- **Mine for the tail, not generic advice.** The library earns its keep on
  environment-specific traps and owner/project policy — quirks and decisions the
  model can't infer unaided — not on generic traps a capable model already
  handles. Prefer entries that encode real, non-obvious environment state.
- **Traces cannot capture** discarded alternatives, total dead ends that
  produced no artifact, or base rates. Do not invent them.
