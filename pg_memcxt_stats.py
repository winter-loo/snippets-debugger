import lldb
import sys


# C macros in PostgreSQL
ALLOC_CHUNKHDRSZ = 8
ALLOCSET_NUM_FREELISTS = 11
ALLOC_MINBITS = 3


lldb_target = lldb.debugger.GetSelectedTarget()


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
        name = memcxt.GetChildMemberWithName("name")
        self.name = read_c_string_from_memory(name.GetValueAsUnsigned())
        ident = memcxt.GetChildMemberWithName("ident")
        self.ident = read_c_string_from_memory(ident.GetValueAsUnsigned())
        self.methods = memcxt.GetChildMemberWithName("methods")
        self.firstchild = memcxt.GetChildMemberWithName("firstchild")
        self.nextchild = memcxt.GetChildMemberWithName("nextchild")

    def is_not_null(self):
        return self._c_memcxt.GetValueAsUnsigned() != 0


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


def pgmem_help():
    print("Usage: pgmem [ -N | -o <file> ] <memory context>")
    print("  -N: overwrite the dump file")
    print("  -o: dump to the file instead of stdout")


def pgmem(debugger, raw_args, result, internal_dict):
    raw_args = raw_args.split()

    dump_file = None
    dump_mode = 'a'
    memcxt_var = None

    i = 0
    while i < len(raw_args):
        if raw_args[i] == '-N':
            dump_mode = 'w'
            i += 1
        elif raw_args[i] == '-o':
            if i + 1 >= len(raw_args):
                pgmem_help()
                return
            dump_file = raw_args[i + 1]
            i += 2
        elif not raw_args[i].startswith('-'):
            memcxt_var = raw_args[i]
            i += 1
        else:
            pgmem_help()
            return

    if memcxt_var is None:
        pgmem_help()
        return

    print(f"dump `{memcxt_var}` @{dump_file}!{dump_mode}")
    if dump_file is not None:
        sys.stdout = open(dump_file, dump_mode)

    process = debugger.GetSelectedTarget().GetProcess()
    frame = process.GetSelectedThread().GetSelectedFrame()
    memcxt = frame.FindVariable(memcxt_var)
    memcxt = MemoryContext(memcxt)

    grand_totals = MemoryContextCounters()
    max_children = 100
    MemoryContextStatsInternal(memcxt, 0, max_children, grand_totals)
    print(grand_totals)


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
            freespace += chksz + ALLOC_CHUNKHDRSZ
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


def MemoryContextStatsInternal(memcxt, level, max_children, totals):
    local_totals = MemoryContextCounters()

    # Examine the context itself
    AllocSetStats(memcxt,
                  MemoryContextStatsPrint,
                  level,
                  totals)

    ichild = 0
    child = MemoryContext(memcxt.firstchild)
    while child.is_not_null():
        if ichild < max_children:
            MemoryContextStatsInternal(
                child, level + 1, max_children, totals)
        else:
            MemoryContextStatsInternal(
                child, level + 1, max_children, local_totals)
        child = MemoryContext(child.nextchild)
        ichild += 1

    if ichild > max_children:
        for i in range(level):
            print("  ", end="")
        print("{} more child contexts containing {} total in {} blocks; \
            {} free ({} chunks); {} used",
              ichild - max_children,
              local_totals.totalspace,
              local_totals.nblocks,
              local_totals.freespace,
              local_totals.freechunks,
              local_totals.totalspace - local_totals.freespace,
              )

        if totals:
            totals.nblocks += local_totals.nblocks
            totals.freechunks += local_totals.freechunks
            totals.totalspace += local_totals.totalspace
            totals.freespace += local_totals.freespace


def MemoryContextStatsPrint(context, passthru, stats_string):
    level = passthru
    name = context.name
    ident = context.ident

    #
    # It seems preferable to label dynahash contexts with just the hash table
    # name.  Those are already unique enough, so the "dynahash" part isn't
    # very helpful, and this way is more consistent with pre-v11 practice.
    #
    if name == "dynahash":
        name = ident
        ident = ""

    for i in range(level):
        print("  ", end="")
    ident = f"({ident})" if len(ident) > 0 else ""
    print(f"{name}{ident}: {stats_string}")


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
