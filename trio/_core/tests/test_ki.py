import pytest
import sys
import os
import signal
import threading
import contextlib
import time

from async_generator import async_generator, yield_, isasyncgenfunction

from ... import _core
from ...testing import wait_all_tasks_blocked
from ..._util import acontextmanager, signal_raise
from ..._timeouts import sleep
from .tutil import slow

def ki_self():
    signal_raise(signal.SIGINT)

def test_ki_self():
    with pytest.raises(KeyboardInterrupt):
        ki_self()

async def test_ki_enabled():
    # Regular tasks aren't KI-protected
    assert not _core.currently_ki_protected()

    # Low-level call-soon callbacks are KI-protected
    call_soon = _core.current_call_soon_thread_and_signal_safe()
    record = []
    def check():
        record.append(_core.currently_ki_protected())
    call_soon(check)
    await wait_all_tasks_blocked()
    assert record == [True]

    @_core.enable_ki_protection
    def protected():
        assert _core.currently_ki_protected()
        unprotected()

    @_core.disable_ki_protection
    def unprotected():
        assert not _core.currently_ki_protected()

    protected()

    @_core.enable_ki_protection
    async def aprotected():
        assert _core.currently_ki_protected()
        await aunprotected()

    @_core.disable_ki_protection
    async def aunprotected():
        assert not _core.currently_ki_protected()

    await aprotected()

    # make sure that the decorator here overrides the automatic manipulation
    # that spawn() does:
    async with _core.open_nursery() as nursery:
        nursery.spawn(aprotected)
        nursery.spawn(aunprotected)

    @_core.enable_ki_protection
    def gen_protected():
        assert _core.currently_ki_protected()
        yield

    for _ in gen_protected():
        pass

    @_core.disable_ki_protection
    def gen_unprotected():
        assert not _core.currently_ki_protected()
        yield

    for _ in gen_unprotected():
        pass


# This used to be broken due to
#
#   https://bugs.python.org/issue29590
#
# Specifically, after a coroutine is resumed with .throw(), then the stack
# makes it look like the immediate caller is the function that called
# .throw(), not the actual caller. So child() here would have a caller deep in
# the guts of the run loop, and always be protected, even when it shouldn't
# have been. (Solution: we don't use .throw() anymore.)
async def test_ki_enabled_after_yield_briefly():
    @_core.enable_ki_protection
    async def protected():
        await child(True)

    @_core.disable_ki_protection
    async def unprotected():
        await child(False)

    async def child(expected):
        import traceback
        traceback.print_stack()
        assert _core.currently_ki_protected() == expected
        await _core.yield_briefly()
        traceback.print_stack()
        assert _core.currently_ki_protected() == expected

    await protected()
    await unprotected()


# This also used to be broken due to
#   https://bugs.python.org/issue29590
async def test_generator_based_context_manager_throw():
    @contextlib.contextmanager
    @_core.enable_ki_protection
    def protected_manager():
        assert _core.currently_ki_protected()
        try:
            yield
        finally:
            assert _core.currently_ki_protected()

    with protected_manager():
        assert not _core.currently_ki_protected()

    with pytest.raises(KeyError):
        # This is the one that used to fail
        with protected_manager():
            raise KeyError


async def test_agen_protection():
    @_core.enable_ki_protection
    @async_generator
    async def agen_protected1():
        assert _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert _core.currently_ki_protected()

    @_core.disable_ki_protection
    @async_generator
    async def agen_unprotected1():
        assert not _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert not _core.currently_ki_protected()

    # Swap the order of the decorators:
    @async_generator
    @_core.enable_ki_protection
    async def agen_protected2():
        assert _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert _core.currently_ki_protected()

    @async_generator
    @_core.disable_ki_protection
    async def agen_unprotected2():
        assert not _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert not _core.currently_ki_protected()

    for agen_fn in [
            agen_protected1, agen_protected2,
            agen_unprotected1, agen_unprotected2,
    ]:
        async for _ in agen_fn():
            assert not _core.currently_ki_protected()

        # acontextmanager insists that the function passed must itself be an
        # async gen function, not a wrapper around one
        if isasyncgenfunction(agen_fn):
            async with acontextmanager(agen_fn)():
                assert not _core.currently_ki_protected()

            # Another case that's tricky due to:
            #   https://bugs.python.org/issue29590
            with pytest.raises(KeyError):
                async with acontextmanager(agen_fn)():
                    raise KeyError


# Test the case where there's no magic local anywhere in the call stack
def test_ki_enabled_out_of_context():
    assert not _core.currently_ki_protected()


