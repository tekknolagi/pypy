from __future__ import with_statement
from pypy.interpreter.error import OperationError, operationerrfmt
from pypy.rpython.lltypesystem import lltype, llmemory, rffi
from pypy.rlib.rarithmetic import r_ulonglong
from pypy.rlib.unroll import unrolling_iterable
from pypy.rlib import jit

# ____________________________________________________________

_prim_signed_types = unrolling_iterable([
    (rffi.SIGNEDCHAR, rffi.SIGNEDCHARP),
    (rffi.SHORT, rffi.SHORTP),
    (rffi.INT, rffi.INTP),
    (rffi.LONG, rffi.LONGP),
    (rffi.LONGLONG, rffi.LONGLONGP)])

_prim_unsigned_types = unrolling_iterable([
    (rffi.UCHAR, rffi.UCHARP),
    (rffi.USHORT, rffi.USHORTP),
    (rffi.UINT, rffi.UINTP),
    (rffi.ULONG, rffi.ULONGP),
    (rffi.ULONGLONG, rffi.ULONGLONGP)])

_prim_float_types = unrolling_iterable([
    (rffi.FLOAT, rffi.FLOATP),
    (rffi.DOUBLE, rffi.DOUBLEP)])

def read_raw_signed_data(target, size):
    for TP, TPP in _prim_signed_types:
        if size == rffi.sizeof(TP):
            return rffi.cast(lltype.SignedLongLong, rffi.cast(TPP, target)[0])
    raise NotImplementedError("bad integer size")

def read_raw_long_data(target, size):
    for TP, TPP in _prim_signed_types:
        if size == rffi.sizeof(TP):
            assert rffi.sizeof(TP) <= rffi.sizeof(lltype.Signed)
            return rffi.cast(lltype.Signed, rffi.cast(TPP, target)[0])
    raise NotImplementedError("bad integer size")

def read_raw_unsigned_data(target, size):
    for TP, TPP in _prim_unsigned_types:
        if size == rffi.sizeof(TP):
            return rffi.cast(lltype.UnsignedLongLong, rffi.cast(TPP,target)[0])
    raise NotImplementedError("bad integer size")

def read_raw_ulong_data(target, size):
    for TP, TPP in _prim_unsigned_types:
        if size == rffi.sizeof(TP):
            assert rffi.sizeof(TP) < rffi.sizeof(lltype.Signed)
            return rffi.cast(lltype.Signed, rffi.cast(TPP,target)[0])
    raise NotImplementedError("bad integer size")

def read_raw_float_data(target, size):
    for TP, TPP in _prim_float_types:
        if size == rffi.sizeof(TP):
            return rffi.cast(lltype.Float, rffi.cast(TPP, target)[0])
    raise NotImplementedError("bad float size")

def read_raw_longdouble_data(target):
    return rffi.cast(rffi.LONGDOUBLEP, target)[0]

def write_raw_integer_data(target, source, size):
    for TP, TPP in _prim_unsigned_types:
        if size == rffi.sizeof(TP):
            rffi.cast(TPP, target)[0] = rffi.cast(TP, source)
            return
    raise NotImplementedError("bad integer size")

def write_raw_float_data(target, source, size):
    for TP, TPP in _prim_float_types:
        if size == rffi.sizeof(TP):
            rffi.cast(TPP, target)[0] = rffi.cast(TP, source)
            return
    raise NotImplementedError("bad float size")

def write_raw_longdouble_data(target, source):
    rffi.cast(rffi.LONGDOUBLEP, target)[0] = source

# ____________________________________________________________

sprintf_longdouble = rffi.llexternal(
    "sprintf", [rffi.CCHARP, rffi.CCHARP, rffi.LONGDOUBLE], lltype.Void,
    _nowrapper=True, sandboxsafe=True)

FORMAT_LONGDOUBLE = rffi.str2charp("%LE")

def longdouble2str(lvalue):
    with lltype.scoped_alloc(rffi.CCHARP.TO, 128) as p:    # big enough
        sprintf_longdouble(p, FORMAT_LONGDOUBLE, lvalue)
        return rffi.charp2str(p)

# ____________________________________________________________


UNSIGNED = 0x1000

