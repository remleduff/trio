import trio
import trio.code

import sys

_hook =  getattr(sys, "__interactivehook__", None)

if _hook:
    _hook()
del _hook
del sys

trio.run(trio.code.interact, locals())
