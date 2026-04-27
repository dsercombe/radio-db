def model_registry() -> None:
    # Import side-effects register all ORM models with SQLAlchemy metadata.
    from radio_db.models import entities  # noqa: F401


__all__ = ["model_registry"]
