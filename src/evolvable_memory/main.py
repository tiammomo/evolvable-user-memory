from __future__ import annotations

from copy import deepcopy
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from evolvable_memory.bootstrap import build_api_application
from evolvable_memory.config import Settings


def _runtime_logging_config(level: str) -> dict[str, Any]:
    config: dict[str, Any] = deepcopy(LOGGING_CONFIG)
    config["formatters"]["emf_json"] = {"format": "%(message)s"}
    config["handlers"]["emf_json"] = {
        "class": "logging.StreamHandler",
        "formatter": "emf_json",
        "stream": "ext://sys.stdout",
    }
    for logger_name in (
        "evolvable_memory.access",
        "evolvable_memory.authorization",
        "evolvable_memory.error",
        "evolvable_memory.projection",
    ):
        config["loggers"][logger_name] = {
            "handlers": ["emf_json"],
            "level": level,
            "propagate": False,
        }
    return config


def run() -> None:
    settings = Settings.from_environment()
    application = build_api_application(settings)
    uvicorn.run(
        application,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
        log_config=_runtime_logging_config(settings.log_level),
    )


if __name__ == "__main__":
    run()
