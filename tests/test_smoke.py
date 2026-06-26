"""Confirms pytest discovers tests and can import the src package."""
import src


def test_package_importable() -> None:
    assert src is not None
