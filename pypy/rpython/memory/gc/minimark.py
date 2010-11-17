""" MiniMark GC.

Environment variables can be used to fine-tune the following parameters:
    
 PYPY_GC_NURSERY        The nursery size.  Defaults to half the size of
                        the L2 cache.  Try values like '1.2MB'.

 PYPY_GC_MAJOR_COLLECT  Major collection memory factor.  Default is '1.82',
                        which means trigger a major collection when the
                        memory consumed equals 1.82 times the memory
                        really used at the end of the previous major
                        collection.

 PYPY_GC_GROWTH         Major collection threshold's max growth rate.
                        Default is '1.3'.  Useful to collect more often
                        than normally on sudden memory growth, e.g. when
                        there is a temporary peak in memory usage.

 PYPY_GC_MAX            The max heap size.  If coming near this limit, it
                        will first collect more often, then raise an
                        RPython MemoryError, and if that is not enough,
                        crash the program with a fatal error.  Try values
                        like '1.6GB'.

 PYPY_GC_MIN            Don't collect while the memory size is below this
                        limit.  Useful to avoid spending all the time in
                        the GC in very small programs.  Defaults to 8
                        times the nursery.
"""
# XXX Should find a way to bound the major collection threshold by the
# XXX total addressable size.  Maybe by keeping some minimarkpage arenas
# XXX pre-reserved, enough for a few nursery collections?  What about
# XXX raw-malloced memory?
import sys
from pypy.rpython.lltypesystem import lltype, llmemory, llarena, llgroup
from pypy.rpython.lltypesystem.lloperation import llop
from pypy.rpython.lltypesystem.llmemory import raw_malloc_usage
from pypy.rpython.memory.gc.base import GCBase, MovingGCBase
from pypy.rpython.memory.gc import minimarkpage, base, generation
from pypy.rlib.rarithmetic import ovfcheck, LONG_BIT, intmask, r_uint
from pypy.rlib.rarithmetic import LONG_BIT_SHIFT
from pypy.rlib.debug import ll_assert, debug_print, debug_start, debug_stop
from pypy.rlib.objectmodel import we_are_translated
from pypy.tool.sourcetools import func_with_new_name

WORD = LONG_BIT // 8
NULL = llmemory.NULL

