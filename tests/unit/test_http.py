import httpx
import pytest

from decdn._http import CODES, error_of, is_success, network_error
from decdn.errors import ErrorCode

_A = "a" * 64


@pytest.mark.parametrize(("status", "code"), sorted(CODES.items()))
def test_status_maps_to_code(status: int, code: ErrorCode) -> None:
    err = error_of(status, httpx.Headers(), "nope")
    assert err.code == code
    assert err.status == status
    assert str(err) == "nope"
    assert err.body == "nope"


def test_codes_has_twelve_entries() -> None:
    assert len(CODES) == 12


def test_unlisted_status_is_http() -> None:
    assert error_of(500, httpx.Headers(), "").code == "HTTP"


def test_error_of_reads_json_402() -> None:
    body = f'{{"error":"allowance spent","uri":"decdn:{_A}","retry_after_secs":120}}'
    err = error_of(402, httpx.Headers({"content-type": "application/json"}), body)
    assert err.code == "PAYMENT_REQUIRED"
    assert str(err) == "allowance spent"
    assert err.fallback_uri == f"decdn:{_A}"
    assert err.retry_after_secs == 120


def test_error_of_reads_link_and_retry_after_headers() -> None:
    headers = httpx.Headers({"retry-after": " 30 ", "link": f'<decdn:{_A}>; rel="alternate"'})
    err = error_of(503, headers, "")
    assert err.retry_after_secs == 30
    assert err.fallback_uri == f"decdn:{_A}"
    assert str(err) == "gateway answered 503"


def test_error_of_reads_seq_digits_beyond_2_53() -> None:
    err = error_of(409, httpx.Headers({"content-type": "application/json"}), '{"seq": 18446744073709551615}')
    assert err.seq == 2**64 - 1


def test_error_of_416_size() -> None:
    err = error_of(416, httpx.Headers({"content-range": "bytes */1234"}), "")
    assert err.size == 1234


def test_error_of_default_message() -> None:
    err = error_of(404, httpx.Headers(), "  ")
    assert str(err) == "gateway answered 404"
    assert err.body is None


def test_error_of_ignores_non_json_body_marked_json() -> None:
    err = error_of(400, httpx.Headers({"content-type": "application/json"}), "oops")
    assert str(err) == "oops"


def test_network_error_names_the_origin() -> None:
    exc = httpx.ConnectError("refused")
    err = network_error("http://127.0.0.1:8080/x/y?z=1", exc)
    assert err.code == "NETWORK"
    assert str(err) == "cannot reach http://127.0.0.1:8080: refused"


def test_is_success() -> None:
    assert is_success(200) and is_success(299)
    assert not is_success(199) and not is_success(300)
