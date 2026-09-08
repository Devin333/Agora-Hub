from __future__ import annotations

from fastapi import FastAPI

from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.api.routers import (
    auth,
    entities,
    events,
    health,
    harness_graph,
    harness_waits,
    mcp,
    memory,
    projects,
    research,
    research_history,
    reports,
    runs,
    schedules,
    sources,
    storage,
    subscriptions,
    workers,
)


def include_routers(api: FastAPI, *, services: ApiServices, helpers: ApiRouteHelpers) -> None:
    for router in (
        health.create_router(services, helpers),
        auth.create_router(services, helpers),
        runs.create_router(services, helpers),
        workers.create_router(services, helpers),
        reports.create_router(services, helpers),
        research.create_router(services, helpers),
        research_history.create_router(services, helpers),
        projects.create_router(services, helpers),
        events.create_router(services, helpers),
        memory.create_router(services, helpers),
        storage.create_router(services, helpers),
        sources.create_router(services, helpers),
        entities.create_router(services, helpers),
        subscriptions.create_router(services, helpers),
        mcp.create_router(services, helpers),
        schedules.create_router(services, helpers),
        harness_graph.create_router(services, helpers),
        harness_waits.create_router(services, helpers),
    ):
        # FastAPI 0.1xx stores an ``_IncludedRouter`` wrapper when using
        # ``include_router``. The application has no router prefixes or
        # include-level dependencies, so append the concrete routes directly;
        # this keeps route introspection and the OpenAPI contract consistent
        # across FastAPI versions.
        api.router.routes.extend(router.routes)
        mark_routes_changed = getattr(api.router, "_mark_routes_changed", None)
        if callable(mark_routes_changed):
            mark_routes_changed()
        for handler in router.on_startup:
            api.router.add_event_handler("startup", handler)
        for handler in router.on_shutdown:
            api.router.add_event_handler("shutdown", handler)
