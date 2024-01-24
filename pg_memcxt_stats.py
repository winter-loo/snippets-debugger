import lldb
import sys
import argparse
import shlex
import os
import shutil


# C macros in PostgreSQL
ALLOCSET_NUM_FREELISTS = 11
ALLOC_MINBITS = 3


lldb_target = lldb.debugger.GetSelectedTarget()
CONTEXT_KINDS = [
    "T_AllocSetContext",
    "T_SlabContext",
    "T_GenerationContext",
]


class MemoryContextCounters:
    def __init__(self):
        # Total number of malloc blocks
        self.nblocks = 0
        # Total number of free chunks
        self.freechunks = 0
        # Total bytes requested from malloc
        self.totalspace = 0
        # The unused portion of totalspace
        self.freespace = 0

    def __str__(self):
        return "\
Grand total: {} bytes in {} blocks; {} free ({} chunks); {} used" \
    .format(
            self.totalspace,
            self.nblocks,
            self.freespace,
            self.freechunks,
            self.totalspace - self.freespace,
        )


def sizeof(typname):
    return lldb_target.FindFirstType(typname).GetByteSize()


def cast_memcxt(value, typname):
    memcxt = value._c_memcxt
    assert memcxt.GetType().IsPointerType(), "memcxt is not a pointer type"
    return memcxt.Cast(lldb_target.FindFirstType(typname).GetPointerType())


def read_c_string_from_memory(address):
    if address == 0:
        return ""

    process = lldb_target.GetProcess()

    # Read memory in chunks until a null terminator is encountered
    chunk_size = 64
    max_length = 4096  # Maximum length to prevent infinite loops

    c_string = b''
    offset = 0

    while offset < max_length:
        error = lldb.SBError()
        memory_data = process.ReadMemory(address + offset, chunk_size, error)

        if error.Fail():
            print(f"Error reading memory: {error}")
            return None

        if b'\0' in memory_data:
            # If null terminator is found, stop reading
            null_index = memory_data.index(b'\0')
            c_string += memory_data[:null_index]
            break
        else:
            # Append the entire chunk to the string
            c_string += memory_data

        offset += chunk_size

    return c_string.decode('utf-8')


# Determine the size of the chunk based on the freelist index
def GetChunkSizeFromFreeListIdx(fidx):
    return 1 << ALLOC_MINBITS << fidx


class MemoryContext:
    def __init__(self, memcxt):
        """
        memcxt: lldb.SBValue
        """
        self._c_memcxt = memcxt
        # memory context type is an C enum name
        self.typcxt = memcxt.GetChildMemberWithName("type").GetValue()
        name = memcxt.GetChildMemberWithName("name")
        self.name = read_c_string_from_memory(name.GetValueAsUnsigned())
        ident = memcxt.GetChildMemberWithName("ident")
        self.ident = read_c_string_from_memory(ident.GetValueAsUnsigned())
        self.methods = memcxt.GetChildMemberWithName("methods")
        self.firstchild = memcxt.GetChildMemberWithName("firstchild")
        self.nextchild = memcxt.GetChildMemberWithName("nextchild")
        self._current = lldb_target.FindFirstGlobalVariable(
            "CurrentMemoryContext")

    def is_not_null(self):
        return self._c_memcxt.GetValueAsUnsigned() != 0

    def Current(self):
        name = self._current.GetChildMemberWithName("name")
        name = read_c_string_from_memory(name.GetValueAsUnsigned())
        return name


class AllocSetContext:
    def __init__(self, aset):
        self._c_aset = aset
        self.blocks = aset.GetChildMemberWithName("blocks")
        freelist = aset.GetChildMemberWithName("freelist")
        assert freelist.GetType().IsArrayType(), \
            "freelist is not an array type"
        self.freelist = [
            MemoryChunk(freelist.GetChildAtIndex(i))
            for i in range(ALLOCSET_NUM_FREELISTS)
        ]


