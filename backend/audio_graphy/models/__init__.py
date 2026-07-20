"""SQLAlchemy ORM models package.

All models register themselves on `Base.metadata` via import side-effects.
M1.4 will populate `models/` with the 16 tables defined in DESIGN.md §6.1.

For M1.2 we only need `Base` itself so alembic/env.py can import it cleanly.
"""

from audio_graphy.models.base import Base

__all__ = ["Base"]
