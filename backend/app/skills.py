import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.models import ToolExecutionError


_SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class SkillManual(BaseModel):
    name: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillRegistry:
    def __init__(self, skills_dir: str | Path = "skills") -> None:
        self.skills_dir = Path(skills_dir)

    def load(self, skill_name: str) -> SkillManual:
        name = skill_name.strip()
        if not _SKILL_NAME_PATTERN.fullmatch(name):
            raise ToolExecutionError(
                "invalid_skill_name",
                "Skill name must contain only letters, numbers, underscores, or hyphens.",
            )

        skills_root = self.skills_dir.resolve(strict=False)
        skill_dir = (skills_root / name).resolve(strict=False)
        if not self._is_relative_to(skill_dir, skills_root):
            raise ToolExecutionError("invalid_skill_name", "Skill path escapes the skills directory.")
        if not skill_dir.is_dir():
            raise ToolExecutionError("skill_not_found", f"Skill not found: {name}.")

        manual_path = (skill_dir / "SKILL.md").resolve(strict=False)
        if not self._is_relative_to(manual_path, skill_dir):
            raise ToolExecutionError("invalid_skill_name", "Skill manual path is invalid.")
        if not manual_path.is_file():
            raise ToolExecutionError("skill_manual_missing", f"Skill manual is missing: {name}.")

        try:
            content = manual_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(
                "skill_load_failed",
                f"Skill manual could not be read: {name}.",
            ) from exc

        return SkillManual(
            name=name,
            content=content,
            metadata={"content_length": len(content)},
        )

    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True
