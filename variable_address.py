import lldb


def get_global_variable_address(target, variable_name):
    variable = target.FindFirstGlobalVariable(variable_name)
    if variable:
        return variable.GetLoadAddress()
    else:
        print(f"Global variable {variable_name} not found.")
        return None


# Example: Replace 'yourVariable' with the name of your global variable
variable_name = 'mcxt_methods'

# Connect to the current target
target = lldb.debugger.GetSelectedTarget()

# Get the address of the global variable
variable_address = get_global_variable_address(target, variable_name)

if variable_address is not None:
    print(f"The address of {variable_name} is: {hex(variable_address)}")
