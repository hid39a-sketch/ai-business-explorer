from ai_business_explorer.domain.ids import uuid7


def test_uuid7_version_and_ordering() -> None:
    ids = [uuid7() for _ in range(50)]
    assert all(i.version == 7 for i in ids)
    assert ids[0].int >> 80 <= ids[-1].int >> 80  # 先頭48bitはタイムスタンプ
