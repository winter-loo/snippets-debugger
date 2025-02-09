# put this script under your {workspaceFolder}
# in code-lldb debug terminal, execute:
# command script import debug.py
import lldb

# this file will be under your {workspaceFolder}
with open("./lldb_output.txt", "w") as f:
    # Run an LLDB command and capture its output
    interpreter = lldb.debugger.GetCommandInterpreter()
    # redirect lldb command output to this object
    result = lldb.SBCommandReturnObject()

    for i in range(632):
        # your lldb command
        # this command will print one element in Vec<DeltaEntry>
        cmd = f'p *({i} + (pageserver::tenant::storage_layer::delta_layer::DeltaEntry *)(unsigned char *)all_keys.buf.inner.ptr.pointer.pointer)'
        interpreter.HandleCommand(cmd, result)

        if result.Succeeded():
            f.write(result.GetOutput())
        else:
            f.write("Command failed: " + result.GetError())
