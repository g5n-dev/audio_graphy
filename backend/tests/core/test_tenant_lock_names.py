"""Lock-name construction — pure, so it needs no database.

Split from ``test_tenant_lock.py`` because that module is marked
``pytest.mark.asyncio`` wholesale and needs a live MySQL; naming is neither.
"""

from __future__ import annotations

from audio_graphy.core.tenant_lock import _lock_name


def test_two_long_tenant_ids_sharing_a_prefix_get_different_locks() -> None:
    """Truncation made these serialize against each other.

    ``ag:speaker_link:`` is 16 bytes, leaving 48 of the tenant id. Tenant codes
    are unvalidated operator input and the column holds 64 characters, so the
    input that triggers this is accepted everywhere else. The symptom is latency
    coupling rather than a wrong answer — B acquires once A releases — which is
    why it would be found late, if at all.
    """

    shared = "t" * 48
    first = _lock_name("speaker_link", shared + "-north")
    second = _lock_name("speaker_link", shared + "-south")

    assert first != second
    assert len(first.encode("utf-8")) <= 64
    assert len(second.encode("utf-8")) <= 64


def test_ordinary_tenant_ids_keep_a_readable_name() -> None:
    """SHOW PROCESSLIST should still say which deployment and tenant hold what."""

    assert _lock_name("speaker_link", "chang_an") == "ag:audiography:speaker_link:chang_an"


def test_two_deployments_on_one_mysql_get_different_locks() -> None:
    """GET_LOCK is scoped to a SERVER, not a schema.

    Two stacks sharing one MySQL — even with separate databases — used to
    compute identical lock names, contend for 30s, then both proceed
    unserialized: the duplicate-speaker window this lock exists to close.
    Both stacks default to tenant code "default", so the tenant id alone
    cannot tell them apart.
    """

    a = _lock_name("speaker_link", "default", deployment_id="audiography")
    b = _lock_name("speaker_link", "default", deployment_id="accept")

    assert a != b
    assert len(a.encode("utf-8")) <= 64 and len(b.encode("utf-8")) <= 64


def test_long_tenant_digest_is_not_deployment_blind() -> None:
    """The overflow digest must hash deployment and tenant together.

    The deployment prefix shrinks the inline budget, pushing long tenant codes
    into the digest path more often — a digest over the tenant alone would
    resurrect the cross-deployment collision exactly there.
    """

    tenant = "t" * 60
    a = _lock_name("speaker_link", tenant, deployment_id="audiography")
    b = _lock_name("speaker_link", tenant, deployment_id="accept")

    assert a != b


def test_a_multibyte_tenant_id_stays_within_the_byte_cap() -> None:
    """The cap is bytes, not characters: 32 Chinese characters is 96 bytes."""

    name = _lock_name("speaker_link", "长安汽车" * 8)

    assert len(name.encode("utf-8")) <= 64
