"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.exc import OperationalError

from app import __version__, db
from app.api import documents, expenses, findings, projects
from app.api import settings as settings_routes
from app.config import get_settings
from app.samples import ensure_sample_projects
from app.schemas import HealthOut
from app.services.jobs import mark_interrupted_jobs

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """On startup: close the runs a previous process left half-done and make sure the sample
    projects exist (when their folders are in DATA_DIR)."""
    try:
        with db.SessionLocal() as session:
            mark_interrupted_jobs(session)
            ensure_sample_projects(session)
    except OperationalError as error:  # the API still serves /health and explains the problem
        logger.warning("Base de datos no disponible al arrancar: %s", error)
    yield


app = FastAPI(
    title="Revisor de justificación",
    description="Revisión asistida de la justificación económica de proyectos de cooperación.",
    version=__version__,
    lifespan=lifespan,
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


app.include_router(settings_routes.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(findings.router, prefix="/api")
app.include_router(expenses.router, prefix="/api")

WEB_APP = Path(__file__).parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
def web_app() -> FileResponse:
    """The review app (a single static page that talks to /api)."""
    return FileResponse(WEB_APP, media_type="text/html")


WEB_JS = WEB_APP.parent / "js"
WEB_JS_FILES = {p.name for p in WEB_JS.glob("*.js")}


@app.get("/js/{name}", include_in_schema=False)
def web_js(name: str) -> FileResponse:
    """The page's ES modules. Only the files of web/js, always as JavaScript (some Windows
    installs map .js to text/plain, and browsers refuse to run modules served like that)."""
    if name not in WEB_JS_FILES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe")
    return FileResponse(
        WEB_JS / name, media_type="text/javascript", headers={"Cache-Control": "no-cache"}
    )
