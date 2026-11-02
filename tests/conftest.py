from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from incident_helper.config import Settings
from incident_helper.gateway.cache import PromptCache
from incident_helper.gateway.core import ModelGateway
from incident_helper.gateway.telemetry import FinOpsLedger
from incident_helper.ops import OpsStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        _env_file=None,
        openai_api_key="test-primary",
        openai_fallback_api_key="test-fallback",
        var_dir=tmp_path / "var",
        output_dir=tmp_path / "output",
        gateway_chaos="none",
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def store(tmp_path: Path) -> OpsStore:
    return OpsStore(tmp_path / "ops.sqlite")


def stub_route_embed(text: str) -> list[float]:
    return [1.0, float(len(text) % 7), 0.5]


@pytest.fixture
def make_gateway(settings: Settings, tmp_path: Path):
    """Gateway over scripted models, keyed by the model spec."""

    def _make(models: dict[str, object], **kw) -> ModelGateway:
        return ModelGateway(
            settings=kw.pop("settings", settings),
            model_factory=lambda spec, key: models[spec],
            ledger=FinOpsLedger(),
            cache=kw.pop("cache", PromptCache(tmp_path / "cache.sqlite")),
            **kw,
        )

    return _make
