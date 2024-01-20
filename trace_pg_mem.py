import lldb

br_regexes = ["^MemoryContext*"]
br_exprs = [
    "mcxt_methods[3].alloc",
    "mcxt_methods[3].free_p",
    "mcxt_methods[3].realloc",
    "mcxt_methods[3].reset",
    "mcxt_methods[3].delete_context",
    "mcxt_methods[3].get_chunk_context",
    "mcxt_methods[3].get_chunk_space",
    "mcxt_methods[3].is_empty",
    "mcxt_methods[4].alloc",
    "mcxt_methods[4].free_p",
    "mcxt_methods[4].realloc",
    "mcxt_methods[4].reset",
    "mcxt_methods[4].delete_context",
    "mcxt_methods[4].get_chunk_context",
    "mcxt_methods[4].get_chunk_space",
    "mcxt_methods[4].is_empty",
    "mcxt_methods[5].alloc",
    "mcxt_methods[5].free_p",
    "mcxt_methods[5].realloc",
    "mcxt_methods[5].reset",
    "mcxt_methods[5].delete_context",
    "mcxt_methods[5].get_chunk_context",
    "mcxt_methods[5].get_chunk_space",
    "mcxt_methods[5].is_empty",
]


def trace_pg_mem(debugger, command, result, internal_dict):
    debugger.SetUseColor(False)
    outfile = open("trace_pg_mem.txt", "a")
    debugger.SetOutputFileHandle(outfile, True)
    command = "bt"
    debugger.HandleCommand(command)


def configure_breakpoint(bp):
    bp.SetAutoContinue(True)
    commands = lldb.SBStringList()
    commands.AppendString("trace_pg_mem")
    bp.SetCommandLineCommands(commands)


def get_expression_address(frame, expression):
    result = frame.EvaluateExpression(expression)
    if result.GetError().Success():
        return result.GetValueAsUnsigned()
    else:
        print(f"Failed to evaluate '{expression}': {result.GetError()}")
        return None


def breakpoint_set_by_regex(target, regexes):
    for regex in regexes:
        bp = target.BreakpointCreateByRegex(regex)
        configure_breakpoint(bp)


def breakpoint_set_by_address(target, addresses):
    for address in addresses:
        bp = target.BreakpointCreateByAddress(address)
        configure_breakpoint(bp)


lldb.debugger.SetUseColor(False)
outfile = open("trace_pg_mem.txt", "w")
lldb.debugger.SetOutputFileHandle(outfile, True)
target = lldb.debugger.GetSelectedTarget()
process = target.GetProcess()


def breakpoint_set_by_expr(target, exprs):
    frame = process.GetSelectedThread().GetSelectedFrame()
    addrs = []
    for expr in exprs:
        addr = get_expression_address(frame, expr)
        if addr is not None:
            addrs.append(addr)
    breakpoint_set_by_address(target, addrs)


breakpoint_set_by_regex(target, br_regexes)
breakpoint_set_by_expr(target, br_exprs)


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand("command script add -f trace_pg_mem.trace_pg_mem trace_pg_mem")
    print("The 'trace_pg_mem' python command has been installed and is ready for use.")
