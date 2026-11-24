import hashlib
import json
from pathlib import Path

from markupsafe import Markup as Safe
from markupsafe import escape


class Octicons:
    """Inline SVG icons from GitHub's MIT-licensed Primer Octicons set (16 px paths in ``static/octicons.json``)."""

    def __init__(self, path: Path) -> None:
        self._paths: dict[str, str] = json.loads(path.read_text())

    def __call__(self, name: str, size: int = 16, classes: str = "") -> Safe:
        """Return the icon ``name`` as an inline SVG element of ``size`` pixels."""
        return Safe(
            f'<svg class="octicon octicon-{escape(name)} {escape(classes)}" viewBox="0 0 16 16" width="{size}" '
            f'height="{size}" aria-hidden="true" fill="currentColor">{self._paths[name]}</svg>'
        )


class Identicon:
    """Deterministic five-by-five symmetric avatars in the style of GitHub's default identicons.

    The collection holds no avatar images, so every login gets a generated pattern
    derived from the MD5 digest of its name.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def __call__(self, login: str | None, size: int = 20, square: bool = False) -> Safe:
        """Return the avatar of ``login`` as an inline SVG element of ``size`` pixels."""
        name = login or "ghost"

        if name not in self._cache:
            self._cache[name] = self._pattern(name)

        shape = "avatar square" if square else "avatar"
        return Safe(
            f'<svg class="{shape}" viewBox="0 0 6 6" width="{size}" height="{size}" role="img" '
            f'aria-label="{escape(name)}"><title>{escape(name)}</title>{self._cache[name]}</svg>'
        )

    def _pattern(self, name: str) -> str:
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()
        hue = int(digest[-7:-4], 16) * 360 // 4096
        cells = []

        for index in range(15):
            if int(digest[index], 16) % 2:
                continue

            column, row = index // 5, index % 5
            cells.append(f"M{column + 0.5} {row + 0.5}h1v1h-1z")

            if column < 2:
                cells.append(f"M{4.5 - column} {row + 0.5}h1v1h-1z")

        return f'<rect width="6" height="6" fill="#f0f0f0"/><path fill="hsl({hue} 55% 58%)" d="{"".join(cells)}"/>'