class AllocBlock:
    def __init__(self, blk):
        self._c_blk = blk
        self.next = blk.GetChildMemberWithName("next")
        self.endptr = blk.GetChildMemberWithName("endptr")
        self.freeptr = blk.GetChildMemberWithName("freeptr")

    def __len__(self):
        start = self._c_blk.GetValueAsUnsigned()
        end = self.endptr.GetValueAsUnsigned()
        return end - start

    def available(self):
        end = self.endptr.GetValueAsUnsigned()
        free = self.freeptr.GetValueAsUnsigned()
        return end - free


class AllocFreeListLink:
    def __init__(self, link):
        self._c_link = link
        self.next = link.GetChildMemberWithName("next")


class MemoryChunk:
    def __init__(self, chunk):
        self._c_chunk = chunk

    def is_not_null(self):
        return self._c_chunk.GetValueAsUnsigned() != 0

    def GetFreeListLink(self):
        chkptr = self._c_chunk.GetValueAsUnsigned()
        addr = lldb.SBAddress(chkptr + sizeof('MemoryChunk'), lldb_target)
        link_type = lldb_target.FindFirstType('AllocFreeListLink')
        link = lldb_target.CreateValueFromAddress("link", addr, link_type)
        return AllocFreeListLink(link)


Args = None
Newdumpfile = None


def _handle_args(raw_args):
    parser = argparse.ArgumentParser(description='Dump memory context stats')

    parser.add_argument('memory_context_var', nargs='?',
                        default='TopMemoryContext',
                        metavar='<memory context>',
                        help='Memory context to be dumped')
    parser.add_argument('-N', '--overwrite', action='store_true',
                        help='overwrite the dump file')
    parser.add_argument('-o', '--output',
                        help='dump to file instead of stdout')
    parser.add_argument('-i', '--include', nargs='+',
                        metavar='memory_context_name',
                        help='only dump stats for given memory context name')
    parser.add_argument('-x', '--exclude', nargs='+',
                        metavar='memory_context_name',
                        help='exclude memory context stats from dump')
    parser.add_argument('-d', '--diff', action='store_true',
                        help='diff the stats with previous dump')
    parser.add_argument('-m', '--max-children', type=int, default=100,
                        help='max number of children to dump')
    parser.add_argument('-c', '--use-cc', action='store_true',
                        help='use global CurrentMemoryContext')

    global Args
    args_list = shlex.split(raw_args)
    Args = parser.parse_args(args_list)


def pgmem(debugger, raw_args, result, internal_dict):
    if lldb_target.GetProcess().GetState() == lldb.eStateRunning:
        print("Process is running.  Use 'process interrupt' to pause execution.")
        return

    global Args
    global Newdumpfile
    Newdumpfile = None
    _handle_args(raw_args)

    dump_mode = 'a'
    if Args.overwrite:
        dump_mode = 'w'
    if Args.output:
        sys.stdout = open(Args.output, dump_mode)
    if Args.diff:
        # copy new file to old file if it exists
        if os.path.isfile('_pgmem.dump.new'):
            shutil.copyfile('_pgmem.dump.new', '_pgmem.dump.old')
        Newdumpfile = open('_pgmem.dump.new', 'w')

    process = debugger.GetSelectedTarget().GetProcess()
    frame = process.GetSelectedThread().GetSelectedFrame()

    if Args.use_cc:
        Args.memory_context_var = "CurrentMemoryContext"
    memcxt = frame.FindVariable(Args.memory_context_var)
    if not memcxt.IsValid():
        memcxt = lldb_target.FindFirstGlobalVariable(Args.memory_context_var)
        if not memcxt.IsValid():
            print("variable `%s` not found in current frame",
                  Args.memory_context_var)
            return
    memcxt = MemoryContext(memcxt)
    assert memcxt.typcxt in CONTEXT_KINDS, \
        f"{Args.memory_context_var} is not an MemoryContext"

    grand_totals = MemoryContextCounters()
    MemoryContextStatsInternal(
        memcxt, 0, True, Args.max_children, grand_totals)
    print(grand_totals)
    if Args.diff:
        Newdumpfile.close()
        os.system("diff -duN --color=always _pgmem.dump.old _pgmem.dump.new")
        # os.remove('_pgmem.dump.old')
        # os.remove('_pgmem.dump.new')


