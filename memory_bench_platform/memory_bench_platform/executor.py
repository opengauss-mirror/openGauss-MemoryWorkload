from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillCommand:
    script: str
    args: list[str] = field(default_factory=list)

    def to_argv(self) -> list[str]:
        return [self.script, *self.args]
