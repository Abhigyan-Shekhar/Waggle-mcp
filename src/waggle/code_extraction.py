"""Extract code entities (functions, classes) from conversation text.

When a user pastes code into a conversation, Waggle can pull out the named
symbols and store them as ENTITY nodes, linking conversation context to code
structure (and, if a Graphify graph was imported, to the actual codebase graph).

Parsing strategy:
  1. Locate fenced code blocks (```lang ... ```) in the text.
  2. For each block, detect the language (from the fence hint or a heuristic).
  3. Parse with tree-sitter when the optional ``code-analysis`` extra is
     installed; otherwise fall back to language-aware regular expressions.

The regex fallback is always available, so this module never hard-depends on
tree-sitter. Install ``waggle-mcp[code-analysis]`` for more accurate parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# Fenced code block: ```lang\n ... \n```  (lang optional)
_FENCE_RE = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)

# Language hint normalization
_LANG_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "node": "javascript",
}

# Regex symbol extractors per language family.
_PY_FUNC_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_PY_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*[:\(]", re.MULTILINE)

_JS_FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)
_JS_CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
# const foo = (...) => / const foo = function / const foo = async (...) =>
_JS_CONST_FN_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class CodeEntity:
    """A named symbol extracted from a code block."""

    name: str
    entity_type: str  # "function" | "class"
    language: str
    snippet: str  # the line(s) the symbol was declared on (best-effort)


def _normalize_language(hint: str, code: str) -> str:
    """Resolve a fence hint (or guess from code) to a canonical language name."""
    lang = _LANG_ALIASES.get(hint.strip().lower(), hint.strip().lower())
    if lang in {"python", "javascript", "typescript"}:
        return lang
    # Heuristic guess when no/unknown hint
    if re.search(r"^\s*def\s+\w+\s*\(", code, re.MULTILINE) or ("import " in code and ":" in code):
        return "python"
    if re.search(r"\bfunction\b|=>|\bconst\b|\blet\b", code):
        return "javascript"
    return lang or "unknown"


def _snippet_for(code: str, name: str) -> str:
    """Return the first source line mentioning ``name`` (trimmed), else name."""
    for line in code.splitlines():
        if name in line:
            return line.strip()[:200]
    return name


def _regex_extract(code: str, language: str) -> list[CodeEntity]:
    entities: list[CodeEntity] = []
    seen: set[tuple[str, str]] = set()

    def _add(name: str, entity_type: str) -> None:
        key = (name, entity_type)
        if key in seen:
            return
        seen.add(key)
        entities.append(
            CodeEntity(name=name, entity_type=entity_type, language=language, snippet=_snippet_for(code, name))
        )

    if language == "python":
        for m in _PY_CLASS_RE.finditer(code):
            _add(m.group(1), "class")
        for m in _PY_FUNC_RE.finditer(code):
            _add(m.group(1), "function")
    elif language in {"javascript", "typescript"}:
        for m in _JS_CLASS_RE.finditer(code):
            _add(m.group(1), "class")
        for m in _JS_FUNC_RE.finditer(code):
            _add(m.group(1), "function")
        for m in _JS_CONST_FN_RE.finditer(code):
            _add(m.group(1), "function")
    else:
        # Generic best-effort: try both families
        for m in _PY_CLASS_RE.finditer(code):
            _add(m.group(1), "class")
        for m in _PY_FUNC_RE.finditer(code):
            _add(m.group(1), "function")
        for m in _JS_FUNC_RE.finditer(code):
            _add(m.group(1), "function")

    return entities


@lru_cache(maxsize=1)
def _tree_sitter_available() -> bool:
    try:
        import tree_sitter_language_pack  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return False


def _tree_sitter_extract(code: str, language: str) -> list[CodeEntity] | None:
    """Parse with tree-sitter if installed. Returns None to signal fallback."""
    if not _tree_sitter_available():
        return None
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]

        parser = get_parser(language)
        tree = parser.parse(code.encode("utf-8"))
    except Exception:
        return None

    entities: list[CodeEntity] = []
    seen: set[tuple[str, str]] = set()
    # Node types that carry a name child, per the common tree-sitter grammars
    func_types = {"function_definition", "function_declaration", "method_definition", "arrow_function"}
    class_types = {"class_definition", "class_declaration"}

    def _walk(node: object) -> None:
        node_type = getattr(node, "type", "")
        if node_type in func_types or node_type in class_types:
            name_node = node.child_by_field_name("name") if hasattr(node, "child_by_field_name") else None
            if name_node is not None:
                name = code[name_node.start_byte : name_node.end_byte]
                entity_type = "class" if node_type in class_types else "function"
                key = (name, entity_type)
                if name and key not in seen:
                    seen.add(key)
                    entities.append(
                        CodeEntity(
                            name=name,
                            entity_type=entity_type,
                            language=language,
                            snippet=_snippet_for(code, name),
                        )
                    )
        for child in getattr(node, "children", []):
            _walk(child)

    _walk(tree.root_node)
    return entities


def extract_code_entities(text: str) -> list[CodeEntity]:
    """Extract named code symbols from all fenced code blocks in ``text``.

    Returns a de-duplicated list of :class:`CodeEntity`. If no fenced code
    blocks are present, returns an empty list. Never raises on malformed code.
    """
    if not text or "```" not in text:
        return []

    results: list[CodeEntity] = []
    seen: set[tuple[str, str]] = set()
    for match in _FENCE_RE.finditer(text):
        hint, code = match.group(1), match.group(2)
        if not code.strip():
            continue
        language = _normalize_language(hint, code)
        entities = _tree_sitter_extract(code, language)
        if entities is None:
            entities = _regex_extract(code, language)
        for entity in entities:
            key = (entity.name, entity.entity_type)
            if key in seen:
                continue
            seen.add(key)
            results.append(entity)
    return results