TYPES = [
    ("int8_t",        1),
    ("uint8_t",       1 | UNSIGNED),
    ("int16_t",       2),
    ("uint16_t",      2 | UNSIGNED),
    ("int32_t",       4),
    ("uint32_t",      4 | UNSIGNED),
    ("int64_t",       8),
    ("uint64_t",      8 | UNSIGNED),

    ("intptr_t",      rffi.sizeof(rffi.INTPTR_T)),
    ("uintptr_t",     rffi.sizeof(rffi.UINTPTR_T) | UNSIGNED),
    ("ptrdiff_t",     rffi.sizeof(rffi.INTPTR_T)),   # XXX can it be different?
    ("size_t",        rffi.sizeof(rffi.SIZE_T) | UNSIGNED),
    ("ssize_t",       rffi.sizeof(rffi.SSIZE_T)),
]


def nonstandard_integer_types(space):
    w_d = space.newdict()
    for name, size in TYPES:
        space.setitem(w_d, space.wrap(name), space.wrap(size))
    return w_d

# ____________________________________________________________

def _is_a_float(space, w_ob):
    from pypy.module._cffi_backend.cdataobj import W_CData
    from pypy.module._cffi_backend.ctypeprim import W_CTypePrimitiveFloat
    ob = space.interpclass_w(w_ob)
    if isinstance(ob, W_CData):
        return isinstance(ob.ctype, W_CTypePrimitiveFloat)
    return space.isinstance_w(w_ob, space.w_float)

def as_long_long(space, w_ob):
    # (possibly) convert and cast a Python object to a long long.
    # This version accepts a Python int too, and does convertions from
    # other types of objects.  It refuses floats.
    if space.is_w(space.type(w_ob), space.w_int):   # shortcut
        return space.int_w(w_ob)
    try:
        bigint = space.bigint_w(w_ob)
    except OperationError, e:
        if not e.match(space, space.w_TypeError):
            raise
        if _is_a_float(space, w_ob):
            raise
        bigint = space.bigint_w(space.int(w_ob))
    try:
        return bigint.tolonglong()
    except OverflowError:
        raise OperationError(space.w_OverflowError, space.wrap(ovf_msg))

def as_unsigned_long_long(space, w_ob, strict):
    # (possibly) convert and cast a Python object to an unsigned long long.
    # This accepts a Python int too, and does convertions from other types of
    # objects.  If 'strict', complains with OverflowError; if 'not strict',
    # mask the result and round floats.
    if space.is_w(space.type(w_ob), space.w_int):   # shortcut
        value = space.int_w(w_ob)
        if strict and value < 0:
            raise OperationError(space.w_OverflowError, space.wrap(neg_msg))
        return r_ulonglong(value)
    try:
        bigint = space.bigint_w(w_ob)
    except OperationError, e:
        if not e.match(space, space.w_TypeError):
            raise
        if strict and _is_a_float(space, w_ob):
            raise
        bigint = space.bigint_w(space.int(w_ob))
    if strict:
        try:
            return bigint.toulonglong()
        except ValueError:
            raise OperationError(space.w_OverflowError, space.wrap(neg_msg))
        except OverflowError:
            raise OperationError(space.w_OverflowError, space.wrap(ovf_msg))
    else:
        return bigint.ulonglongmask()

neg_msg = "can't convert negative number to unsigned"
ovf_msg = "long too big to convert"

# ____________________________________________________________

def _raw_memcopy(source, dest, size):
    if jit.isconstant(size):
        # for the JIT: first handle the case where 'size' is known to be
        # a constant equal to 1, 2, 4, 8
        for TP, TPP in _prim_unsigned_types:
            if size == rffi.sizeof(TP):
                rffi.cast(TPP, dest)[0] = rffi.cast(TPP, source)[0]
                return
    _raw_memcopy_opaque(source, dest, size)

@jit.dont_look_inside
def _raw_memcopy_opaque(source, dest, size):
    # push push push at the llmemory interface (with hacks that are all
    # removed after translation)
    zero = llmemory.itemoffsetof(rffi.CCHARP.TO, 0)
    llmemory.raw_memcopy(
        llmemory.cast_ptr_to_adr(source) + zero,
        llmemory.cast_ptr_to_adr(dest) + zero,
        size * llmemory.sizeof(lltype.Char))

def _raw_memclear(dest, size):
    # for now, only supports the cases of size = 1, 2, 4, 8
    for TP, TPP in _prim_unsigned_types:
        if size == rffi.sizeof(TP):
            rffi.cast(TPP, dest)[0] = rffi.cast(TP, 0)
            return
    raise NotImplementedError("bad clear size")
