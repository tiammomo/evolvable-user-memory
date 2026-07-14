from __future__ import annotations

import uvicorn

from evolvable_memory.api.app import app
from evolvable_memory.config import Settings


def run() -> None:
    settings = Settings.from_environment()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
