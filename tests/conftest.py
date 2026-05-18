"""Pytest configuration shared across the tests/ tree."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: live-model / live-endpoint regressions; skipped by default "
        "via per-test env-var gates (see individual test modules).",
    )
