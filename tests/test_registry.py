"""Registry: prompt file parsing (YAML + Markdown frontmatter)."""

import pytest

from promptpin import registry
from promptpin.errors import InvalidPromptError, PromptNotFoundError

YAML_OK = """\
name: classifier
description: Ticket classifier
model: gpt-4o-mini
params:
  temperature: 0.2
variables:
  - ticket_text
template: |
  Classify: {{ticket_text}}
"""

MD_OK = """\
---
name: summarizer
description: One-line summarizer
model: gpt-4o-mini
params:
  temperature: 0.5
variables:
  - text
---
Summarize in one sentence:

{{text}}
"""


def test_parse_yaml_prompt():
    p = registry.parse_text(YAML_OK, "prompts/classifier.yaml")
    assert p.name == "classifier"
    assert p.model == "gpt-4o-mini"
    assert p.params == {"temperature": 0.2}
    assert p.variables == ["ticket_text"]
    assert "{{ticket_text}}" in p.template


def test_parse_markdown_frontmatter():
    p = registry.parse_text(MD_OK, "prompts/summarizer.md")
    assert p.name == "summarizer"
    assert p.template.strip().endswith("{{text}}")
    assert "Summarize in one sentence" in p.template


def test_missing_name_raises():
    with pytest.raises(InvalidPromptError, match="name"):
        registry.parse_text("template: hi\n", "p.yaml")


def test_missing_template_raises():
    with pytest.raises(InvalidPromptError, match="template"):
        registry.parse_text("name: x\n", "p.yaml")


def test_markdown_without_frontmatter_raises():
    with pytest.raises(InvalidPromptError, match="frontmatter"):
        registry.parse_text("just some text\n", "p.md")


def test_bad_yaml_raises():
    with pytest.raises(InvalidPromptError, match="invalid YAML"):
        registry.parse_text("name: [unclosed\ntemplate: x\n", "p.yaml")


def test_bad_name_characters_raises():
    with pytest.raises(InvalidPromptError, match="must match"):
        registry.parse_text("name: 'has space'\ntemplate: x\n", "p.yaml")


def test_params_must_be_mapping():
    with pytest.raises(InvalidPromptError, match="params"):
        registry.parse_text("name: x\ntemplate: y\nparams: 5\n", "p.yaml")


def test_variables_must_be_string_list():
    with pytest.raises(InvalidPromptError, match="variables"):
        registry.parse_text("name: x\ntemplate: y\nvariables: [1, 2]\n", "p.yaml")


def test_template_variables_extraction():
    p = registry.parse_text(YAML_OK, "p.yaml")
    assert p.template_variables() == ["ticket_text"]


def test_template_sha256_stable():
    a = registry.parse_text(YAML_OK, "p.yaml")
    b = registry.parse_text(YAML_OK, "p.yaml")
    assert a.template_sha256 == b.template_sha256
    assert len(a.template_sha256) == 64


def test_load_and_list_prompt(prompt_repo):
    from promptpin.config import Config

    cfg = Config.load(prompt_repo)
    names = registry.list_prompts(prompt_repo, cfg.prompts_dir)
    assert names == ["classifier"]
    p = registry.load_prompt(prompt_repo, cfg.prompts_dir, "classifier")
    assert p.name == "classifier"


def test_load_missing_prompt_raises(prompt_repo):
    from promptpin.config import Config

    cfg = Config.load(prompt_repo)
    with pytest.raises(PromptNotFoundError, match="nope"):
        registry.load_prompt(prompt_repo, cfg.prompts_dir, "nope")


def test_filename_must_match_prompt_name(prompt_repo, tmp_path):
    from promptpin.config import Config

    (prompt_repo / "prompts" / "other.yaml").write_text(
        "name: different\ntemplate: hi\n", encoding="utf-8"
    )
    cfg = Config.load(prompt_repo)
    with pytest.raises(InvalidPromptError, match="does not match"):
        registry.load_prompt(prompt_repo, cfg.prompts_dir, "other")
