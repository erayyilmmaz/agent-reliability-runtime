from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy metadata holder; ARR-3 introduces persistence models."""
