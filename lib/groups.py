#!/usr/bin/env python3
"""scheming groups — deterministic goal_group construction from EXPLICIT links.

goal_groups are a *derived* structure computed
once at storage from relationships the miner established during refinement. No
fuzzy NLP, no LLM — only links already written into the library:

  subworkflow-family : each subworkflow + its composes_parents (subworkflow
                       is canonical). members = [s.id] + s.composes_parents.
  sibling-conflict   : connected components (union-find) over undirected
                       `cross_ref` links among entries. Component size >=2.
  corroboration      : connected components over explicit `corroborates` links
                       the miner writes. Component size >=2.

Rules: NO MERGING (entries stay distinct), groups MAY overlap (an arm can be in
several), singletons are NOT emitted (an unlinked entry is its own implicit
goal). Idempotent: recomputes goal_groups from scratch each run.

CLI: python lib/groups.py [--dry-run] [--selftest]
"""
import sys

import scheming_core  # sibling in lib/


# ---------------- union-find (tiny, path-halving) ----------------------------

class _UF:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)

    def components(self):
        comps = {}
        for node in list(self.parent):
            comps.setdefault(self.find(node), []).append(node)
        return list(comps.values())


# ---------------- helpers ----------------------------------------------------

def _sortkey(arm_id):
    """Numeric arm_ids sort numerically; everything else lexically after them."""
    s = str(arm_id)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def _entry_arm_id(entry):
    return str(entry.get("idx"))


def _goal_map(lib):
    """arm_id -> goal, across entries and subworkflows (for labels)."""
    m = {}
    for e in lib.get("entries", []):
        m[_entry_arm_id(e)] = e.get("goal", "") or ""
    for s in lib.get("subworkflows", []):
        m[str(s.get("id"))] = s.get("goal", "") or ""
    return m


def _label(members, goal_map, width=60):
    """Cheap label: shortest non-empty member goal, else first member id."""
    goals = [goal_map.get(str(m), "") for m in members]
    goals = [g for g in goals if g]
    if goals:
        pick = min(goals, key=lambda g: (len(g), g))
    else:
        pick = str(min(members, key=_sortkey))
    pick = " ".join(pick.split())
    return pick[:width - 1] + "…" if len(pick) > width else pick


def _group(source, members, goal_map):
    members = sorted({str(m) for m in members}, key=_sortkey)
    smallest = min(members, key=_sortkey)
    return {
        "group_id": f"grp-{source}-{smallest}",
        "label": _label(members, goal_map),
        "source": source,
        "members": members,
    }


def _components_over(lib, link_field, source, goal_map):
    """Union-find groups over an undirected link_field on entries."""
    uf = _UF()
    linked = set()
    for e in lib.get("entries", []):
        aid = _entry_arm_id(e)
        refs = e.get(link_field) or []
        for ref in refs:
            uf.union(aid, str(ref))
            linked.add(aid)
            linked.add(str(ref))
    groups = []
    for comp in uf.components():
        if len(comp) >= 2:
            groups.append(_group(source, comp, goal_map))
    return groups


def build_groups(lib):
    """Compute the full goal_groups list from the library's explicit links."""
    goal_map = _goal_map(lib)
    groups = []

    # subworkflow-family: subworkflow + its parents (promoted sw is canonical)
    for s in lib.get("subworkflows", []):
        members = [str(s.get("id"))] + [str(p) for p in (s.get("composes_parents") or [])]
        if len(set(members)) >= 2:  # a sw with no parents is a singleton → skip
            groups.append(_group("subworkflow-family", members, goal_map))

    # sibling-conflict: cross_ref components
    groups += _components_over(lib, "cross_ref", "sibling-conflict", goal_map)
    # corroboration: corroborates components
    groups += _components_over(lib, "corroborates", "corroboration", goal_map)

    groups.sort(key=lambda g: (g["source"], _sortkey(g["group_id"].rsplit("-", 1)[-1])))
    return groups


