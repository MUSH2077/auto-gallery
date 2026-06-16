from datetime import datetime, timezone

from app.jobs.import_runner import _parse_posted_at, _posted_at_json


def test_parse_posted_at_common_string() -> None:
    parsed = _parse_posted_at("2022-08-12 15:57:49")

    assert parsed == datetime(2022, 8, 12, 15, 57, 49, tzinfo=timezone.utc)


def test_parse_posted_at_iso_z() -> None:
    parsed = _parse_posted_at("2022-08-12T15:57:49Z")

    assert parsed == datetime(2022, 8, 12, 15, 57, 49, tzinfo=timezone.utc)


def test_parse_posted_at_date_only() -> None:
    parsed = _parse_posted_at("2022-08-12")

    assert parsed == datetime(2022, 8, 12, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_posted_at_unix_seconds() -> None:
    parsed = _parse_posted_at(1660319869)

    assert parsed == datetime.fromtimestamp(1660319869, tz=timezone.utc)


def test_parse_posted_at_unix_milliseconds() -> None:
    parsed = _parse_posted_at(1660319869000)

    assert parsed == datetime.fromtimestamp(1660319869, tz=timezone.utc)


def test_parse_posted_at_datetime_gets_timezone() -> None:
    parsed = _parse_posted_at(datetime(2022, 8, 12, 15, 57, 49))

    assert parsed == datetime(2022, 8, 12, 15, 57, 49, tzinfo=timezone.utc)


def test_parse_posted_at_empty_or_invalid() -> None:
    assert _parse_posted_at(None) is None
    assert _parse_posted_at("") is None
    assert _parse_posted_at("not-a-date") is None


def test_posted_at_json_uses_isoformat() -> None:
    value = datetime(2022, 8, 12, 15, 57, 49, tzinfo=timezone.utc)

    assert _posted_at_json(value) == "2022-08-12T15:57:49+00:00"
    assert _posted_at_json(None) is None
