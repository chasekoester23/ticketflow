"""Authoritative list of API routes and the permission each one requires.

Walks our own routers rather than ``app.routes``: FastAPI's internal route
structure changes between versions, and a walk that silently finds nothing
would make every guard test pass without checking anything. ``openapi_paths``
gives an independent view so a router that is mounted but not listed here is
detected.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.routing import APIRoute

from .main import app
from .routers import api, auth, invoice_documents, invoices, pricing, projects, records, reports, scans, setup, system, team

ROUTERS = (auth.router, setup.router, api.router, pricing.router, invoices.router,
           projects.router, reports.router, records.router, system.router, team.router, scans.router, invoice_documents.router)
# Sign-in, sign-out and changing your own password act on the caller alone. First-run
# setup works only while no user exists, so there's nobody yet to need a permission.
SELF_SERVICE = {"/api/auth/login", "/api/auth/logout", "/api/auth/password", "/api/setup"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# Declared directly on the app (read-only, requires sign-in; see main.py).
APP_LEVEL = {("GET", "/api/status")}


@dataclass(frozen=True)
class RouteInfo:
    method: str
    path: str
    permissions: tuple[str, ...]


def _permissions(dependant) -> list[str]:
    found: list[str] = []
    for dep in dependant.dependencies:
        permission = getattr(dep.call, "permission", None)
        if permission:
            found.append(permission)
        found.extend(_permissions(dep))
    return found


def all_routes() -> list[RouteInfo]:
    routes = []
    for router in ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                routes.append(RouteInfo(method, route.path,
                                        tuple(sorted(set(_permissions(route.dependant))))))
    return sorted(routes, key=lambda r: (r.path, r.method))


def openapi_paths() -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, item in app.openapi()["paths"].items()
            for method in item if path.startswith("/api/")} - APP_LEVEL
