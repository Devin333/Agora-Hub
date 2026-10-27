"""Lazy re-exports for package facades (PEP 562).

A package ``__init__`` that eagerly imports every public symbol makes importing any
single leaf module of that package pay for the whole package graph: Python always
executes the parent ``__init__`` first. Facades built with :class:`LazyExports`
resolve a re-exported name on first attribute access instead, so
``from framework.harness.runtime.result_errors import X`` loads only what that
module needs, while ``from framework.harness import X`` keeps working unchanged.

Facades keep their eager import statements under ``if TYPE_CHECKING:`` so type
checkers and static import inventories still see the exact same export surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib import import_module
from typing import Any


class LazyExports:
    """Resolve a facade's public names from their owner modules on demand."""

    def __init__(
        self,
        package: str,
        exports: Mapping[str, tuple[str, str]],
        *,
        star_modules: Iterable[str] = (),
        explicit_all: Iterable[str] | None = None,
    ) -> None:
        self._package = package
        self._exports = dict(exports)
        self._star_modules = tuple(star_modules)
        self._explicit_all = list(explicit_all) if explicit_all is not None else None

    def _namespace(self) -> dict[str, Any]:
        return vars(import_module(self._package))

    def _star_names(self, module_name: str) -> list[str]:
        module = import_module(module_name)
        names = getattr(module, "__all__", None)
        if names is None:
            names = [name for name in vars(module) if not name.startswith("_")]
        return list(names)

    def all_names(self) -> list[str]:
        names = list(self._explicit_all) if self._explicit_all is not None else list(self._exports)
        for module_name in self._star_modules:
            names.extend(name for name in self._star_names(module_name) if name not in names)
        return names

    def getattr(self, name: str) -> Any:
        if name == "__all__" and (self._star_modules or self._explicit_all is not None):
            value: Any = self.all_names()
        elif name in self._exports:
            module_name, attribute = self._exports[name]
            value = getattr(import_module(module_name), attribute)
        else:
            for module_name in self._star_modules:
                if name in self._star_names(module_name):
                    value = getattr(import_module(module_name), name)
                    break
            else:
                value = self._submodule(name)
        # Cache on the facade so later lookups are plain attribute reads.
        self._namespace()[name] = value
        return value

    def _submodule(self, name: str) -> Any:
        # An eager facade bound every subpackage it imported from as an attribute
        # (``framework.harness.context``); keep that reachable without importing it
        # up front. Only a missing module named exactly ``package.name`` means the
        # attribute does not exist; errors raised inside it propagate.
        qualified = f"{self._package}.{name}"
        try:
            return import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name != qualified:
                raise
        raise AttributeError(f"module {self._package!r} has no attribute {name!r}")

    def dir(self) -> list[str]:
        names = set(self._namespace()) | set(self._exports)
        for module_name in self._star_modules:
            names.update(self._star_names(module_name))
        return sorted(names)


__all__ = ["LazyExports"]
