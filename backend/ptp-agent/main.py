import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import execute
from app.core.grpc_demo_runtime import GrpcDemoRuntime
from app.core.nacos_register import AgentNacosRegister, build_agent_runtime_metadata
from common.config.settings import ensure_host_runtime_safe, settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.TESTING:
        ensure_host_runtime_safe(service_name="ptp-agent")
    registrar = AgentNacosRegister()
    grpc_demo_runtime = GrpcDemoRuntime()
    await registrar.start()
    await grpc_demo_runtime.start()
    yield
    await grpc_demo_runtime.stop()
    await registrar.stop()


app = FastAPI(title="OpenLoadHub Agent", version="2.0.0", docs_url="/api/docs", lifespan=lifespan)
app.include_router(execute.router, prefix="/agent", tags=["execute"])


@app.get("/health")
def health_check():
    metadata = build_agent_runtime_metadata()
    return {
        "status": "ok",
        "service": "ptp-agent",
        "version": settings.VERSION,
        "runtime_kind": metadata.get("runtime_kind"),
        "compose_service": metadata.get("compose_service"),
        "metadata": metadata,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=9096, reload=True)
