#!/usr/bin/env python3
"""Tiny always-on HTTP wrapper around export_raw_graph's pipeline.

Serves the same GraphViz SVG that export_raw_graph.py writes to a file, but
generated fresh on every request and returned directly, so it can sit behind
Caddy and be opened straight in a browser (see Caddyfile's /raw-graph* route).

Gated by HTTP Basic Auth via RAW_GRAPH_USERNAME/RAW_GRAPH_PASSWORD -- if
either is unset, every request is rejected (fail closed, not fail open).
"""
import asyncio
import base64
import os
import secrets

from aiohttp import web
from taskdependencygraph.task_dependency_graph import TaskDependencyGraph

from export_raw_graph import (
    build_graph_inputs,
    describe_finding,
    exclude_done_tasks,
    fetch_all_tasks,
    get_session,
    render_svg,
)

_TRUTHY = {"1", "true", "yes"}


def _check_auth(request):
    username = os.environ.get("RAW_GRAPH_USERNAME") or ""
    password = os.environ.get("RAW_GRAPH_PASSWORD") or ""
    if not username or not password:
        return False
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[len("Basic ") :]).decode("utf-8")
        req_user, _, req_pass = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(req_user, username) and secrets.compare_digest(req_pass, password)


@web.middleware
async def basic_auth_middleware(request, handler):
    if request.path == "/health":
        return await handler(request)
    if not _check_auth(request):
        return web.Response(
            status=401,
            text="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="raw-graph"'},
        )
    return await handler(request)


async def handle_graph(request):
    base_url = os.environ.get("VIKUNJA_URL", "http://api:3456").rstrip("/")
    kroki_host = os.environ.get("KROKI_URL", "http://kroki:8000")

    # get_session/fetch_all_tasks use `requests` (blocking); run them off the
    # event loop so one slow Vikunja response doesn't stall other requests.
    session = await asyncio.to_thread(get_session, base_url)
    tasks = await asyncio.to_thread(fetch_all_tasks, session, base_url)
    nodes, edges, finding_labels, node_meta = build_graph_inputs(tasks)

    if request.query.get("hide_done", "").lower() in _TRUTHY:
        nodes, edges, node_meta = exclude_done_tasks(nodes, edges, node_meta)

    validation = TaskDependencyGraph.validate_definition(nodes, edges)
    if not validation.is_valid:
        problems = "\n".join(f"- {describe_finding(f, finding_labels)}" for f in validation.findings)
        return web.Response(
            status=500,
            text=f"Cannot render: the task relations don't form a valid dependency graph:\n{problems}",
        )

    svg = await render_svg(nodes, edges, node_meta, kroki_host)
    return web.Response(text=svg, content_type="image/svg+xml")


async def handle_health(request):
    return web.Response(text="ok")


def main():
    if not os.environ.get("RAW_GRAPH_USERNAME") or not os.environ.get("RAW_GRAPH_PASSWORD"):
        raise SystemExit("RAW_GRAPH_USERNAME and RAW_GRAPH_PASSWORD must both be set")
    if not os.environ.get("VIKUNJA_API_TOKEN") and not (
        os.environ.get("VIKUNJA_USERNAME") and os.environ.get("VIKUNJA_PASSWORD")
    ):
        raise SystemExit("VIKUNJA_API_TOKEN, or VIKUNJA_USERNAME and VIKUNJA_PASSWORD, must be set")

    app = web.Application(middlewares=[basic_auth_middleware])
    app.router.add_get("/", handle_graph)
    app.router.add_get("/health", handle_health)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
