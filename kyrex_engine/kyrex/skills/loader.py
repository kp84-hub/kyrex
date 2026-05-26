from pathlib import Path


class Skill:
    def __init__(self, name: str, description: str, instructions: str):
        self.name = name
        self.description = description
        self.instructions = instructions


class SkillsLoader:
    def __init__(self, search_dirs: list[Path] | None = None):
        self.search_dirs = search_dirs or [
            Path.home() / ".kyrex" / "skills",
            Path(".px_skills"),
        ]
        self._cache: dict[str, Skill] = {}

    def discover(self) -> dict[str, Skill]:
        skills = {}
        for d in self.search_dirs:
            if not d.exists():
                continue
            for p in sorted(d.glob("*.md")):
                name = p.stem
                if name in skills:
                    continue
                content = p.read_text(errors="ignore").strip()
                lines = content.split("\n", 2)
                description = (
                    lines[0].lstrip("#").strip() if lines else name
                )
                skills[name] = Skill(
                    name=name, description=description, instructions=content
                )
        self._cache = skills
        return skills

    def get(self, name: str) -> Skill | None:
        if name not in self._cache:
            self.discover()
        return self._cache.get(name)

    def match(self, user_input: str) -> Skill | None:
        self.discover()
        lower = user_input.lower()

        for name, skill in self._cache.items():
            if name.lower() == lower:
                return skill

        best_match = None
        best_score = -1

        for name, skill in self._cache.items():
            score = sum(1 for word in name.lower().split("_") if word in lower)
            if score > best_score:
                best_score = score
                best_match = skill

        return best_match if best_score > 0 else None


__all__ = ["Skill", "SkillsLoader"]
