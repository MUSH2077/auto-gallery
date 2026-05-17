"""Test fixtures for auto-gallery backend."""

import pytest


@pytest.fixture
def sample_pixiv_metadata():
    return {
        "id": 38362603,
        "title": "Test Illustration",
        "description": "A test artwork",
        "date": "2024-01-01 12:00:00",
        "user": {"id": 1980643, "name": "ASK", "account": "askzy"},
        "tags": [{"name": "original"}, {"name": "test_tag"}],
        "pages": [
            {"url": "https://example.com/img_p0.jpg", "width": 800, "height": 600},
            {"url": "https://example.com/img_p1.jpg", "width": 800, "height": 600},
        ],
    }


@pytest.fixture
def sample_iwara_metadata():
    return {
        "id": "abc123",
        "title": "Test Video",
        "description": "A test iwara video",
        "type": "video",
        "rating": "ecchi",
        "tags": ["tag1", "tag2"],
        "file_id": "file_001",
        "filename": "test_video",
        "extension": "mp4",
        "width": 1920,
        "height": 1080,
        "date": "2024-06-01T12:00:00.000Z",
        "mime": "video/mp4",
        "user": {
            "id": "user123",
            "username": "test_user",
            "name": "Test User",
        },
        "url": "https://example.com/video.mp4",
    }


@pytest.fixture
def sample_twitter_metadata():
    return {
        "id_str": "1234567890123456789",
        "full_text": "Check out this artwork! #fanart",
        "created_at": "Wed Jan 01 12:00:00 +0000 2024",
        "id": 1234567890123456789,
        "user": {
            "id": 111222333,
            "name": "artist_handle",
            "nick": "Artist Name",
        },
        "entities": {
            "media": [
                {
                    "id_str": "media_001",
                    "media_url": "https://pbs.twimg.com/media/img1.jpg",
                    "media_url_https": "https://pbs.twimg.com/media/img1.jpg",
                    "sizes": {"large": {"w": 1200, "h": 800}},
                }
            ],
            "hashtags": [{"text": "fanart"}, {"text": "illustration"}],
            "user_mentions": [],
        },
        "url": "https://pbs.twimg.com/media/img1.jpg",
        "width": 1200,
        "height": 800,
    }
