#!/usr/bin/env python3
"""Render Vikunja tasks and their blocking relations as a GraphViz "dot" graph via
Hochfrequenz's taskdependencygraph + a self-hosted kroki, using its layered
left-to-right layout (rankdir=LR) instead of depviz's grid/rank placement.

Usage:
    python export_raw_graph.py [--out raw_graph.svg]

Config is read from the environment (see docker-compose.yml / .env).

taskdependencygraph is a critical-path-scheduling library repurposed here for
its graph structure only. Vikunja tasks have no duration, so every task gets
a fabricated placeholder planned_duration just to satisfy the library's
required fields and drive its critical-path calculation (shown as a red node
outline) -- the duration itself isn't real. The rendered label is built
ourselves (see build_dot()) rather than via the library's to_dot(), so it
only shows the task name, assignees, and Vikunja label (used here to mark who's
actually responsible, filled in as the node's color) -- each only when present.
"""
import argparse
import asyncio
import os
import re
import sys
import uuid
from datetime import timedelta, timezone
from datetime import datetime as dt

import aiohttp
import requests
from dotenv import load_dotenv
from taskdependencygraph.models.ids import TaskDependencyId, TaskId
from taskdependencygraph.models.task_dependency_edge import TaskDependencyEdge
from taskdependencygraph.models.task_node import TaskNode
from taskdependencygraph.plotting import KrokiClient, KrokiConfig
from taskdependencygraph.task_dependency_graph import TaskDependencyGraph

# Every Vikunja task gets the same placeholder duration -- there is no
# duration concept in Vikunja, and taskdependencygraph requires one to build
# the schedule it needs internally even when only the "dot" graph is rendered.
_PLACEHOLDER_DURATION = timedelta(hours=1)

_UUID_NAMESPACE = uuid.NAMESPACE_URL


def task_uuid(task_id):
    return TaskId(uuid.uuid5(_UUID_NAMESPACE, f"vikunja-task:{task_id}"))


def edge_uuid(blocker_id, blocked_id):
    return TaskDependencyId(uuid.uuid5(_UUID_NAMESPACE, f"vikunja-edge:{blocker_id}->{blocked_id}"))


def get_session(base_url):
    """Return a requests.Session with Vikunja auth already attached."""
    token = os.environ.get("VIKUNJA_API_TOKEN")
    session = requests.Session()

    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        return session

    username = os.environ.get("VIKUNJA_USERNAME")
    password = os.environ.get("VIKUNJA_PASSWORD")
    if not username or not password:
        sys.exit(
            "No credentials found. Set VIKUNJA_API_TOKEN, or VIKUNJA_USERNAME "
            "and VIKUNJA_PASSWORD, in your .env file."
        )

    resp = requests.post(
        f"{base_url}/api/v1/login",
        json={"username": username, "password": password},
        timeout=15,
    )
    resp.raise_for_status()
    jwt = resp.json()["token"]
    session.headers["Authorization"] = f"Bearer {jwt}"
    return session