first_gcflag = 1 << (LONG_BIT//2)

# The following flag is never set on young objects, i.e. the ones living
# in the nursery.  It is initially set on all prebuilt and old objects,
# and gets cleared by the write_barrier() when we write in them a
# pointer to a young object.
GCFLAG_NO_YOUNG_PTRS = first_gcflag << 0

# The following flag is set on some prebuilt objects.  The flag is set
# unless the object is already listed in 'prebuilt_root_objects'.
# When a pointer is written inside an object with GCFLAG_NO_HEAP_PTRS
# set, the write_barrier clears the flag and adds the object to
# 'prebuilt_root_objects'.
GCFLAG_NO_HEAP_PTRS = first_gcflag << 1

# The following flag is set on surviving objects during a major collection.
GCFLAG_VISITED      = first_gcflag << 2

# The following flag is set on nursery objects of which we asked the id
# or the identityhash.  It means that a space of the size of the object
# has already been allocated in the nonmovable part.  The same flag is
# abused to mark prebuilt objects whose hash has been taken during
# translation and is statically recorded.
GCFLAG_HAS_SHADOW   = first_gcflag << 3

# The following flag is set temporarily on some objects during a major
# collection.  See pypy/doc/discussion/finalizer-order.txt
GCFLAG_FINALIZATION_ORDERING = first_gcflag << 4

# The following flag is set on externally raw_malloc'ed arrays of pointers.
# They are allocated with some extra space in front of them for a bitfield,
# one bit per 'card_page_indices' indices.
GCFLAG_HAS_CARDS    = first_gcflag << 5
GCFLAG_CARDS_SET    = first_gcflag << 6     # <- at least one card bit is set


FORWARDSTUB = lltype.GcStruct('forwarding_stub',
                              ('forw', llmemory.Address))
FORWARDSTUBPTR = lltype.Ptr(FORWARDSTUB)


# ____________________________________________________________

class MiniMarkGC(MovingGCBase):
    _alloc_flavor_ = "raw"
    inline_simple_malloc = True
    inline_simple_malloc_varsize = True
    needs_write_barrier = True
    prebuilt_gc_objects_are_static_roots = False
    malloc_zero_filled = True    # xxx experiment with False

    # All objects start with a HDR, i.e. with a field 'tid' which contains
    # a word.  This word is divided in two halves: the lower half contains
    # the typeid, and the upper half contains various flags, as defined
    # by GCFLAG_xxx above.
    HDR = lltype.Struct('header', ('tid', lltype.Signed))
    typeid_is_in_field = 'tid'
    withhash_flag_is_in_field = 'tid', GCFLAG_HAS_SHADOW
    # ^^^ prebuilt objects may have the flag GCFLAG_HAS_SHADOW;
    #     then they are one word longer, the extra word storing the hash.


    # During a minor collection, the objects in the nursery that are
    # moved outside are changed in-place: their header is replaced with
    # the value -42, and the following word is set to the address of
    # where the object was moved.  This means that all objects in the
    # nursery need to be at least 2 words long, but objects outside the
    # nursery don't need to.
    minimal_size_in_nursery = (
        llmemory.sizeof(HDR) + llmemory.sizeof(llmemory.Address))


    TRANSLATION_PARAMS = {
        # Automatically adjust the size of the nursery and the
        # 'major_collection_threshold' from the environment.
        # See docstring at the start of the file.
        "read_from_env": True,

        # The size of the nursery.  Note that this is only used as a
        # fall-back number.
        "nursery_size": 896*1024,

        # The system page size.  Like obmalloc.c, we assume that it is 4K
        # for 32-bit systems; unlike obmalloc.c, we assume that it is 8K
        # for 64-bit systems, for consistent results.
        "page_size": 1024*WORD,

        # The size of an arena.  Arenas are groups of pages allocated
        # together.
        "arena_size": 65536*WORD,

        # The maximum size of an object allocated compactly.  All objects
        # that are larger are just allocated with raw_malloc().  Note that
        # the size limit for being first allocated in the nursery is much
        # larger; see below.
        "small_request_threshold": 35*WORD,

        # Full collection threshold: after a major collection, we record
        # the total size consumed; and after every minor collection, if the
        # total size is now more than 'major_collection_threshold' times,
        # we trigger the next major collection.
        "major_collection_threshold": 1.82,

        # Threshold to avoid that the total heap size grows by a factor of
        # major_collection_threshold at every collection: it can only
        # grow at most by the following factor from one collection to the
        # next.  Used e.g. when there is a sudden, temporary peak in memory
        # usage; this avoids that the upper bound grows too fast.
        "growth_rate_max": 1.3,

        # The number of array indices that are mapped to a single bit in
        # write_barrier_from_array().  Must be a power of two.  The default
        # value of 128 means that card pages are 512 bytes (1024 on 64-bits)
        # in regular arrays of pointers; more in arrays whose items are
        # larger.  A value of 0 disables card marking.
        "card_page_indices": 128,

        # Objects whose total size is at least 'large_object' bytes are
        # allocated out of the nursery immediately.  If the object
        # has GC pointers in its varsized part, we use instead the
        # higher limit 'large_object_gcptrs'.  The idea is that
        # separately allocated objects are allocated immediately "old"
        # and it's not good to have too many pointers from old to young
        # objects.
        "large_object": 1600*WORD,
        "large_object_gcptrs": 8250*WORD,
        }

    def __init__(self, config,
                 read_from_env=False,
                 nursery_size=32*WORD,
                 page_size=16*WORD,
                 arena_size=64*WORD,
                 small_request_threshold=5*WORD,
                 major_collection_threshold=2.5,
                 growth_rate_max=2.5,   # for tests
                 card_page_indices=0,
                 large_object=8*WORD,
                 large_object_gcptrs=10*WORD,
                 ArenaCollectionClass=None,
                 **kwds):
        MovingGCBase.__init__(self, config, **kwds)
        assert small_request_threshold % WORD == 0
        self.read_from_env = read_from_env
        self.nursery_size = nursery_size
        self.small_request_threshold = small_request_threshold
        self.major_collection_threshold = major_collection_threshold
        self.growth_rate_max = growth_rate_max
        self.num_major_collects = 0
        self.min_heap_size = 0.0
        self.max_heap_size = 0.0
        self.max_heap_size_already_raised = False
        #
        self.card_page_indices = card_page_indices
        if self.card_page_indices > 0:
            self.card_page_shift = 0
            while (1 << self.card_page_shift) < self.card_page_indices:
                self.card_page_shift += 1
        #
        # 'large_object' and 'large_object_gcptrs' limit how big objects
        # can be in the nursery, so they give a lower bound on the allowed
        # size of the nursery.
        self.nonlarge_max = large_object - 1
        self.nonlarge_gcptrs_max = large_object_gcptrs - 1
        assert self.nonlarge_max <= self.nonlarge_gcptrs_max
        #
        self.nursery      = NULL
        self.nursery_free = NULL
        self.nursery_top  = NULL
        self.debug_always_do_minor_collect = False
        #
        # The ArenaCollection() handles the nonmovable objects allocation.
        if ArenaCollectionClass is None:
            ArenaCollectionClass = minimarkpage.ArenaCollection
        self.ac = ArenaCollectionClass(arena_size, page_size,
                                       small_request_threshold)
        #
        # Used by minor collection: a list of non-young objects that
        # (may) contain a pointer to a young object.  Populated by
        # the write barrier.
        self.old_objects_pointing_to_young = self.AddressStack()
        #
        # Similar to 'old_objects_pointing_to_young', but lists objects
        # that have the GCFLAG_CARDS_SET bit.  For large arrays.  Note
        # that it is possible for an object to be listed both in here
        # and in 'old_objects_pointing_to_young', in which case we
        # should just clear the cards and trace it fully, as usual.
        self.old_objects_with_cards_set = self.AddressStack()
        #
        # A list of all prebuilt GC objects that contain pointers to the heap
        self.prebuilt_root_objects = self.AddressStack()
        #
        self._init_writebarrier_logic()


    def setup(self):
        """Called at run-time to initialize the GC."""
        #
        # Hack: MovingGCBase.setup() sets up stuff related to id(), which
        # we implement differently anyway.  So directly call GCBase.setup().
        GCBase.setup(self)
        #
        # A list of all raw_malloced objects (the objects too large)
        self.rawmalloced_objects = self.AddressStack()
        self.rawmalloced_total_size = r_uint(0)
        #
        # A list of all objects with finalizers (never in the nursery).
        self.objects_with_finalizers = self.AddressDeque()
        #
        # Two lists of the objects with weakrefs.  No weakref can be an
        # old object weakly pointing to a young object: indeed, weakrefs
        # are immutable so they cannot point to an object that was
        # created after it.
        self.young_objects_with_weakrefs = self.AddressStack()
        self.old_objects_with_weakrefs = self.AddressStack()
        #
        # Support for id and identityhash: map nursery objects with
        # GCFLAG_HAS_SHADOW to their future location at the next
        # minor collection.
        self.young_objects_shadows = self.AddressDict()
        #
        # Allocate a nursery.  In case of auto_nursery_size, start by
        # allocating a very small nursery, enough to do things like look
        # up the env var, which requires the GC; and then really
        # allocate the nursery of the final size.
        if not self.read_from_env:
            self.allocate_nursery()
        else:
            #
            defaultsize = self.nursery_size
            minsize = 2 * (self.nonlarge_gcptrs_max + 1)
            self.nursery_size = minsize
            self.allocate_nursery()
            #
            # From there on, the GC is fully initialized and the code
            # below can use it
            newsize = base.read_from_env('PYPY_GC_NURSERY')
            # PYPY_GC_NURSERY=1 forces a minor collect for every malloc.
            # Useful to debug external factors, like trackgcroot or the
            # handling of the write barrier.
            self.debug_always_do_minor_collect = newsize == 1
            if newsize <= 0:
                newsize = generation.estimate_best_nursery_size()
                if newsize <= 0:
                    newsize = defaultsize
            newsize = max(newsize, minsize)
            #
            major_coll = base.read_float_from_env('PYPY_GC_MAJOR_COLLECT')
            if major_coll > 1.0:
                self.major_collection_threshold = major_coll
            #
            growth = base.read_float_from_env('PYPY_GC_GROWTH')
            if growth > 1.0:
                self.growth_rate_max = growth
            #
            min_heap_size = base.read_uint_from_env('PYPY_GC_MIN')
            if min_heap_size > 0:
                self.min_heap_size = float(min_heap_size)
            else:
                # defaults to 8 times the nursery
                self.min_heap_size = newsize * 8
            #
            max_heap_size = base.read_uint_from_env('PYPY_GC_MAX')
            if max_heap_size > 0:
                self.max_heap_size = float(max_heap_size)
            #
            self.minor_collection()    # to empty the nursery
            llarena.arena_free(self.nursery)
            self.nursery_size = newsize
            self.allocate_nursery()


    def allocate_nursery(self):
        debug_start("gc-set-nursery-size")
        debug_print("nursery size:", self.nursery_size)
        # the start of the nursery: we actually allocate a bit more for
        # the nursery than really needed, to simplify pointer arithmetic
        # in malloc_fixedsize_clear().  The few extra pages are never used
        # anyway so it doesn't even count.
        extra = self.nonlarge_gcptrs_max + 1
        self.nursery = llarena.arena_malloc(self.nursery_size + extra, 2)
        if not self.nursery:
            raise MemoryError("cannot allocate nursery")
        # the current position in the nursery:
        self.nursery_free = self.nursery
        # the end of the nursery:
        self.nursery_top = self.nursery + self.nursery_size
        # initialize the threshold
        self.min_heap_size = max(self.min_heap_size, self.nursery_size *
                                              self.major_collection_threshold)
        self.next_major_collection_threshold = self.min_heap_size
        self.set_major_threshold_from(0.0)
        debug_stop("gc-set-nursery-size")


    def set_major_threshold_from(self, threshold, reserving_size=0):
        # Set the next_major_collection_threshold.
        threshold_max = (self.next_major_collection_threshold *
                         self.growth_rate_max)
        if threshold > threshold_max:
            threshold = threshold_max
        #
        threshold += reserving_size
        if threshold < self.min_heap_size:
            threshold = self.min_heap_size
        #
        if self.max_heap_size > 0.0 and threshold > self.max_heap_size:
            threshold = self.max_heap_size
            bounded = True
        else:
            bounded = False
        #
        self.next_major_collection_threshold = threshold
        return bounded


    def malloc_fixedsize_clear(self, typeid, size, can_collect=True,
                               needs_finalizer=False, contains_weakptr=False):
        ll_assert(can_collect, "!can_collect")
        size_gc_header = self.gcheaderbuilder.size_gc_header
        totalsize = size_gc_header + size
        rawtotalsize = raw_malloc_usage(totalsize)
        #
        # If the object needs a finalizer, ask for a rawmalloc.
        # The following check should be constant-folded.
        if needs_finalizer:
            ll_assert(not contains_weakptr,
                     "'needs_finalizer' and 'contains_weakptr' both specified")
            obj = self.external_malloc(typeid, 0)
            self.objects_with_finalizers.append(obj)
        #
        # If totalsize is greater than nonlarge_max (which should never be
        # the case in practice), ask for a rawmalloc.  The following check
        # should be constant-folded.
        elif rawtotalsize > self.nonlarge_max:
            ll_assert(not contains_weakptr,
                      "'contains_weakptr' specified for a large object")
            obj = self.external_malloc(typeid, 0)
            #
        else:
            # If totalsize is smaller than minimal_size_in_nursery, round it
            # up.  The following check should also be constant-folded.
            min_size = raw_malloc_usage(self.minimal_size_in_nursery)
            if rawtotalsize < min_size:
                totalsize = rawtotalsize = min_size
            #
            # Get the memory from the nursery.  If there is not enough space
            # there, do a collect first.
            result = self.nursery_free
            self.nursery_free = result + totalsize
            if self.nursery_free > self.nursery_top:
                result = self.collect_and_reserve(totalsize)
            #
            # Build the object.
            llarena.arena_reserve(result, totalsize)
            self.init_gc_object(result, typeid, flags=0)
            #
            # If it is a weakref, record it (check constant-folded).
            if contains_weakptr:
                self.young_objects_with_weakrefs.append(result+size_gc_header)
            #
            obj = result + size_gc_header
        #
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)


    def malloc_varsize_clear(self, typeid, length, size, itemsize,
                             offset_to_length, can_collect):
        ll_assert(can_collect, "!can_collect")
        size_gc_header = self.gcheaderbuilder.size_gc_header
        nonvarsize = size_gc_header + size
        #
        # Compute the maximal length that makes the object still
        # below 'nonlarge_max'.  All the following logic is usually
        # constant-folded because self.nonlarge_max, size and itemsize
        # are all constants (the arguments are constant due to
        # inlining) and self.has_gcptr_in_varsize() is constant-folded.
        if self.has_gcptr_in_varsize(typeid):
            nonlarge_max = self.nonlarge_gcptrs_max
        else:
            nonlarge_max = self.nonlarge_max

        if not raw_malloc_usage(itemsize):
            too_many_items = raw_malloc_usage(nonvarsize) > nonlarge_max
        else:
            maxlength = nonlarge_max - raw_malloc_usage(nonvarsize)
            maxlength = maxlength // raw_malloc_usage(itemsize)
            too_many_items = length > maxlength

        if too_many_items:
            #
            # If the total size of the object would be larger than
            # 'nonlarge_max', then allocate it externally.
            obj = self.external_malloc(typeid, length)
            #
        else:
            # With the above checks we know now that totalsize cannot be more
            # than 'nonlarge_max'; in particular, the + and * cannot overflow.
            totalsize = nonvarsize + itemsize * length
            totalsize = llarena.round_up_for_allocation(totalsize)
            #
            # 'totalsize' should contain at least the GC header and
            # the length word, so it should never be smaller than
            # 'minimal_size_in_nursery'
            ll_assert(raw_malloc_usage(totalsize) >=
                      raw_malloc_usage(self.minimal_size_in_nursery),
                      "malloc_varsize_clear(): totalsize < minimalsize")
            #
            # Get the memory from the nursery.  If there is not enough space
            # there, do a collect first.
            result = self.nursery_free
            self.nursery_free = result + totalsize
            if self.nursery_free > self.nursery_top:
                result = self.collect_and_reserve(totalsize)
            #
            # Build the object.
            llarena.arena_reserve(result, totalsize)
            self.init_gc_object(result, typeid, flags=0)
            #
            # Set the length and return the object.
            obj = result + size_gc_header
            (obj + offset_to_length).signed[0] = length
        #
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)


    def collect(self, gen=1):
        """Do a minor (gen=0) or major (gen>0) collection."""
        self.minor_collection()
        if gen > 0:
            self.major_collection()

    def collect_and_reserve(self, totalsize):
        """To call when nursery_free overflows nursery_top.
        Do a minor collection, and possibly also a major collection,
        and finally reserve 'totalsize' bytes at the start of the
        now-empty nursery.
        """
        self.minor_collection()
        #
        if self.get_total_memory_used() > self.next_major_collection_threshold:
            self.major_collection()
            #
            # The nursery might not be empty now, because of
            # execute_finalizers().  If it is almost full again,
            # we need to fix it with another call to minor_collection().
            if self.nursery_free + totalsize > self.nursery_top:
                self.minor_collection()
        #
        result = self.nursery_free
        self.nursery_free = result + totalsize
        ll_assert(self.nursery_free <= self.nursery_top, "nursery overflow")
        #
        if self.debug_always_do_minor_collect:
            self.nursery_free = self.nursery_top
        #
        return result
    collect_and_reserve._dont_inline_ = True


    def external_malloc(self, typeid, length):
        """Allocate a large object using the ArenaCollection or
        raw_malloc(), possibly as an object with card marking enabled,
        if it has gc pointers in its var-sized part.  'length' should be
        specified as 0 if the object is not varsized.  The returned
        object is fully initialized and zero-filled."""
        #
        # Compute the total size, carefully checking for overflows.
        size_gc_header = self.gcheaderbuilder.size_gc_header
        nonvarsize = size_gc_header + self.fixed_size(typeid)
        if length == 0:
            # this includes the case of fixed-size objects, for which we
            # should not even ask for the varsize_item_sizes().
            totalsize = nonvarsize
        else:
            itemsize = self.varsize_item_sizes(typeid)
            try:
                varsize = ovfcheck(itemsize * length)
                totalsize = ovfcheck(nonvarsize + varsize)
            except OverflowError:
                raise MemoryError
        #
        # If somebody calls this function a lot, we must eventually
        # force a full collection.
        if (float(self.get_total_memory_used()) + raw_malloc_usage(totalsize) >
                self.next_major_collection_threshold):
            self.minor_collection()
            self.major_collection(raw_malloc_usage(totalsize))
        #
        # Check if the object would fit in the ArenaCollection.
        if raw_malloc_usage(totalsize) <= self.small_request_threshold:
            #
            # Yes.  Round up 'totalsize' (it cannot overflow and it
            # must remain <= self.small_request_threshold.)
            totalsize = llarena.round_up_for_allocation(totalsize)
            ll_assert(raw_malloc_usage(totalsize) <=
                      self.small_request_threshold,
                      "rounding up made totalsize > small_request_threshold")
            #
            # Allocate from the ArenaCollection and clear the memory returned.
            result = self.ac.malloc(totalsize)
            llmemory.raw_memclear(result, totalsize)
            extra_flags = 0
            #
        else:
            # No, so proceed to allocate it externally with raw_malloc().
            # Check if we need to introduce the card marker bits area.
            if (self.card_page_indices <= 0  # <- this check is constant-folded
                or not self.has_gcptr_in_varsize(typeid) or
                raw_malloc_usage(totalsize) <= self.nonlarge_gcptrs_max):
                #
                # In these cases, we don't want a card marker bits area.
                # This case also includes all fixed-size objects.
                cardheadersize = 0
                extra_flags = 0
                #
            else:
                # Reserve N extra words containing card bits before the object.
                extra_words = self.card_marking_words_for_length(length)
                cardheadersize = WORD * extra_words
                extra_flags = GCFLAG_HAS_CARDS
            #
            # Detect very rare cases of overflows
            if raw_malloc_usage(totalsize) > (sys.maxint - (WORD-1)
                                              - cardheadersize):
                raise MemoryError("rare case of overflow")
            #
            # Now we know that the following computations cannot overflow.
            # Note that round_up_for_allocation() is also needed to get the
            # correct number added to 'rawmalloced_total_size'.
            allocsize = (cardheadersize + raw_malloc_usage(
                            llarena.round_up_for_allocation(totalsize)))
            #
            # Allocate the object using arena_malloc(), which we assume here
            # is just the same as raw_malloc(), but allows the extra
            # flexibility of saying that we have extra words in the header.
            # The memory returned is cleared by a raw_memclear().
            arena = llarena.arena_malloc(allocsize, 2)
            if not arena:
                raise MemoryError("cannot allocate large object")
            #
            # Reserve the card mark bits as a list of single bytes
            # (the loop is empty in C).
            i = 0
            while i < cardheadersize:
                llarena.arena_reserve(arena + i, llmemory.sizeof(lltype.Char))
                i += 1
            #
            # Reserve the actual object.  (This is also a no-op in C).
            result = arena + cardheadersize
            llarena.arena_reserve(result, totalsize)
            #
            # Record the newly allocated object and its full malloced size.
            self.rawmalloced_total_size += allocsize
            self.rawmalloced_objects.append(result + size_gc_header)
        #
        # Common code to fill the header and length of the object.
        self.init_gc_object(result, typeid, GCFLAG_NO_YOUNG_PTRS | extra_flags)
        if self.is_varsize(typeid):
            offset_to_length = self.varsize_offset_to_length(typeid)
            (result + size_gc_header + offset_to_length).signed[0] = length
        return result + size_gc_header


    # ----------
    # Other functions in the GC API

    def set_max_heap_size(self, size):
        self.max_heap_size = float(size)
        if self.max_heap_size > 0.0:
            if self.max_heap_size < self.next_major_collection_threshold:
                self.next_major_collection_threshold = self.max_heap_size

    def can_malloc_nonmovable(self):
        return True

    def can_optimize_clean_setarrayitems(self):
        if self.card_page_indices > 0:
            return False
        return MovingGCBase.can_optimize_clean_setarrayitems(self)

    def can_move(self, obj):
        """Overrides the parent can_move()."""
        return self.is_in_nursery(obj)


    def shrink_array(self, obj, smallerlength):
        #
        # Only objects in the nursery can be "resized".  Resizing them
        # means recording that they have a smaller size, so that when
        # moved out of the nursery, they will consume less memory.
        # In particular, an array with GCFLAG_HAS_CARDS is never resized.
        if not self.is_in_nursery(obj):
            return False
        #
        size_gc_header = self.gcheaderbuilder.size_gc_header
        typeid = self.get_type_id(obj)
        totalsmallersize = (
            size_gc_header + self.fixed_size(typeid) +
            self.varsize_item_sizes(typeid) * smallerlength)
        llarena.arena_shrink_obj(obj - size_gc_header, totalsmallersize)
        #
        offset_to_length = self.varsize_offset_to_length(typeid)
        (obj + offset_to_length).signed[0] = smallerlength
        return True


    def malloc_fixedsize_nonmovable(self, typeid):
        obj = self.external_malloc(typeid, 0)
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)

    def malloc_varsize_nonmovable(self, typeid, length):
        obj = self.external_malloc(typeid, length)
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)

    def malloc_nonmovable(self, typeid, length, zero):
        # helper for testing, same as GCBase.malloc
        return self.external_malloc(typeid, length or 0)    # None -> 0


    # ----------
    # Simple helpers

    def get_type_id(self, obj):
        tid = self.header(obj).tid
        return llop.extract_ushort(llgroup.HALFWORD, tid)

    def combine(self, typeid16, flags):
        return llop.combine_ushort(lltype.Signed, typeid16, flags)

    def init_gc_object(self, addr, typeid16, flags=0):
        # The default 'flags' is zero.  The flags GCFLAG_NO_xxx_PTRS
        # have been chosen to allow 'flags' to be zero in the common
        # case (hence the 'NO' in their name).
        hdr = llmemory.cast_adr_to_ptr(addr, lltype.Ptr(self.HDR))
        hdr.tid = self.combine(typeid16, flags)

    def init_gc_object_immortal(self, addr, typeid16, flags=0):
        # For prebuilt GC objects, the flags must contain
        # GCFLAG_NO_xxx_PTRS, at least initially.
        flags |= GCFLAG_NO_HEAP_PTRS | GCFLAG_NO_YOUNG_PTRS
        self.init_gc_object(addr, typeid16, flags)

    def is_in_nursery(self, addr):
        ll_assert(llmemory.cast_adr_to_int(addr) & 1 == 0,
                  "odd-valued (i.e. tagged) pointer unexpected here")
        return self.nursery <= addr < self.nursery_top

    def appears_to_be_in_nursery(self, addr):
        # same as is_in_nursery(), but may return True accidentally if
        # 'addr' is a tagged pointer with just the wrong value.
        if not self.translated_to_c:
            if not self.is_valid_gc_object(addr):
                return False
        return self.nursery <= addr < self.nursery_top

    def is_forwarded(self, obj):
        """Returns True if the nursery obj is marked as forwarded.
        Implemented a bit obscurely by checking an unrelated flag
        that can never be set on a young object -- except if tid == -42.
        """
        assert self.is_in_nursery(obj)
        result = (self.header(obj).tid & GCFLAG_FINALIZATION_ORDERING != 0)
        if result:
            ll_assert(self.header(obj).tid == -42, "bogus header for young obj")
        return result

    def get_forwarding_address(self, obj):
        return llmemory.cast_adr_to_ptr(obj, FORWARDSTUBPTR).forw

    def get_total_memory_used(self):
        """Return the total memory used, not counting any object in the
        nursery: only objects in the ArenaCollection or raw-malloced.
        """
        return self.ac.total_memory_used + self.rawmalloced_total_size

    def card_marking_words_for_length(self, length):
        # --- Unoptimized version:
        #num_bits = ((length-1) >> self.card_page_shift) + 1
        #return (num_bits + (LONG_BIT - 1)) >> LONG_BIT_SHIFT
        # --- Optimized version:
        return intmask(
            ((r_uint(length) + ((LONG_BIT << self.card_page_shift) - 1)) >>
             (self.card_page_shift + LONG_BIT_SHIFT)))

    def card_marking_bytes_for_length(self, length):
        # --- Unoptimized version:
        #num_bits = ((length-1) >> self.card_page_shift) + 1
        #return (num_bits + 7) >> 3
        # --- Optimized version:
        return intmask(
            ((r_uint(length) + ((8 << self.card_page_shift) - 1)) >>
             (self.card_page_shift + 3)))

    def debug_check_object(self, obj):
        # after a minor or major collection, no object should be in the nursery
        ll_assert(not self.is_in_nursery(obj),
                  "object in nursery after collection")
        # similarily, all objects should have this flag:
        ll_assert(self.header(obj).tid & GCFLAG_NO_YOUNG_PTRS,
                  "missing GCFLAG_NO_YOUNG_PTRS")
        # if we have GCFLAG_NO_HEAP_PTRS, then we have GCFLAG_NO_YOUNG_PTRS
        if self.header(obj).tid & GCFLAG_NO_HEAP_PTRS:
            ll_assert(self.header(obj).tid & GCFLAG_NO_YOUNG_PTRS,
                      "GCFLAG_NO_HEAP_PTRS && !GCFLAG_NO_YOUNG_PTRS")
        # the GCFLAG_VISITED should not be set between collections
        ll_assert(self.header(obj).tid & GCFLAG_VISITED == 0,
                  "unexpected GCFLAG_VISITED")
        # the GCFLAG_FINALIZATION_ORDERING should not be set between coll.
        ll_assert(self.header(obj).tid & GCFLAG_FINALIZATION_ORDERING == 0,
                  "unexpected GCFLAG_FINALIZATION_ORDERING")
        # the GCFLAG_CARDS_SET should not be set between collections
        ll_assert(self.header(obj).tid & GCFLAG_CARDS_SET == 0,
                  "unexpected GCFLAG_CARDS_SET")
        # if the GCFLAG_HAS_CARDS is set, check that all bits are zero now
        if self.header(obj).tid & GCFLAG_HAS_CARDS:
            if self.card_page_indices <= 0:
                ll_assert(False, "GCFLAG_HAS_CARDS but not using card marking")
                return
            typeid = self.get_type_id(obj)
            ll_assert(self.has_gcptr_in_varsize(typeid),
                      "GCFLAG_HAS_CARDS but not has_gcptr_in_varsize")
            ll_assert(self.header(obj).tid & GCFLAG_NO_HEAP_PTRS == 0,
                      "GCFLAG_HAS_CARDS && GCFLAG_NO_HEAP_PTRS")
            offset_to_length = self.varsize_offset_to_length(typeid)
            length = (obj + offset_to_length).signed[0]
            extra_words = self.card_marking_words_for_length(length)
            #
            size_gc_header = self.gcheaderbuilder.size_gc_header
            p = llarena.getfakearenaaddress(obj - size_gc_header)
            i = extra_words * WORD
            while i > 0:
                p -= 1
                ll_assert(p.char[0] == '\x00',
                          "the card marker bits are not cleared")
                i -= 1

    # ----------
    # Write barrier

    # for the JIT: a minimal description of the write_barrier() method
    # (the JIT assumes it is of the shape
    #  "if addr_struct.int0 & JIT_WB_IF_FLAG: remember_young_pointer()")
    JIT_WB_IF_FLAG = GCFLAG_NO_YOUNG_PTRS

    @classmethod
    def JIT_max_size_of_young_obj(cls):
        return cls.TRANSLATION_PARAMS['large_object']

    @classmethod
    def JIT_minimal_size_in_nursery(cls):
        return cls.minimal_size_in_nursery

    def write_barrier(self, newvalue, addr_struct):
        if self.header(addr_struct).tid & GCFLAG_NO_YOUNG_PTRS:
            self.remember_young_pointer(addr_struct, newvalue)

    def write_barrier_from_array(self, newvalue, addr_array, index):
        if self.header(addr_array).tid & GCFLAG_NO_YOUNG_PTRS:
            if self.card_page_indices > 0:     # <- constant-folded
                self.remember_young_pointer_from_array(addr_array, index)
            else:
                self.remember_young_pointer(addr_array, newvalue)

    def _init_writebarrier_logic(self):
        DEBUG = self.DEBUG
        # The purpose of attaching remember_young_pointer to the instance
        # instead of keeping it as a regular method is to help the JIT call it.
        # Additionally, it makes the code in write_barrier() marginally smaller
        # (which is important because it is inlined *everywhere*).
        # For x86, there is also an extra requirement: when the JIT calls
        # remember_young_pointer(), it assumes that it will not touch the SSE
        # registers, so it does not save and restore them (that's a *hack*!).
        def remember_young_pointer(addr_struct, newvalue):
            # 'addr_struct' is the address of the object in which we write.
            # 'newvalue' is the address that we are going to write in there.
            if DEBUG:
                ll_assert(not self.is_in_nursery(addr_struct),
                          "nursery object with GCFLAG_NO_YOUNG_PTRS")
            #
            # If it seems that what we are writing is a pointer to the nursery
            # (as checked with appears_to_be_in_nursery()), then we need
            # to remove the flag GCFLAG_NO_YOUNG_PTRS and add the old object
            # to the list 'old_objects_pointing_to_young'.  We know that
            # 'addr_struct' cannot be in the nursery, because nursery objects
            # never have the flag GCFLAG_NO_YOUNG_PTRS to start with.
            objhdr = self.header(addr_struct)
            if self.appears_to_be_in_nursery(newvalue):
                self.old_objects_pointing_to_young.append(addr_struct)
                objhdr.tid &= ~GCFLAG_NO_YOUNG_PTRS
            #
            # Second part: if 'addr_struct' is actually a prebuilt GC
            # object and it's the first time we see a write to it, we
            # add it to the list 'prebuilt_root_objects'.  Note that we
            # do it even in the (rare?) case of 'addr' being NULL or another
            # prebuilt object, to simplify code.
            if objhdr.tid & GCFLAG_NO_HEAP_PTRS:
                objhdr.tid &= ~GCFLAG_NO_HEAP_PTRS
                self.prebuilt_root_objects.append(addr_struct)

        remember_young_pointer._dont_inline_ = True
        self.remember_young_pointer = remember_young_pointer
        #
        if self.card_page_indices > 0:
            self._init_writebarrier_with_card_marker()


    def _init_writebarrier_with_card_marker(self):
        DEBUG = self.DEBUG
        def remember_young_pointer_from_array(addr_array, index):
            # 'addr_array' is the address of the object in which we write,
            # which must have an array part;  'index' is the index of the
            # item that is (or contains) the pointer that we write.
            if DEBUG:
                ll_assert(not self.is_in_nursery(addr_array),
                          "nursery array with GCFLAG_NO_YOUNG_PTRS")
            objhdr = self.header(addr_array)
            if objhdr.tid & GCFLAG_HAS_CARDS == 0:
                #
                # no cards, use default logic.  Mostly copied from above.
                self.old_objects_pointing_to_young.append(addr_array)
                objhdr = self.header(addr_array)
                objhdr.tid &= ~GCFLAG_NO_YOUNG_PTRS
                if objhdr.tid & GCFLAG_NO_HEAP_PTRS:
                    objhdr.tid &= ~GCFLAG_NO_HEAP_PTRS
                    self.prebuilt_root_objects.append(addr_array)
                return
            #
            # 'addr_array' is a raw_malloc'ed array with card markers
            # in front.  Compute the index of the bit to set:
            bitindex = index >> self.card_page_shift
            byteindex = bitindex >> 3
            bitmask = 1 << (bitindex & 7)
            #
            # If the bit is already set, leave now.
            size_gc_header = self.gcheaderbuilder.size_gc_header
            addr_byte = addr_array - size_gc_header
            addr_byte = llarena.getfakearenaaddress(addr_byte) + (~byteindex)
            byte = ord(addr_byte.char[0])
            if byte & bitmask:
                return
            #
            # We set the flag (even if the newly written address does not
            # actually point to the nursery, which seems to be ok -- actually
            # it seems more important that remember_young_pointer_from_array()
            # does not take 3 arguments).
            addr_byte.char[0] = chr(byte | bitmask)
            #
            if objhdr.tid & GCFLAG_CARDS_SET == 0:
                self.old_objects_with_cards_set.append(addr_array)
                objhdr.tid |= GCFLAG_CARDS_SET

        remember_young_pointer_from_array._dont_inline_ = True
        self.remember_young_pointer_from_array = (
            remember_young_pointer_from_array)


    def assume_young_pointers(self, addr_struct):
        """Called occasionally by the JIT to mean ``assume that 'addr_struct'
        may now contain young pointers.''
        """
        objhdr = self.header(addr_struct)
        if objhdr.tid & GCFLAG_NO_YOUNG_PTRS:
            self.old_objects_pointing_to_young.append(addr_struct)
            objhdr.tid &= ~GCFLAG_NO_YOUNG_PTRS
            #
            if objhdr.tid & GCFLAG_NO_HEAP_PTRS:
                objhdr.tid &= ~GCFLAG_NO_HEAP_PTRS
                self.prebuilt_root_objects.append(addr_struct)

    def writebarrier_before_copy(self, source_addr, dest_addr):
        """ This has the same effect as calling writebarrier over
        each element in dest copied from source, except it might reset
        one of the following flags a bit too eagerly, which means we'll have
        a bit more objects to track, but being on the safe side.
        """
        source_hdr = self.header(source_addr)
        dest_hdr = self.header(dest_addr)
        if dest_hdr.tid & GCFLAG_NO_YOUNG_PTRS == 0:
            return True
        # ^^^ a fast path of write-barrier
        #
        if (source_hdr.tid & GCFLAG_NO_YOUNG_PTRS == 0 or
            source_hdr.tid & GCFLAG_CARDS_SET != 0):
            # there might be an object in source that is in nursery
            self.old_objects_pointing_to_young.append(dest_addr)
            dest_hdr.tid &= ~GCFLAG_NO_YOUNG_PTRS
        #
        if dest_hdr.tid & GCFLAG_NO_HEAP_PTRS:
            if source_hdr.tid & GCFLAG_NO_HEAP_PTRS == 0:
                dest_hdr.tid &= ~GCFLAG_NO_HEAP_PTRS
                self.prebuilt_root_objects.append(dest_addr)
        return True


    # ----------
    # Nursery collection

    def minor_collection(self):
        """Perform a minor collection: find the objects from the nursery
        that remain alive and move them out."""
        #
        debug_start("gc-minor")
        #
        # First, find the roots that point to nursery objects.  These
        # nursery objects are copied out of the nursery.  Note that
        # references to further nursery objects are not modified by
        # this step; only objects directly referenced by roots are
        # copied out.  They are also added to the list
        # 'old_objects_pointing_to_young'.
        self.collect_roots_in_nursery()
        #
        # If we are using card marking, do a partial trace of the arrays
        # that are flagged with GCFLAG_CARDS_SET.
        if self.card_page_indices > 0:
            self.collect_cardrefs_to_nursery()
        #
        # Now trace objects from 'old_objects_pointing_to_young'.
        # All nursery objects they reference are copied out of the
        # nursery, and again added to 'old_objects_pointing_to_young'.
        # We proceed until 'old_objects_pointing_to_young' is empty.
        self.collect_oldrefs_to_nursery()
        #
        # Now all live nursery objects should be out.  Update the
        # young weakrefs' targets.
        if self.young_objects_with_weakrefs.non_empty():
            self.invalidate_young_weakrefs()
        #
        # Clear this mapping.
        if self.young_objects_shadows.length() > 0:
            self.young_objects_shadows.clear()
        #
        # All live nursery objects are out, and the rest dies.  Fill
        # the whole nursery with zero and reset the current nursery pointer.
        llarena.arena_reset(self.nursery, self.nursery_size, 2)
        self.nursery_free = self.nursery
        #
        debug_print("minor collect, total memory used:",
                    self.get_total_memory_used())
        debug_stop("gc-minor")
        if 0:  # not we_are_translated():
            self.debug_check_consistency()     # xxx expensive!


    def collect_roots_in_nursery(self):
        # we don't need to trace prebuilt GcStructs during a minor collect:
        # if a prebuilt GcStruct contains a pointer to a young object,
        # then the write_barrier must have ensured that the prebuilt
        # GcStruct is in the list self.old_objects_pointing_to_young.
        self.root_walker.walk_roots(
            MiniMarkGC._trace_drag_out1,  # stack roots
            MiniMarkGC._trace_drag_out1,  # static in prebuilt non-gc
            None)                         # static in prebuilt gc

    def collect_cardrefs_to_nursery(self):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        oldlist = self.old_objects_with_cards_set
        while oldlist.non_empty():
            obj = oldlist.pop()
            #
            # Remove the GCFLAG_CARDS_SET flag.
            ll_assert(self.header(obj).tid & GCFLAG_CARDS_SET != 0,
                "!GCFLAG_CARDS_SET but object in 'old_objects_with_cards_set'")
            self.header(obj).tid &= ~GCFLAG_CARDS_SET
            #
            # Get the number of card marker bytes in the header.
            typeid = self.get_type_id(obj)
            offset_to_length = self.varsize_offset_to_length(typeid)
            length = (obj + offset_to_length).signed[0]
            bytes = self.card_marking_bytes_for_length(length)
            p = llarena.getfakearenaaddress(obj - size_gc_header)
            #
            # If the object doesn't have GCFLAG_NO_YOUNG_PTRS, then it
            # means that it is in 'old_objects_pointing_to_young' and
            # will be fully traced by collect_oldrefs_to_nursery() just
            # afterwards.
            if self.header(obj).tid & GCFLAG_NO_YOUNG_PTRS == 0:
                #
                # In that case, we just have to reset all card bits.
                while bytes > 0:
                    p -= 1
                    p.char[0] = '\x00'
                    bytes -= 1
                #
            else:
                # Walk the bytes encoding the card marker bits, and for
                # each bit set, call trace_and_drag_out_of_nursery_partial().
                interval_start = 0
                while bytes > 0:
                    p -= 1
                    cardbyte = ord(p.char[0])
                    p.char[0] = '\x00'           # reset the bits
                    bytes -= 1
                    next_byte_start = interval_start + 8*self.card_page_indices
                    #
                    while cardbyte != 0:
                        interval_stop = interval_start + self.card_page_indices
                        #
                        if cardbyte & 1:
                            if interval_stop > length:
                                interval_stop = length
                                ll_assert(cardbyte <= 1 and bytes == 0,
                                          "premature end of object")
                            self.trace_and_drag_out_of_nursery_partial(
                                obj, interval_start, interval_stop)
                        #
                        interval_start = interval_stop
                        cardbyte >>= 1
                    interval_start = next_byte_start


    def collect_oldrefs_to_nursery(self):
        # Follow the old_objects_pointing_to_young list and move the
        # young objects they point to out of the nursery.
        oldlist = self.old_objects_pointing_to_young
        while oldlist.non_empty():
            obj = oldlist.pop()
            #
            # Add the flag GCFLAG_NO_YOUNG_PTRS.  All live objects should have
            # this flag set after a nursery collection.
            self.header(obj).tid |= GCFLAG_NO_YOUNG_PTRS
            #
            # Trace the 'obj' to replace pointers to nursery with pointers
            # outside the nursery, possibly forcing nursery objects out
            # and adding them to 'old_objects_pointing_to_young' as well.
            self.trace_and_drag_out_of_nursery(obj)

    def trace_and_drag_out_of_nursery(self, obj):
        """obj must not be in the nursery.  This copies all the
        young objects it references out of the nursery.
        """
        self.trace(obj, self._trace_drag_out, None)

    def trace_and_drag_out_of_nursery_partial(self, obj, start, stop):
        """Like trace_and_drag_out_of_nursery(), but limited to the array
        indices in range(start, stop).
        """
        ll_assert(start < stop, "empty or negative range "
                                "in trace_and_drag_out_of_nursery_partial()")
        #print 'trace_partial:', start, stop, '\t', obj
        self.trace_partial(obj, start, stop, self._trace_drag_out, None)


    def _trace_drag_out1(self, root):
        self._trace_drag_out(root, None)

    def _trace_drag_out(self, root, ignored):
        obj = root.address[0]
        #
        # If 'obj' is not in the nursery, nothing to change.
        if not self.is_in_nursery(obj):
            return
        #
        # If 'obj' was already forwarded, change it to its forwarding address.
        if self.is_forwarded(obj):
            root.address[0] = self.get_forwarding_address(obj)
            return
        #
        # First visit to 'obj': we must move it out of the nursery.
        size_gc_header = self.gcheaderbuilder.size_gc_header
        size = self.get_size(obj)
        totalsize = size_gc_header + size
        #
        if self.header(obj).tid & GCFLAG_HAS_SHADOW == 0:
            #
            # Common case: allocate a new nonmovable location for it.
            newhdr = self._malloc_out_of_nursery(totalsize)
            #
        else:
            # The object has already a shadow.
            newobj = self.young_objects_shadows.get(obj)
            ll_assert(newobj != NULL, "GCFLAG_HAS_SHADOW but no shadow found")
            newhdr = newobj - size_gc_header
            #
            # Remove the flag GCFLAG_HAS_SHADOW, so that it doesn't get
            # copied to the shadow itself.
            self.header(obj).tid &= ~GCFLAG_HAS_SHADOW
        #
        # Copy it.  Note that references to other objects in the
        # nursery are kept unchanged in this step.
        llmemory.raw_memcopy(obj - size_gc_header, newhdr, totalsize)
        #
        # Set the old object's tid to -42 (containing all flags) and
        # replace the old object's content with the target address.
        # A bit of no-ops to convince llarena that we are changing
        # the layout, in non-translated versions.
        obj = llarena.getfakearenaaddress(obj)
        llarena.arena_reset(obj - size_gc_header, totalsize, 0)
        llarena.arena_reserve(obj - size_gc_header,
                              size_gc_header + llmemory.sizeof(FORWARDSTUB))
        self.header(obj).tid = -42
        newobj = newhdr + size_gc_header
        llmemory.cast_adr_to_ptr(obj, FORWARDSTUBPTR).forw = newobj
        #
        # Change the original pointer to this object.
        root.address[0] = newobj
        #
        # Add the newobj to the list 'old_objects_pointing_to_young',
        # because it can contain further pointers to other young objects.
        # We will fix such references to point to the copy of the young
        # objects when we walk 'old_objects_pointing_to_young'.
        self.old_objects_pointing_to_young.append(newobj)


    def _malloc_out_of_nursery(self, totalsize):
        """Allocate non-movable memory for an object of the given
        'totalsize' that lives so far in the nursery."""
        if raw_malloc_usage(totalsize) <= self.small_request_threshold:
            # most common path
            return self.ac.malloc(totalsize)
        else:
            # for nursery objects that are not small
            return self._malloc_out_of_nursery_nonsmall(totalsize)
    _malloc_out_of_nursery._always_inline_ = True

    def _malloc_out_of_nursery_nonsmall(self, totalsize):
        # 'totalsize' should be aligned.
        ll_assert(raw_malloc_usage(totalsize) & (WORD-1) == 0,
                  "misaligned totalsize in _malloc_out_of_nursery_nonsmall")
        #
        arena = llarena.arena_malloc(raw_malloc_usage(totalsize), False)
        if not arena:
            raise MemoryError("cannot allocate object")
        llarena.arena_reserve(arena, totalsize)
        #
        size_gc_header = self.gcheaderbuilder.size_gc_header
        self.rawmalloced_total_size += raw_malloc_usage(totalsize)
        self.rawmalloced_objects.append(arena + size_gc_header)
        return arena


    # ----------
    # Full collection

    def major_collection(self, reserving_size=0):
        """Do a major collection.  Only for when the nursery is empty."""
        #
        debug_start("gc-collect")
        debug_print()
        debug_print(".----------- Full collection ------------------")
        debug_print("| used before collection:")
        debug_print("|          in ArenaCollection:     ",
                    self.ac.total_memory_used, "bytes")
        debug_print("|          raw_malloced:           ",
                    self.rawmalloced_total_size, "bytes")
        #
        # Debugging checks
        ll_assert(self.nursery_free == self.nursery,
                  "nursery not empty in major_collection()")
        self.debug_check_consistency()
        #
        # Note that a major collection is non-moving.  The goal is only to
        # find and free some of the objects allocated by the ArenaCollection.
        # We first visit all objects and toggle the flag GCFLAG_VISITED on
        # them, starting from the roots.
        self.objects_to_trace = self.AddressStack()
        self.collect_roots()
        self.visit_all_objects()
        #
        # Weakref support: clear the weak pointers to dying objects
        if self.old_objects_with_weakrefs.non_empty():
            self.invalidate_old_weakrefs()
        #
        # Finalizer support: adds the flag GCFLAG_VISITED to all objects
        # with a finalizer and all objects reachable from there (and also
        # moves some objects from 'objects_with_finalizers' to
        # 'run_finalizers').
        if self.objects_with_finalizers.non_empty():
            self.deal_with_objects_with_finalizers()
        #
        self.objects_to_trace.delete()
        #
        # Walk all rawmalloced objects and free the ones that don't
        # have the GCFLAG_VISITED flag.
        self.free_unvisited_rawmalloc_objects()
        #
        # Ask the ArenaCollection to visit all objects.  Free the ones
        # that have not been visited above, and reset GCFLAG_VISITED on
        # the others.
        self.ac.mass_free(self._free_if_unvisited)
        #
        # We also need to reset the GCFLAG_VISITED on prebuilt GC objects.
        self.prebuilt_root_objects.foreach(self._reset_gcflag_visited, None)
        #
        self.debug_check_consistency()
        #
        self.num_major_collects += 1
        debug_print("| used after collection:")
        debug_print("|          in ArenaCollection:     ",
                    self.ac.total_memory_used, "bytes")
        debug_print("|          raw_malloced:           ",
                    self.rawmalloced_total_size, "bytes")
        debug_print("| number of major collects:        ",
                    self.num_major_collects)
        debug_print("`----------------------------------------------")
        debug_stop("gc-collect")
        #
        # Set the threshold for the next major collection to be when we
        # have allocated 'major_collection_threshold' times more than
        # we currently have.
        bounded = self.set_major_threshold_from(
            self.get_total_memory_used() * self.major_collection_threshold,
            reserving_size)
        #
        # Max heap size: gives an upper bound on the threshold.  If we
        # already have at least this much allocated, raise MemoryError.
        if bounded and (float(self.get_total_memory_used()) + reserving_size >=
                        self.next_major_collection_threshold):
            #
            # First raise MemoryError, giving the program a chance to
            # quit cleanly.  It might still allocate in the nursery,
            # which might eventually be emptied, triggering another
            # major collect and (possibly) reaching here again with an
            # even higher memory consumption.  To prevent it, if it's
            # the second time we are here, then abort the program.
            if self.max_heap_size_already_raised:
                llop.debug_fatalerror(lltype.Void,
                                      "Using too much memory, aborting")
            self.max_heap_size_already_raised = True
            raise MemoryError
        #
        # At the end, we can execute the finalizers of the objects
        # listed in 'run_finalizers'.  Note that this will typically do
        # more allocations.
        self.execute_finalizers()


    def _free_if_unvisited(self, hdr):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        obj = hdr + size_gc_header
        if self.header(obj).tid & GCFLAG_VISITED:
            self.header(obj).tid &= ~GCFLAG_VISITED
            return False     # survives
        else:
            return True      # dies

    def _reset_gcflag_visited(self, obj, ignored):
        self.header(obj).tid &= ~GCFLAG_VISITED

    def free_unvisited_rawmalloc_objects(self):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        list = self.rawmalloced_objects
        self.rawmalloced_objects = self.AddressStack()
        #
        while list.non_empty():
            obj = list.pop()
            if self.header(obj).tid & GCFLAG_VISITED:
                self.header(obj).tid &= ~GCFLAG_VISITED   # survives
                self.rawmalloced_objects.append(obj)
            else:
                totalsize = size_gc_header + self.get_size(obj)
                allocsize = raw_malloc_usage(totalsize)
                arena = llarena.getfakearenaaddress(obj - size_gc_header)
                #
                # Must also include the card marker area, if any
                if (self.card_page_indices > 0    # <- this is constant-folded
                    and self.header(obj).tid & GCFLAG_HAS_CARDS):
                    #
                    # Get the length and compute the number of extra bytes
                    typeid = self.get_type_id(obj)
                    ll_assert(self.has_gcptr_in_varsize(typeid),
                              "GCFLAG_HAS_CARDS but not has_gcptr_in_varsize")
                    offset_to_length = self.varsize_offset_to_length(typeid)
                    length = (obj + offset_to_length).signed[0]
                    extra_words = self.card_marking_words_for_length(length)
                    arena -= extra_words * WORD
                    allocsize += extra_words * WORD
                #
                llarena.arena_free(arena)
                self.rawmalloced_total_size -= allocsize
        #
        list.delete()


    def collect_roots(self):
        # Collect all roots.  Starts from all the objects
        # from 'prebuilt_root_objects'.
        self.prebuilt_root_objects.foreach(self._collect_obj,
                                           self.objects_to_trace)
        #
        # Add the roots from the other sources.
        self.root_walker.walk_roots(
            MiniMarkGC._collect_ref,  # stack roots
            MiniMarkGC._collect_ref,  # static in prebuilt non-gc structures
            None)   # we don't need the static in all prebuilt gc objects
        #
        # If we are in an inner collection caused by a call to a finalizer,
        # the 'run_finalizers' objects also need to kept alive.
        self.run_finalizers.foreach(self._collect_obj,
                                    self.objects_to_trace)

    def enumerate_all_roots(self, callback, arg):
        self.prebuilt_root_objects.foreach(callback, arg)
        MovingGCBase.enumerate_all_roots(self, callback, arg)
    enumerate_all_roots._annspecialcase_ = 'specialize:arg(1)'

    @staticmethod
    def _collect_obj(obj, objects_to_trace):
        objects_to_trace.append(obj)

    def _collect_ref(self, root):
        self.objects_to_trace.append(root.address[0])

    def _collect_ref_rec(self, root, ignored):
        self.objects_to_trace.append(root.address[0])

    def visit_all_objects(self):
        pending = self.objects_to_trace
        while pending.non_empty():
            obj = pending.pop()
            self.visit(obj)

    def visit(self, obj):
        #
        # 'obj' is a live object.  Check GCFLAG_VISITED to know if we
        # have already seen it before.
        #
        # Moreover, we can ignore prebuilt objects with GCFLAG_NO_HEAP_PTRS.
        # If they have this flag set, then they cannot point to heap
        # objects, so ignoring them is fine.  If they don't have this
        # flag set, then the object should be in 'prebuilt_root_objects',
        # and the GCFLAG_VISITED will be reset at the end of the
        # collection.
        hdr = self.header(obj)
        if hdr.tid & (GCFLAG_VISITED | GCFLAG_NO_HEAP_PTRS):
            return
        #
        # It's the first time.  We set the flag.
        hdr.tid |= GCFLAG_VISITED
        #
        # Trace the content of the object and put all objects it references
        # into the 'objects_to_trace' list.
        self.trace(obj, self._collect_ref_rec, None)


    # ----------
    # id() and identityhash() support

    def id_or_identityhash(self, gcobj, special_case_prebuilt):
        """Implement the common logic of id() and identityhash()
        of an object, given as a GCREF.
        """
        obj = llmemory.cast_ptr_to_adr(gcobj)
        #
        if self.is_valid_gc_object(obj):
            if self.is_in_nursery(obj):
                #
                # The object is not a tagged pointer, and it is still in the
                # nursery.  Find or allocate a "shadow" object, which is
                # where the object will be moved by the next minor
                # collection
                if self.header(obj).tid & GCFLAG_HAS_SHADOW:
                    shadow = self.young_objects_shadows.get(obj)
                    ll_assert(shadow != NULL,
                              "GCFLAG_HAS_SHADOW but no shadow found")
                else:
                    size_gc_header = self.gcheaderbuilder.size_gc_header
                    size = self.get_size(obj)
                    shadowhdr = self._malloc_out_of_nursery(size_gc_header +
                                                            size)
                    # initialize to an invalid tid *without* GCFLAG_VISITED,
                    # so that if the object dies before the next minor
                    # collection, the shadow will stay around but be collected
                    # by the next major collection.
                    shadow = shadowhdr + size_gc_header
                    self.header(shadow).tid = 0
                    self.header(obj).tid |= GCFLAG_HAS_SHADOW
                    self.young_objects_shadows.setitem(obj, shadow)
                #
                # The answer is the address of the shadow.
                obj = shadow
                #
            elif special_case_prebuilt:
                if self.header(obj).tid & GCFLAG_HAS_SHADOW:
                    #
                    # For identityhash(), we need a special case for some
                    # prebuilt objects: their hash must be the same before
                    # and after translation.  It is stored as an extra word
                    # after the object.  But we cannot use it for id()
                    # because the stored value might clash with a real one.
                    size = self.get_size(obj)
                    return (obj + size).signed[0]
        #
        return llmemory.cast_adr_to_int(obj)


    def id(self, gcobj):
        return self.id_or_identityhash(gcobj, False)

    def identityhash(self, gcobj):
        return self.id_or_identityhash(gcobj, True)


    # ----------
    # Finalizers

    def deal_with_objects_with_finalizers(self):
        # Walk over list of objects with finalizers.
        # If it is not surviving, add it to the list of to-be-called
        # finalizers and make it survive, to make the finalizer runnable.
        # We try to run the finalizers in a "reasonable" order, like
        # CPython does.  The details of this algorithm are in
        # pypy/doc/discussion/finalizer-order.txt.
        new_with_finalizer = self.AddressDeque()
        marked = self.AddressDeque()
        pending = self.AddressStack()
        self.tmpstack = self.AddressStack()
        while self.objects_with_finalizers.non_empty():
            x = self.objects_with_finalizers.popleft()
            ll_assert(self._finalization_state(x) != 1,
                      "bad finalization state 1")
            if self.header(x).tid & GCFLAG_VISITED:
                new_with_finalizer.append(x)
                continue
            marked.append(x)
            pending.append(x)
            while pending.non_empty():
                y = pending.pop()
                state = self._finalization_state(y)
                if state == 0:
                    self._bump_finalization_state_from_0_to_1(y)
                    self.trace(y, self._append_if_nonnull, pending)
                elif state == 2:
                    self._recursively_bump_finalization_state_from_2_to_3(y)
            self._recursively_bump_finalization_state_from_1_to_2(x)

        while marked.non_empty():
            x = marked.popleft()
            state = self._finalization_state(x)
            ll_assert(state >= 2, "unexpected finalization state < 2")
            if state == 2:
                self.run_finalizers.append(x)
                # we must also fix the state from 2 to 3 here, otherwise
                # we leave the GCFLAG_FINALIZATION_ORDERING bit behind
                # which will confuse the next collection
                self._recursively_bump_finalization_state_from_2_to_3(x)
            else:
                new_with_finalizer.append(x)

        self.tmpstack.delete()
        pending.delete()
        marked.delete()
        self.objects_with_finalizers.delete()
        self.objects_with_finalizers = new_with_finalizer

    def _append_if_nonnull(pointer, stack):
        stack.append(pointer.address[0])
    _append_if_nonnull = staticmethod(_append_if_nonnull)

    def _finalization_state(self, obj):
        tid = self.header(obj).tid
        if tid & GCFLAG_VISITED:
            if tid & GCFLAG_FINALIZATION_ORDERING:
                return 2
            else:
                return 3
        else:
            if tid & GCFLAG_FINALIZATION_ORDERING:
                return 1
            else:
                return 0

    def _bump_finalization_state_from_0_to_1(self, obj):
        ll_assert(self._finalization_state(obj) == 0,
                  "unexpected finalization state != 0")
        hdr = self.header(obj)
        hdr.tid |= GCFLAG_FINALIZATION_ORDERING

    def _recursively_bump_finalization_state_from_2_to_3(self, obj):
        ll_assert(self._finalization_state(obj) == 2,
                  "unexpected finalization state != 2")
        pending = self.tmpstack
        ll_assert(not pending.non_empty(), "tmpstack not empty")
        pending.append(obj)
        while pending.non_empty():
            y = pending.pop()
            hdr = self.header(y)
            if hdr.tid & GCFLAG_FINALIZATION_ORDERING:     # state 2 ?
                hdr.tid &= ~GCFLAG_FINALIZATION_ORDERING   # change to state 3
                self.trace(y, self._append_if_nonnull, pending)

    def _recursively_bump_finalization_state_from_1_to_2(self, obj):
        # recursively convert objects from state 1 to state 2.
        # The call to visit_all_objects() will add the GCFLAG_VISITED
        # recursively.
        self.objects_to_trace.append(obj)
        self.visit_all_objects()


    # ----------
    # Weakrefs

    # The code relies on the fact that no weakref can be an old object
    # weakly pointing to a young object.  Indeed, weakrefs are immutable
    # so they cannot point to an object that was created after it.
    def invalidate_young_weakrefs(self):
        """Called during a nursery collection."""
        # walk over the list of objects that contain weakrefs and are in the
        # nursery.  if the object it references survives then update the
        # weakref; otherwise invalidate the weakref
        while self.young_objects_with_weakrefs.non_empty():
            obj = self.young_objects_with_weakrefs.pop()
            if not self.is_forwarded(obj):
                continue # weakref itself dies
            obj = self.get_forwarding_address(obj)
            offset = self.weakpointer_offset(self.get_type_id(obj))
            pointing_to = (obj + offset).address[0]
            if self.is_in_nursery(pointing_to):
                if self.is_forwarded(pointing_to):
                    (obj + offset).address[0] = self.get_forwarding_address(
                        pointing_to)
                else:
                    (obj + offset).address[0] = llmemory.NULL
                    continue    # no need to remember this weakref any longer
            self.old_objects_with_weakrefs.append(obj)


    def invalidate_old_weakrefs(self):
        """Called during a major collection."""
        # walk over list of objects that contain weakrefs
        # if the object it references does not survive, invalidate the weakref
        new_with_weakref = self.AddressStack()
        while self.old_objects_with_weakrefs.non_empty():
            obj = self.old_objects_with_weakrefs.pop()
            if self.header(obj).tid & GCFLAG_VISITED == 0:
                continue # weakref itself dies
            offset = self.weakpointer_offset(self.get_type_id(obj))
            pointing_to = (obj + offset).address[0]
            if self.header(pointing_to).tid & GCFLAG_VISITED:
                new_with_weakref.append(obj)
            else:
                (obj + offset).address[0] = llmemory.NULL
        self.old_objects_with_weakrefs.delete()
        self.old_objects_with_weakrefs = new_with_weakref


