"""Read/write conflict detection and wave scheduling.

Two tasks writing the same file is never acceptable and refuses the plan.

A read/write overlap is not refused -- it is serialised. If task A reads a file
task B writes, the edge runs B -> A: the WRITER GOES FIRST, so the reader
observes the post-change state and a fan-out behaves like the sequential script
the parent would otherwise have written by hand.

Waves run sequentially; the workers inside one wave run in parallel (Phase 3).
"""

from __future__ import annotations

from collections import deque

from .errors import PlanRefused
from .models import Task


def find_write_write_conflicts(tasks: list[Task]) -> list[tuple[str, str, list[str]]]:
    out: list[tuple[str, str, list[str]]] = []
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            shared = sorted(set(a.writes_norm) & set(b.writes_norm))
            if shared:
                out.append((a.task_ref, b.task_ref, shared))
    return out


def build_edges(tasks: list[Task]) -> dict[str, set[str]]:
    """Return writer -> {readers}. An edge means the writer must finish first."""
    edges: dict[str, set[str]] = {t.task_ref: set() for t in tasks}
    for reader in tasks:
        if not reader.reads:
            continue
        reads = set(reader.reads_norm)
        for writer in tasks:
            if writer.task_ref == reader.task_ref:
                continue
            if reads & set(writer.writes_norm):
                edges[writer.task_ref].add(reader.task_ref)
    return edges


def _find_cycle(edges: dict[str, set[str]]) -> list[str]:
    """Return one concrete cycle as a path, for an actionable error message."""
    colour: dict[str, int] = {n: 0 for n in edges}  # 0 unvisited, 1 in stack, 2 done
    stack: list[str] = []

    def walk(node: str) -> list[str] | None:
        colour[node] = 1
        stack.append(node)
        for nxt in sorted(edges[node]):
            if colour[nxt] == 1:
                return stack[stack.index(nxt):] + [nxt]
            if colour[nxt] == 0:
                found = walk(nxt)
                if found:
                    return found
        stack.pop()
        colour[node] = 2
        return None

    for node in sorted(edges):
        if colour[node] == 0:
            found = walk(node)
            if found:
                return found
    return []


def assign_waves(tasks: list[Task]) -> dict[str, int]:
    """Return {task_ref: wave_index}. Refuses on write-write overlap or a cycle."""
    conflicts = find_write_write_conflicts(tasks)
    if conflicts:
        detail = "; ".join(
            f"{a} and {b} both write {', '.join(paths)}" for a, b, paths in conflicts
        )
        raise PlanRefused("two tasks write the same file", detail)

    edges = build_edges(tasks)
    indegree: dict[str, int] = {t.task_ref: 0 for t in tasks}
    for _writer, readers in edges.items():
        for reader in readers:
            indegree[reader] += 1

    # Kahn's algorithm, carrying a level so wave_index is the longest path from
    # a root rather than merely *a* topological position.
    wave: dict[str, int] = {ref: 0 for ref in indegree}
    queue = deque(sorted(ref for ref, deg in indegree.items() if deg == 0))
    processed = 0

    while queue:
        node = queue.popleft()
        processed += 1
        for nxt in sorted(edges[node]):
            wave[nxt] = max(wave[nxt], wave[node] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if processed != len(tasks):
        cycle = _find_cycle(edges)
        detail = (
            " -> ".join(cycle)
            if cycle
            else ", ".join(sorted(ref for ref, deg in indegree.items() if deg > 0))
        )
        raise PlanRefused(
            "read/write dependencies form a cycle",
            f"{detail} -- each task reads a file another writes, so no ordering is safe",
        )

    return wave


def group_into_waves(tasks: list[Task]) -> list[list[Task]]:
    wave_of = assign_waves(tasks)
    count = max(wave_of.values()) + 1 if wave_of else 0
    waves: list[list[Task]] = [[] for _ in range(count)]
    for task in sorted(tasks, key=lambda t: t.task_ref):
        waves[wave_of[task.task_ref]].append(task)
    return waves