def main(argv):
    try:  # goals/labels may hold arbitrary Unicode; never crash a print on cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in argv:
        return _selftest()
    dry = "--dry-run" in argv

    lib = scheming_core.load_library()
    groups_before = len(lib.get("goal_groups", []))
    groups = build_groups(lib)

    by_source = {}
    for g in groups:
        by_source[g["source"]] = by_source.get(g["source"], 0) + 1
    print(f"goal_groups: {len(groups)} "
          f"(subworkflow-family={by_source.get('subworkflow-family', 0)}, "
          f"sibling-conflict={by_source.get('sibling-conflict', 0)}, "
          f"corroboration={by_source.get('corroboration', 0)})")
    for g in groups:
        print(f"  {g['group_id']:<34} [{len(g['members'])}] {g['label']}")

    if dry:
        print("\n--dry-run: library not written.")
        return 0
    lib["goal_groups"] = groups
    scheming_core.save_library(lib)
    scheming_core.emit("library_events", {   # a regroup changes every later selection's choice set
        "event": "groups", "groups_before": groups_before,
        "groups_after": len(groups), "by_source": by_source,
        "group_ids": [g["group_id"] for g in groups]})
    print(f"\nwrote {len(groups)} goal_groups to {scheming_core.library_path()}")
    return 0


def _selftest():
    """Synthetic library: a subworkflow+parents, a cross_ref pair, a
    corroborates triple, and an unlinked entry. Assert exactly the right
    groups, overlap allowed, singleton absent, and idempotent re-run.
    Isolated SCHEMING_HOME; cleans up."""
    import os, shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="scheming_groups_test_")
    iso = scheming_core.isolated_home(tmp); iso.__enter__()   # isolates SCHEMING_HOME + all log overrides
    try:
        lib = scheming_core.load_library()
        lib["entries"] = [
            {"idx": 1, "goal": "arm one", "cross_ref": ["2"]},      # sibling pair
            {"idx": 2, "goal": "a longer goal for arm two"},         # linked back implicitly
            {"idx": 3, "goal": "corr three", "corroborates": ["4", "5"]},
            {"idx": 4, "goal": "corr four", "corroborates": ["3"]},
            {"idx": 5, "goal": "corr five"},
            {"idx": 6, "goal": "lonely unlinked entry"},             # singleton → NO group
        ]
        lib["subworkflows"] = [
            {"id": "sw-x", "goal": "the subworkflow", "composes_parents": ["1", "3"]},
            {"id": "sw-orphan", "goal": "no parents", "composes_parents": []},  # singleton
        ]
        scheming_core.save_library(lib)

        groups = build_groups(scheming_core.load_library())
        by = {g["group_id"]: g for g in groups}

        # exactly three groups
        assert len(groups) == 3, [g["group_id"] for g in groups]

        fam = by["grp-subworkflow-family-1"]  # slug uses smallest member id
        assert fam["source"] == "subworkflow-family"
        assert fam["members"] == ["1", "3", "sw-x"], fam["members"]
        assert fam["label"] == "arm one"  # shortest member goal

        sib = by["grp-sibling-conflict-1"]
        assert sib["source"] == "sibling-conflict"
        assert sib["members"] == ["1", "2"], sib["members"]

        corr = by["grp-corroboration-3"]
        assert corr["source"] == "corroboration"
        assert corr["members"] == ["3", "4", "5"], corr["members"]

        # singleton entry (6) and orphan subworkflow appear in NO group
        allmembers = {m for g in groups for m in g["members"]}
        assert "6" not in allmembers
        assert "sw-orphan" not in allmembers

        # overlap is allowed: arm 1 is in both the family and the sibling group
        assert sum("1" in g["members"] for g in groups) == 2

        # idempotent: persist, then re-run → identical, no dupes
        lib2 = scheming_core.load_library()
        lib2["goal_groups"] = groups
        scheming_core.save_library(lib2)
        again = build_groups(scheming_core.load_library())
        assert again == groups, "re-run not idempotent"
        assert main(["--dry-run"]) == 0
        print("groups selftest ok")
        return 0
    finally:
        iso.__exit__(None, None, None)     # restore env
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
