from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

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
