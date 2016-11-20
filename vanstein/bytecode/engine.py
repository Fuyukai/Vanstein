"""
The engine class actually runs the bytecode.
"""
from vanstein.bytecode.vs_exceptions import safe_raise

try:
    import dis
    dis.Instruction
except AttributeError:
    from vanstein.backports import dis
import inspect

from vanstein.context import _VSContext, VSCtxState, VSWrappedFunction
from vanstein.decorators import native_invoke

from vanstein.bytecode import instructions


class VansteinEngine(object):
    """
    The bytecode virtual machine object runs bytecode that is generated by CPython.
    """

    def __init__(self):
        self.current_instruction = None  # type: dis.Instruction
        self.current_context = None  # type: _VSContext

    @native_invoke
    def __run_natively(self, context: _VSContext, instruction: dis.Instruction):
        """
        Invokes a function natively.
        """
        # Get the number of arguments to pop off of the stack.
        number_of_args = instruction.arg
        args = []
        for x in range(0, number_of_args):
            # Pop each argument off of the stack.
            args.append(context.stack.pop())

        args = reversed(args)

        # Now pop the function, which is underneath all the others.
        fn = context.stack.pop()
        if not callable(fn):
            safe_raise(context, TypeError("'{}' object is not callable".format(fn)))
            return

        # Run the function.
        try:
            result = fn(*args)
        except BaseException as e:
            safe_raise(context, e)
            return

        return result

    @native_invoke
    def run_context(self, context: _VSContext) -> _VSContext:
        """
        Runs the current bytecode for a context.

        This will instructions off of the instruction stack, until it reaches a context switch.
        """
        # Welcome to the main bulk of Vanstein.
        # Enjoy your stay!

        # Switch to running state for this context.
        context.state = VSCtxState.RUNNING
        self.current_context = context
        while True:
            if context.state is VSCtxState.FINISHED:
                # Done after a successful RETURN_VALUE.
                # Break the loop, and return the context.
                context.finish()
                return

            if context.state is VSCtxState.ERRORED:
                return

            next_instruction = context.next_instruction()
            assert isinstance(next_instruction, dis.Instruction)
            self.current_instruction = next_instruction

            # First, we check if we need to context switch.
            # Check if it's CALL_FUNCTION.
            if next_instruction.opname == "CALL_FUNCTION":
                # This is the instruction for CALL_FUNCTION. No specialized one exists in the instructions.py file.

                # We need to context switch, so suspend this current one.
                context.state = VSCtxState.SUSPENDED
                # Get STACK[-arg]
                # CALL_FUNCTION(arg) => arg is number of positional arguments to use, so pop that off of the stack.
                bottom_of_stack = context.stack[-(next_instruction.arg + 1)]

                # method wrappers die
                if type(bottom_of_stack) is type:
                    bottom_of_stack = bottom_of_stack.__new__

                # Here's some context switching.
                # First, check if it's a builtin or is a native invoke.
                if inspect.isbuiltin(bottom_of_stack) or hasattr(bottom_of_stack, "_native_invoke"):
                    # Run it!
                    result = self.__run_natively(context, next_instruction)
                    # Set the result on the context.
                    context.state = VSCtxState.RUNNING
                    # Push the result onto the stack.
                    context.push(result)
                    # Continue the loop to the next instruction.
                    continue

                if isinstance(bottom_of_stack, VSWrappedFunction):
                    # Call the VSWrappedFunction to get a new context.
                    # We'll manually fill these args.
                    new_ctx = bottom_of_stack()

                else:
                    # Wrap the function in a context.
                    new_ctx = _VSContext(bottom_of_stack)

                # Set the previous context, for stack frame chaining.
                new_ctx.prev_ctx = context
                # Doubly linked list!
                context.next_ctx = new_ctx
                # Set the new state to PENDING so it knows to run it on the next run.
                new_ctx.state = VSCtxState.PENDING

                # Add a callback to the new context.
                # This is so the loop can schedule execution of the new context soon.
                new_ctx.add_done_callback(context._on_result_cb)
                new_ctx.add_exception_callback(context._on_exception_cb)

                # Fill the number of arguments the function call requests.
                args = []
                for _ in range(0, next_instruction.arg):
                    args.append(context.pop())

                args = reversed(args)

                new_ctx.fill_args(*args)

                # Pop the function object off, too.
                context.pop()

                return new_ctx

            # Else, we run the respective instruction.
            try:
                i = getattr(instructions, next_instruction.opname)
            except AttributeError:
                raise NotImplementedError(next_instruction.opname)

            # Call the instruction handler.
            i(context, next_instruction)
