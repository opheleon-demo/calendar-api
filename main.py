"""
FastAPI application entry point.

Creates/migrates tables and mounts routes. No sample data is seeded: every
event belongs to a registered user (see seed.py for opt-in demo data).
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from database import engine
from migrations import run_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    from auth import validate_auth_config
    validate_auth_config()
    run_migrations(engine)
    yield


app = FastAPI(title="Calendar API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError):
    # The request's session is rolled back and closed by get_db.
    return JSONResponse(status_code=500, content={"detail": "Database error; nothing was changed"})


from auth import router as auth_router  # noqa: E402
from routes import router  # noqa: E402
app.include_router(auth_router)
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
