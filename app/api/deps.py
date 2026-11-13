from typing import Annotated, Any

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from app.core.errors import PermissionDeniedError, TooManyRequestsError
from app.core.logging import tenant_id_var
from app.core.security import Principal
from app.services import Services

_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


async def get_principal(request: Request, api_key: str | None = Security(_api_key)) -> Principal:
    # async on purpose: FastAPI runs sync dependencies in a worker thread, so a ContextVar set
    # there would not be visible to the endpoint (and its log lines would lose the tenant).
    principal = get_services(request).auth.authenticate(api_key)
    tenant_id_var.set(principal.tenant_id)  # for log lines emitted inside the endpoint
    request.state.tenant_id = principal.tenant_id  # for the access log in the middleware
    return principal


ServicesDep = Annotated[Services, Depends(get_services)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


def rate_limited(route: str) -> Any:
    """Dependency: one token from the caller's bucket for this route class, or 429."""

    async def check(request: Request, principal: PrincipalDep) -> None:
        services = get_services(request)
        if not services.settings.rate_limit_enabled:
            return
        wait = services.rate_limiter.acquire(principal.tenant_id, route)
        if wait is not None:
            raise TooManyRequestsError(
                f"Rate limit exceeded for {route} requests", retry_after_s=wait
            )

    return Depends(check)


async def require_admin(request: Request, principal: PrincipalDep) -> Principal:
    """Evaluations, experiments, and system-wide monitoring are operator functions: they run
    the shared local model for minutes and read data across tenants."""
    if principal.tenant_id not in get_services(request).settings.admin_tenant_ids:
        raise PermissionDeniedError("This operation requires an operator (admin) tenant")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]
