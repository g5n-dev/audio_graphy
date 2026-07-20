"""Integration tests for the Prompt (prompts) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.prompt import Prompt
from audio_graphy.models.user import User


@pytest.mark.integration
class TestPromptCRUD:
    """CRUD operations for the prompts table."""

    def test_create_prompt(self, db_session: pytest.fixture) -> None:
        p = Prompt(
            name="entity_extraction",
            version="v1",
            content="Extract entities from the following text:",
        )
        db_session.add(p)
        db_session.commit()

        assert p.id is not None
        assert p.active is False  # default

    def test_read_prompt(self, db_session: pytest.fixture) -> None:
        p = Prompt(
            name="tag_extraction",
            version="v2",
            content="Extract tags...",
            changelog="Added new tags",
        )
        db_session.add(p)
        db_session.commit()

        result = db_session.scalar(select(Prompt).where(Prompt.name == "tag_extraction"))
        assert result is not None
        assert result.version == "v2"
        assert result.changelog == "Added new tags"

    def test_update_prompt(self, db_session: pytest.fixture) -> None:
        p = Prompt(name="update_test", version="v1", content="Original")
        db_session.add(p)
        db_session.commit()

        p.active = True
        p.content = "Updated content"
        db_session.commit()

        result = db_session.get(Prompt, p.id)
        assert result is not None
        assert result.active is True
        assert result.content == "Updated content"

    def test_delete_prompt(self, db_session: pytest.fixture) -> None:
        p = Prompt(name="delete_test", version="v1", content="Delete me")
        db_session.add(p)
        db_session.commit()
        p_id = p.id

        db_session.delete(p)
        db_session.commit()

        assert db_session.get(Prompt, p_id) is None


@pytest.mark.integration
class TestPromptConstraints:
    """Constraint validation for the prompts table."""

    def test_unique_name_version(self, db_session: pytest.fixture) -> None:
        p1 = Prompt(name="dup", version="v1", content="A")
        db_session.add(p1)
        db_session.commit()

        p2 = Prompt(name="dup", version="v1", content="B")
        db_session.add(p2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_same_name_different_version(self, db_session: pytest.fixture) -> None:
        p1 = Prompt(name="multi_ver", version="v1", content="A")
        p2 = Prompt(name="multi_ver", version="v2", content="B")
        db_session.add_all([p1, p2])
        db_session.commit()  # Should succeed

    def test_fk_created_by_user(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", name="Creator", email="creator@test.com")
        db_session.add(user)
        db_session.flush()

        p = Prompt(
            name="with_creator",
            version="v1",
            content="C",
            created_by=user.id,
        )
        db_session.add(p)
        db_session.commit()

        result = db_session.get(Prompt, p.id)
        assert result is not None
        assert result.created_by == user.id

    def test_not_null_content(self, db_session: pytest.fixture) -> None:
        p = Prompt(name="no_content", version="v1")  # type: ignore[call-arg]
        db_session.add(p)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()
