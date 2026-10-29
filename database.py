"""Database engine, session factory, and dependency injection."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:///./calendar.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate_schema():
    """Add nullable ownership only. Existing events retain NULL and no meetings."""
    from sqlalchemy import inspect, text
    from models import Base
    with engine.connect() as connection:
        # Serialize startup DDL as well as subsequent event provisioning.
        connection.execute(text("BEGIN IMMEDIATE"))
        Base.metadata.create_all(bind=connection)
        columns = {column["name"] for column in inspect(connection).get_columns("events")}
        if "organizer_id" not in columns:
            connection.execute(text("ALTER TABLE events ADD COLUMN organizer_id INTEGER REFERENCES users(id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_events_organizer_id ON events (organizer_id)"))
        connection.commit()
