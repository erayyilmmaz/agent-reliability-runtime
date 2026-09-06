from fastapi import FastAPI

from agent_runtime.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the HTTP process without initializing workers or providers."""

    runtime_settings = settings or get_settings()
    app = FastAPI(title="Agent Reliability Runtime", version="0.1.0")
    app.state.settings = runtime_settings

    @app.get("/healthz", tags=["operations"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "api"}

    return app


app = create_app()
