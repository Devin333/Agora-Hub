from copy import deepcopy

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.mutation_fence import MutationFence, MutationFenceState


def _acquired(**overrides):
    fields = dict(owner_scope="tenant/service", resource_key="document/1", operation_key="acquire-1", owner_id="attempt-1", now_ms=100, ttl_ms=50)
    fields.update(overrides)
    return MutationFence.acquire(**fields)


def test_fence_renew_release_recovery_and_next_generation_roundtrip():
    acquired = _acquired()
    renewed = acquired.renew(operation_key="renew-1", owner_id="attempt-1", generation=1, now_ms=125, expires_at_ms=175)
    assert renewed.renew(operation_key="renew-1", owner_id="attempt-1", generation=1, now_ms=125, expires_at_ms=175) is renewed
    lost = renewed.mark_lost(operation_key="lost-1", owner_id="attempt-1", generation=1, now_ms=130)
    assert lost.mark_lost(operation_key="lost-1", owner_id="attempt-1", generation=1, now_ms=130) is lost
    active = lost.recover_active(operation_key="recover-1", owner_id="attempt-1", generation=1, now_ms=135)
    assert active.recover_active(operation_key="recover-1", owner_id="attempt-1", generation=1, now_ms=135) is active
    active.require_owned(owner_id="attempt-1", generation=1, now_ms=160)
    released = active.release(operation_key="release-1", owner_id="attempt-1", generation=1, now_ms=165, termination_confirmed=True)
    assert released.state is MutationFenceState.RELEASED
    next_fence = _acquired(previous=released, operation_key="acquire-2", owner_id="attempt-2", now_ms=170)
    assert next_fence.history[-1]["generation"] == 2
    assert MutationFence.from_dict(next_fence.to_dict()) == next_fence
    assert [event["revision"] for event in next_fence.history] == [1, 2, 3, 4, 5, 6]


def test_fence_operations_are_idempotent_by_key_and_conflicting_reuse_fails():
    acquired = _acquired()
    assert _acquired(previous=acquired) is acquired
    released = acquired.transitioned("RELEASED", operation_key="release-1", owner_id="attempt-1", generation=1, now_ms=120, termination_confirmed=True)
    assert released.transitioned("RELEASED", operation_key="release-1", owner_id="attempt-1", generation=1, now_ms=120, termination_confirmed=True) is released
    with pytest.raises(HarnessValidationError):
        released.transitioned("RELEASED", operation_key="release-1", owner_id="attempt-1", generation=1, now_ms=121, termination_confirmed=True)


@pytest.mark.parametrize("owner,generation,at_ms", [("attempt-2", 1, 110), ("attempt-1", 2, 110), ("attempt-1", 1, 150), ("attempt-1", 1, 99)])
def test_stale_owner_generation_or_ttl_never_authorizes_mutation(owner, generation, at_ms):
    with pytest.raises(HarnessValidationError) as caught:
        _acquired().require_owned(owner_id=owner, generation=generation, now_ms=at_ms)
    assert caught.value.code == "SIDE_EFFECT_FENCE_LOST"


