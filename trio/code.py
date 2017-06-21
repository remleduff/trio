
import ast
import inspect
import sys
import traceback

from ast import Expr, NameConstant, Tuple, Await, Return, Load
from code import InteractiveConsole
from codeop import CommandCompiler, Compile, _features
from textwrap import indent, dedent

from . import _core

from ._timeouts import sleep
from ._threads import run_in_worker_thread

__all__ = ['interact', 'async_exec']

# Much of this was originally from IPython which means we need the following:
#
# Copyright (c) 2008-2014, IPython Development Team
# Copyright (c) 2001-2007, Fernando Perez <fernando.perez@colorado.edu>
# Copyright (c) 2001, Janko Hauser <jhauser@zscout.de>
# Copyright (c) 2001, Nathaniel Gray <n8gray@caltech.edu>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# Redistributions in binary form must reproduce the above copyright notice, this
# list of conditions and the following disclaimer in the documentation and/or
# other materials provided with the distribution.
#
# Neither the name of the IPython Development Team nor the names of its
# contributors may be used to endorse or promote products derived from this
# software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

def _asyncify(code):
    """wrap code in async def definition.

    And setup a bit of context to run it later.

    The following names are part of the API:

     - ``user_ns`` is used as the name for the namespace to run code in
     - ``coro_ret`` is used to get a coroutine _out_ which will return 
                    (user_ns, last_expr) when awaited, where last_expr
                    is the value of the last expression of ``code``
    """
    return dedent("""
        async def module():
        {usercode}
            return locals()

        coro_ret = module(**user_ns)
        """).format(usercode=indent(code,' '*4))


def _ast_asyncify(code:str, localnames) -> ast.Module:
    """
    Parse code with top-level await and modify the AST to be able to run it.

    The given code is wrapped in a async-def function, parsed into an AST, and
    the resulting function definition AST is modified to return a tuple
    containing the curent values of ``locals()`` in the function, as well as the
    last expression of the user code.

    The last expression or await node is moved into the return statement, and
    replaced by `None` at its original location. If the last node is not Expr or
    Await, the second item of the return value will be None.

    We will use this fact to outside of the loop update the User Namespace, and
    return the value of the last expression in order to run it "Interactively"
    and trigger display.
    """
    tree = ast.parse(_asyncify(code))

    function_df = tree.body[0]

    ret = function_df.body[-1]
    lastexpr = function_df.body[-2]
    for name in localnames:
        function_df.args.args.append(ast.arg(name,None))
    if isinstance(lastexpr, (Expr, Await)):
        function_df.body[-1] = Return(Tuple(elts=[ret.value, lastexpr.value], ctx=Load()))
        function_df.body[-2] = Expr(value=NameConstant(value=None))
    else:
        function_df.body[-1] = Return(Tuple(elts=[ret.value, NameConstant(value=None)], ctx=Load()))
    ast.fix_missing_locations(tree)
    return tree


async def _async_exec(code_obj, user_ns):
    """
    Fake async execution of code_object in a namespace via a proxy namespace.

    WARNING: The semantics of `async_exec` are quite different from `exec`,
    in particular you can only pass a single namespace. It also return a
    handle to the value of the last_expression of user_code.

    `async_exec` make the following assumption,

    Code object MUST represent a structure like the following::

        async def function(**user_ns):
            {{user code executing in user_ns}}
            return (user_ns, last_expression)

        coro_ret = function(**user_ns)


    In particular:

      - the ``code_obj``'s ``coro_ret`` node MUST be coroutine
      that should be returned by the code object, and it MUST be bound to
      the ``coro_ret`` name. Awaiting the coroutine MUST return a tuple
      containing (user_ns, lastexpr) where user_ns is the modified user_ns
      and lastexpr is the value of the last expression to executed or None
      - the user namespace MUST be named ``user_ns`` outside of the
      ``async def`` function.

    We do run the async function in a third namespace for multiple reasons:

      - do not pollute use namespace or get influenced by it.
      - wrapper can get arbitrary complex without concern for backward
      compatibility
      - user_ns.clear() ; user_ns.update(**stuff) does not fails with
      `user_ns` is not defined.

    """
    loop_ns = {'user_ns': user_ns, 'last_expr':None}
    exec(code_obj, globals(), loop_ns)
    coro_ret = loop_ns['coro_ret']
    post_loop_user_ns, last_expr = await coro_ret
    if post_loop_user_ns == loop_ns:
        raise ValueError('Async Code may not modify copy of User Namespace and need to return a new one')
    user_ns.clear()
    user_ns.update(**post_loop_user_ns)
    return last_expr


def _async_compile(source, filename, localnames, flags=0, dont_inherit=0):
    tree = _ast_asyncify(source, localnames)
    mod = ast.Module(tree.body)
    codeob = compile(mod, filename, 'exec', flags, dont_inherit)
    return codeob


class AsyncCompiler(Compile):

    def __init__(self, locals):
        super().__init__()
        self.locals = locals

    def __call__(self, source, filename, symbol):
        codeob = _async_compile(source, filename, self.locals.keys(), self.flags, 1)
        for feature in _features:
            if codeob.co_flags & feature.compiler_flag:
                self.flags |= feature.compiler_flag
        return codeob


