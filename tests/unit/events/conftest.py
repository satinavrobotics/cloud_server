"""Events-library tests run with strict payload validation."""

import pytest

from packages.events import schemas


@pytest.fixture(autouse=True)
def _strict_payloads():
    previous = schemas.strict_validation()
    schemas.set_strict_validation(True)
    yield
    schemas.set_strict_validation(previous)
