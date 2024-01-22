import lldb
import re

g_bin_name = "postgres"

# why do I use python `re` module instead of lldb's regex?
# because lldb.CreateBreakpointByRegex() seems to be broken
# with the complex pattern. I have to create a breakpoint
# resolver to create a custom breakpoint.
#
# break on symbols that begin with `MemoryContext` but not
# `MemoryContextCheck` or `MemoryContextSwitchTo`
br_regexes = [r'\bMemoryContext(?!Check|SwitchTo)\w+']

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


class BreakpointResolver:
    def __init__(self, bkpt, extra_args, dict):
        self.bkpt = bkpt

    def __callback__(self, sym_ctx):
        module = sym_ctx.GetModule()
        filename = module.GetFileSpec().GetFilename()
        if filename == g_bin_name:
            br_symbols = []
            for symbol in module:
                name = symbol.GetName()
                for regex in br_regexes:
                    if re.search(regex, name):
                        br_symbols.append(symbol.GetStartAddress())
                        break
            for symaddr in br_symbols:
                self.bkpt.AddLocation(symaddr)

    def __get_depth__(self):
        return lldb.eSearchDepthModule


def trace_custom_api(debugger, command, result, internal_dict):
    debugger.HandleCommand("breakpoint set -P trace_pg_mem.BreakpointResolver")


def _configure_breakpoint(bp):
    bp.SetAutoContinue(True)
    commands = lldb.SBStringList()
    commands.AppendString("dump_bt")
    bp.SetCommandLineCommands(commands)


def _get_expression_address(frame, expression):
    result = frame.EvaluateExpression(expression)
    if result.GetError().Success():
        return result.GetValueAsUnsigned()
    else:
        print(f"Failed to evaluate '{expression}': {result.GetError()}")
        return None


def _breakpoint_set_by_regex(target, regexes):
    for regex in regexes:
        bp = target.BreakpointCreateByRegex(regex)
        _configure_breakpoint(bp)


def _breakpoint_set_by_address(target, addresses):
    for address in addresses:
        bp = target.BreakpointCreateByAddress(address)
        _configure_breakpoint(bp)


def _breakpoint_set_by_expr(target, exprs):
    process = target.GetProcess()
    frame = process.GetSelectedThread().GetSelectedFrame()
    addrs = []
    for expr in exprs:
        addr = _get_expression_address(frame, expr)
        if addr is not None:
            addrs.append(addr)
    _breakpoint_set_by_address(target, addrs)


def dump_bt(debugger, command, result, internal_dict):
    debugger.SetUseColor(False)
    outfile = open("trace_pg_mem.txt", "a")
    debugger.SetOutputFileHandle(outfile, True)
    command = "bt"
    debugger.HandleCommand(command)


def trace_memory_context_api(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    _breakpoint_set_by_regex(target, br_regexes)


def trace_mcxt_methods(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    _breakpoint_set_by_expr(target, br_exprs)


def trace_mem_api(debugger, command, result, internal_dict):
    trace_memory_context_api(debugger, command, result, internal_dict)
    trace_mcxt_methods(debugger, command, result, internal_dict)


def __lldb_init_module(debugger, internal_dict):
    add_cmd = "command script add -f trace_pg_mem"
    exported_cmd = [
        "dump_bt",
        "trace_mem_api",
        "trace_mcxt_methods",
        "trace_memory_context_api",
        "trace_custom_api"
    ]
    for cmd in exported_cmd:
        debugger.HandleCommand(f"{add_cmd}.{cmd} {cmd}")

    print("new commands installed and ready for use:")
    for cmd in exported_cmd:
        print(f"    \033[1;32m{cmd}\033[0m")