def maxalign(len):
    return (len + 7) & ~7


def AllocSetStats(context, printfunc, passthru, totals):
    totalspace = sizeof("AllocSetContext")
    nblocks = 0
    freespace = 0
    freechunks = 0
    aset = AllocSetContext(cast_memcxt(context, "AllocSetContext"))

    block = AllocBlock(aset.blocks)
    while block:
        nblocks += 1
        totalspace += len(block)
        freespace += block.available()
        block = AllocBlock(block.next)

    for fidx in range(ALLOCSET_NUM_FREELISTS):
        chksz = GetChunkSizeFromFreeListIdx(fidx)
        chunk = aset.freelist[fidx]

        while chunk.is_not_null():
            link = chunk.GetFreeListLink()
            freechunks += 1
            freespace += chksz + sizeof("MemoryChunk")
            chunk = MemoryChunk(link.next)

    if printfunc:
        stats_string = \
            "{} total in {} blocks; {} free ({} chunks); {} used" \
            .format(totalspace, nblocks, freespace, freechunks,
                    totalspace - freespace)
        printfunc(context, passthru, stats_string)

    if totals:
        totals.nblocks += nblocks
        totals.freechunks += freechunks
        totals.totalspace += totalspace
        totals.freespace += freespace


class dlist_node:
    def __init__(self, node):
        self._c_node = node
        self.next = node.GetChildMemberWithName("next")
        self.prev = node.GetChildMemberWithName("prev")


#
# GenerationBlock
#  	GenerationBlock is the unit of memory that is obtained by generation.c
#  	from malloc().  It contains zero or more MemoryChunks, which are the
#  	units requested by palloc() and freed by pfree().  MemoryChunks cannot
#  	be returned to malloc() individually, instead pfree() updates the free
#  	counter of the block and when all chunks in a block are free the whole
#  	block can be returned to malloc().
#
#  	GenerationBlock is the header data for a block --- the usable space
#  	within the block begins at the next alignment boundary.
#
class GenerationBlock:
    def __init__(self, blk):
        self._c_blk = blk
        self.node = dlist_node(blk.GetChildMemberWithName("node"))
        self.context = blk.GetChildMemberWithName("context")
        self.blksize = blk.GetChildMemberWithName("blksize").GetValue()
        self.nchunks = blk.GetChildMemberWithName("nchunks").GetValue()
        self.nfree = blk.GetChildMemberWithName("nfree").GetValue()
        freeptr = blk.GetChildMemberWithName("freeptr")
        self.freeptr = freeptr.GetValueAsUnsigned()
        endptr = blk.GetChildMemberWithName("endptr")
        self.endptr = endptr.GetValueAsUnsigned()


class dlist_head:
    def __init__(self, head):
        self._c_head = head
        self.head = dlist_node(head.GetChildMemberWithName("head"))

    # TODO:
    def __iter__(self):
        node = self.head
        while node.is_not_null():
            yield node
            node = dlist_node(node.next)


class GenerationContext:
    def __init__(self, gen):
        self._c_gen = gen
        # Standard memory-context fields
        self.header = MemoryContext(gen.GetChildMemberWithName("header"))
        # current (most recently allocated) block, or
        # NULL if we've just freed the most recent block
        self.block = GenerationBlock(gen.GetChildMemberWithName("block"))
        # pointer to a block that's being recycled,
        # or NULL if there's no such block.
        self.freeblock = GenerationBlock(
            gen.GetChildMemberWithName("freeblock"))
        # list of blocks
        self.blocks = dlist_head(gen.GetChildMemberWithName("blocks"))


