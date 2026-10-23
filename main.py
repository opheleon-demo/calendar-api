"""
FastAPI application entry point.

Creates tables, brings event ownership up to date, and mounts routes.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import engine
from models import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    from auth import validate_auth_config
    validate_auth_config()

    Base.metadata.create_all(bind=engine)

    from migrate_ownership import (
        LEGACY_EVENTS_OWNER_ENV, OwnershipMigrationError, ensure_owner_column, migrate_ownership,
    )
    ensure_owner_column(engine)
    legacy_owner = os.getenv(LEGACY_EVENTS_OWNER_ENV)
    if legacy_owner:
        # A missing account must not stop the API from serving: it still has
        # to accept the registration. Ownerless events stay hidden meanwhile.
        try:
            migrate_ownership(engine, legacy_owner)
        except OwnershipMigrationError as exc:
            logging.getLogger(__name__).error("Ownership migration failed: %s", exc)
    yield


app = FastAPI(title="Calendar API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from auth import router as auth_router  # noqa: E402
from routes import router  # noqa: E402
app.include_router(auth_router)
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
