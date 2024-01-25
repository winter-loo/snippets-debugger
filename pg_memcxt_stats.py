import lldb  # type ignore
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


# Determine the size of the chunk based on the freelist index
def GetChunkSizeFromFreeListIdx(fidx):
    return 1 << ALLOC_MINBITS << fidx


class MemoryContext:
    def __init__(self, memcxt: lldb.SBValue):
        assert memcxt.GetType().IsPointerType(), "memcxt is not a pointer type"
        self._c_memcxt = memcxt
        # memory context type is an C enum name
        self.typcxt = memcxt.GetChildMemberWithName("type").GetValue()
        name = memcxt.GetChildMemberWithName("name").GetSummary()
        self.name = name.strip("\"") if name else ""
        ident = memcxt.GetChildMemberWithName("ident").GetSummary()
        self.ident = ident.strip("\"") if ident else ""
        self.methods = memcxt.GetChildMemberWithName("methods")
        self.firstchild = memcxt.GetChildMemberWithName("firstchild")
        self.nextchild = memcxt.GetChildMemberWithName("nextchild")

    def __eq__(self, other):
        return self._c_memcxt.GetValueAsUnsigned() == \
            other._c_memcxt.GetValueAsUnsigned()

    def is_not_null(self):
        return self._c_memcxt.GetValueAsUnsigned() != 0

    def CastAs(self, typname):
        return cast_memcxt(self, typname)


class GlobalMemoryContext:
    current = MemoryContext(
        lldb_target.FindFirstGlobalVariable("CurrentMemoryContext"))


def cast_memcxt(value: MemoryContext, typname):
    memcxt = value._c_memcxt
    assert memcxt.GetType().IsPointerType(), "memcxt is not a pointer type"
    return memcxt.Cast(lldb_target.FindFirstType(typname).GetPointerType())


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
                        default='CurrentMemoryContext',
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
    parser.add_argument('-a', '--all-contexts', action='store_true',
                        help='show all memory contexts')

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

    if Args.all_contexts:
        Args.memory_context_var = "TopMemoryContext"
    memcxt = frame.EvaluateExpression(Args.memory_context_var)
    if not memcxt.GetError().Success():
        print("expression `{}` is not valid"
              .format(Args.memory_context_var))
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


def AllocSetStats(context: MemoryContext, printfunc, passthru, totals):
    totalspace = maxalign(sizeof("AllocSetContext"))
    nblocks = 0
    freespace = 0
    freechunks = 0
    aset = AllocSetContext(context.CastAs("AllocSetContext"))

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
    def __init__(self, node: lldb.SBValue):
        assert not node.GetType().IsPointerType(), "node is a pointer type"
        self._c_node = node
        self.next = node.GetChildMemberWithName("next")
        assert self.next.GetType().IsPointerType(), "next is not a pointer type"
        self.prev = node.GetChildMemberWithName("prev")
        assert self.prev.GetType().IsPointerType(), "prev is not a pointer type"

    def Next(self):
        if self.next.GetValueAsUnsigned() == 0:
            return None
        return dlist_node(self.next.Dereference())

    def Prev(self):
        if self.prev.GetValueAsUnsigned() == 0:
            return None
        return dlist_node(self.prev.Dereference())

    def _is_empty(self):
        next_ptr = self.next.GetValueAsUnsigned()
        prev_ptr = self.prev.GetValueAsUnsigned()
        node_ptr = self._c_node.AddressOf().GetValueAsUnsigned()
        return (next_ptr == 0 and prev_ptr == 0) or \
            (next_ptr == node_ptr and prev_ptr == node_ptr)

    def is_valid(self):
        return not self._is_empty()

    def CastAs(self, typname):
        """
        dlist_node* -> typname*
        """
        target_typ = lldb_target.FindFirstType(typname)
        target_typ_ptr = target_typ.GetPointerType()  # typname*
        generic_ptr = self._c_node.AddressOf()  # char*
        target_ptr = generic_ptr.Cast(target_typ_ptr)
        return target_ptr

    def __eq__(self, other):
        return self._c_node.AddressOf().GetValueAsUnsigned() == \
            other._c_node.AddressOf().GetValueAsUnsigned()

    def __str__(self):
        return "<dlist_node: {{next: {},, prev: {}}}>".format(
            self.next.GetValueAsUnsigned(), self.prev.GetValueAsUnsigned())


class dlist_head:
    def __init__(self, blocks: lldb.SBValue):
        assert not blocks.GetType().IsPointerType(), "blocks is a pointer type"
        self._c_head = blocks.GetChildMemberWithName("head")
        #
        # head.next either points to the first element of the list; to &head if
        # it's a circular empty list; or to NULL if empty and not circular.
        #
        # head.prev either points to the last element of the list; to &head if
        # it's a circular empty list; or to NULL if empty and not circular.
        #
        self.head = dlist_node(self._c_head)

    def is_empty(self):
        return self.head._is_empty()

    def __iter__(self):
        end = self.head
        cur = self.head.Next()
        while cur and cur.is_valid() and cur != end:
            yield cur
            cur = cur.Next()


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
    def __init__(self, blk: lldb.SBValue):
        """
        blk is a pointer to a GenerationBlock
        """
        self._c_blk = blk
        self.node = dlist_node(blk.GetChildMemberWithName("node"))
        self.context = blk.GetChildMemberWithName("context")

        blksize = blk.GetChildMemberWithName("blksize")
        self.blksize = blksize.GetValueAsUnsigned()
        nchunks = blk.GetChildMemberWithName("nchunks")
        self.nchunks = nchunks.GetValueAsUnsigned()
        nfree = blk.GetChildMemberWithName("nfree")
        self.nfree = nfree.GetValueAsUnsigned()
        freeptr = blk.GetChildMemberWithName("freeptr")
        self.freeptr = freeptr.GetValueAsUnsigned()
        endptr = blk.GetChildMemberWithName("endptr")
        self.endptr = endptr.GetValueAsUnsigned()

    def available(self):
        return self.endptr - self.freeptr


class GenerationContext:
    def __init__(self, gen: lldb.SBValue):
        self._c_gen = gen
        # list of blocks
        # same: &gen.blocks, &gen.blocks.head, &gen.blocks.head.prev
        blocks = gen.GetChildMemberWithName("blocks")
        self.blocks = dlist_head(blocks)


def GenerationStats(context: MemoryContext, printfunc, passthru, totals):
    gen = GenerationContext(context.CastAs("GenerationContext"))

    totalspace = maxalign(sizeof("GenerationContext"))
    nblocks = 0
    nchunks = 0
    nfreechunks = 0
    freespace = 0

    for node in gen.blocks:
        block = GenerationBlock(node.CastAs("GenerationBlock"))
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


def MemoryContextStatsPrint(context: MemoryContext, passthru, stats_string):
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
    if context == GlobalMemoryContext.current:
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
