"""Mapper — the anti-corruption layer (domain logic).

Mapper coerces an event payload from one registered schema version to another.
It is how Cavi ERP evolves contracts without flag-day migrations: an old
producer keeps emitting v1, and Mapper upgrades to v2 for consumers that moved
on.

Two design choices do the heavy lifting:

  * **Composition over enumeration** — transforms form a directed graph keyed by
    (subject, from_version, to_version). To go v1 -> v3 you only register
    v1 -> v2 and v2 -> v3; Mapper finds the shortest path by BFS and chains them.
  * **Validate the result against the target schema** — the anti-corruption
    guarantee. A transform that produces a payload violating the target contract
    raises, so a corrupt event can never leave Mapper.
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Mapping

from agents.base.registry import SchemaRegistry

TransformFn = Callable[[dict], dict]


class MapperError(Exception):
    pass


class NoTransformPath(MapperError):
    pass


class Mapper:
    def __init__(
        self, registry: SchemaRegistry | None = None, *, validate_output: bool = True
    ) -> None:
        self._registry = registry or SchemaRegistry()
        self._transforms: dict[tuple[str, int, int], TransformFn] = {}
        self._validate_output = validate_output

    # --- registration -------------------------------------------------------
    def register(
        self, subject: str, from_version: int, to_version: int, fn: TransformFn
    ) -> TransformFn:
        self._transforms[(subject, from_version, to_version)] = fn
        return fn

    def transform_for(self, subject: str, from_version: int, to_version: int):
        """Decorator form of `register`."""

        def decorator(fn: TransformFn) -> TransformFn:
            return self.register(subject, from_version, to_version, fn)

        return decorator

    # --- coercion -----------------------------------------------------------
    def transform(
        self, subject: str, from_version: int, to_version: int, payload: Mapping
    ) -> dict:
        """Coerce `payload` from `from_version` to `to_version`.

        Composes registered transforms along the shortest version path, then
        validates the result against the target schema (unless disabled).
        """
        result = dict(payload)
        if from_version != to_version:
            path = self._find_path(subject, from_version, to_version)
            if path is None:
                raise NoTransformPath(
                    f"no transform path for {subject} v{from_version} -> v{to_version}"
                )
            for step in path:
                result = self._transforms[(subject, step[0], step[1])](result)

        if self._validate_output:
            self._registry.validate(subject, to_version, result)
        return result

    def _find_path(
        self, subject: str, src: int, dst: int
    ) -> list[tuple[int, int]] | None:
        """Shortest version path as a list of (from, to) edges, or None."""
        adjacency: dict[int, list[int]] = {}
        for (subj, a, b) in self._transforms:
            if subj == subject:
                adjacency.setdefault(a, []).append(b)

        queue: deque[list[int]] = deque([[src]])
        seen = {src}
        while queue:
            node_path = queue.popleft()
            node = node_path[-1]
            if node == dst:
                return list(zip(node_path, node_path[1:]))
            for nxt in sorted(adjacency.get(node, [])):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(node_path + [nxt])
        return None
