import lldb


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

    def __repr__(self):
        print(
            "Grand total: {} bytes in {} blocks; {} free ({} chunks); {} used",
            self.totalspace,
            self.nblocks,
            self.freespace,
            self.freechunks,
            self.totalspace - self.freespace,
        )


def print_memcxt(memcxt):
    grand_totals = MemoryContextCounters()
    max_children = 100
    MemoryContextStatsInternal(memcxt, 0, True, max_children, grand_totals)
    print(grand_totals)


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
    level = passthru
    name = context.name
    ident = context.ident

    #
    # It seems preferable to label dynahash contexts with just the hash table
    # name.  Those are already unique enough, so the "dynahash" part isn't
    # very helpful, and this way is more consistent with pre-v11 practice.
    #
    if ident and name == "dynahash":
        name = ident
        ident = None

    if ident:
        #
        # Some contexts may have very long identifiers (e.g., SQL queries).
        # Arbitrarily truncate at 100 bytes, but be careful not to break
        # multibyte characters.  Also, replace ASCII control characters, such
        # as newlines, with spaces.
        #
        idlen = len(ident)
        truncated = False
        truncated_ident = ": "

        if idlen > 100:
            idlen = pg_mbcliplen(ident, idlen, 100)
            truncated = True

        idlen -= 1;
        while idlen > 0:
            # unsigned char c = *ident++;

            # if (c < ' ')
            #     c = ' ';
            # truncated_ident[i++] = c;
            ident -= 1

        if truncated:
            truncated_ident.append("...")

    for i in range(level):
        print("  ", end="")
    print(f"{name}: {stats_string}{truncated_ident}")


def AllocSetStats(context, printfunc, passthru, totals):
    set = AllocSet(context)

    # Include context header in totalspace
    totalspace = MAXALIGN(sizeof(AllocSetContext))

    nblocks = 0
    block = set.blocks
    while block:
        nblocks += 1
        totalspace += block.endptr - block  # TODO:
        freespace += block.endptr - block.freeptr
        block = block.next
    fidx = 0
    while fidx < ALLOCSET_NUM_FREELISTS:
        chksz = GetChunkSizeFromFreeListIdx(fidx)
        chunk = set.freelist[fidx]

        while chunk:
            link = GetFreeListLink(chunk)

            freechunks += 1
            freespace += chksz + ALLOC_CHUNKHDRSZ

            chunk = link.next

        fidx += 1

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


def __lldb_init_module(debugger, internal_dict):
    summary = "type summary add -F pg_memcxt_stats.print_memcxt MemoryContext"
    debugger.HandleCommand(summary)
