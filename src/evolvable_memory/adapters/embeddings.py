from __future__ import annotations

import json
import math
import re
from hashlib import blake2b
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from evolvable_memory.domain.common import require_text

_TOKEN_PATTERN = re.compile(r"[\w]+|[\u3400-\u9fff]", re.UNICODE)


def _unit_vector(values: list[float], *, dimensions: int) -> tuple[float, ...]:
    if len(values) != dimensions:
        raise ValueError("embedding provider returned unexpected dimensions")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding provider returned a non-finite value")
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude == 0:
        return tuple(0.0 for _ in values)
    return tuple(value / magnitude for value in values)


class HashingEmbedder:
    """Deterministic, offline embedding suitable for a zero-dependency baseline."""

    def __init__(self, *, dimensions: int = 384) -> None:
        if not 32 <= dimensions <= 32_768:
            raise ValueError("embedding dimensions must be in [32, 32768]")
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"hash-blake2b-v1-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        normalized = require_text(text, "embedding text").lower()
        tokens = _TOKEN_PATTERN.findall(normalized)
        features = tokens + [
            f"{tokens[index]}\x1f{tokens[index + 1]}" for index in range(len(tokens) - 1)
        ]
        if not features:
            features = [normalized]

        vector = [0.0] * self._dimensions
        for feature in features:
            digest = blake2b(feature.encode("utf-8"), digest_size=16).digest()
            bucket = int.from_bytes(digest[:8], "big") % self._dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[bucket] += sign
        return _unit_vector(vector, dimensions=self._dimensions)


class OpenAICompatibleEmbedder:
    """Minimal OpenAI-compatible embedding client with no SDK leakage."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not 32 <= dimensions <= 32_768:
            raise ValueError("embedding dimensions must be in [32, 32768]")
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._model = require_text(model, "embedding model")
        self._dimensions = dimensions
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        payload: dict[str, Any] = {
            "input": [require_text(text, "embedding text")],
            "model": self._model,
            "dimensions": self._dimensions,
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(
            f"{self._base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                body = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("embedding provider request failed") from exc

        try:
            values = [float(value) for value in body["data"][0]["embedding"]]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("embedding provider response is invalid") from exc
        return _unit_vector(values, dimensions=self._dimensions)
