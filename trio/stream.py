import os
import operator
import sys
import termios

from .hazmat import wait_readable, wait_writable

from . import abc as _abc
from . import _core
from . import _streams

class FDReceiveStream(_abc.ReceiveStream):

    def __init__(self,
                 fd,
                 encoding=None,
                 receive_some_hook=None,
                 close_hook=None):
        self._fd = fd
        self._encoding = encoding
        self._lock = trio.Lock()
        self._buf = bytearray(1024)
        self._closed = False
        self.receive_some_hook = receive_some_hook
        self.close_hook = close_hook

    async def receive_some(self, max_bytes):
        await _core.yield_briefly()
        if max_bytes is None:
            raise TypeError("max_bytes must not be None")
        max_bytes = operator.index(max_bytes)
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if self._closed:
            raise _streams.ClosedStreamError
        if self.receive_some_hook is not None:
            await self.receive_some_hook()
        await wait_readable(self._fd)
        return os.read(self._fd, max_bytes)

    def forceful_close(self):
        if not self._closed:
            self._closed = True
            os.close(self._fd)
            if self.close_hook is not None:
                self.close_hook()

    async def readline(self):
        async with self._lock:
            await _core.yield_briefly()
            while not self._buf or self._buf[-1] != 10:
                self._buf += await self.receive_some(1)
            if self._encoding:
                line = self._buf.decode(self._encoding)
                self._buf.clear()
            else:
                line = self._buf
                self._buf = bytearray(1024)
            return line


class FDSendStream(_abc.SendStream):

    def __init__(self,
                 fd,
                 encoding=None,
                 send_all_hook=None,
                 wait_send_all_might_not_block_hook=None,
                 close_hook=None):
        self._fd = fd
        self._encoding = encoding
        self._lock = trio.Lock()
        self._closed = False
        self.send_all_hook = send_all_hook
        self.wait_send_all_might_not_block_hook = wait_send_all_might_not_block_hook
        self.close_hook = close_hook

    async def send_all(self, data):
        if self._closed:
            await _core.yield_briefly()
            raise _streams.ClosedStreamError
        await wait_writable(self._fd)
        if self._encoding:
            data = data.encode(self._encoding)
        try:
            os.write(self._fd, data)
        except BrokenPipeError:
            raise _streams.BrokenStreamError
           
        if self.send_all_hook is not None:
            await self.send_all_hook()

    async def wait_send_all_might_not_block(self):
        if self._closed:
            await _core.yield_briefly()
            raise _streams.ClosedStreamError
        async with self._lock:
            await wait_writable(self._fd)
            if self.wait_send_all_might_not_block_hook is not None:
                await self.wait_send_all_might_not_block_hook()

    def forceful_close(self):
        if not self._closed:
            self._closed = True
            os.close(self._fd)
            if self.close_hook is not None:
                self.close_hook()


def open_tty_nonblocking(tty, mode):
    fd = os.open(tty, mode | os.O_NONBLOCK)
    attr = termios.tcgetattr(fd)
    attr[3] &= ~termios.ICANON
    termios.tcsetattr(fd, termios.TCSANOW, attr)
    return fd

def open_stdin():
    stdin = "/proc/self/fd/{fd}".format(fd=sys.stdin.fileno())
    fd = open_tty_nonblocking(stdin, os.O_RDONLY)
    return FDReceiveStream(fd, sys.stdin.encoding)

def open_stdout():
    stdout = "/proc/self/fd/{fd}".format(fd=sys.stdout.fileno())
    fd = open_tty_nonblocking(stdout, os.O_WRONLY)
    return FDSendStream(fd, sys.stdout.encoding)

def fifo_one_way_pair(name=None):
    if name is None:
        dir = tempfile.mkdtemp()
        name = os.path.join(dir, "fifo")
    if os.path.exists(name):
        os.unlink(name)
    os.mkfifo(name)
    r = os.open(name, os.O_RDONLY | os.O_NONBLOCK)
    s = os.open(name, os.O_WRONLY | os.O_NONBLOCK)
    return (FDSendStream(s), FDReceiveStream(r))

async def fifo_maker():
    return fifo_one_way_pair()

if __name__ == '__main__':
    import tempfile
    import trio
    import trio.testing

    stdin = open_stdin()
    stdout = open_stdout()

    prompt_lock = trio.Lock()
    async def prompt(prompt):
        async with prompt_lock:
            await stdout.send_all(prompt)
            line = await stdin.readline()
            await stdout.send_all(line)

    async def test():
        async with trio.open_nursery() as nursery:
            nursery.spawn(prompt, "1> ")
            nursery.spawn(prompt, "2> ")

    trio.run(test)
 
    async def test_pair():
        s,r = fifo_one_way_pair()
        async with trio.open_nursery() as nursery:
            child = nursery.spawn(r.receive_some, 1024)
            nursery.spawn(s.send_all, b"test")
        print(child.result.unwrap())
        s.forceful_close()
        s.forceful_close()
        await r.graceful_close()
        await r.graceful_close()

    trio.run(test_pair)

    trio.run(trio.testing.check_one_way_stream, fifo_maker, None)
