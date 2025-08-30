import asyncio
from dataclasses import dataclass
from solidbox.uow import UOW

import pytest


@dataclass
class DummyTx:
    idx: int


class DummyUOW(UOW[DummyTx]):
    def __init__(
        self, *, fail_on_start: bool = False, fail_on_commit: bool = False
    ) -> None:
        super().__init__()
        self.fail_on_start = fail_on_start
        self.fail_on_commit = fail_on_commit
        self.started: int = 0
        self._next_idx: int = 1
        self.commits: int = 0
        self.rollbacks: int = 0

    async def start_transaction(self) -> DummyTx:
        if self.fail_on_start:
            raise RuntimeError("start failed")
        self.started += 1
        tx = DummyTx(idx=self._next_idx)
        self._next_idx += 1
        return tx

    async def commit(self) -> None:
        if self.fail_on_commit:
            raise RuntimeError("commit failed")
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_success_commit_and_tx_access():
    uow = DummyUOW()
    assert not uow.in_atomic_block
    async with uow:
        assert uow.in_atomic_block
        tx = uow.tx_obj
        assert isinstance(tx, DummyTx)
    assert not uow.in_atomic_block
    assert uow.started == 1
    assert uow.commits == 1
    assert uow.rollbacks == 0


@pytest.mark.asyncio
async def test_rollback_on_exception_and_propagation():
    uow = DummyUOW()
    with pytest.raises(ValueError):
        async with uow:
            _ = uow.tx_obj
            raise ValueError("boom")
    assert uow.commits == 0
    assert uow.rollbacks == 1
    assert not uow.in_atomic_block


@pytest.mark.asyncio
@pytest.mark.skip
async def test_commit_error_triggers_rollback_and_propagates():
    uow = DummyUOW(fail_on_commit=True)
    with pytest.raises(RuntimeError):
        async with uow:
            _ = uow.tx_obj
            # normal body; commit will fail on exit
            pass
    assert uow.commits == 0  # commit increments only on success in DummyUOW
    assert uow.rollbacks == 1
    assert not uow.in_atomic_block


@pytest.mark.asyncio
async def test_start_failure_does_not_pollute_context():
    uow = DummyUOW(fail_on_start=True)
    with pytest.raises(RuntimeError):
        async with uow:
            pass  # should not reach here
    assert not uow.in_atomic_block
    with pytest.raises(ValueError):
        _ = uow.tx_obj


@pytest.mark.asyncio
async def test_nested_transactions_forbidden():
    uow = DummyUOW()
    async with uow:
        with pytest.raises(RuntimeError):
            async with uow:
                pass
    assert not uow.in_atomic_block


@pytest.mark.asyncio
async def test_concurrent_tasks_isolate_tx_objects():
    uow = DummyUOW()

    async def worker() -> int:
        async with uow:
            # yield to ensure overlapping contexts possible
            await asyncio.sleep(0)
            return uow.tx_obj.idx

    left, right = await asyncio.gather(worker(), worker())
    assert left != right  # different transactions per task
    assert uow.started == 2
    assert uow.commits == 2
    assert uow.rollbacks == 0
    assert not uow.in_atomic_block
