def test_mark_ready_wins_over_previous_seen(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import review_badges

    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE",
                        tmp_path / "review-badges.json")

    review_badges.mark_seen("upgrade", now=500.0)
    assert review_badges.snapshot()["upgrade"] is False

    review_badges.mark_ready("upgrade", now=100.0)

    assert review_badges.snapshot()["upgrade"] is True


def test_badge_write_failures_are_non_fatal(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import review_badges

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("blocks mkdir", encoding="utf-8")
    monkeypatch.setattr(cfg, "REVIEW_BADGE_STATE_FILE", blocker / "badges.json")

    review_badges.mark_ready("library")
    review_badges.mark_seen("library")
    review_badges.clear_ready("library")
