from __future__ import annotations

import pytest

import conftest

TEST_DATABASE_URL = "postgresql://emf:secret@127.0.0.1:5432/evolvable_memory_test"


def test_destructive_postgres_helper_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMF_ALLOW_DESTRUCTIVE_TEST_DATABASE", raising=False)

    with pytest.raises(RuntimeError, match="EMF_ALLOW_DESTRUCTIVE_TEST_DATABASE=1"):
        conftest._destructive_test_conninfo(TEST_DATABASE_URL)


@pytest.mark.parametrize(
    "database_name",
    ("postgres", "evolvable_memory", "production", "test_evolvable_memory"),
)
def test_destructive_postgres_helper_rejects_non_test_database_names(
    monkeypatch: pytest.MonkeyPatch,
    database_name: str,
) -> None:
    monkeypatch.setenv("EMF_ALLOW_DESTRUCTIVE_TEST_DATABASE", "1")

    with pytest.raises(RuntimeError, match="ending in '_test'"):
        conftest._destructive_test_conninfo(
            f"postgresql://emf:secret@127.0.0.1:5432/{database_name}"
        )


def test_destructive_postgres_guard_runs_before_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_called = False

    def unexpected_migration(_database_url: str) -> None:
        nonlocal migration_called
        migration_called = True

    monkeypatch.setenv("EMF_ALLOW_DESTRUCTIVE_TEST_DATABASE", "1")
    monkeypatch.setattr(conftest, "upgrade_database", unexpected_migration)

    with pytest.raises(RuntimeError, match="ending in '_test'"):
        conftest.prepare_postgres_database(
            "postgresql://emf:secret@127.0.0.1:5432/evolvable_memory"
        )

    assert migration_called is False


def test_destructive_postgres_helper_accepts_driver_url_for_test_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMF_ALLOW_DESTRUCTIVE_TEST_DATABASE", "1")

    conninfo = conftest._destructive_test_conninfo(
        TEST_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    )

    assert conninfo == TEST_DATABASE_URL