def fetch_all_tasks(session, base_url, per_page=50):
    """Fetch every task the authenticated user can see, across all projects.

    GET /api/v1/tasks returns all tasks regardless of project, with
    related_tasks already populated (confirmed against a live instance --
    no per-task detail fetch needed).
    """
    tasks = {}
    page = 1
    while True:
        resp = session.get(
            f"{base_url}/api/v1/tasks",
            params={"page": page, "per_page": per_page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for task in batch:
            tasks[task["id"]] = task

        total_pages = int(resp.headers.get("x-pagination-total-pages", page))
        if page >= total_pages:
            break
        page += 1

    return tasks


def assignee_display(task):
    """Comma-joined display names of a task's assignees, or None if unassigned."""
    names = [a.get("name") or a.get("username") for a in task.get("assignees") or []]
    names = [n for n in names if n]
    return ", ".join(names) if names else None


def responsibility_label(task):
    """(comma-joined title(s) of a task's Vikunja labels, first label's hex color)
    or (None, None) if the task has no labels. Vikunja labels are freeform tags
    in general, but here they're used to mark who's actually responsible for a
    task -- if a task somehow carries more than one, only the first one's color
    is usable as a single node fill color, so the rest only show up in the text.
    """
    task_labels = task.get("labels") or []
    if not task_labels:
        return None, None
    text = ", ".join(l["title"] for l in task_labels if l.get("title"))
    color = task_labels[0].get("hex_color") or None
    return (text or None), color


def escape_record_field(text):
    """Escape characters that are syntactically meaningful inside a graphviz
    record-shape label (field separators/nesting) or a quoted attribute value.
    """
    return re.sub(r'([{}|<>"\\])', r"\\\1", text)


def build_graph_inputs(tasks):
    """Turn a dict of {task_id: task} into (task_nodes, dependency_edges, finding_labels, node_meta).

    Only the "blocked" side of each task's related_tasks is read. Vikunja
    stores relations bidirectionally (confirmed live: task A's "blocking"
    list and task B's "blocked" list both describe the same edge), so
    reading just "blocked" gives each real edge exactly once.

    Edges are built as predecessor=blocker, successor=blocked, matching this
    library's to_dot() output (arrow drawn from predecessor to successor) --
    so the rendered arrow points from blocker to blocked, upstream to
    downstream, left to right.
    """
    nodes = {}
    edges = {}
    finding_labels = {}
    node_meta = {}

    def add_node(task):
        tid = task["id"]
        if tid not in nodes:
            node = TaskNode(
                id=task_uuid(tid),
                external_id=f"vikunja:{tid}",
                name=task.get("title") or f"(untitled task {tid})",
                planned_duration=timedelta(0) if task.get("done") else _PLACEHOLDER_DURATION,
            )
            nodes[tid] = node
            finding_labels[node.id] = f"vikunja:{tid} {task.get('title', '')}"
            label_text, label_color = responsibility_label(task)
            node_meta[node.id] = {
                "assignee": assignee_display(task),
                "label_text": label_text,
                "label_color": label_color,
                "done": bool(task.get("done")),
            }

    for task in tasks.values():
        add_node(task)
        related = task.get("related_tasks") or {}
        for blocker in related.get("blocked", []) or []:
            # Vikunja doesn't populate labels/assignees on the embedded task
            # stubs inside related_tasks (confirmed live) -- always resolve
            # back to the full record we already fetched, so which task
            # happens to be processed first doesn't decide whose data wins.
            add_node(tasks.get(blocker["id"], blocker))
            key = (blocker["id"], task["id"])
            if key not in edges:
                edges[key] = TaskDependencyEdge(
                    id=edge_uuid(blocker["id"], task["id"]),
                    task_predecessor=task_uuid(blocker["id"]),
                    task_successor=task_uuid(task["id"]),
                )

    return list(nodes.values()), list(edges.values()), finding_labels, node_meta


def exclude_done_tasks(nodes, edges, node_meta):
    """Drop done tasks from a build_graph_inputs() result, along with any edge
    touching one -- an edge can't reference a node that's no longer there.
    """
    keep_ids = {node.id for node in nodes if not node_meta[node.id]["done"]}
    filtered_nodes = [node for node in nodes if node.id in keep_ids]
    filtered_edges = [
        edge for edge in edges if edge.task_predecessor in keep_ids and edge.task_successor in keep_ids
    ]
    filtered_meta = {node_id: meta for node_id, meta in node_meta.items() if node_id in keep_ids}
    return filtered_nodes, filtered_edges, filtered_meta


def describe_finding(finding, finding_labels):
    task_desc = finding_labels.get(finding.task_id, finding.task_id) if finding.task_id else None
    if task_desc:
        return f"{finding.code}: {finding.message} ({task_desc})"
    return f"{finding.code}: {finding.message}"


def compute_component_sizes(nodes, edges):
    """Size of each node's weakly-connected component (ignoring edge direction).
    A task with no blocking relations at all forms its own component of size 1 --
    exactly the "standalone" tasks the sort in build_dot pushes to the bottom.
    """
    adjacency = {node.id: set() for node in nodes}
    for edge in edges:
        adjacency[edge.task_predecessor].add(edge.task_successor)
        adjacency[edge.task_successor].add(edge.task_predecessor)

    sizes = {}
    seen = set()
    for node in nodes:
        if node.id in seen:
            continue
        component = []
        stack = [node.id]
        seen.add(node.id)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in adjacency[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        for member_id in component:
            sizes[member_id] = len(component)
    return sizes


def build_dot(tdg, nodes, edges, node_meta):
    """Build the dot source ourselves instead of calling TaskDependencyGraph.to_dot().

    That method's label format (external_id/name/assignee-or-"(nobody)"/fabricated
    start time, all four lines always shown) is hardcoded in a private method with
    no way to configure it. TaskNode.to_dot()/TaskDependencyEdge.to_dot() are public
    and just format a given attributes dict, so we reuse those with our own label:
    name, assignees (if any), and the Vikunja label marking who's responsible (if
    any) -- filling the node with that label's color.

    Graphviz's dot engine has no explicit control for vertical order within a
    rankdir=LR column -- it minimizes edge crossings, and falls back to
    declaration order as the initial placement for nodes that don't
    participate in that (standalone tasks have no edges to minimize crossings
    against, so they just stay wherever declaration order put them, which in
    practice meant near the top, since Vikunja returns newer tasks first).

    An earlier version tried to force this explicitly with per-rank
    `{rank=same}` groups plus an invisible same-rank ordering edge chain
    (style=invis, constraint=false). That's a real Graphviz technique in
    general, but it broke here: dot reported "lost edge" on nearly every real
    edge and silently dropped them from the render. The cause is a documented
    dot limitation (visible as a "flat edge ... record shape" warning even
    without the invis edges) where same-rank ("flat") edges between
    record-shaped nodes confuse the layout engine -- not something fixable
    from our side without dropping record shapes for HTML-like labels
    entirely, which would be a much bigger change for a cosmetic ordering fix.

    So instead we lean on the declaration-order fallback directly: nodes are
    emitted sorted by connected-component size up front, without adding any
    new edges at all. Confirmed empirically (not just assumed) against a real
    render: for this graph, dot's mincross placement puts *earlier*-declared
    nodes toward the *bottom* of the rankdir=LR page, not the top -- so
    standalone tasks (component size 1) are declared first/bottom, and bigger
    dependency chains are declared last/top.
    """
    component_sizes = compute_component_sizes(nodes, edges)
    ordered_nodes = sorted(nodes, key=lambda n: (component_sizes[n.id], n.name))

    lines = ["digraph fahrplan{\nrankdir = LR;\nnode [shape=record fontname=Calibri];\n"]
    for node in ordered_nodes:
        meta = node_meta[node.id]
        name = f"✓ {node.name}" if meta["done"] else node.name
        parts = [name]
        if meta["assignee"]:
            parts.append(meta["assignee"])
        if meta["label_text"]:
            parts.append(meta["label_text"])
        attributes = {"label": "|".join(escape_record_field(p) for p in parts)}
        if meta["done"]:
            # Muted regardless of the responsibility label's color, and never
            # flagged critical -- a finished task isn't a schedule risk anymore.
            attributes["style"] = "filled"
            attributes["fillcolor"] = "#e8e8e8"
            attributes["fontcolor"] = "#8a8a8a"
        else:
            if meta["label_color"]:
                attributes["style"] = "filled"
                attributes["fillcolor"] = f"#{meta['label_color']}"
            if tdg.is_on_critical_path(node.id):
                attributes["color"] = "red"
        lines.append(node.to_dot(attributes))
    for edge in edges:
        lines.append(edge.to_dot())
    lines.append("}")
    return "".join(lines)


async def plot_dot_as_svg(session, kroki_host, dot_string):
    """POST the dot source to kroki directly instead of KrokiClient.plot_as_svg(),
    which only accepts a TaskDependencyGraph and always renders it via to_dot().
    """
    payload = {"diagram_source": dot_string, "diagram_type": "graphviz", "output_format": "svg"}
    async with session.post(f"{kroki_host}/graphviz/svg", json=payload) as resp:
        resp.raise_for_status()
        return await resp.text()


async def render_svg(nodes, edges, node_meta, kroki_host):
    tdg = TaskDependencyGraph(
        task_list=nodes,
        dependency_list=edges,
        starting_time_of_run=dt.now(timezone.utc),
    )
    client = KrokiClient(config=KrokiConfig(host=kroki_host))
    try:
        for attempt in range(1, 16):
            if await client.is_ready():
                break
            print(f"Waiting for kroki at {kroki_host} to become ready ({attempt}/15)...", file=sys.stderr)
            await asyncio.sleep(2)
        else:
            sys.exit(f"kroki at {kroki_host} never became ready")

        dot_string = build_dot(tdg, nodes, edges, node_meta)
        async with aiohttp.ClientSession() as session:
            return await plot_dot_as_svg(session, kroki_host, dot_string)
    finally:
        await client.close_session()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="raw_graph.svg", help="Output SVG file path")
    parser.add_argument("--hide-done", action="store_true", help="Exclude done tasks from the graph entirely")
    args = parser.parse_args()

    load_dotenv()
    base_url = os.environ.get("VIKUNJA_URL", "http://localhost:3456").rstrip("/")
    kroki_host = os.environ.get("KROKI_URL", "http://kroki:8000")

    session = get_session(base_url)
    tasks = fetch_all_tasks(session, base_url)
    nodes, edges, finding_labels, node_meta = build_graph_inputs(tasks)

    if args.hide_done:
        nodes, edges, node_meta = exclude_done_tasks(nodes, edges, node_meta)

    validation = TaskDependencyGraph.validate_definition(nodes, edges)
    if not validation.is_valid:
        print("Cannot render: the task relations don't form a valid dependency graph:", file=sys.stderr)
        for finding in validation.findings:
            print(f"  - {describe_finding(finding, finding_labels)}", file=sys.stderr)
        sys.exit(1)

    svg = asyncio.run(render_svg(nodes, edges, node_meta, kroki_host))

    with open(args.out, "w") as f:
        f.write(svg)

    print(f"Wrote {len(nodes)} tasks and {len(edges)} blocking relations to {args.out}")


if __name__ == "__main__":
    main()
