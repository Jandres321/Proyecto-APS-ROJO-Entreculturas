"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import findings, projects
from app.config import get_settings
from app.schemas import HealthOut

settings = get_settings()

app = FastAPI(
    title="Revisor de justificación",
    description="Revisión asistida de la justificación económica de proyectos de cooperación.",
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthOut, tags=["health"])
def health() -> HealthOut:
    return HealthOut(status="ok", version=__version__)


app.include_router(projects.router, prefix="/api")
app.include_router(findings.router, prefix="/api")
