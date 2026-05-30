"""Component 3: Prompt builder.

Renders a single instruction prompt for the coding agent from the issue. Pure
function (no I/O) so it is trivial to unit-test and so the same prompt can be
logged, diffed, or replayed.
"""

from __future__ import annotations

from .tracker import Issue


def build_prompt(issue: Issue) -> str:
    """Render the coding-agent prompt from an ``ImprovementIssue``.

    Emphasizes: edit ONLY the target file, make the minimal change that satisfies
    the plan and acceptance criteria, and do not touch anything else.
    """
    target = issue.target_file or "(no target file specified)"
    sections = [
        "You are an automated coding agent in the Dark Factory pipeline for the "
        "Helix project. Implement the following improvement issue with the "
        "smallest possible change. Edit code directly; do not ask questions.",
        "",
        f"# Issue {issue.id}: {issue.title}",
        "",
        "## Hypothesis",
        issue.hypothesis or "(none provided)",
        "",
        "## Target file (edit ONLY this file)",
        target,
        "",
        "## Plan",
        issue.plan or "(no plan provided — make the minimal change implied by the title)",
        "",
        "## Acceptance criteria",
        issue.acceptance_criteria or "(none provided)",
        "",
        "## Rules",
        f"- Edit only `{target}`. Do not modify any other file.",
        "- Make the minimal change that satisfies the plan and acceptance criteria.",
        "- Do not change behavior beyond what the plan asks for.",
        "- Do not run tests, commit, or open a PR — the orchestrator handles that.",
        "- When finished, briefly state what you changed.",
    ]
    return "\n".join(sections)
