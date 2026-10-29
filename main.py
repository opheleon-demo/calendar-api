"""
FastAPI application entry point.

Creates tables, seeds data on first run, and mounts routes.
"""
from contextlib import asynccontextmanager
import logging
import os


class RedactOAuthQuery(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].startswith("/zoom/callback?"):
                args[2] = "/zoom/callback?[redacted]"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactOAuthQuery())

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from database import SessionLocal


@asynccontextmanager
async def lifespan(app: FastAPI):
    from auth import validate_auth_config
    validate_auth_config()

    from database import migrate_schema
    migrate_schema()
    db = SessionLocal()
    try:
        from models import Event
        if db.query(Event).count() == 0:
            from seed import seed_database
            seed_database(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Calendar API", lifespan=lifespan)

@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("CALENDAR_UI_ORIGIN", "http://localhost:5173")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from auth import router as auth_router  # noqa: E402
from routes import router  # noqa: E402
from zoom import router as zoom_router
app.include_router(zoom_router)
app.include_router(auth_router)
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