# ____________________________________________________________

# For testing, a simple implementation of ArenaCollection.
# This version could be used together with obmalloc.c, but
# it requires an extra word per object in the 'all_objects'
# list.

class SimpleArenaCollection(object):

    def __init__(self, arena_size, page_size, small_request_threshold):
        self.arena_size = arena_size   # ignored
        self.page_size = page_size
        self.small_request_threshold = small_request_threshold
        self.all_objects = []
        self.total_memory_used = 0

    def malloc(self, size):
        nsize = raw_malloc_usage(size)
        ll_assert(nsize > 0, "malloc: size is null or negative")
        ll_assert(nsize <= self.small_request_threshold,"malloc: size too big")
        ll_assert((nsize & (WORD-1)) == 0, "malloc: size is not aligned")
        #
        result = llarena.arena_malloc(nsize, False)
        llarena.arena_reserve(result, size)
        self.all_objects.append((result, nsize))
        self.total_memory_used += nsize
        return result

    def mass_free(self, ok_to_free_func):
        objs = self.all_objects
        self.all_objects = []
        self.total_memory_used = 0
        for rawobj, nsize in objs:
            if ok_to_free_func(rawobj):
                llarena.arena_free(rawobj)
            else:
                self.all_objects.append((rawobj, nsize))
                self.total_memory_used += nsize
