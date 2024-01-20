import lldb

breakpoint_locations = ["mcxt.c:985"]
breakpoint_regexes = ["^MemoryContext*"]
breakpoint_addresses = [0x00000001052ebbc0]


def breakpoint_set_by_location(target, breakpoints):
    for bp_loc in breakpoints:
        file, line = bp_loc.split(":")
        line = int(line)
        target.BreakpointCreateByLocation(file, line)


def breakpoint_set_by_regex(target, regexes):
    for regex in regexes:
        target.BreakpointCreateByRegex(regex)


def breakpoint_set_by_address(target, addresses):
    for address in addresses:
        target.BreakpointCreateByAddress(address)


# Connect to the current target
target = lldb.debugger.GetSelectedTarget()

# Set breakpoints
# breakpoint_set_by_location(target, breakpoint_locations)
# breakpoint_set_by_regex(target, breakpoint_regexes)
breakpoint_set_by_address(target, breakpoint_addresses)
