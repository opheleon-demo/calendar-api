"""
FastAPI application entry point.

Creates tables, seeds data on first run, and mounts routes.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import engine, SessionLocal
from models import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    from auth import validate_auth_config
    validate_auth_config()

    Base.metadata.create_all(bind=engine)
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
