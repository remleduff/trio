import py.path

# The following files aren't loadable on all platforms and don't have any
# doctests in any event
IGNORES = {py.path.local(p) for p in ["_io_epoll.py", "_io_kqueue.py"]}

def pytest_ignore_collect(path, config):
    """ return True to prevent considering this path for collection.
    This hook is consulted for all files and directories prior to calling
    more specific hooks.
    """
    return path in IGNORES