def GenerationStats(context, printfunc, passthru, totals):
    aset = GenerationContext(cast_memcxt(context, "GenerationContext"))

    totalspace = sizeof("GenerationContext")
    nblocks = 0
    nchunks = 0
    nfreechunks = 0
    freespace = 0

    for block in aset.blocks:
        nblocks += 1
        nchunks += block.nchunks
        nfreechunks += block.nfree
        totalspace += block.blksize
        freespace += block.available()

    if printfunc:
        stats_string = \
            "{} total in {} blocks ({} chunks); {} free ({} chunks); {} used" \
            .format(totalspace, nblocks, nchunks, freespace, nfreechunks,
                    totalspace - freespace)
        printfunc(context, passthru, stats_string)

    if totals:
        totals.nblocks += nblocks
        totals.freechunks += nfreechunks
        totals.totalspace += totalspace
        totals.freespace += freespace


def SlabStats(context, printfunc, passthru, totals):
    pass


MEMORY_CONTEXT_STATS_IMPL = {
    "T_AllocSetContext": AllocSetStats,
    "T_SlabContext": SlabStats,
    "T_GenerationContext": GenerationStats,
}


def MemoryContextStatsInternal(memcxt, level, printit, max_children, totals):
    local_totals = MemoryContextCounters()

    # Examine the context itself
    fn_stats = MEMORY_CONTEXT_STATS_IMPL[memcxt.typcxt]
    fn_print = MemoryContextStatsPrint if printit else None
    fn_stats(memcxt, fn_print, level, totals)

    ichild = 0
    child = MemoryContext(memcxt.firstchild)
    while child.is_not_null():
        if ichild < max_children:
            MemoryContextStatsInternal(
                child, level + 1, printit, max_children, totals)
        else:
            MemoryContextStatsInternal(
                child, level + 1, False, max_children, local_totals)
        child = MemoryContext(child.nextchild)
        ichild += 1

    if ichild > max_children:
        if printit:
            for i in range(level + 1):
                print("  ", end="")
            print("\
{} more child contexts containing {} total in {} blocks;  \
{} free ({} chunks); {} used"
                  .format(
                      ichild - max_children,
                      local_totals.totalspace,
                      local_totals.nblocks,
                      local_totals.freespace,
                      local_totals.freechunks,
                      local_totals.totalspace - local_totals.freespace
                  ))

        if totals:
            totals.nblocks += local_totals.nblocks
            totals.freechunks += local_totals.freechunks
            totals.totalspace += local_totals.totalspace
            totals.freespace += local_totals.freespace


def MemoryContextStatsPrint(context, passthru, stats_string):
    level = passthru
    name = context.name
    ident = context.ident

    if Args.include:
        if name not in Args.include:
            return

    if Args.exclude:
        if name in Args.exclude:
            return

    def dprint(*args, **kwargs):
        if Newdumpfile:
            Newdumpfile.write(*args)
            if kwargs.get("end", None) is None:
                Newdumpfile.write("\n")
        else:
            print(*args, **kwargs)

    #
    # It seems preferable to label dynahash contexts with just the hash table
    # name.  Those are already unique enough, so the "dynahash" part isn't
    # very helpful, and this way is more consistent with pre-v11 practice.
    #
    if name == "dynahash":
        name = ident
        ident = ""

    for i in range(level):
        dprint("  ", end="")
    ident = f": {ident}" if len(ident) > 0 else ""
    if context.Current() == name:
        name = f"*{name}"
    dprint(f"{name}: {stats_string}{ident}")


def __lldb_init_module(debugger, internal_dict):
    add_cmd = "command script add -o -f pg_memcxt_stats"
    exported_cmd = [
        "pgmem",
    ]
    for cmd in exported_cmd:
        debugger.HandleCommand(f"{add_cmd}.{cmd} {cmd}")

    print("new commands installed and ready for use:")
    for cmd in exported_cmd:
        print(f"    \033[1;32m{cmd}\033[0m")
