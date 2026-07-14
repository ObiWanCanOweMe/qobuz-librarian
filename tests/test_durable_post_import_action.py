"""Focused guards for durable post-import action integration."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.integrations.beets import ManagedCarrierRetirementOutcome
from qobuz_librarian.queue import durable_runner, journal, startup_recovery


class _StopAfterPolicyCapture(RuntimeError):
    pass


def test_new_durable_item_freezes_multi_artist_filing_policy(monkeypatch):
    saved = SimpleNamespace(operation_id="a" * 64)
    saved_item = SimpleNamespace(
        item_id="b" * 64,
        phase=journal.QueuePhase.PENDING,
    )
    monkeypatch.setattr(durable_runner, "_require_authority", lambda _value: None)
    monkeypatch.setattr(durable_runner, "_require_current_plan", lambda *_args: None)
    monkeypatch.setattr(
        durable_runner,
        "_claim_pending_item",
        lambda *_args, **_kwargs: (saved, saved_item),
    )
    monkeypatch.setattr(
        durable_runner,
        "initial_completion_input",
        lambda *_args: {"frozen": True},
    )
    monkeypatch.setattr(durable_runner, "isolated_staging_run_names", lambda: ())

    def capture_policy(current, item_id, phase, **values):
        assert current is saved
        assert item_id == saved_item.item_id
        assert phase is journal.QueuePhase.ACTIVE
        assert values["multi_artist_filing"] is True
        raise _StopAfterPolicyCapture

    monkeypatch.setattr(
        durable_runner.queue_state,
        "transition_journal_item",
        capture_policy,
    )

    with pytest.raises(_StopAfterPolicyCapture):
        durable_runner.execute_durable_new_album(
            [],
            {},
            SimpleNamespace(migrate_multi_artist=True),
            plan=object(),
            origin=object(),
            mode="test",
            authority=object(),
        )


def test_recovered_completion_persists_action_before_leaving_live_proof(
    tmp_path,
    monkeypatch,
):
    events = []
    live_lease = SimpleNamespace(
        revalidate=lambda: events.append("revalidate")
    )
    evidence = SimpleNamespace(
        library_root=str(tmp_path),
        album_path="Artist/Album",
    )
    saved = object()
    action = {"action_id": "c" * 64}

    @contextmanager
    def capture_live(_journal, _item):
        events.append("enter-live-proof")
        try:
            yield object(), live_lease, evidence
        finally:
            events.append("leave-live-proof")

    def plan_action(current, item_id, post_dir, *, authority):
        assert current == "journal"
        assert item_id == "item"
        assert post_dir == tmp_path / "Artist" / "Album"
        assert authority == "authority"
        events.append("plan-action")
        return action

    def commit_removal(current, *, item_id, live_evidence, post_import_action):
        assert current == "journal"
        assert item_id == "item"
        assert live_evidence is evidence
        assert post_import_action is action
        events.append("commit-removal-and-action")
        return saved

    monkeypatch.setattr(startup_recovery, "_require_authority", lambda _value: None)
    monkeypatch.setattr(startup_recovery, "_capture_live_completion", capture_live)
    monkeypatch.setattr(startup_recovery, "plan_post_import_action", plan_action)
    monkeypatch.setattr(
        startup_recovery.queue_state,
        "commit_recovered_completed_item_removal",
        commit_removal,
    )

    result = startup_recovery._recover_complete(
        "authority",
        "journal",
        SimpleNamespace(item_id="item"),
        lambda *_args, **_kwargs: pytest.fail(
            "completion was acknowledged before durable action publication"
        ),
    )

    assert result is saved
    assert events == [
        "enter-live-proof",
        "plan-action",
        "revalidate",
        "commit-removal-and-action",
        "revalidate",
        "leave-live-proof",
    ]


@pytest.mark.parametrize(
    ("queue_result", "web_result", "expected"),
    [
        (True, False, True),
        (False, True, True),
        (None, False, None),
        (False, None, None),
        (False, False, False),
    ],
)
def test_relocation_handoff_accepts_either_owner_and_preserves_unknown(
    queue_result,
    web_result,
    expected,
    monkeypatch,
):
    web_calls = []
    monkeypatch.setattr(
        startup_recovery.queue_state,
        "post_import_relocation_handoff_matches",
        lambda *_args: queue_result,
    )

    def web_match(*_args):
        web_calls.append(True)
        return web_result

    monkeypatch.setattr(
        startup_recovery,
        "_post_import_relocation_handoff_matches",
        web_match,
    )

    assert (
        startup_recovery._combined_post_import_relocation_handoff_matches(
            "d" * 64,
            {"handoff": True},
        )
        is expected
    )
    assert bool(web_calls) is (queue_result is not True)


def test_startup_retirement_uses_action_finalizer(monkeypatch):
    retirement = SimpleNamespace(item_id="e" * 64)
    current = SimpleNamespace(
        operation_id="f" * 64,
        items=(),
        retirements=(retirement,),
    )
    state = {"journal": current}
    callback = object()

    monkeypatch.setattr(startup_recovery, "_require_authority", lambda _value: None)
    monkeypatch.setattr(
        startup_recovery,
        "reconcile_post_import_relocations",
        lambda **_kwargs: SimpleNamespace(
            status=startup_recovery.RelocationRecoveryStatus.CLEAR
        ),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_load_namespace",
        lambda _authority: (
            ()
            if state["journal"] is None
            else (
                SimpleNamespace(
                    status=journal.QueueLoadStatus.READY,
                    journal=state["journal"],
                ),
            )
        ),
    )
    monkeypatch.setattr(
        startup_recovery,
        "_recover_active_library_backups",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        startup_recovery,
        "_has_unclaimed_staging_run",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        startup_recovery,
        "_recover_staging_references",
        lambda *_args: False,
    )

    def finalize(saved, item_id, *, authority, acknowledge_completion):
        assert saved is current
        assert item_id == retirement.item_id
        assert authority == "authority"
        assert acknowledge_completion is callback
        state["journal"] = SimpleNamespace(
            operation_id=current.operation_id,
            items=(),
            retirements=(),
        )
        return (
            state["journal"],
            Path("/music/Various Artists/Album"),
            SimpleNamespace(
                outcome=ManagedCarrierRetirementOutcome.RETIRED
            ),
        )

    def clear(operation_id):
        assert operation_id == current.operation_id
        state["journal"] = None

    monkeypatch.setattr(
        startup_recovery,
        "finalize_carrier_retirement",
        finalize,
    )
    monkeypatch.setattr(startup_recovery.queue_state, "clear_queue_journal", clear)

    result = startup_recovery.recover_startup_state(
        authority="authority",
        acknowledge_completion=callback,
    )

    assert result.status is startup_recovery.StartupRecoveryStatus.CLEAR
