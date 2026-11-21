from __future__ import annotations

import pytest
from pipeline_doctor_agent.codeowners import Rule, choose_assignee, owners_for

RULES = [
    Rule("*", ("@acme/platform",)),
    Rule("/app/pricing.py", ("@dev1", "@acme/billing")),
    Rule("/docs/", ("@dev3",)),
    Rule("*.md", ("@writer",)),
    Rule("/apps/**/tests/", ("@qa",)),
    Rule("src/*.py", ("@srcowner",)),
    Rule("**/migrations", ("@dba",)),
]


@pytest.mark.parametrize(
    ("path", "owners"),
    [
        ("main.py", ("@acme/platform",)),  # catch-all
        ("app/pricing.py", ("@dev1", "@acme/billing")),
        ("app/other.py", ("@acme/platform",)),
        ("docs/guide/setup.md", ("@writer",)),  # later rule (*.md) beats /docs/
        ("docs/data.json", ("@dev3",)),
        ("README.md", ("@writer",)),
        ("deep/nested/README.md", ("@writer",)),  # unanchored: any depth
        ("apps/web/tests/test_a.py", ("@qa",)),
        ("apps/web/src/a.py", ("@acme/platform",)),
        ("src/a.py", ("@srcowner",)),
        ("src/sub/a.py", ("@acme/platform",)),  # 'src/*.py' does not cross directories
        ("services/x/migrations/001.sql", ("@dba",)),
        ("migrations/001.sql", ("@dba",)),
        ("/app/pricing.py", ("@dev1", "@acme/billing")),  # leading slash tolerated
    ],
)
def test_last_matching_rule_wins(path: str, owners: tuple[str, ...]) -> None:
    assert owners_for(path, RULES) == owners


def test_no_rules_no_owners() -> None:
    assert owners_for("a.py", []) == ()


def test_special_characters_in_patterns_are_literal() -> None:
    rules = [Rule("/a+b(1).py", ("@x",))]
    assert owners_for("a+b(1).py", rules) == ("@x",)
    assert owners_for("aab(1).py", rules) == ()


def test_assignee_is_the_individual_owning_most_files() -> None:
    who, everyone = choose_assignee(["app/pricing.py"], RULES)
    assert who == "dev1" and everyone == ["@dev1", "@acme/billing"]
    who, _ = choose_assignee(["docs/data.json", "docs/more.json", "app/pricing.py"], RULES)
    assert who == "dev3"  # two files vs one
    who, _ = choose_assignee(["app/pricing.py", "docs/data.json"], RULES)
    assert who == "dev1"  # tie: first seen wins


def test_team_only_ownership_leaves_the_ticket_unassigned_but_lists_the_team() -> None:
    who, everyone = choose_assignee(["main.py"], RULES)
    assert who is None and everyone == ["@acme/platform"]
    assert choose_assignee([], RULES) == (None, [])


def test_email_owners_can_be_assignees() -> None:
    who, _ = choose_assignee(["a.py"], [Rule("*", ("dev@example.com",))])
    assert who == "dev@example.com"
