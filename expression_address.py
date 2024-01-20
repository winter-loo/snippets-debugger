import lldb


def get_expression_address(frame, expression):
    result = frame.EvaluateExpression(expression)
    if result.GetError().Success():
        return result.GetValueAsUnsigned()
    else:
        print(f"Failed to evaluate expression '{expression}': {result.GetError()}")
        return None


# Example: Replace 'yourExpression' with the expression you want to evaluate
expression_to_evaluate = 'mcxt_methods[3].alloc'

# Get the current frame
frame = lldb.debugger.GetSelectedTarget().GetProcess().GetSelectedThread().GetSelectedFrame()

# Get the address of the expression
expression_address = get_expression_address(frame, expression_to_evaluate)

if expression_address is not None:
    print(f"The address of '{expression_to_evaluate}' is: {hex(expression_address)}")