class AsyncInteractiveConsole(InteractiveConsole):

    async def interact(self, banner=None, exitmsg=None, ps1=None, ps2=None):
        """Closely emulate the interactive Python console.
        The optional banner argument specifies the banner to print
        before the first interaction; by default it prints a banner
        similar to the one printed by the real Python interpreter,
        followed by the current class name in parentheses (so as not
        to confuse this with the real interpreter -- since it's so
        close!).
        The optional exitmsg argument specifies the exit message
        printed when exiting. Pass the empty string to suppress
        printing an exit message. If exitmsg is not given or None,
        a default message is printed.
        """
        old_ps1 = getattr(sys, 'ps1', None)
        old_ps2 = getattr(sys, 'ps2', None)
        sys.ps1 = ps1 or old_ps1 or '>>> '
        sys.ps2 = ps2 or old_ps2 or '... '
        try:
            cprt = 'Type "help", "copyright", "credits" or "license" for more information.'
            if banner is None:
                self.write("Python %s on %s\n%s\n(%s)\n" %
                           (sys.version, sys.platform, cprt,
                            self.__class__.__name__))
            elif banner:
                self.write("%s\n" % str(banner))
            more = 0
            while 1:
                try:
                    if more:
                        prompt = sys.ps2
                    else:
                        prompt = sys.ps1
                    try:
                        line = await self.raw_input(prompt)
                    except EOFError:
                        self.write("\n")
                        break
                    else:
                        more = await self.push(line)
                except KeyboardInterrupt:
                    self.write("\nKeyboardInterrupt\n")
                    self.resetbuffer()
                    more = 0
            if exitmsg is None:
                self.write('now exiting %s...\n' % self.__class__.__name__)
            elif exitmsg != '':
                self.write('%s\n' % exitmsg)
        finally:
            sys.ps1 = old_ps1
            sys.ps2 = old_ps2


    async def push(self, line):
        self.buffer.append(line)
        more = line.strip().endswith(':') or (len(self.buffer) > 1 and line != '')
        if not more:
            source = "\n".join(self.buffer)
            await self.runsource(source, self.filename)
            self.resetbuffer()
        return more


    async def raw_input(self, prompt=""):
        # We can't currently Ctrl-C input in the worker thread, but if we
        # add a checkpoint, we at least let "Ctrl-C, Enter" work to cancel
        # bad input
        line = await run_in_worker_thread(input, prompt)
        await sleep(0)
        return line


    async def runsource(self, source, filename="<input>", symbol="single"):
        """Compile and run some source in the interpreter.
        Arguments are as for compile_command().
        One several things can happen:
        1) The input is incorrect; compile_command() raised an
        exception (SyntaxError or OverflowError).  A syntax traceback
        will be printed by calling the showsyntaxerror() method.
        2) The input is incomplete, and more input is required;
        compile_command() returned None.  Nothing happens.
        3) The input is complete; compile_command() returned a code
        object.  The code is executed by calling self.runcode() (which
        also handles run-time exceptions, except for SystemExit).
        The return value is True in case 2, False in the other cases (unless
        an exception is raised).  The return value can be used to
        decide whether to use sys.ps1 or sys.ps2 to prompt the next
        line.
        """
        try:
            code = self.compile(source, filename, symbol)
        except (OverflowError, SyntaxError, ValueError):
            # Case 1
            self.showsyntaxerror(filename)
            return False

        if code is None:
            # Case 2
            return True

        # Case 3
        await self.runcode(code)
        return False


    async def runcode(self, code):
        """Execute a code object.
        When an exception occurs, self.showtraceback() is called to
        display a traceback.  All exceptions are caught except
        SystemExit, which is reraised.
        A note about KeyboardInterrupt: this exception may occur
        elsewhere in this code, and may not always be caught.  The
        caller should be prepared to deal with it.
        """
        try:
            ret = await _async_exec(code, self.locals)
            exec(compile('ret', '<input>', 'single'))
        except SystemExit:
            raise
        except:
            self.showtraceback()

    
def _code_namespace():
    stack = inspect.stack()
    # Stack frames to skip: [ _code_namespace, (interact or async_exec), trio_internals, user_code ]
    caller = stack[4].frame
    return caller.f_locals


async def async_exec(source, filename="<input>", namespace=None, flags=0, dont_inherit=0):
    """Similar to Python's exec statement, but allows execution of async code

    Example::

        async def foo(l):
            await trio.code.async_exec("await l.acquire()")
    """
    if namespace is None:
        namespace = _code_namespace()
    codeob = _async_compile(source, filename, namespace.keys(), flags=flags, dont_inherit=dont_inherit)
    return await _async_exec(codeob, namespace)


_default_banner = """
Trio Async Console
Type "help", "copyright", "credits" or "license" for more information.

A nursery has been created for you, named "nursery".

Try:
  >>> await trio.sleep(5)
  >>> async def foo():
  ...    await trio.sleep(5)
  ...    print("Done")
  ...
  >>> nursery.spawn(foo)
  >>> nursery.cancel_scope.cancel()

Have fun!
"""

_default_exit = """
now exiting Trio Console...
"""

async def interact(namespace=None, banner=_default_banner,
                   exitmsg=_default_exit, ps1="(async) >>> ", ps2="        ... "):
    """Convenience function to run a read-eval-print loop.
    The resulting REPL is in an async context and allows executing arbitrary
    async code, ie:

    Example::

        trio.run(trio.code.interact)

    Trio Async Console
    Type "help", "copyright", "credits" or "license" for more information.
    
    (async) >>> l = trio.Lock()
    (async) >>> await l.acquire()
    (async) >>> l.locked()
    True
    (async) >>> l.release()
    (async) >>> 
    now exiting AsyncInteractiveConsole...
    """
    if namespace is None:
        namespace = _code_namespace()
    compiler = CommandCompiler()
    compiler.compiler = AsyncCompiler(namespace)
    console = AsyncInteractiveConsole(namespace)
    console.compile = compiler
    async with _core.open_nursery() as nursery:
        namespace['nursery'] = nursery
        with _core.open_cancel_scope(shield=True):
            await console.interact(banner, exitmsg, ps1, ps2)

