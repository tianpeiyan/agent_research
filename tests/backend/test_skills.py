import pytest

from app.models import ToolExecutionError
from app.skills import SkillRegistry


def test_skill_registry_loads_skill_manual(tmp_path):
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Foo\n\nUse carefully.", encoding="utf-8")

    manual = SkillRegistry(tmp_path / "skills").load("foo")

    assert manual.name == "foo"
    assert manual.content == "# Foo\n\nUse carefully."
    assert manual.metadata == {"content_length": len(manual.content)}


def test_skill_registry_returns_testable_error_when_skill_is_missing(tmp_path):
    with pytest.raises(ToolExecutionError) as exc_info:
        SkillRegistry(tmp_path / "skills").load("missing")

    assert exc_info.value.code == "skill_not_found"


def test_skill_registry_returns_testable_error_when_manual_is_missing(tmp_path):
    (tmp_path / "skills" / "foo").mkdir(parents=True)

    with pytest.raises(ToolExecutionError) as exc_info:
        SkillRegistry(tmp_path / "skills").load("foo")

    assert exc_info.value.code == "skill_manual_missing"


@pytest.mark.parametrize("skill_name", ["../foo", "foo/bar", ".hidden", "foo bar"])
def test_skill_registry_rejects_unsafe_skill_names(tmp_path, skill_name):
    with pytest.raises(ToolExecutionError) as exc_info:
        SkillRegistry(tmp_path / "skills").load(skill_name)

    assert exc_info.value.code == "invalid_skill_name"


def test_skill_registry_does_not_execute_scripts(tmp_path):
    skill_dir = tmp_path / "skills" / "foo"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Foo", encoding="utf-8")
    marker = tmp_path / "executed.txt"
    (scripts_dir / "run.ps1").write_text(
        f"Set-Content -LiteralPath '{marker}' -Value executed",
        encoding="utf-8",
    )

    manual = SkillRegistry(tmp_path / "skills").load("foo")

    assert manual.content == "# Foo"
    assert not marker.exists()


def test_skill_registry_does_not_read_outside_skills_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside", encoding="utf-8")

    with pytest.raises(ToolExecutionError) as exc_info:
        SkillRegistry(tmp_path / "skills").load("../outside")

    assert exc_info.value.code == "invalid_skill_name"
