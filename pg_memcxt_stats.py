import lldb


# C macros in PostgreSQL
ALLOC_CHUNKHDRSZ = 8
ALLOCSET_NUM_FREELISTS = 11
ALLOC_MINBITS = 3


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
        return """
Grand total: {} bytes in {} blocks; {} free ({} chunks); {} used
    """.format(
            self.totalspace,
            self.nblocks,
            self.freespace,
            self.freechunks,
            self.totalspace - self.freespace,
        )


def sizeof(typname):
    return lldb.target.FindFirstType(typname).GetByteSize()


def cast_memcxt(value, typname):
    memcxt = value._c_memcxt
    assert memcxt.IsPointerType(), "memcxt is not a pointer type"
    return memcxt.Cast(lldb.target.FindFirstType(typname).GetPointerType())


# Determine the size of the chunk based on the freelist index
def GetChunkSizeFromFreeListIdx(fidx):
    return 1 << ALLOC_MINBITS << fidx


class MemoryContext:
    def __init__(self, memcxt):
        """
        memcxt: lldb.SBValue
        """
        self._c_memcxt = memcxt
        self.name = memcxt.GetChildMemberWithName("name")
        self.ident = memcxt.GetChildMemberWithName("ident")
        self.methods = memcxt.GetChildMemberWithName("methods")
        self.firstchild = memcxt.GetChildMemberWithName("firstchild")
        self.nextchild = memcxt.GetChildMemberWithName("nextchild")


class AllocSetContext:
    def __init__(self, aset):
        self._c_aset = aset
        self.blocks = aset.GetChildMemberWithName("blocks")
        freelist = aset.GetChildMemberWithName("freelist")
        assert freelist.GetType().IsArrayType(), "freelist is not an array type"
        self.freelist = [
            MemoryChunk(freelist.GetChildAtIndex(i)) for i in range(ALLOCSET_NUM_FREELISTS)
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


lldb_target = lldb.debugger.GetSelectedTarget()


class MemoryChunk:
    def __init__(self, chunk):
        self._c_chunk = chunk

    def GetFreeListLink(self):
        chkptr = self._c_chunk.GetValueAsUnsigned()
        addr = lldb.SBAddress(chkptr + sizeof('MemoryChunk'), lldb_target)
        link_type = lldb_target.FindFirstType('AllocFreeListLink')
        link = lldb_target.CreateValueFromAddress("link", addr, link_type)
        return AllocFreeListLink(link)


def print_memcxt(memcxt):
    memcxt = MemoryContext(memcxt)
    grand_totals = MemoryContextCounters()
    max_children = 100
    MemoryContextStatsInternal(memcxt, 0, True, max_children, grand_totals)
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
        block = block.next

    for fidx in range(ALLOCSET_NUM_FREELISTS):
        chksz = GetChunkSizeFromFreeListIdx(fidx)
        chunk = aset.freelist[fidx]

        while chunk:
            link = GetFreeListLink(chunk)
            freechunks += 1
            freespace += chksz + ALLOC_CHUNKHDRSZ
            chunk = link.next

    if printfunc:
        stats_string = format(
                 "{} total in {} blocks; {} free ({} chunks); {} used",
                 totalspace, nblocks, freespace, freechunks,
                 totalspace - freespace)
        printfunc(context, passthru, stats_string)

    if totals:
        totals.nblocks += nblocks
        totals.freechunks += freechunks
        totals.totalspace += totalspace
        totals.freespace += freespace


def MemoryContextStatsInternal(memcxt, level, print, max_children, totals):
    local_totals = MemoryContextCounters()

    # Examine the context itself
    # memcxt.methods.stats(context,
    #                     MemoryContextStatsPrint if print else None,
    #                         (void * ) & level,
    #                         totals)

    ichild = 0
    child = memcxt.firstchild
    while child is not None:
        if ichild < max_children:
            MemoryContextStatsInternal(
                child, level + 1, print, max_children, totals)
        else:
            MemoryContextStatsInternal(
                child, level + 1, False, max_children, local_totals)
        child = child.nextchild
        ichild += 1

    if ichild > max_children:
        if print:
            for i in range(level):
                print("  ", end="")
            print(
                "{} more child contexts containing {} total in {} blocks; \
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
    pass
    # level = passthru
    # name = context.name
    # ident = context.ident

    # #
    # # It seems preferable to label dynahash contexts with just the hash table
    # # name.  Those are already unique enough, so the "dynahash" part isn't
    # # very helpful, and this way is more consistent with pre-v11 practice.
    # #
    # if ident and name == "dynahash":
    #     name = ident
    #     ident = None

    # if ident:
    #     #
    #     # Some contexts may have very long identifiers (e.g., SQL queries).
    #     # Arbitrarily truncate at 100 bytes, but be careful not to break
    #     # multibyte characters.  Also, replace ASCII control characters, such
    #     # as newlines, with spaces.
    #     #
    #     idlen = len(ident)
    #     truncated = False
    #     truncated_ident = ": "

    #     if idlen > 100:
    #         idlen = pg_mbcliplen(ident, idlen, 100)
    #         truncated = True

    #     idlen -= 1;
    #     while idlen > 0:
    #         # unsigned char c = *ident++;

    #         # if (c < ' ')
    #         #     c = ' ';
    #         # truncated_ident[i++] = c;
    #         ident -= 1

    #     if truncated:
    #         truncated_ident.append("...")

    # for i in range(level):
    #     print("  ", end="")
    # print(f"{name}: {stats_string}{truncated_ident}")


def AllocSetStats(context, printfunc, passthru, totals):
    pass
    # set = AllocSet(context)

    # # Include context header in totalspace
    # totalspace = MAXALIGN(sizeof(AllocSetContext))

    # nblocks = 0
    # block = set.blocks
    # while block:
    #     nblocks += 1
    #     totalspace += block.endptr - block  # TODO:
    #     freespace += block.endptr - block.freeptr
    #     block = block.next
    # fidx = 0
    # while fidx < ALLOCSET_NUM_FREELISTS:
    #     chksz = GetChunkSizeFromFreeListIdx(fidx)
    #     chunk = set.freelist[fidx]

    #     while chunk:
    #         link = GetFreeListLink(chunk)

    #         freechunks += 1
    #         freespace += chksz + ALLOC_CHUNKHDRSZ

    #         chunk = link.next

    #     fidx += 1

    # if printfunc:
    #     stats_string = format(
    #              "{} total in {} blocks; {} free ({} chunks); {} used",
    #              totalspace, nblocks, freespace, freechunks,
    #              totalspace - freespace)
    #     printfunc(context, passthru, stats_string)

    # if totals:
    #     totals.nblocks += nblocks
    #     totals.freechunks += freechunks
    #     totals.totalspace += totalspace
    #     totals.freespace += freespace


def __lldb_init_module(debugger, internal_dict):
    add_cmd = "command script add -f pg_memcxt_stats"
    exported_cmd = [
        "print_memcxt",
    ]
    for cmd in exported_cmd:
        debugger.HandleCommand(f"{add_cmd}.{cmd} {cmd}")

    print("new commands installed and ready for use:")
    for cmd in exported_cmd:
        print(f"    \033[1;32m{cmd}\033[0m")
