import decdn


def test_version_is_a_pep440_placeholder() -> None:
    assert decdn.__version__ == "0.0.0"
