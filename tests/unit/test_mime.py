"""The site's MIME rules: which entries a gateway rewrites and the type each is served as."""

from decdn._mime import is_rewritten_type, rewritten, site_type


def test_rewritten() -> None:
    assert rewritten("a/b.HTML")
    assert rewritten("x.css")
    assert not rewritten("x.js")


def test_site_type() -> None:
    assert site_type("a.txt") == "text/plain; charset=utf-8"
    assert site_type("a.png") == "image/png"
    assert site_type("a.unknown") == "application/octet-stream"
    assert site_type("noext") == "application/octet-stream"


def test_is_rewritten_type() -> None:
    assert is_rewritten_type("text/html; charset=utf-8")
    assert not is_rewritten_type(None)
    assert not is_rewritten_type("text/plain")
