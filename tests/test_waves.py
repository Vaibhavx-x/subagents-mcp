"""Conflict detection and wave scheduling.

Phase 3 runs a whole wave in parallel, so a wrong answer here does not crash --
it silently produces a worker that read stale content and reported confidently.
That is why the ordering assertions are explicit about which task goes first.
"""

from __future__ import annotations

import pytest
from conftest import make_task

from subagents.errors import PlanRefused
from subagents.waves import assign_waves, build_edges, find_write_write_conflicts, group_into_waves


def refs(waves) -> list[list[str]]:
    return [[t.task_ref for t in wave] for wave in waves]


def test_independent_tasks_share_one_wave():
    tasks = [
        make_task("a", reads=("D:/ws/x.py",), writes=("D:/ws/p.py",)),
        make_task("b", reads=("D:/ws/y.py",), writes=("D:/ws/q.py",)),
    ]
    assert refs(group_into_waves(tasks)) == [["a", "b"]]


def test_reader_and_writer_are_serialised_writer_first():
    tasks = [
        make_task("reader", reads=("D:/ws/f.py",)),
        make_task("writer", writes=("D:/ws/f.py",)),
    ]
    assert refs(group_into_waves(tasks)) == [["writer"], ["reader"]]


def test_case_differing_paths_still_conflict():
    """Windows: 'F.py' and 'f.py' are one file and must serialise."""
    tasks = [
        make_task("reader", reads=("D:/ws/F.py",)),
        make_task("writer", writes=("D:/ws/f.py",)),
    ]
    assert refs(group_into_waves(tasks)) == [["writer"], ["reader"]]


def test_chain_produces_three_waves():
    tasks = [
        make_task("c", reads=("D:/ws/y.py",)),
        make_task("b", reads=("D:/ws/x.py",), writes=("D:/ws/y.py",)),
        make_task("a", writes=("D:/ws/x.py",)),
    ]
    assert refs(group_into_waves(tasks)) == [["a"], ["b"], ["c"]]


def test_diamond_layers_correctly():
    """root -> {left, right} -> join is three waves, not four."""
    tasks = [
        make_task("root", writes=("D:/ws/base.py",)),
        make_task("left", reads=("D:/ws/base.py",), writes=("D:/ws/l.py",)),
        make_task("right", reads=("D:/ws/base.py",), writes=("D:/ws/r.py",)),
        make_task("join", reads=("D:/ws/l.py", "D:/ws/r.py")),
    ]
    assert refs(group_into_waves(tasks)) == [["root"], ["left", "right"], ["join"]]


def test_wave_index_is_longest_path_not_any_topological_position():
    """'late' depends on both a root and a deeper task; it belongs in wave 2."""
    tasks = [
        make_task("a", writes=("D:/ws/1.py",)),
        make_task("b", reads=("D:/ws/1.py",), writes=("D:/ws/2.py",)),
        make_task("late", reads=("D:/ws/1.py", "D:/ws/2.py")),
    ]
    assert assign_waves(tasks)["late"] == 2


def test_write_write_collision_refuses_plan():
    tasks = [
        make_task("a", writes=("D:/ws/same.py",)),
        make_task("b", writes=("D:/ws/same.py",)),
    ]
    with pytest.raises(PlanRefused) as exc:
        group_into_waves(tasks)
    message = str(exc.value)
    assert "same.py" in message
    assert "a" in message and "b" in message


def test_write_write_conflict_detection_lists_shared_paths():
    tasks = [
        make_task("a", writes=("D:/ws/one.py", "D:/ws/two.py")),
        make_task("b", writes=("D:/ws/two.py",)),
    ]
    conflicts = find_write_write_conflicts(tasks)
    assert len(conflicts) == 1
    assert conflicts[0][2] == ["d:\\ws\\two.py"] or "two.py" in conflicts[0][2][0]


def test_cycle_is_refused_and_names_the_tasks():
    """A refusal that does not say which tasks collided is unactionable."""
    tasks = [
        make_task("a", reads=("D:/ws/f.py",), writes=("D:/ws/g.py",)),
        make_task("b", reads=("D:/ws/g.py",), writes=("D:/ws/f.py",)),
    ]
    with pytest.raises(PlanRefused) as exc:
        group_into_waves(tasks)
    message = str(exc.value)
    assert "cycle" in message.lower()
    assert "a" in message and "b" in message


def test_three_task_cycle_is_refused():
    tasks = [
        make_task("a", reads=("D:/ws/c.py",), writes=("D:/ws/a.py",)),
        make_task("b", reads=("D:/ws/a.py",), writes=("D:/ws/b.py",)),
        make_task("c", reads=("D:/ws/b.py",), writes=("D:/ws/c.py",)),
    ]
    with pytest.raises(PlanRefused):
        group_into_waves(tasks)


def test_task_reading_its_own_write_is_not_a_cycle():
    """A task that reads and writes the same file is ordinary, not circular."""
    tasks = [make_task("edit", reads=("D:/ws/f.py",), writes=("D:/ws/f.py",))]
    assert refs(group_into_waves(tasks)) == [["edit"]]


def test_edges_point_from_writer_to_reader():
    tasks = [
        make_task("reader", reads=("D:/ws/f.py",)),
        make_task("writer", writes=("D:/ws/f.py",)),
    ]
    edges = build_edges(tasks)
    assert edges["writer"] == {"reader"}
    assert edges["reader"] == set()


def test_single_task_is_one_wave():
    assert refs(group_into_waves([make_task("solo")])) == [["solo"]]
