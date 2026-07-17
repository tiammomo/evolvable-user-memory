from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn

from evolvable_memory import main as main_module
from evolvable_memory.config import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "statement",
    (
        "from evolvable_memory.api.app import create_app",
        "from evolvable_memory.api.app import app",
    ),
)
def test_api_import_does_not_compose_postgres(statement: str) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "EMF_STORE": "postgres",
            "EMF_DATABASE_URL": "postgresql://invalid:invalid@127.0.0.1:1/unreachable",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", f"{statement}\nprint('imported')"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "imported"


def test_legacy_asgi_symbol_composes_on_first_use() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "EMF_AUTH_MODE": "development",
            "EMF_ENVIRONMENT": "development",
            "EMF_STORE": "memory",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from fastapi.testclient import TestClient\n"
                "from evolvable_memory.api.app import app\n"
                "with TestClient(app) as client:\n"
                "    response = client.get('/health')\n"
                "    assert response.status_code == 200\n"
                "print('served')"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "served"


def test_cli_explicitly_composes_the_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(store="memory")
    application = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(Settings, "from_environment", lambda: settings)

    def fake_build(received: Settings) -> object:
        captured["settings"] = received
        return application

    def fake_run(received: object, **options: object) -> None:
        captured["application"] = received
        captured["options"] = options

    monkeypatch.setattr(main_module, "build_api_application", fake_build)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    main_module.run()

    assert captured["settings"] is settings
    assert captured["application"] is application
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["host"] == settings.host
    assert options["port"] == settings.port
