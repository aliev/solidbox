# Async Unit of Work (UOW)

ContextVar-powered, asyncio-friendly Unit of Work base class that lets you:

- Use a single (even global) UOW instance across the app
- Access the current transaction object (`tx_obj`) anywhere in the call stack (or external modules) without passing it explicitly
- Run many concurrent requests safely — each task gets its own transaction

This README shows how to use it effectively and avoid common pitfalls.

## Design Goals

- Single global UOW instance works safely in asyncio
- No explicit propagation of `tx_obj` through function signatures
- Correct context cleanup on success, error, and cancellation
- No shared class-level mutable state; isolated per-instance ContextVars

## Quick Start

```python
# my_uow.py
from solidbox.uow import UOW

class AppUOW(UOW[YourTxType]):
    async def start_transaction(self) -> YourTxType: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...

uow = AppUOW()
```

```python
# usage.py
from my_uow import uow

async def do_something():
    async with uow:
        # tx_obj is bound to current asyncio task
        tx = uow.tx_obj
        await do_work()
```

```python
# external_module.py (in another package)
from my_uow import uow

async def function_from_external_module():
    # Uses the same ContextVar-bound tx_obj
    print(uow.tx_obj)
```

## Behavior Guarantees

- One transaction per task scope
  - Two concurrent tasks using the same `uow` see different `tx_obj` objects
- Access anywhere
  - Any code that imports the same `uow` instance can read `uow.tx_obj` within an active transaction
- Cleanup
  - Context is always reset on exit (commit/rollback), even if exceptions occur
- Nesting
  - Nested blocks for the same `uow` are forbidden and raise `RuntimeError`

## Common Use Cases

### Global UOW with external modules

```python
# my_uow.py
uow = AppUOW()

# local module
async def service_fn():
    async with uow:
        await external_module.function_one()  # sees the same tx_obj

# external_module.py
from my_uow import uow

async def function_one():
    tx = uow.tx_obj  # same object as in service_fn
    ...
```

### Multiple concurrent requests

```python
from my_uow import uow
import asyncio

async def worker():
    async with uow:
        return id(uow.tx_obj)

ids = await asyncio.gather(worker(), worker())
assert ids[0] != ids[1]  # isolated transactions
```

### Error handling and rollback

- Exceptions inside the `async with` block trigger `rollback()` and propagate the original error
- Exceptions in `commit()` are logged, `rollback()` is attempted, and the error propagates

```python
from my_uow import uow

async def create_user():
    async with uow:
        await repo.insert(uow.tx_obj, ...)
        raise ValueError("boom")  # rollback happens; error bubbles up
```

## Background Tasks: Pitfalls and Solutions

ContextVars are copied into new tasks at creation time. If you create background tasks inside the transaction, they inherit the current `tx_obj`. If those tasks outlive the `async with` block, they might continue using a transaction that was already committed/rolled back.

Problem example:

```python
async def handler():
    async with uow:
        task = asyncio.create_task(background_work())
        # Leaving the block before task finishes
    await task  # background_work still sees tx_obj, but transaction is closed
```

Recommended solutions:

- Finish tasks inside the transaction

```python
async def handler():
    async with uow:
        await background_work()  # or TaskGroup in 3.11+
```

- Use TaskGroup (Python 3.11+)

```python
async def handler():
    async with uow:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(background_work())
            # exits only after background_work finishes
```

- Explicitly pass `tx_obj` to long-lived tasks

```python
async def handler():
    async with uow:
        tx = uow.tx_obj
        asyncio.create_task(background_work(tx))  # explicit dependency
        # The task decides how/when to use tx; ideally finishes before exit
```

- Avoid using `uow.tx_obj` in tasks that may run after the transaction scope
  - If you must run after the scope, pass data, not the live transaction, or open a new transaction in the task as needed

Optional defensive check (if you implement it in your repo): Before using `tx_obj`, validate that the transaction is still active in your concrete `Tx` type.

## Nested Transactions

- Nested `async with uow:` is intentionally forbidden and raises `RuntimeError`
- If you need savepoints/re-entrancy, implement them in your concrete UOW:
  - Track a nesting counter in a ContextVar
  - On the first level: open real transaction
  - On deeper levels: create release/rollback savepoints

This repository’s base UOW does not implement savepoints by default.

## Cancellation

- If the task is cancelled while inside the block, `__aexit__` still tries to `rollback()`
- The cancellation is not swallowed — the `CancelledError` propagates

Ensure any I/O in `rollback()` is safe to call under cancellation.

## Threads

- ContextVars don’t flow across threads
- Don’t use `uow.tx_obj` in another thread unless you explicitly pass the transaction or recreate an appropriate context in that thread

## Multiple Databases / Multiple UOWs

- You can define multiple UOW instances (e.g., `mongo_uow`, `pg_uow`)
- Because ContextVar is per-instance in this implementation, different UOWs won’t interfere
- Ensure you don’t accidentally call `uow_a.tx_obj` inside a transaction opened by `uow_b`

## API Summary

- `uow.tx_obj -> Tx`
  Returns the transaction object bound to the current task; raises `ValueError` if no active transaction

- `uow.in_atomic_block -> bool`
  True if a transaction is active in the current task

- `async with uow:`
  Starts a new transaction with `start_transaction()`; on exit commits or rolls back depending on errors

- Methods to implement in subclass:
  - `async def start_transaction(self) -> Tx`
  - `async def commit(self) -> None`
  - `async def rollback(self) -> None`

## Compatibility

- Works on Python 3.10+
- If you target Python 3.12+, you may switch to PEP 695 generic syntax if desired

## Testing Tips

- Test success path, rollback on user error, rollback on commit failure
- Test concurrent tasks to ensure isolated `tx_obj`
- Test that starting the transaction fails cleanly without polluting context
- Test that nested transactions raise `RuntimeError`

```
pytest -k uow -vv
```

If you need an example, see `tests/test_uow.py` in this repo.
