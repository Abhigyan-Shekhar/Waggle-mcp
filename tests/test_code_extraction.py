"""Tests for code_extraction module: tree-sitter and regex parse paths."""

from __future__ import annotations

from waggle.code_extraction import CodeEntity, extract_code_entities


class TestCodeExtraction:
    def test_no_code_blocks_returns_empty(self) -> None:
        assert extract_code_entities("just a plain sentence with no code") == []

    def test_python_function_and_class(self) -> None:
        text = "Here:\n```python\nclass UserService:\n    def authenticate(self, user):\n        return True\n```"
        entities = extract_code_entities(text)
        names = {(e.name, e.entity_type) for e in entities}
        assert ("UserService", "class") in names
        assert ("authenticate", "function") in names

    def test_python_async_function(self) -> None:
        text = "```py\nasync def fetch_data(url):\n    pass\n```"
        entities = extract_code_entities(text)
        assert ("fetch_data", "function") in {(e.name, e.entity_type) for e in entities}

    def test_javascript_function_and_class(self) -> None:
        text = "```js\nexport class Widget {}\nfunction render(props) { return null; }\n```"
        entities = extract_code_entities(text)
        names = {(e.name, e.entity_type) for e in entities}
        assert ("Widget", "class") in names
        assert ("render", "function") in names

    def test_javascript_arrow_const(self) -> None:
        text = "```javascript\nconst handleClick = (e) => { console.log(e); };\n```"
        entities = extract_code_entities(text)
        assert ("handleClick", "function") in {(e.name, e.entity_type) for e in entities}

    def test_multiple_blocks_deduped(self) -> None:
        text = "```python\ndef foo():\n    pass\n```\nand\n```python\ndef foo():\n    pass\n```"
        entities = extract_code_entities(text)
        assert sum(1 for e in entities if e.name == "foo") == 1

    def test_language_detected_without_hint(self) -> None:
        text = "```\ndef compute_total(items):\n    return sum(items)\n```"
        entities = extract_code_entities(text)
        assert any(e.name == "compute_total" for e in entities)

    def test_malformed_code_does_not_raise(self) -> None:
        text = "```python\ndef broken(:\n  ???\n```"
        extract_code_entities(text)

    def test_entity_carries_language_and_snippet(self) -> None:
        text = "```python\ndef greet(name):\n    pass\n```"
        entity = next(e for e in extract_code_entities(text) if e.name == "greet")
        assert isinstance(entity, CodeEntity)
        assert entity.language == "python"
        assert "greet" in entity.snippet