def test_ki_protection_works():
    async def sleeper(name, record):
        try:
            while True:
                await _core.yield_briefly()
        except _core.Cancelled:
            record.add((name + " ok"))

    async def raiser(name, record):
        try:
            # os.kill runs signal handlers before returning, so we don't need
            # to worry that the handler will be delayed
            print("killing, protection =", _core.currently_ki_protected())
            ki_self()
        except KeyboardInterrupt:
            print("raised!")
            # Make sure we aren't getting cancelled as well as siginted
            await _core.yield_briefly()
            record.add((name + " raise ok"))
            raise
        else:
            print("didn't raise!")
            # If we didn't raise (b/c protected), then we *should* get
            # cancelled at the next opportunity
            try:
                await _core.yield_indefinitely(lambda _: _core.Abort.SUCCEEDED)
            except _core.Cancelled:
                record.add((name + " cancel ok"))

    # simulated control-C during raiser, which is *unprotected*
    print("check 1")
    record = set()
    async def check_unprotected_kill():
        async with _core.open_nursery() as nursery:
            nursery.spawn(sleeper, "s1", record)
            nursery.spawn(sleeper, "s2", record)
            nursery.spawn(raiser, "r1", record)
    with pytest.raises(KeyboardInterrupt):
        _core.run(check_unprotected_kill)
    assert record == {"s1 ok", "s2 ok", "r1 raise ok"}

    # simulated control-C during raiser, which is *protected*, so the KI gets
    # delivered to the main task instead
    print("check 2")
    record = set()
    async def check_protected_kill():
        async with _core.open_nursery() as nursery:
            nursery.spawn(sleeper, "s1", record)
            nursery.spawn(sleeper, "s2", record)
            nursery.spawn(_core.enable_ki_protection(raiser), "r1", record)
            # __aexit__ blocks, and then receives the KI
    with pytest.raises(KeyboardInterrupt):
        _core.run(check_protected_kill)
    assert record == {"s1 ok", "s2 ok", "r1 cancel ok"}

    # kill at last moment still raises (call_soon until it raises an error,
    # then kill)
    print("check 3")
    async def check_kill_during_shutdown():
        call_soon = _core.current_call_soon_thread_and_signal_safe()
        def kill_during_shutdown():
            assert _core.currently_ki_protected()
            try:
                call_soon(kill_during_shutdown)
            except _core.RunFinishedError:
                # it's too late for regular handling! handle this!
                print("kill! kill!")
                ki_self()
        call_soon(kill_during_shutdown)

    with pytest.raises(KeyboardInterrupt):
        _core.run(check_kill_during_shutdown)

    # KI arrives very early, before main is even spawned
    print("check 4")
    class InstrumentOfDeath:
        def before_run(self):
            ki_self()
    async def main():
        await _core.yield_briefly()
    with pytest.raises(KeyboardInterrupt):
        _core.run(main, instruments=[InstrumentOfDeath()])

    # yield_if_cancelled notices pending KI
    print("check 5")
    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        with pytest.raises(KeyboardInterrupt):
            await _core.yield_if_cancelled()
    _core.run(main)

    # KI arrives while main task is not abortable, b/c already scheduled
    print("check 6")
    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        await _core.yield_briefly_no_cancel()
        await _core.yield_briefly_no_cancel()
        await _core.yield_briefly_no_cancel()
        with pytest.raises(KeyboardInterrupt):
            await _core.yield_briefly()
    _core.run(main)

    # KI arrives while main task is not abortable, b/c refuses to be aborted
    print("check 7")
    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        task = _core.current_task()
        def abort(_):
            _core.reschedule(task, _core.Value(1))
            return _core.Abort.FAILED
        assert await _core.yield_indefinitely(abort) == 1
        with pytest.raises(KeyboardInterrupt):
            await _core.yield_briefly()
    _core.run(main)

    # KI delivered via slow abort
    print("check 8")
    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        task = _core.current_task()
        def abort(raise_cancel):
            result = _core.Result.capture(raise_cancel)
            _core.reschedule(task, result)
            return _core.Abort.FAILED
        with pytest.raises(KeyboardInterrupt):
            assert await _core.yield_indefinitely(abort)
        await _core.yield_briefly()
    _core.run(main)

    # KI arrives just before main task exits, so the call_soon machinery is
    # still functioning and will accept the callback to deliver the KI, but by
    # the time the callback is actually run, main has exited and can't be
    # aborted.
    print("check 9")
    @_core.enable_ki_protection
    async def main():
        ki_self()
    with pytest.raises(KeyboardInterrupt):
        _core.run(main)

    print("check 10")
    # KI in unprotected code, with
    # restrict_keyboard_interrupt_to_checkpoints=True
    record = []
    async def main():
        # We're not KI protected...
        assert not _core.currently_ki_protected()
        ki_self()
        # ...but even after the KI, we keep running uninterrupted...
        record.append("ok")
        # ...until we hit a checkpoint:
        with pytest.raises(KeyboardInterrupt):
            await sleep(10)
    _core.run(main, restrict_keyboard_interrupt_to_checkpoints=True)
    assert record == ["ok"]
    record = []
    # Exact same code raises KI early if we leave off the argument, doesn't
    # even reach the record.append call:
    with pytest.raises(KeyboardInterrupt):
        _core.run(main)
    assert record == []

    # KI arrives while main task is inside a cancelled cancellation scope
    # the KeyboardInterrupt should take priority
    print("check 11")
    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        with _core.open_cancel_scope() as cancel_scope:
            cancel_scope.cancel()
            with pytest.raises(_core.Cancelled):
                await _core.yield_briefly()
            ki_self()
            with pytest.raises(KeyboardInterrupt):
                await _core.yield_briefly()
            with pytest.raises(_core.Cancelled):
                await _core.yield_briefly()
    _core.run(main)



