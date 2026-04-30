"""Language detection by file extension and tree-sitter parser cache."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Map extension -> (language_id used by tree-sitter-language-pack, label)
EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
}


def detect_language(path: Path) -> str | None:
    return EXT_LANGUAGE.get(path.suffix.lower())


@lru_cache(maxsize=64)
def get_parser(language: str):
    """Return a tree-sitter Parser configured for the given language id.

    Uses tree-sitter-language-pack (Goldziher) which ships precompiled wheels.
    """
    try:
        from tree_sitter_language_pack import get_parser as _get_parser
    except ImportError as e:
        raise RuntimeError(
            "tree-sitter-language-pack not installed. "
            "Run: pip install tree-sitter-language-pack"
        ) from e
    return _get_parser(language)
