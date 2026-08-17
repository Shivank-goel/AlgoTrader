"""The knowledge base must stay internally consistent.

`docs/KNOWLEDGE.md` exists so research does not loop. That only works if its
fact IDs actually resolve — a `K-xx` reference pointing at nothing is worse than
no reference, because it looks authoritative. These tests run with the rest of
the suite so the docs cannot rot silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "docs" / "KNOWLEDGE.md"
RESEARCH_LOG = ROOT / "docs" / "RESEARCH_LOG.md"
CLAUDE_MD = ROOT / "CLAUDE.md"

# A fact is *defined* by a table row `| K-xx | ... |` or a prose heading
# `**K-xx —` / `` `K-xx` — ``. Anything else is a reference.
_DEFINITION_PATTERNS = (
    re.compile(r"^\|\s*(K-\d{2})\s*\|", re.M),
    re.compile(r"\*\*(K-\d{2})\s*—"),
    re.compile(r"`(K-\d{2})`\s*—"),
)
_ANY_REF = re.compile(r"\bK-\d{2}\b")


def _defined_ids() -> set[str]:
    text = KNOWLEDGE.read_text()
    found: set[str] = set()
    for pattern in _DEFINITION_PATTERNS:
        found.update(pattern.findall(text))
    return found


@pytest.fixture(scope="module")
def defined() -> set[str]:
    return _defined_ids()


def test_knowledge_base_files_exist():
    for path in (KNOWLEDGE, RESEARCH_LOG, CLAUDE_MD):
        assert path.exists(), f"{path.name} is missing — see CLAUDE.md for the contract"


def test_knowledge_defines_facts(defined):
    assert len(defined) > 40, f"only {len(defined)} facts defined; expected the full set"


@pytest.mark.parametrize("doc", ["docs/RESEARCH_LOG.md", "CLAUDE.md", "docs/KNOWLEDGE.md"])
def test_no_dangling_fact_references(doc, defined):
    """Every K-xx mentioned anywhere must be defined in KNOWLEDGE.md."""
    refs = set(_ANY_REF.findall((ROOT / doc).read_text()))
    dangling = sorted(refs - defined)
    assert not dangling, (
        f"{doc} references undefined facts: {dangling}. "
        "Either define them in KNOWLEDGE.md or fix the reference."
    )


def test_fact_ids_are_unique():
    """A duplicated ID means two facts silently claim the same name."""
    text = KNOWLEDGE.read_text()
    counts: dict[str, int] = {}
    for pattern in _DEFINITION_PATTERNS:
        for fact_id in pattern.findall(text):
            counts[fact_id] = counts.get(fact_id, 0) + 1

    duplicated = sorted(fid for fid, n in counts.items() if n > 1)
    assert not duplicated, f"defined more than once in KNOWLEDGE.md: {duplicated}"


def test_research_log_entries_are_sequential():
    entries = re.findall(r"^## (R-\d{2})", RESEARCH_LOG.read_text(), re.M)
    assert entries, "research log has no entries"
    numbers = [int(e.split("-")[1]) for e in entries]
    assert numbers == sorted(numbers), f"entries out of order: {entries}"
    assert len(numbers) == len(set(numbers)), "duplicate R-ids"


def test_every_research_entry_cites_facts():
    """An entry that changes nothing and uses nothing is not worth keeping."""
    text = RESEARCH_LOG.read_text()
    blocks = re.split(r"^## (R-\d{2})", text, flags=re.M)[1:]
    for rid, body in zip(blocks[::2], blocks[1::2]):
        assert "Uses:" in body or "Changes:" in body, (
            f"{rid} cites no K-xx facts; it should say what it used or changed"
        )


def test_claude_md_points_at_the_knowledge_base():
    """A fresh session must be routed to KNOWLEDGE.md before anything else."""
    text = CLAUDE_MD.read_text()
    assert "KNOWLEDGE.md" in text
    assert "RESEARCH_LOG.md" in text


def test_pass_mark_is_stated_and_unweakened():
    """The pass mark is pre-committed. Guard the numbers against drift."""
    text = KNOWLEDGE.read_text()
    assert "0.95" in text, "deflated-Sharpe threshold missing"
    assert "100 observations" in text or "≥ 100 observations" in text
    assert "both halves" in text
