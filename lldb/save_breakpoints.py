import lldb


target = lldb.debugger.GetSelectedTarget()
target.BreakpointsWriteToFile(lldb.SBFileSpec('breakpoints.txt'))