def test_ki_is_good_neighbor():
    # in the unlikely event someone overwrites our signal handler, we leave
    # the overwritten one be
    try:
        orig = signal.getsignal(signal.SIGINT)

        def my_handler(signum, frame):  # pragma: no cover
            pass
        async def main():
            signal.signal(signal.SIGINT, my_handler)
        _core.run(main)

        assert signal.getsignal(signal.SIGINT) is my_handler
    finally:
        signal.signal(signal.SIGINT, orig)


# For details on why this test is non-trivial, see:
#   https://github.com/python-trio/trio/issues/42
#   https://github.com/python-trio/trio/issues/109
# To make it an even better test, we should try doing
#   pthread_kill(pthread_self, SIGINT)
# in the child thread, to make sure signals in non-main threads also wake up
# the main loop... but currently that test would fail (see gh-109 again).
@slow
def test_ki_wakes_us_up():
    assert threading.current_thread() == threading.main_thread()

    # This test is flaky due to a race condition on Windows; see:
    #   https://github.com/python-trio/trio/issues/119
    #   https://bugs.python.org/issue30038
    # I think the only fix is to wait for fixed CPython to be released, so in
    # the mean time, on affected versions we send two signals (equivalent to
    # hitting control-C twice). This works because the problem is that the C
    # level signal handler does
    #
    #   write-to-fd -> set-flags
    #
    # and we need
    #
    #   set-flags -> write-to-fd
    #
    # so running the C level signal handler twice does
    #
    #   write-to-fd -> set-flags -> write-to-fd -> set-flags
    #
    # which contains the desired sequence.
    #
    # Affected version of CPython include:
    # - 3.5.3
    # - 3.6.1
    # Let's be optimistic and assume the next releases won't be affected...
    buggy_wakeup_fd = False
    import platform
    if os.name == "nt" and platform.python_implementation() == "CPython":
        if (3, 5, 0) <= sys.version_info < (3, 5, 4):
            buggy_wakeup_fd = True
        if (3, 6, 0) <= sys.version_info < (3, 6, 2):
            buggy_wakeup_fd = True

    # lock is only needed to avoid an annoying race condition where the
    # *second* ki_self() call arrives *after* the first one woke us up and its
    # KeyboardInterrupt was caught, and then generates a second
    # KeyboardInterrupt that aborts the test run. The kill_soon thread holds
    # the lock while doing the calls to ki_self, which means that it holds it
    # while the C-level signal handler is running. Then in the main thread,
    # when we're woken up we know that ki_self() has been run at least once;
    # if we then take the lock it guaranteeds that ki_self() has been run
    # twice, so if a second KeyboardInterrupt is going to arrive it should
    # arrive by the time we've acquired the lock. This lets us force it to
    # happen inside the pytest.raises block.
    #
    # It will be very nice when the buggy_wakeup_fd bug is fixed.
    lock = threading.Lock()

    def kill_soon():
        # We want the signal to be raised after the main thread has entered
        # the IO manager blocking primitive. There really is no way to
        # deterministically interlock with that, so we have to use sleep and
        # hope it's long enough.
        time.sleep(1)
        with lock:
            print("thread doing ki_self()")
            ki_self()
            if buggy_wakeup_fd:
                print("buggy_wakeup_fd =", buggy_wakeup_fd)
                ki_self()

    async def main():
        thread = threading.Thread(target=kill_soon)
        print("Starting thread")
        thread.start()
        try:
            with pytest.raises(KeyboardInterrupt):
                # To limit the damage on CI if this does get broken (as
                # compared to sleep_forever())
                print("Going to sleep")
                try:
                    await sleep(20)
                    print("Woke without raising?!")  # pragma: no cover
                # The only purpose of this finally: block is to soak up the
                # second KeyboardInterrupt that might arrive on
                # buggy_wakeup_fd platforms. So it might get aborted at any
                # moment randomly on some runs, so pragma: no cover avoids
                # coverage flapping:
                finally:  # pragma: no cover
                    print("waiting for lock")
                    with lock:
                        print("got lock")
                    # And then we want to force a PyErr_CheckSignals. Which is
                    # not so easy on Windows. Weird kluge: builtin_repr calls
                    # PyObject_Repr, which does an unconditional
                    # PyErr_CheckSignals for some reason.
                    print(repr(None))
        finally:
            print("joining thread", sys.exc_info())
            thread.join()

    start = time.time()
    try:
        _core.run(main)
    finally:
        end = time.time()
        print("duration", end - start)
        print("sys.exc_info", sys.exc_info())
    assert 1.0 <= (end - start) < 2
