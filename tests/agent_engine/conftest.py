"""Keep the deployed runtime's permanent logging policy local to each test."""
import logging

import pytest


@pytest.fixture(autouse=True)
def restore_process_logging_policy():
    previous = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous)