def test_lost_or_expired_fence_requires_audited_release_and_never_automatically_reacquires():
    acquired = _acquired()
    with pytest.raises(HarnessValidationError):
        _acquired(previous=acquired, operation_key="retry-acquire", now_ms=200)
    with pytest.raises(HarnessValidationError):
        acquired.renew(operation_key="expired-renew", owner_id="attempt-1", generation=1, now_ms=150, expires_at_ms=200)
    with pytest.raises(HarnessValidationError):
        acquired.release(operation_key="expired-release", owner_id="attempt-1", generation=1, now_ms=150, termination_confirmed=True)
    lost = acquired.mark_lost(operation_key="lost-1", owner_id="attempt-1", generation=1, now_ms=150)
    assert lost.state is MutationFenceState.INDETERMINATE
    for action in ("RENEWED", "RECOVERED_ACTIVE", "RELEASED"):
        with pytest.raises(HarnessValidationError):
            lost.transitioned(action, operation_key="invalid", owner_id="attempt-1", generation=1, now_ms=160, expires_at_ms=200, termination_confirmed=action == "RELEASED")
    recovered = lost.recover_release(
        operation_key="recovery-release",
        owner_id="attempt-1",
        generation=1,
        now_ms=160,
        termination_confirmed=True,
    )
    assert (
        recovered.recover_release(
            operation_key="recovery-release",
            owner_id="attempt-1",
            generation=1,
            now_ms=160,
            termination_confirmed=True,
        )
        == recovered
    )
    assert recovered.state is MutationFenceState.RELEASED
    assert _acquired(previous=recovered, operation_key="acquire-next", now_ms=170).history[-1]["generation"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 3),
        ("generation", 8),
        ("at_ms", 99),
        ("owner_id", "forged"),
        ("previous_checksum", None),
        ("termination_confirmed", False),
    ],
)
def test_resigned_but_illegal_fence_history_is_rejected(field, value):
    released = _acquired().transitioned("RELEASED", operation_key="release", owner_id="attempt-1", generation=1, now_ms=125, termination_confirmed=True)
    raw = deepcopy(released.to_dict())
    event = raw["history"][-1]
    event[field] = value
    event["event_checksum"] = canonical_payload_checksum({"owner_scope": raw["owner_scope"], "resource_key": raw["resource_key"], "event": {key: item for key, item in event.items() if key != "event_checksum"}})
    raw["fence_checksum"] = canonical_payload_checksum({key: item for key, item in raw.items() if key != "fence_checksum"})
    with pytest.raises(HarnessValidationError):
        MutationFence.from_dict(raw)


def test_independent_resource_fences_do_not_share_owners_or_generations():
    left = _acquired(resource_key="document/1")
    right = _acquired(resource_key="document/2", owner_id="attempt-2")
    left.require_owned(owner_id="attempt-1", generation=1, now_ms=110)
    right.require_owned(owner_id="attempt-2", generation=1, now_ms=110)
    assert left.fence_checksum != right.fence_checksum


def test_recovery_actions_require_an_indeterminate_history_state():
    acquired = _acquired()
    with pytest.raises(HarnessValidationError):
        acquired.recover_active(
            operation_key="invalid-recover-active",
            owner_id="attempt-1",
            generation=1,
            now_ms=110,
        )
    with pytest.raises(HarnessValidationError):
        acquired.recover_release(
            operation_key="invalid-recover-release",
            owner_id="attempt-1",
            generation=1,
            now_ms=110,
            termination_confirmed=True,
        )


def test_first_event_has_no_previous_checksum_and_chain_uses_exact_hash():
    raw = _acquired().to_dict()
    raw["history"][0]["previous_checksum"] = "not-a-checksum"
    raw["fence_checksum"] = canonical_payload_checksum(
        {key: value for key, value in raw.items() if key != "fence_checksum"}
    )
    with pytest.raises(HarnessValidationError):
        MutationFence.from_dict(raw)

    acquired = _acquired()
    released = acquired.release(
        operation_key="release-1",
        owner_id="attempt-1",
        generation=1,
        now_ms=120,
        termination_confirmed=True,
    )
    raw = deepcopy(released.to_dict())
    raw["history"][-1]["previous_checksum"] = "sha256:" + "0" * 64
    raw["history"][-1]["event_checksum"] = canonical_payload_checksum(
        {
            "owner_scope": raw["owner_scope"],
            "resource_key": raw["resource_key"],
            "event": {
                key: value
                for key, value in raw["history"][-1].items()
                if key != "event_checksum"
            },
        }
    )
    raw["fence_checksum"] = canonical_payload_checksum(
        {key: value for key, value in raw.items() if key != "fence_checksum"}
    )
    with pytest.raises(HarnessValidationError):
        MutationFence.from_dict(raw)
