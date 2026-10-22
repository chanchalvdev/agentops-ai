from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import ServicesDep

router = APIRouter(tags=["operations"])


@router.get("/health", summary="Liveness: the process is up")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness: dependencies are reachable and models are installed")
async def ready(services: ServicesDep) -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        async with services.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unreachable"

    s = services.settings
    if "ollama" in (s.llm_provider, s.embedding_provider):
        try:
            resp = await services.http.get(f"{s.ollama_base_url}/api/tags", timeout=3)
            resp.raise_for_status()
            installed = {m["name"] for m in resp.json().get("models", [])}
            needed = []
            if s.llm_provider == "ollama":
                needed += [s.llm_model_main, s.llm_model_small]
            if s.embedding_provider == "ollama":
                needed.append(s.embedding_model)
            missing = [m for m in needed if m not in installed and f"{m}:latest" not in installed]
            checks["ollama"] = "ok" if not missing else {"missing_models": missing}
        except Exception:
            checks["ollama"] = "unreachable"

    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
