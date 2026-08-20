#!/usr/bin/env python3
"""scheming triage — rank Claude Code sessions by tool-call density (zero-LLM).

MINING.md §2: the median session yields nothing; a handful of long, dense,
failure-rich sessions carry most of the mining yield. This tool finds them so
the agent-driven miner (skills/scheming-mine) reads the top decile in full rather
than the corpus flat.

Pure stdlib JSONL scan. Counts tool-use invocations per transcript, ranks
descending, prints the top decile (min 1) as a table.

CLI: python lib/triage.py [--top N] [--all] [--selftest]
"""
import json, os, sys

import scheming_core  # sibling in lib/


def _extract_text(content):
    """First textual payload of a message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "")
    return None


def _count_tools_in_record(rec):
    """Count tool invocations in one JSONL record, defensively.

    Real transcripts put `tool_use` blocks inside an assistant record's
    message.content list. We also accept a record-level tool_use type and a
    top-level content list, so a shape change does not silently zero the count.
    """
    if not isinstance(rec, dict):
        return 0
    n = 0
    if rec.get("type") == "tool_use":
        n += 1
    msg = rec.get("message")
    for c in ((msg.get("content") if isinstance(msg, dict) else None),
              rec.get("content")):
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n += 1
        elif isinstance(c, dict) and c.get("type") == "tool_use":
            n += 1
    return n


def scan_session(path):
    """Return (tool_count, size_bytes, first_user_prompt) for one transcript.

    Tolerant of unparseable lines — a corrupt record never crashes the scan.
    """
    tool_count = 0
    first_prompt = None
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                tool_count += _count_tools_in_record(rec)
                if first_prompt is None and isinstance(rec, dict) and rec.get("type") == "user":
                    msg = rec.get("message")
                    txt = _extract_text(msg.get("content") if isinstance(msg, dict) else None)
                    if txt:
                        first_prompt = txt
    except OSError:
        pass
    return tool_count, size, first_prompt


def rank_sessions(projects_dir=None):
    """All *.jsonl transcripts under projects_dir, ranked by tool_count desc.

    Returns a list of dicts. Tie-break by size then id for a stable order.
    """
    projects_dir = projects_dir or scheming_core.claude_projects_dir()
    rows = []
    if os.path.isdir(projects_dir):
        for root, _dirs, files in os.walk(projects_dir):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                tool_count, size, prompt = scan_session(path)
                rows.append({
                    "id": os.path.splitext(name)[0],
                    "path": path,
                    "tool_count": tool_count,
                    "size": size,
                    "prompt": prompt or "",
                })
    rows.sort(key=lambda r: (-r["tool_count"], -r["size"], r["id"]))
    return rows


def _snippet(text, width=54):
    text = " ".join((text or "").split())
    return text[:width - 1] + "…" if len(text) > width else text


def print_table(rows, limit):
    shown = rows[:limit]
    print(f"{'session id':<38} {'tools':>6} {'bytes':>10}  first-user-prompt")
    print("-" * 100)
    for r in shown:
        print(f"{r['id']:<38} {r['tool_count']:>6} {r['size']:>10}  {_snippet(r['prompt'])}")
        print(f"    {r['path']}")
    print(f"\n{len(shown)} of {len(rows)} sessions shown (ranked by tool-call density).")


def main(argv):
    try:  # arbitrary Unicode in prompts must never crash a print on a cp1252 console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    top = None
    show_all = False
    args = list(argv)
    if "--selftest" in args:
        return _selftest()
    if "--all" in args:
        show_all = True
        args.remove("--all")
    if "--top" in args:
        i = args.index("--top")
        try:
            top = int(args[i + 1])
        except (IndexError, ValueError):
            print("--top needs an integer", file=sys.stderr)
            return 2

    rows = rank_sessions()
    if not rows:
        print("0 sessions found")
        return 0

    if show_all:
        limit = len(rows)
    elif top is not None:
        limit = max(0, top)
    else:
        limit = max(1, len(rows) // 10)  # top decile, min 1
    print_table(rows, limit)
    return 0


def _selftest():
    """Synthetic projects dir with 3 transcripts of differing density; assert
    ranking order and top-N slice. Touches no real state; cleans up."""
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="scheming_triage_test_")
    old = os.environ.get("CLAUDE_PROJECTS_DIR")
    os.environ["CLAUDE_PROJECTS_DIR"] = tmp
    try:
        def write(name, n_tools, prompt):
            recs = [{"type": "user", "message": {"content": prompt}}]
            for k in range(n_tools):
                recs.append({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "thinking"},
                    {"type": "tool_use", "name": "Bash", "input": {"i": k}},
                ]}})
            sub = os.path.join(tmp, "proj")
            os.makedirs(sub, exist_ok=True)
            with open(os.path.join(sub, name), "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")

        write("dense.jsonl", 10, "big dense session")
        write("mid.jsonl", 3, "middle session")
        write("sparse.jsonl", 0, "nothing much here")

        rows = rank_sessions()
        assert [r["id"] for r in rows] == ["dense", "mid", "sparse"], rows
        assert [r["tool_count"] for r in rows] == [10, 3, 0]
        assert rows[0]["prompt"] == "big dense session"
        assert rows[0]["size"] > 0
        # top-N slice
        assert [r["id"] for r in rows[:1]] == ["dense"]
        assert [r["id"] for r in rows[:2]] == ["dense", "mid"]

        # empty / missing projects dir → 0 sessions, exit 0, no crash
        os.environ["CLAUDE_PROJECTS_DIR"] = os.path.join(tmp, "does-not-exist")
        assert rank_sessions() == []
        assert main([]) == 0
        print("triage selftest ok")
        return 0
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECTS_DIR", None)
        else:
            os.environ["CLAUDE_PROJECTS_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
