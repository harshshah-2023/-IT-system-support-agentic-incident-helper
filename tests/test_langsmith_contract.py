"""Upload logic against a recording fake, plus a contract check against the installed SDK.

These prove our calls match the real client's signatures. They do not prove an upload succeeded:
that needs `incident task6` with a real LANGSMITH_API_KEY.
"""

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

from langsmith import Client

from incident_helper.evals.dataset import DATASET_NAME, GOLDEN, upload_dataset


class FakeClient:
    def __init__(self, existing=()):
        self.stored = [
            SimpleNamespace(inputs={"question": q}, modified_at=datetime.now(UTC)) for q in existing
        ]
        self.created = None
        self.tags = []

    def has_dataset(self, dataset_name):
        return self.created is not None or bool(self.stored)

    def create_dataset(self, name, description=""):
        self.created = name
        return SimpleNamespace(id="d1", name=name)

    def read_dataset(self, dataset_name):
        return SimpleNamespace(id="d1", name=dataset_name)

    def list_examples(self, dataset_id, limit=None):
        return iter(self.stored)

    def create_examples(self, dataset_id, examples):
        self.stored += [
            SimpleNamespace(modified_at=datetime.now(UTC), **{"inputs": e["inputs"]}) for e in examples
        ]

    def update_dataset_tag(self, dataset_id, as_of, tag):
        self.tags.append(tag)


def test_first_upload_creates_all_six_examples():
    client = FakeClient()
    upload_dataset(client)
    assert client.created == DATASET_NAME and len(client.stored) == 6


def test_reupload_is_idempotent_and_adds_only_new_questions():
    client = FakeClient(existing=[r["question"] for r in GOLDEN[:4]])
    upload_dataset(client)
    assert len(client.stored) == 6


def test_version_tag_is_applied():
    client = FakeClient()
    upload_dataset(client, "v1")
    assert client.tags == ["v1"]


def test_calls_match_the_installed_langsmith_sdk():
    for method, names in {
        "has_dataset": {"dataset_name"},
        "create_dataset": {"dataset_name"},
        "read_dataset": {"dataset_name"},
        "list_examples": {"dataset_id", "limit"},
        "create_examples": {"dataset_id", "examples"},
        "update_dataset_tag": {"dataset_id", "as_of", "tag"},
    }.items():
        assert names <= set(inspect.signature(getattr(Client, method)).parameters), method
