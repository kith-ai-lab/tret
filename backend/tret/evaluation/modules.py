"""Module sets: what the Stage 2 extractor looks for in a report.

A module set is a YAML file (`module_set`, `version`, `modules`); each module has a `slug`,
a `title`, a `definition` (what counts), `include` / `exclude` bullet lists and `max_words`
(default 4000), the most text kept for the module. `TRET_DISCLOSURE_MODULE_SET` names a file;
empty, the built-in `data/modules-v0.1.yaml` is used. A private pack may supply a tuned set
through that setting. The extractor records `<module_set>@<version>` on every extraction, so a
changed set never overwrites the results of an earlier one.
"""
from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
SET_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
DEFAULT_MAX_WORDS = 4000
MAX_MODULES = 24
DEFAULT_RESOURCE = "modules-v0.1.yaml"


class ModuleSetError(ValueError):
    """A module-set file that cannot be used; the message says why."""


class Module(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=4000)
    include: list[str] = Field(default_factory=list, max_length=40)
    exclude: list[str] = Field(default_factory=list, max_length=40)
    max_words: int = Field(default=DEFAULT_MAX_WORDS, ge=100, le=100_000)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must be 2-60 lowercase letters, digits or underscores, starting with a letter")
        return v


class ModuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_set: str
    version: str = Field(min_length=1, max_length=40)
    modules: list[Module] = Field(min_length=1, max_length=MAX_MODULES)

    @field_validator("module_set")
    @classmethod
    def _set_slug(cls, v: str) -> str:
        if not SET_SLUG_RE.match(v):
            raise ValueError("module_set must be a short lowercase slug")
        return v

    @field_validator("version", mode="before")
    @classmethod
    def _version_text(cls, v: Any) -> str:
        # Never coerced: YAML reads an unquoted 1.10 as the number 1.1, a different version.
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f'version must be a quoted string such as "0.1" (got {v!r})')
        return v.strip()

    @model_validator(mode="after")
    def _unique(self) -> "ModuleSet":
        slugs = [m.slug for m in self.modules]
        dupes = sorted({s for s in slugs if slugs.count(s) > 1})
        if dupes:
            raise ValueError(f"duplicate module slug(s): {', '.join(dupes)}")
        return self

    @property
    def ident(self) -> str:
        """`slug@version`, as stored on `report_extractions.module_set`."""
        return f"{self.module_set}@{self.version}"

    def get(self, slug: str) -> Module | None:
        return next((m for m in self.modules if m.slug == slug), None)

    @property
    def slugs(self) -> list[str]:
        return [m.slug for m in self.modules]


def parse_module_set(text: str, *, source: str = "module set") -> ModuleSet:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ModuleSetError(f"{source}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ModuleSetError(f"{source}: expected a mapping with module_set, version and modules")
    try:
        return ModuleSet.model_validate(raw)
    except ValueError as e:
        raise ModuleSetError(f"{source}: {e}") from e


def load_module_set(path: str | Path | None = None) -> ModuleSet:
    """The module set in the YAML file at `path`, else the one `TRET_DISCLOSURE_MODULE_SET`
    names, else the built-in default. Raises `ModuleSetError`."""
    if path is None or not str(path).strip():
        from tret.config import get_settings

        path = (get_settings().disclosure_module_set or "").strip() or None
    if path is None:
        return default_module_set()
    p = Path(str(path)).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ModuleSetError(f"module set file {p} cannot be read: {e.strerror or e}") from e
    return parse_module_set(text, source=str(p))


@lru_cache(maxsize=1)
def default_module_set() -> ModuleSet:
    text = resources.files("tret.evaluation").joinpath("data", DEFAULT_RESOURCE).read_text(encoding="utf-8")
    return parse_module_set(text, source=DEFAULT_RESOURCE)
