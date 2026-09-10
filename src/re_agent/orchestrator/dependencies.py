"""Deterministic prerequisite chains with iterative strongly connected components."""
from __future__ import annotations


def prerequisites(order: list[str], edges: dict[str, set[str]]) -> dict[str, set[str]]:
    """Collapse cycles, then serialize each cycle while allowing independent SCCs."""
    nodes = set(order)
    edges = {n: edges.get(n, set()) & nodes for n in order}
    seen: set[str] = set()
    finish: list[str] = []
    for root in order:
        stack = [(root, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                finish.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in sorted(edges[node], reverse=True) if child not in seen)
    reverse: dict[str, set[str]] = {n: set() for n in order}
    for node, children in edges.items():
        for child in children:
            reverse[child].add(node)
    groups: list[list[str]] = []
    membership: dict[str, int] = {}
    rank = {n: i for i, n in enumerate(order)}
    for root in reversed(finish):
        if root in membership:
            continue
        group: list[str] = []
        todo = [root]
        while todo:
            node = todo.pop()
            if node in membership:
                continue
            membership[node] = len(groups)
            group.append(node)
            todo.extend(reverse[node])
        groups.append(sorted(group, key=rank.__getitem__))
    result: dict[str, set[str]] = {}
    for group in groups:
        required = {groups[membership[child]][-1] for node in group for child in edges[node]
                    if membership[child] != membership[node]}
        for index, node in enumerate(group):
            result[node] = required if index == 0 else {group[index - 1]}
    return result
