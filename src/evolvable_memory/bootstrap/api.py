"""API process composition."""

from fastapi import FastAPI

from evolvable_memory.api.app import create_app
from evolvable_memory.config import Settings


def build_api_application(settings: Settings) -> FastAPI:
    """Build one owned API application from validated runtime settings."""
    return create_app(settings=settings)
