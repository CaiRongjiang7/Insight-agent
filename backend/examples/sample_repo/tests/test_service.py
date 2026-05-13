from app.service import get_user_summary


def test_user_summary() -> None:
    payload = get_user_summary(1)
    assert "name" in payload
