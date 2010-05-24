import py, os
from pypy.rpython.lltypesystem import llmemory
from pypy.rpython.ootypesystem import ootype
from pypy.rpython.llinterp import LLException
from pypy.rlib.objectmodel import we_are_translated
from pypy.rlib.unroll import unrolling_iterable
from pypy.rlib.debug import debug_start, debug_stop, debug_print
from pypy.rlib.debug import make_sure_not_resized

from pypy.jit.metainterp import history, compile, resume
from pypy.jit.metainterp.history import Const, ConstInt, ConstPtr, ConstFloat
from pypy.jit.metainterp.history import Box
from pypy.jit.metainterp.resoperation import rop
from pypy.jit.metainterp import executor
from pypy.jit.metainterp.logger import Logger
from pypy.jit.metainterp.jitprof import EmptyProfiler
from pypy.jit.metainterp.jitprof import GUARDS, RECORDED_OPS, ABORT_ESCAPE
from pypy.jit.metainterp.jitprof import ABORT_TOO_LONG, ABORT_BRIDGE
from pypy.jit.metainterp.blackhole import get_llexception
from pypy.rlib.rarithmetic import intmask
from pypy.rlib.objectmodel import specialize
from pypy.rlib.jit import DEBUG_OFF, DEBUG_PROFILE, DEBUG_STEPS, DEBUG_DETAILED
from pypy.jit.metainterp.compile import GiveUp
from pypy.jit.codewriter.jitcode import JitCode, SwitchDictDescr

# ____________________________________________________________

def arguments(*args):
    def decorate(func):
        func.argtypes = args
        return func
    return decorate

class FixME:      # XXX temporary hack
    def __init__(self, func):
        self.func = func
    def __getattr__(self, _):
        raise Exception("@FixME: " + self.func.__name__)

# ____________________________________________________________


class MIFrame(object):

    env = property(lambda: xxx, lambda: xxx)     # temporary: no read/write!

    def __init__(self, metainterp):
        self.metainterp = metainterp
        self.registers_i = [None] * 256
        self.registers_r = [None] * 256
        self.registers_f = [None] * 256

    def setup(self, jitcode, greenkey=None):
        assert isinstance(jitcode, JitCode)
        self.jitcode = jitcode
        self.bytecode = jitcode.code
        self.name = jitcode.name # purely for having name attribute
        # this is not None for frames that are recursive portal calls
        self.greenkey = greenkey
        # copy the constants in place
        self.copy_constants(self.registers_i, jitcode.constants_i, ConstInt)
        self.copy_constants(self.registers_r, jitcode.constants_r, ConstPtr)
        self.copy_constants(self.registers_f, jitcode.constants_f, ConstFloat)
        self._result_argcode = 'v'
        # for resume.py operation
        self.parent_resumedata_snapshot = None
        self.parent_resumedata_frame_info_list = None

    def copy_constants(self, registers, constants, ConstClass):
        """Copy jitcode.constants[0] to registers[255],
                jitcode.constants[1] to registers[254],
                jitcode.constants[2] to registers[253], etc."""
        i = len(constants) - 1
        while i >= 0:
            j = 255 - i
            assert j >= 0
            registers[j] = ConstClass(constants[i])
            i -= 1
    copy_constants._annspecialcase_ = 'specialize:arg(3)'

    def cleanup_registers(self):
        # To avoid keeping references alive, this cleans up the registers_r.
        # It does not clear the references set by copy_constants(), but
        # these are all prebuilt constants anyway.
        for i in range(self.jitcode.num_regs_r()):
            self.registers_r[i] = None

    # ------------------------------
    # Decoding of the JitCode

    def prepare_list_of_boxes(self, outvalue, startindex, position, argcode):
        assert argcode in 'IRF'
        code = self.bytecode
        length = ord(code[position])
        position += 1
        for i in range(length):
            index = ord(code[position+i])
            if   argcode == 'I': reg = self.registers_i[index]
            elif argcode == 'R': reg = self.registers_r[index]
            elif argcode == 'F': reg = self.registers_f[index]
            outvalue[startindex+i] = reg
    prepare_list_of_boxes._annspecialcase_ = 'specialize:arg(4)'

    def get_list_of_active_boxes(self, in_a_call):
        if in_a_call:
            # If we are not the topmost frame, self._result_argcode contains
            # the type of the result of the call instruction in the bytecode.
            # We use it to clear the box that will hold the result: this box
            # is not defined yet.
            argcode = self._result_argcode
            index = ord(self.bytecode[self.pc - 1])
            if   argcode == 'i': self.registers_i[index] = None
            elif argcode == 'r': self.registers_r[index] = None
            elif argcode == 'f': self.registers_f[index] = None
            self._result_argcode = 'v'     # done
        #
        self._tmp_count = 0
        count = self.jitcode.enumerate_live_vars(
            self.pc, MIFrame._count_boxes, self,
            self.registers_i, self.registers_r, self.registers_f)
        #
        self._tmp_env = [None] * self._tmp_count
        self._tmp_count = 0
        self.jitcode.enumerate_live_vars(
            self.pc, MIFrame._store_in_env, self,
            self.registers_i, self.registers_r, self.registers_f)
        #
        env = self._tmp_env
        self._tmp_env = None
        make_sure_not_resized(env)
        return env

    def _count_boxes(self, box):
        if box is not None:       # just used to count how many boxes there are
            self._tmp_count += 1

    def _store_in_env(self, box):
        if box is not None:
            index = self._tmp_count
            self._tmp_env[index] = box
            self._tmp_count = index + 1

    def replace_active_box_in_frame(self, oldbox, newbox):
        if isinstance(oldbox, history.BoxInt):
            count = self.jitcode.num_regs_i()
            registers = self.registers_i
        elif isinstance(oldbox, history.BoxPtr):
            count = self.jitcode.num_regs_r()
            registers = self.registers_r
        elif isinstance(oldbox, history.BoxFloat):
            count = self.jitcode.num_regs_f()
            registers = self.registers_f
        else:
            assert 0, repr(oldbox)
        for i in range(count):
            if registers[i] is oldbox:
                registers[i] = newbox
        if not we_are_translated():
            assert oldbox not in registers[count:]

    def make_result_of_lastop(self, resultbox):
        if resultbox is None:
            return
        target_index = ord(self.bytecode[self.pc-1])
        if resultbox.type == history.INT:
            self.registers_i[target_index] = resultbox
        elif resultbox.type == history.REF:
            self.registers_r[target_index] = resultbox
        elif resultbox.type == history.FLOAT:
            self.registers_f[target_index] = resultbox
        else:
            raise AssertionError("bad result box type")

    # ------------------------------

    for _opimpl in ['int_add', 'int_sub', 'int_mul', 'int_floordiv', 'int_mod',
                    'int_lt', 'int_le', 'int_eq',
                    'int_ne', 'int_gt', 'int_ge',
                    'int_and', 'int_or', 'int_xor',
                    'int_rshift', 'int_lshift', 'uint_rshift',
                    'uint_lt', 'uint_le', 'uint_gt', 'uint_ge',
                    'uint_floordiv',
                    'float_add', 'float_sub', 'float_mul', 'float_truediv',
                    'float_lt', 'float_le', 'float_eq',
                    'float_ne', 'float_gt', 'float_ge',
                    'ptr_eq', 'ptr_ne',
                    ]:
        exec py.code.Source('''
            @arguments("box", "box")
            def opimpl_%s(self, b1, b2):
                return self.execute(rop.%s, b1, b2)
        ''' % (_opimpl, _opimpl.upper())).compile()

    for _opimpl in ['int_add_ovf', 'int_sub_ovf', 'int_mul_ovf']:
        exec py.code.Source('''
            @arguments("box", "box")
            def opimpl_%s(self, b1, b2):
                resbox = self.execute(rop.%s, b1, b2)
                self.make_result_of_lastop(resbox)  # same as execute_varargs()
                self.metainterp.handle_possible_overflow_error()
                return resbox
        ''' % (_opimpl, _opimpl.upper())).compile()

    for _opimpl in ['int_is_true', 'int_is_zero', 'int_neg', 'int_invert',
                    'cast_ptr_to_int', 'cast_float_to_int',
                    'cast_int_to_float', 'float_neg', 'float_abs',
                    ]:
        exec py.code.Source('''
            @arguments("box")
            def opimpl_%s(self, b):
                return self.execute(rop.%s, b)
        ''' % (_opimpl, _opimpl.upper())).compile()

    @arguments("box")
    def opimpl_ptr_nonzero(self, box):
        return self.execute(rop.PTR_NE, box, history.CONST_NULL)

    @arguments("box")
    def opimpl_ptr_iszero(self, box):
        return self.execute(rop.PTR_EQ, box, history.CONST_NULL)

    @arguments("box")
    def _opimpl_any_return(self, box):
        self.metainterp.finishframe(box)

    opimpl_int_return = _opimpl_any_return
    opimpl_ref_return = _opimpl_any_return
    opimpl_float_return = _opimpl_any_return

    @arguments()
    def opimpl_void_return(self):
        self.metainterp.finishframe(None)

    @arguments("box")
    def _opimpl_any_copy(self, box):
        return box

    opimpl_int_copy   = _opimpl_any_copy
    opimpl_ref_copy   = _opimpl_any_copy
    opimpl_float_copy = _opimpl_any_copy

    @arguments("box")
    def _opimpl_any_push(self, box):
        self.pushed_box = box

    opimpl_int_push   = _opimpl_any_push
    opimpl_ref_push   = _opimpl_any_push
    opimpl_float_push = _opimpl_any_push

    @arguments()
    def _opimpl_any_pop(self):
        box = self.pushed_box
        self.pushed_box = None
        return box

    opimpl_int_pop   = _opimpl_any_pop
    opimpl_ref_pop   = _opimpl_any_pop
    opimpl_float_pop = _opimpl_any_pop

    @arguments("label")
    def opimpl_catch_exception(self, target):
        """This is a no-op when run normally.  We can check that
        last_exc_value_box is None; it should have been set to None
        by the previous instruction.  If the previous instruction
        raised instead, finishframe_exception() should have been
        called and we would not be there."""
        assert self.metainterp.last_exc_value_box is None

    @arguments("label")
    def opimpl_goto(self, target):
        self.pc = target

    @arguments("box", "label")
    def opimpl_goto_if_not(self, box, target):
        switchcase = box.getint()
        if switchcase:
            opnum = rop.GUARD_TRUE
        else:
            opnum = rop.GUARD_FALSE
        self.generate_guard(opnum, box)
        if not switchcase:
            self.pc = target

    @arguments("box", "label")
    def opimpl_goto_if_not_int_is_true(self, box, target):
        condbox = self.execute(rop.INT_IS_TRUE, box)
        self.opimpl_goto_if_not(condbox, target)

    @arguments("box", "label")
    def opimpl_goto_if_not_int_is_zero(self, box, target):
        condbox = self.execute(rop.INT_IS_ZERO, box)
        self.opimpl_goto_if_not(condbox, target)

    for _opimpl in ['int_lt', 'int_le', 'int_eq', 'int_ne', 'int_gt', 'int_ge',
                    'ptr_eq', 'ptr_ne']:
        exec py.code.Source('''
            @arguments("box", "box", "label")
            def opimpl_goto_if_not_%s(self, b1, b2, target):
                condbox = self.execute(rop.%s, b1, b2)
                self.opimpl_goto_if_not(condbox, target)
        ''' % (_opimpl, _opimpl.upper())).compile()

    @arguments("orgpc", "box", "label")
    def opimpl_goto_if_not_ptr_nonzero(self, orgpc, box, target):
        value = box.nonnull()
        if value:
            opnum = rop.GUARD_NONNULL
        else:
            opnum = rop.GUARD_ISNULL
        self.generate_guard(opnum, box, resumepc=orgpc)
        if not value:
            self.pc = target

    @arguments("orgpc", "box", "label")
    def opimpl_goto_if_not_ptr_iszero(self, orgpc, box, target):
        value = box.nonnull()
        if value:
            opnum = rop.GUARD_NONNULL
        else:
            opnum = rop.GUARD_ISNULL
        self.generate_guard(opnum, box, resumepc=orgpc)
        if value:
            self.pc = target

##    def follow_jump(self):
##        _op_goto_if_not = self.metainterp.staticdata._op_goto_if_not
##        assert ord(self.bytecode[self.pc]) == _op_goto_if_not
##        self.pc += 1          # past the bytecode for 'goto_if_not'
##        target = self.load_3byte()  # load the 'target' argument
##        self.pc = target      # jump

##    def ignore_next_guard_nullness(self, opnum):
##        _op_ooisnull = self.metainterp.staticdata._op_ooisnull
##        _op_oononnull = self.metainterp.staticdata._op_oononnull
##        bc = ord(self.bytecode[self.pc])
##        if bc == _op_ooisnull:
##            if opnum == rop.GUARD_ISNULL:
##                res = ConstInt(0)
##            else:
##                res = ConstInt(1)
##        else:
##            assert bc == _op_oononnull
##            if opnum == rop.GUARD_ISNULL:
##                res = ConstInt(1)
##            else:
##                res = ConstInt(0)
##        self.pc += 1    # past the bytecode for ptr_iszero/ptr_nonzero
##        self.load_int() # past the 'box' argument
##        self.make_result_box(res)

##    def dont_follow_jump(self):
##        _op_goto_if_not = self.metainterp.staticdata._op_goto_if_not
##        assert ord(self.bytecode[self.pc]) == _op_goto_if_not
##        self.pc += 1          # past the bytecode for 'goto_if_not'
##        self.load_3byte()     # past the 'target' argument
##        self.load_int()       # past the 'box' argument
##        self.ignore_varargs() # past the 'livelist' argument

    @arguments("box", "descr", "orgpc")
    def opimpl_switch(self, valuebox, switchdict, orgpc):
        box = self.implement_guard_value(orgpc, valuebox)
        switchvalue = box.getint()
        assert isinstance(switchdict, SwitchDictDescr)
        try:
            self.pc = switchdict.dict[switchvalue]
        except KeyError:
            pass

    @arguments("descr")
    def opimpl_new(self, sizedescr):
        return self.execute_with_descr(rop.NEW, sizedescr)

    @arguments("descr")
    def opimpl_new_with_vtable(self, sizedescr):
        from pypy.jit.codewriter import heaptracker
        cpu = self.metainterp.cpu
        cls = heaptracker.descr2vtable(cpu, sizedescr)
        return self.execute(rop.NEW_WITH_VTABLE, ConstInt(cls))

    @FixME  #arguments("box")
    def opimpl_runtimenew(self, classbox):
        self.execute(rop.RUNTIMENEW, classbox)

    @FixME  #arguments("orgpc", "box", "descr")
    def opimpl_instanceof(self, pc, objbox, typedescr):
        clsbox = self.cls_of_box(objbox)
        if isinstance(objbox, Box):
            self.generate_guard(pc, rop.GUARD_CLASS, objbox, [clsbox])
        self.execute_with_descr(rop.INSTANCEOF, typedescr, objbox)

    @FixME  #arguments("box", "box")
    def opimpl_subclassof(self, box1, box2):
        self.execute(rop.SUBCLASSOF, box1, box2)

    @arguments("descr", "box")
    def opimpl_new_array(self, itemsizedescr, countbox):
        return self.execute_with_descr(rop.NEW_ARRAY, itemsizedescr, countbox)

    @arguments("box", "descr", "box")
    def _opimpl_getarrayitem_gc_any(self, arraybox, arraydescr, indexbox):
        return self.execute_with_descr(rop.GETARRAYITEM_GC,
                                       arraydescr, arraybox, indexbox)

    opimpl_getarrayitem_gc_i = _opimpl_getarrayitem_gc_any
    opimpl_getarrayitem_gc_r = _opimpl_getarrayitem_gc_any
    opimpl_getarrayitem_gc_f = _opimpl_getarrayitem_gc_any

    @arguments("box", "descr", "box")
    def _opimpl_getarrayitem_gc_pure_any(self, arraybox, arraydescr, indexbox):
        return self.execute_with_descr(rop.GETARRAYITEM_GC_PURE,
                                       arraydescr, arraybox, indexbox)

    opimpl_getarrayitem_gc_pure_i = _opimpl_getarrayitem_gc_pure_any
    opimpl_getarrayitem_gc_pure_r = _opimpl_getarrayitem_gc_pure_any
    opimpl_getarrayitem_gc_pure_f = _opimpl_getarrayitem_gc_pure_any

    @arguments("box", "descr", "box", "box")
    def _opimpl_setarrayitem_gc_any(self, arraybox, arraydescr,
                                    indexbox, itembox):
        self.execute_with_descr(rop.SETARRAYITEM_GC, arraydescr, arraybox,
                                indexbox, itembox)

    opimpl_setarrayitem_gc_i = _opimpl_setarrayitem_gc_any
    opimpl_setarrayitem_gc_r = _opimpl_setarrayitem_gc_any
    opimpl_setarrayitem_gc_f = _opimpl_setarrayitem_gc_any

    @arguments("box", "descr")
    def opimpl_arraylen_gc(self, arraybox, arraydescr):
        return self.execute_with_descr(rop.ARRAYLEN_GC, arraydescr, arraybox)

    @arguments("descr", "box", "box", "box", "box", "box", "box", "descr")
    def opimpl_arraycopy(self, calldescr, fnptr, sourcebox, destbox,
                         source_startbox, dest_startbox, lengthbox,
                         arraydescr):
        self.execute_with_descr(rop.ARRAYCOPY, arraydescr, calldescr, fnptr,
                                sourcebox, destbox, source_startbox,
                                dest_startbox, lengthbox)

    @arguments("orgpc", "box", "descr", "box")
    def opimpl_check_neg_index(self, orgpc, arraybox, arraydescr, indexbox):
        negbox = self.metainterp.execute_and_record(
            rop.INT_LT, None, indexbox, history.CONST_FALSE)
        negbox = self.implement_guard_value(orgpc, negbox)
        if negbox.getint():
            # the index is < 0; add the array length to it
            lenbox = self.metainterp.execute_and_record(
                rop.ARRAYLEN_GC, arraydescr, arraybox)
            indexbox = self.metainterp.execute_and_record(
                rop.INT_ADD, None, indexbox, lenbox)
        return indexbox

    @arguments("descr", "descr", "descr", "descr", "box")
    def opimpl_newlist(self, structdescr, lengthdescr, itemsdescr, arraydescr,
                       sizebox):
        sbox = self.metainterp.execute_and_record(rop.NEW, structdescr)
        self.metainterp.execute_and_record(rop.SETFIELD_GC, lengthdescr, 
                                           sbox, sizebox)
        abox = self.metainterp.execute_and_record(rop.NEW_ARRAY, arraydescr,
                                                  sizebox)
        self.metainterp.execute_and_record(rop.SETFIELD_GC, itemsdescr,
                                           sbox, abox)
        return sbox

    @arguments("box", "descr", "descr", "box")
    def _opimpl_getlistitem_gc_any(self, listbox, itemsdescr, arraydescr,
                                   indexbox):
        arraybox = self.metainterp.execute_and_record(rop.GETFIELD_GC,
                                                      itemsdescr, listbox)
        return self.execute_with_descr(rop.GETARRAYITEM_GC,
                                       arraydescr, arraybox, indexbox)

    opimpl_getlistitem_gc_i = _opimpl_getlistitem_gc_any
    opimpl_getlistitem_gc_r = _opimpl_getlistitem_gc_any
    opimpl_getlistitem_gc_f = _opimpl_getlistitem_gc_any

    @arguments("box", "descr", "descr", "box", "box")
    def _opimpl_setlistitem_gc_any(self, listbox, itemsdescr, arraydescr,
                                   indexbox, valuebox):
        arraybox = self.metainterp.execute_and_record(rop.GETFIELD_GC,
                                                      itemsdescr, listbox)
        self.execute_with_descr(rop.SETARRAYITEM_GC, arraydescr, arraybox,
                                indexbox, valuebox)

    opimpl_setlistitem_gc_i = _opimpl_setlistitem_gc_any
    opimpl_setlistitem_gc_r = _opimpl_setlistitem_gc_any
    opimpl_setlistitem_gc_f = _opimpl_setlistitem_gc_any

    @arguments("orgpc", "box", "descr", "box")
    def opimpl_check_resizable_neg_index(self, orgpc, listbox, lengthdescr,
                                         indexbox):
        negbox = self.metainterp.execute_and_record(
            rop.INT_LT, None, indexbox, history.CONST_FALSE)
        negbox = self.implement_guard_value(orgpc, negbox)
        if negbox.getint():
            # the index is < 0; add the array length to it
            lenbox = self.metainterp.execute_and_record(
                rop.GETFIELD_GC, lengthdescr, listbox)
            indexbox = self.metainterp.execute_and_record(
                rop.INT_ADD, None, indexbox, lenbox)
        return indexbox

    @arguments("box", "descr")
    def _opimpl_getfield_gc_any(self, box, fielddescr):
        return self.execute_with_descr(rop.GETFIELD_GC, fielddescr, box)
    opimpl_getfield_gc_i = _opimpl_getfield_gc_any
    opimpl_getfield_gc_r = _opimpl_getfield_gc_any
    opimpl_getfield_gc_f = _opimpl_getfield_gc_any

    @arguments("box", "descr")
    def _opimpl_getfield_gc_pure_any(self, box, fielddescr):
        return self.execute_with_descr(rop.GETFIELD_GC_PURE, fielddescr, box)
    opimpl_getfield_gc_i_pure = _opimpl_getfield_gc_pure_any
    opimpl_getfield_gc_r_pure = _opimpl_getfield_gc_pure_any
    opimpl_getfield_gc_f_pure = _opimpl_getfield_gc_pure_any

    @arguments("box", "descr", "box")
    def _opimpl_setfield_gc_any(self, box, fielddescr, valuebox):
        self.execute_with_descr(rop.SETFIELD_GC, fielddescr, box, valuebox)
    opimpl_setfield_gc_i = _opimpl_setfield_gc_any
    opimpl_setfield_gc_r = _opimpl_setfield_gc_any
    opimpl_setfield_gc_f = _opimpl_setfield_gc_any

    @arguments("box", "descr")
    def _opimpl_getfield_raw_any(self, box, fielddescr):
        return self.execute_with_descr(rop.GETFIELD_RAW, fielddescr, box)
    opimpl_getfield_raw_i = _opimpl_getfield_raw_any
    opimpl_getfield_raw_r = _opimpl_getfield_raw_any
    opimpl_getfield_raw_f = _opimpl_getfield_raw_any

    @arguments("box", "descr")
    def _opimpl_getfield_raw_pure_any(self, box, fielddescr):
        return self.execute_with_descr(rop.GETFIELD_RAW_PURE, fielddescr, box)
    opimpl_getfield_raw_i_pure = _opimpl_getfield_raw_pure_any
    opimpl_getfield_raw_r_pure = _opimpl_getfield_raw_pure_any
    opimpl_getfield_raw_f_pure = _opimpl_getfield_raw_pure_any

    @arguments("box", "descr", "box")
    def _opimpl_setfield_raw_any(self, box, fielddescr, valuebox):
        self.execute_with_descr(rop.SETFIELD_RAW, fielddescr, box, valuebox)
    opimpl_setfield_raw_i = _opimpl_setfield_raw_any
    opimpl_setfield_raw_r = _opimpl_setfield_raw_any
    opimpl_setfield_raw_f = _opimpl_setfield_raw_any

    def _nonstandard_virtualizable(self, pc, box):
        # returns True if 'box' is actually not the "standard" virtualizable
        # that is stored in metainterp.virtualizable_boxes[-1]
        standard_box = self.metainterp.virtualizable_boxes[-1]
        if standard_box is box:
            return False
        eqbox = self.metainterp.execute_and_record(rop.PTR_EQ, None,
                                                   box, standard_box)
        eqbox = self.implement_guard_value(pc, eqbox)
        isstandard = eqbox.getint()
        if isstandard:
            self.metainterp.replace_box(box, standard_box)
        return not isstandard

    def _get_virtualizable_field_descr(self, index):
        vinfo = self.metainterp.staticdata.virtualizable_info
        return vinfo.static_field_descrs[index]

    def _get_virtualizable_array_field_descr(self, index):
        vinfo = self.metainterp.staticdata.virtualizable_info
        return vinfo.array_field_descrs[index]

    def _get_virtualizable_array_descr(self, index):
        vinfo = self.metainterp.staticdata.virtualizable_info
        return vinfo.array_descrs[index]

    @FixME  #arguments("orgpc", "box", "int")
    def opimpl_getfield_vable(self, pc, basebox, index):
        if self._nonstandard_virtualizable(pc, basebox):
            self.execute_with_descr(rop.GETFIELD_GC, self._get_virtualizable_field_descr(index), basebox)
            return
        self.metainterp.check_synchronized_virtualizable()
        resbox = self.metainterp.virtualizable_boxes[index]
        self.make_result_box(resbox)
    @FixME  #arguments("orgpc", "box", "int", "box")
    def opimpl_setfield_vable(self, pc, basebox, index, valuebox):
        if self._nonstandard_virtualizable(pc, basebox):
            self.execute_with_descr(rop.SETFIELD_GC, self._get_virtualizable_field_descr(index), basebox, valuebox)
            return
        self.metainterp.virtualizable_boxes[index] = valuebox
        self.metainterp.synchronize_virtualizable()
        # XXX only the index'th field needs to be synchronized, really

    def _get_arrayitem_vable_index(self, pc, arrayindex, indexbox):
        indexbox = self.implement_guard_value(pc, indexbox)
        vinfo = self.metainterp.staticdata.virtualizable_info
        virtualizable_box = self.metainterp.virtualizable_boxes[-1]
        virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
        index = indexbox.getint()
        if index < 0:
            index += vinfo.get_array_length(virtualizable, arrayindex)
        assert 0 <= index < vinfo.get_array_length(virtualizable, arrayindex)
        return vinfo.get_index_in_array(virtualizable, arrayindex, index)

    @FixME  #arguments("orgpc", "box", "int", "box")
    def opimpl_getarrayitem_vable(self, pc, basebox, arrayindex, indexbox):
        if self._nonstandard_virtualizable(pc, basebox):
            descr = self._get_virtualizable_array_field_descr(arrayindex)
            arraybox = self.metainterp.execute_and_record(rop.GETFIELD_GC,
                                                          descr, basebox)
            descr = self._get_virtualizable_array_descr(arrayindex)
            self.execute_with_descr(rop.GETARRAYITEM_GC, descr,
                                    arraybox, indexbox)
            return
        self.metainterp.check_synchronized_virtualizable()
        index = self._get_arrayitem_vable_index(pc, arrayindex, indexbox)
        resbox = self.metainterp.virtualizable_boxes[index]
        self.make_result_box(resbox)
    @FixME  #arguments("orgpc", "box", "int", "box", "box")
    def opimpl_setarrayitem_vable(self, pc, basebox, arrayindex, indexbox,
                                  valuebox):
        if self._nonstandard_virtualizable(pc, basebox):
            descr = self._get_virtualizable_array_field_descr(arrayindex)
            arraybox = self.metainterp.execute_and_record(rop.GETFIELD_GC,
                                                          descr, basebox)
            descr = self._get_virtualizable_array_descr(arrayindex)
            self.execute_with_descr(rop.SETARRAYITEM_GC, descr,
                                    arraybox, indexbox, valuebox)
            return
        index = self._get_arrayitem_vable_index(pc, arrayindex, indexbox)
        self.metainterp.virtualizable_boxes[index] = valuebox
        self.metainterp.synchronize_virtualizable()
        # XXX only the index'th field needs to be synchronized, really
    @FixME  #arguments("orgpc", "box", "int")
    def opimpl_arraylen_vable(self, pc, basebox, arrayindex):
        if self._nonstandard_virtualizable(pc, basebox):
            descr = self._get_virtualizable_array_field_descr(arrayindex)
            arraybox = self.metainterp.execute_and_record(rop.GETFIELD_GC,
                                                          descr, basebox)
            descr = self._get_virtualizable_array_descr(arrayindex)
            self.execute_with_descr(rop.ARRAYLEN_GC, descr, arraybox)
            return
        vinfo = self.metainterp.staticdata.virtualizable_info
        virtualizable_box = self.metainterp.virtualizable_boxes[-1]
        virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
        result = vinfo.get_array_length(virtualizable, arrayindex)
        self.make_result_box(ConstInt(result))

    @arguments("jitcode", "boxes")
    def _opimpl_inline_call1(self, jitcode, argboxes):
        self.metainterp.perform_call(jitcode, argboxes)
    @arguments("jitcode", "boxes2")
    def _opimpl_inline_call2(self, jitcode, argboxes):
        self.metainterp.perform_call(jitcode, argboxes)
    @arguments("jitcode", "boxes3")
    def _opimpl_inline_call3(self, jitcode, argboxes):
        self.metainterp.perform_call(jitcode, argboxes)

    opimpl_inline_call_r_i = _opimpl_inline_call1
    opimpl_inline_call_r_r = _opimpl_inline_call1
    opimpl_inline_call_r_v = _opimpl_inline_call1
    opimpl_inline_call_ir_i = _opimpl_inline_call2
    opimpl_inline_call_ir_r = _opimpl_inline_call2
    opimpl_inline_call_ir_v = _opimpl_inline_call2
    opimpl_inline_call_irf_i = _opimpl_inline_call3
    opimpl_inline_call_irf_r = _opimpl_inline_call3
    opimpl_inline_call_irf_f = _opimpl_inline_call3
    opimpl_inline_call_irf_v = _opimpl_inline_call3

    @arguments("box", "descr", "boxes")
    def _opimpl_residual_call1(self, funcbox, calldescr, argboxes):
        return self.do_residual_or_indirect_call(funcbox, calldescr, argboxes)
    @arguments("box", "descr", "boxes2")
    def _opimpl_residual_call2(self, funcbox, calldescr, argboxes):
        return self.do_residual_or_indirect_call(funcbox, calldescr, argboxes)
    @arguments("box", "descr", "boxes3")
    def _opimpl_residual_call3(self, funcbox, calldescr, argboxes):
        return self.do_residual_or_indirect_call(funcbox, calldescr, argboxes)

    opimpl_residual_call_r_i = _opimpl_residual_call1
    opimpl_residual_call_r_r = _opimpl_residual_call1
    opimpl_residual_call_r_v = _opimpl_residual_call1
    opimpl_residual_call_ir_i = _opimpl_residual_call2
    opimpl_residual_call_ir_r = _opimpl_residual_call2
    opimpl_residual_call_ir_v = _opimpl_residual_call2
    opimpl_residual_call_irf_i = _opimpl_residual_call3
    opimpl_residual_call_irf_r = _opimpl_residual_call3
    opimpl_residual_call_irf_f = _opimpl_residual_call3
    opimpl_residual_call_irf_v = _opimpl_residual_call3

    @FixME  #arguments("descr", "varargs")
    def opimpl_residual_call_loopinvariant(self, calldescr, varargs):
        return self.execute_varargs(rop.CALL_LOOPINVARIANT, varargs, calldescr,
                                    exc=True)

    @FixME  #arguments("orgpc", "descr", "varargs")
    def opimpl_recursive_call(self, pc, calldescr, varargs):
        warmrunnerstate = self.metainterp.staticdata.state
        token = None
        if not self.metainterp.is_blackholing() and warmrunnerstate.inlining:
            num_green_args = self.metainterp.staticdata.num_green_args
            portal_code = self.metainterp.staticdata.portal_code
            greenkey = varargs[1:num_green_args + 1]
            if warmrunnerstate.can_inline_callable(greenkey):
                return self.perform_call(portal_code, varargs[1:], greenkey)
            token = warmrunnerstate.get_assembler_token(greenkey)
        call_position = 0
        if token is not None:
            call_position = len(self.metainterp.history.operations)
            # verify that we have all green args, needed to make sure
            # that assembler that we call is still correct
            greenargs = varargs[1:num_green_args + 1]
            self.verify_green_args(greenargs)
        res = self.do_residual_call(varargs, descr=calldescr, exc=True)
        if not self.metainterp.is_blackholing() and token is not None:
            # XXX fix the call position, <UGLY!>
            found = False
            while True:
                op = self.metainterp.history.operations[call_position]
                if op.opnum == rop.CALL or op.opnum == rop.CALL_MAY_FORCE:
                    found = True
                    break
                call_position += 1
            assert found
            # </UGLY!>
            # this will substitute the residual call with assembler call
            self.metainterp.direct_assembler_call(pc, varargs, token,
                                                  call_position)
        return res

    @FixME  #arguments("orgpc", "methdescr", "varargs")
    def opimpl_oosend(self, pc, methdescr, varargs):
        objbox = varargs[0]
        clsbox = self.cls_of_box(objbox)
        if isinstance(objbox, Box):
            self.generate_guard(pc, rop.GUARD_CLASS, objbox, [clsbox])
        oocls = clsbox.getref(ootype.Class)
        jitcode = methdescr.get_jitcode_for_class(oocls)
        if jitcode is not None:
            # we should follow calls to this graph
            return self.perform_call(jitcode, varargs)
        else:
            # but we should not follow calls to that graph
            return self.execute_varargs(rop.OOSEND, varargs,
                                        descr=methdescr, exc=True)

    @arguments("box")
    def opimpl_strlen(self, strbox):
        return self.execute(rop.STRLEN, strbox)

    @arguments("box")
    def opimpl_unicodelen(self, unicodebox):
        return self.execute(rop.UNICODELEN, unicodebox)

    @arguments("box", "box")
    def opimpl_strgetitem(self, strbox, indexbox):
        return self.execute(rop.STRGETITEM, strbox, indexbox)

    @arguments("box", "box")
    def opimpl_unicodegetitem(self, unicodebox, indexbox):
        return self.execute(rop.UNICODEGETITEM, unicodebox, indexbox)

    @arguments("box", "box", "box")
    def opimpl_strsetitem(self, strbox, indexbox, newcharbox):
        return self.execute(rop.STRSETITEM, strbox, indexbox, newcharbox)

    @arguments("box", "box", "box")
    def opimpl_unicodesetitem(self, unicodebox, indexbox, newcharbox):
        self.execute(rop.UNICODESETITEM, unicodebox, indexbox, newcharbox)

    @arguments("box")
    def opimpl_newstr(self, lengthbox):
        return self.execute(rop.NEWSTR, lengthbox)

    @arguments("box")
    def opimpl_newunicode(self, lengthbox):
        return self.execute(rop.NEWUNICODE, lengthbox)

    @FixME  #arguments("descr", "varargs")
    def opimpl_residual_oosend_canraise(self, methdescr, varargs):
        return self.execute_varargs(rop.OOSEND, varargs, descr=methdescr,
                                    exc=True)

    @FixME  #arguments("descr", "varargs")
    def opimpl_residual_oosend_noraise(self, methdescr, varargs):
        return self.execute_varargs(rop.OOSEND, varargs, descr=methdescr,
                                    exc=False)

    @FixME  #arguments("descr", "varargs")
    def opimpl_residual_oosend_pure(self, methdescr, boxes):
        return self.execute_varargs(rop.OOSEND_PURE, boxes, descr=methdescr,
                                    exc=False)

    @arguments("orgpc", "box",)
    def _opimpl_guard_value(self, orgpc, box):
        self.implement_guard_value(orgpc, box)

    opimpl_int_guard_value = _opimpl_guard_value
    opimpl_ref_guard_value = _opimpl_guard_value
    opimpl_float_guard_value = _opimpl_guard_value

    @arguments("orgpc", "box")
    def opimpl_guard_class(self, orgpc, box):
        clsbox = self.cls_of_box(box)
        self.generate_guard(rop.GUARD_CLASS, box, [clsbox], resumepc=orgpc)
        return clsbox

    def verify_green_args(self, varargs):
        num_green_args = self.metainterp.staticdata.num_green_args
        for i in range(num_green_args):
            assert isinstance(varargs[i], Const)

    def blackhole_reached_merge_point(self, varargs):
        if self.metainterp.in_recursion:
            portal_code = self.metainterp.staticdata.portal_code
            # small hack: fish for the result box
            lenenv = len(self.env)
            raised = self.perform_call(portal_code, varargs)
            # in general this cannot be assumed, but when blackholing,
            # perform_call returns True only if an exception is called. In
            # this case perform_call has called finishframe_exception
            # already, so we need to return.
            if raised:
                return
            if lenenv == len(self.env):
                res = None
            else:
                assert lenenv == len(self.env) - 1
                res = self.env.pop()
            self.metainterp.finishframe(res)
        else:
            raise self.metainterp.staticdata.ContinueRunningNormally(varargs)

    @arguments()
    def opimpl_can_enter_jit(self):
        if self.metainterp.in_recursion:
            from pypy.jit.metainterp.warmspot import CannotInlineCanEnterJit
            raise CannotInlineCanEnterJit()
        self.metainterp.seen_can_enter_jit = True

    @arguments("boxes3", "boxes3")
    def opimpl_jit_merge_point(self, greenboxes, redboxes):
        self.verify_green_args(greenboxes)
        # xxx we may disable the following line in some context later
        self.debug_merge_point(greenboxes)
        if self.metainterp.seen_can_enter_jit:
            self.metainterp.seen_can_enter_jit = False
            try:
                self.metainterp.reached_can_enter_jit(greenboxes, redboxes)
            except GiveUp:
                XXX
                self.metainterp.switch_to_blackhole(ABORT_BRIDGE)

    def debug_merge_point(self, greenkey):
        # debugging: produce a DEBUG_MERGE_POINT operation
        sd = self.metainterp.staticdata
        loc = sd.state.get_location_str(greenkey)
        debug_print(loc)
        constloc = self.metainterp.cpu.ts.conststr(loc)
        self.metainterp.history.record(rop.DEBUG_MERGE_POINT,
                                       [constloc], None)

    @arguments("box", "label")
    def opimpl_goto_if_exception_mismatch(self, vtablebox, next_exc_target):
        metainterp = self.metainterp
        last_exc_value_box = metainterp.last_exc_value_box
        assert last_exc_value_box is not None
        assert metainterp.class_of_last_exc_is_const
        if not metainterp.cpu.ts.instanceOf(last_exc_value_box, vtablebox):
            self.pc = next_exc_target

    @arguments("orgpc", "box")
    def opimpl_raise(self, orgpc, exc_value_box):
        # xxx hack
        clsbox = self.cls_of_box(exc_value_box)
        self.generate_guard(rop.GUARD_CLASS, exc_value_box, [clsbox],
                            resumepc=orgpc)
        self.metainterp.class_of_last_exc_is_const = True
        self.metainterp.last_exc_value_box = exc_value_box
        self.metainterp.popframe()
        self.metainterp.finishframe_exception()

    @arguments()
    def opimpl_reraise(self):
        assert self.metainterp.last_exc_value_box is not None
        self.metainterp.popframe()
        self.metainterp.finishframe_exception()

    @arguments()
    def opimpl_last_exception(self):
        # Same comment as in opimpl_goto_if_exception_mismatch().
        exc_value_box = self.metainterp.last_exc_value_box
        assert exc_value_box is not None
        assert self.metainterp.class_of_last_exc_is_const
        return self.metainterp.cpu.ts.cls_of_box(exc_value_box)

    @arguments()
    def opimpl_last_exc_value(self):
        exc_value_box = self.metainterp.last_exc_value_box
        assert exc_value_box is not None
        return exc_value_box

    @FixME  #arguments("box")
    def opimpl_virtual_ref(self, box):
        # Details on the content of metainterp.virtualref_boxes:
        #
        #  * it's a list whose items go two by two, containing first the
        #    virtual box (e.g. the PyFrame) and then the vref box (e.g.
        #    the 'virtual_ref(frame)').
        #
        #  * if we detect that the virtual box escapes during tracing
        #    already (by generating a CALl_MAY_FORCE that marks the flags
        #    in the vref), then we replace the vref in the list with
        #    ConstPtr(NULL).
        #
        metainterp = self.metainterp
        if metainterp.is_blackholing():
            resbox = box      # good enough when blackholing
        else:
            vrefinfo = metainterp.staticdata.virtualref_info
            obj = box.getref_base()
            vref = vrefinfo.virtual_ref_during_tracing(obj)
            resbox = history.BoxPtr(vref)
            cindex = history.ConstInt(len(metainterp.virtualref_boxes) // 2)
            metainterp.history.record(rop.VIRTUAL_REF, [box, cindex], resbox)
            # Note: we allocate a JIT_VIRTUAL_REF here
            # (in virtual_ref_during_tracing()), in order to detect when
            # the virtual escapes during tracing already.  We record it as a
            # VIRTUAL_REF operation.  Later, optimizeopt.py should either kill
            # that operation or replace it with a NEW_WITH_VTABLE followed by
            # SETFIELD_GCs.
        metainterp.virtualref_boxes.append(box)
        metainterp.virtualref_boxes.append(resbox)
        self.make_result_box(resbox)

    @FixME  #arguments("box")
    def opimpl_virtual_ref_finish(self, box):
        # virtual_ref_finish() assumes that we have a stack-like, last-in
        # first-out order.
        metainterp = self.metainterp
        vrefbox = metainterp.virtualref_boxes.pop()
        lastbox = metainterp.virtualref_boxes.pop()
        assert box.getref_base() == lastbox.getref_base()
        if not metainterp.is_blackholing():
            vrefinfo = metainterp.staticdata.virtualref_info
            vref = vrefbox.getref_base()
            if vrefinfo.is_virtual_ref(vref):
                metainterp.history.record(rop.VIRTUAL_REF_FINISH,
                                          [vrefbox, lastbox], None)

    # ------------------------------

    def setup_call(self, argboxes):
        self.pc = 0
        count_i = count_r = count_f = 0
        for box in argboxes:
            if box.type == history.INT:
                self.registers_i[count_i] = box
                count_i += 1
            elif box.type == history.REF:
                self.registers_r[count_r] = box
                count_r += 1
            elif box.type == history.FLOAT:
                self.registers_f[count_f] = box
                count_f += 1
            else:
                raise AssertionError(box.type)

    def setup_resume_at_op(self, pc):
        self.pc = pc
        ##  values = ' '.join([box.repr_rpython() for box in self.env])
        ##  log('setup_resume_at_op  %s:%d [%s] %d' % (self.jitcode.name,
        ##                                             self.pc, values,
        ##                                             self.exception_target))

    def run_one_step(self):
        # Execute the frame forward.  This method contains a loop that leaves
        # whenever the 'opcode_implementations' (which is one of the 'opimpl_'
        # methods) returns True.  This is the case when the current frame
        # changes, due to a call or a return.
        try:
            staticdata = self.metainterp.staticdata
            while True:
                pc = self.pc
                op = ord(self.bytecode[pc])
                #print staticdata.opcode_names[op]
                staticdata.opcode_implementations[op](self, pc)
        except ChangeFrame:
            pass

    def generate_guard(self, opnum, box=None, extraargs=[], resumepc=-1):
        if isinstance(box, Const):    # no need for a guard
            return
        metainterp = self.metainterp
        if metainterp.is_blackholing():
            return
        if box is not None:
            moreargs = [box] + extraargs
        else:
            moreargs = list(extraargs)
        metainterp_sd = metainterp.staticdata
        original_greenkey = metainterp.resumekey.original_greenkey
        if opnum == rop.GUARD_NOT_FORCED:
            resumedescr = compile.ResumeGuardForcedDescr(metainterp_sd,
                                                         original_greenkey)
        else:
            resumedescr = compile.ResumeGuardDescr(metainterp_sd,
                                                   original_greenkey)
        guard_op = metainterp.history.record(opnum, moreargs, None,
                                             descr=resumedescr)       
        virtualizable_boxes = None
        if metainterp.staticdata.virtualizable_info is not None:
            virtualizable_boxes = metainterp.virtualizable_boxes
        saved_pc = self.pc
        if resumepc >= 0:
            self.pc = resumepc
        resume.capture_resumedata(metainterp.framestack, virtualizable_boxes,
                                  metainterp.virtualref_boxes, resumedescr)
        self.pc = saved_pc
        self.metainterp.staticdata.profiler.count_ops(opnum, GUARDS)
        # count
        metainterp.attach_debug_info(guard_op)
        return guard_op

    def implement_guard_value(self, orgpc, box):
        """Promote the given Box into a Const.  Note: be careful, it's a
        bit unclear what occurs if a single opcode needs to generate
        several ones and/or ones not near the beginning."""
        if isinstance(box, Const):
            return box     # no promotion needed, already a Const
        else:
            promoted_box = box.constbox()
            self.generate_guard(rop.GUARD_VALUE, box, [promoted_box],
                                resumepc=orgpc)
            self.metainterp.replace_box(box, promoted_box)
            return promoted_box

    def cls_of_box(self, box):
        return self.metainterp.cpu.ts.cls_of_box(box)

    @specialize.arg(1)
    def execute(self, opnum, *argboxes):
        return self.metainterp.execute_and_record(opnum, None, *argboxes)

    @specialize.arg(1)
    def execute_with_descr(self, opnum, descr, *argboxes):
        return self.metainterp.execute_and_record(opnum, descr, *argboxes)

    @specialize.arg(1)
    def execute_varargs(self, opnum, argboxes, descr, exc):
        resbox = self.metainterp.execute_and_record_varargs(opnum, argboxes,
                                                            descr=descr)
        self.make_result_of_lastop(resbox)
        # ^^^ this is done before handle_possible_exception() because we need
        # the box to show up in get_list_of_active_boxes()
        if exc:
            self.metainterp.handle_possible_exception()
        else:
            self.metainterp.assert_no_exception()
        return resbox

    def do_residual_call(self, funcbox, descr, argboxes):
        allboxes = [funcbox] + argboxes
        effectinfo = descr.get_extra_info()
        if (effectinfo is None or effectinfo.extraeffect ==
                                effectinfo.EF_FORCES_VIRTUAL_OR_VIRTUALIZABLE):
            # residual calls require attention to keep virtualizables in-sync
            self.metainterp.vable_and_vrefs_before_residual_call()
            # xxx do something about code duplication
            resbox = self.metainterp.execute_and_record_varargs(
                rop.CALL_MAY_FORCE, allboxes, descr=descr)
            self.metainterp.vable_and_vrefs_after_residual_call()
            if resbox is not None:
                self.make_result_box(resbox)
            self.generate_guard(self.pc, rop.GUARD_NOT_FORCED, None, [])
            if exc:
                return self.metainterp.handle_exception()
            else:
                return self.metainterp.assert_no_exception()
        else:
            effect = effectinfo.extraeffect
            if effect == effectinfo.EF_CANNOT_RAISE:
                opnum, exc = rop.CALL, False
            elif effect == effectinfo.EF_PURE:
                opnum, exc = rop.CALL_PURE, False
            elif effect == effectinfo.EF_LOOPINVARIANT:
                opnum, exc = rop.CALL_LOOPINVARIANT, True
            else:
                opnum, exc = rop.CALL, True
            return self.execute_varargs(opnum, allboxes, descr, exc)

    def do_residual_or_indirect_call(self, funcbox, calldescr, argboxes):
        """The 'residual_call' operation is emitted in two cases:
        when we have to generate a residual CALL operation, but also
        to handle an indirect_call that may need to be inlined."""
        assert isinstance(funcbox, Const)
        sd = self.metainterp.staticdata
        key = sd.cpu.ts.getaddr_for_box(funcbox)
        jitcode = sd.bytecode_for_address(key)
        if jitcode is not None:
            # we should follow calls to this graph
            return self.metainterp.perform_call(jitcode, argboxes)
        else:
            # but we should not follow calls to that graph
            return self.do_residual_call(funcbox, calldescr, argboxes)

# ____________________________________________________________

class MetaInterpStaticData(object):
    virtualizable_info = None
    logger_noopt = None
    logger_ops = None

    def __init__(self, cpu, options,
                 ProfilerClass=EmptyProfiler, warmrunnerdesc=None):
        self.cpu = cpu
        self.stats = self.cpu.stats
        self.options = options
        self.logger_noopt = Logger(self)
        self.logger_ops = Logger(self, guard_number=True)

        self.profiler = ProfilerClass()
        self.warmrunnerdesc = warmrunnerdesc

        backendmodule = self.cpu.__module__
        backendmodule = backendmodule.split('.')[-2]
        self.jit_starting_line = 'JIT starting (%s)' % backendmodule

        self._addr2name_keys = []
        self._addr2name_values = []

        self.__dict__.update(compile.make_done_loop_tokens())
        # store this information for fastpath of call_assembler
        d = self.loop_tokens_done_with_this_frame_int[0].finishdescr
        self.cpu.done_with_this_frame_int_v = self.cpu.get_fail_descr_number(d)

    def _freeze_(self):
        return True

    def setup_insns(self, insns):
        self.opcode_names = ['?'] * len(insns)
        self.opcode_implementations = [None] * len(insns)
        for key, value in insns.items():
            assert self.opcode_implementations[value] is None
            self.opcode_names[value] = key
            name, argcodes = key.split('/')
            opimpl = _get_opimpl_method(name, argcodes)
            self.opcode_implementations[value] = opimpl
        self.op_catch_exception = insns.get('catch_exception/L', -1)

    def setup_descrs(self, descrs):
        self.opcode_descrs = descrs

    def setup_indirectcalltargets(self, indirectcalltargets):
        self.indirectcalltargets = list(indirectcalltargets)

    def finish_setup(self, codewriter, optimizer=None):
        from pypy.jit.metainterp.blackhole import BlackholeInterpBuilder
        self.blackholeinterpbuilder = BlackholeInterpBuilder(codewriter, self)
        #
        asm = codewriter.assembler
        self.setup_insns(asm.insns)
        self.setup_descrs(asm.descrs)
        self.setup_indirectcalltargets(asm.indirectcalltargets)
        #
        self.portal_code = codewriter.mainjitcode
        RESULT = codewriter.portal_graph.getreturnvar().concretetype
        self.result_type = history.getkind(RESULT)
        #
        warmrunnerdesc = self.warmrunnerdesc
        if warmrunnerdesc is not None:
            self.num_green_args = warmrunnerdesc.num_green_args
            self.state = warmrunnerdesc.state
            if optimizer is not None:
                self.state.set_param_optimizer(optimizer)
        else:
            self.num_green_args = 0
            self.state = None
        self.globaldata = MetaInterpGlobalData(self)

    def _setup_once(self):
        """Runtime setup needed by the various components of the JIT."""
        if not self.globaldata.initialized:
            debug_print(self.jit_starting_line)
            self.cpu.setup_once()
            if not self.profiler.initialized:
                self.profiler.start()
                self.profiler.initialized = True
            self.globaldata.initialized = True

    def get_name_from_address(self, addr):
        # for debugging only
        if we_are_translated():
            d = self.globaldata.addr2name
            if d is None:
                # Build the dictionary at run-time.  This is needed
                # because the keys are function/class addresses, so they
                # can change from run to run.
                k = llmemory.cast_ptr_to_adr(self._portal_runner_ptr)
                d = {k: 'recursive call'}
                keys = self._addr2name_keys
                values = self._addr2name_values
                for i in range(len(keys)):
                    d[keys[i]] = values[i]
                self.globaldata.addr2name = d
            return d.get(addr, '')
        else:
            for i in range(len(self._addr2name_keys)):
                if addr == self._addr2name_keys[i]:
                    return self._addr2name_values[i]
            return ''

    def bytecode_for_address(self, fnaddress):
        if we_are_translated():
            d = self.globaldata.indirectcall_dict
            if d is None:
                # Build the dictionary at run-time.  This is needed
                # because the keys are function addresses, so they
                # can change from run to run.
                d = {}
                for jitcode in self.indirectcalltargets:
                    d[jitcode.fnaddr] = jitcode
                self.globaldata.indirectcall_dict = d
            return d.get(fnaddress, None)
        else:
            for jitcode in self.indirectcalltargets:
                if jitcode.fnaddr == fnaddress:
                    return jitcode
            return None

    # ---------------- logging ------------------------

    def log(self, msg):
        debug_print(msg)

# ____________________________________________________________

class MetaInterpGlobalData(object):
    """This object contains the JIT's global, mutable data.

    Warning: for any data that you put here, think that there might be
    multiple MetaInterps accessing it at the same time.  As usual we are
    safe from corruption thanks to the GIL, but keep in mind that any
    MetaInterp might modify any of these fields while another MetaInterp
    is, say, currently in a residual call to a function.  Multiple
    MetaInterps occur either with threads or, in single-threaded cases,
    with recursion.  This is a case that is not well-tested, so please
    be careful :-(  But thankfully this is one of the very few places
    where multiple concurrent MetaInterps may interact with each other.
    """
    def __init__(self, staticdata):
        self.initialized = False
        self.indirectcall_dict = None
        self.addr2name = None
        self.loopnumbering = 0
        self.resume_virtuals = {}
        #
        state = staticdata.state
        if state is not None:
            self.jit_cell_at_key = state.jit_cell_at_key
        else:
            # for tests only; not RPython
            class JitCell:
                compiled_merge_points = None
            _jitcell_dict = {}
            def jit_cell_at_key(greenkey):
                greenkey = tuple(greenkey)
                return _jitcell_dict.setdefault(greenkey, JitCell())
            self.jit_cell_at_key = jit_cell_at_key

    def get_compiled_merge_points(self, greenkey):
        cell = self.jit_cell_at_key(greenkey)
        if cell.compiled_merge_points is None:
            cell.compiled_merge_points = []
        return cell.compiled_merge_points

# ____________________________________________________________

class MetaInterp(object):
    in_recursion = 0

    def __init__(self, staticdata):
        self.staticdata = staticdata
        self.cpu = staticdata.cpu
        self.portal_trace_positions = []
        self.greenkey_of_huge_function = None
        self.free_frames_list = []
        self.last_exc_value_box = None

    def is_blackholing(self):
        return False       # XXX get rid of this method

    def perform_call(self, jitcode, boxes, greenkey=None):
        # causes the metainterp to enter the given subfunction
        f = self.newframe(jitcode, greenkey)
        f.setup_call(boxes)
        raise ChangeFrame

    def newframe(self, jitcode, greenkey=None):
        if jitcode is self.staticdata.portal_code:
            self.in_recursion += 1
        if greenkey is not None and not self.is_blackholing():
            self.portal_trace_positions.append(
                    (greenkey, len(self.history.operations)))
        if len(self.free_frames_list) > 0:
            f = self.free_frames_list.pop()
        else:
            f = MIFrame(self)
        f.setup(jitcode, greenkey)
        self.framestack.append(f)
        return f

    def popframe(self):
        frame = self.framestack.pop()
        if frame.jitcode is self.staticdata.portal_code:
            self.in_recursion -= 1
        if frame.greenkey is not None and not self.is_blackholing():
            self.portal_trace_positions.append(
                    (None, len(self.history.operations)))
        # we save the freed MIFrames to avoid needing to re-create new
        # MIFrame objects all the time; they are a bit big, with their
        # 3*256 register entries.
        frame.cleanup_registers()
        self.free_frames_list.append(frame)

    def finishframe(self, resultbox):
        self.popframe()
        if self.framestack:
            self.framestack[-1].make_result_of_lastop(resultbox)
            raise ChangeFrame
        else:
            if not self.is_blackholing():
                try:
                    self.compile_done_with_this_frame(resultbox)
                except GiveUp:
                    self.switch_to_blackhole(ABORT_BRIDGE)
            sd = self.staticdata
            if sd.result_type == 'void':
                assert resultbox is None
                raise sd.DoneWithThisFrameVoid()
            elif sd.result_type == 'int':
                raise sd.DoneWithThisFrameInt(resultbox.getint())
            elif sd.result_type == 'ref':
                raise sd.DoneWithThisFrameRef(self.cpu, resultbox.getref_base())
            elif sd.result_type == 'float':
                raise sd.DoneWithThisFrameFloat(resultbox.getfloat())
            else:
                assert False

    def finishframe_exception(self):
        # detect and propagate some exceptions early:
        #  - AssertionError
        #  - all subclasses of JitException
        excvaluebox = self.last_exc_value_box
        if we_are_translated():
            from pypy.jit.metainterp.warmspot import JitException
            e = self.cpu.ts.get_exception_obj(excvaluebox)
            if isinstance(e, JitException) or isinstance(e, AssertionError):
                raise Exception, e
        #
        while self.framestack:
            frame = self.framestack[-1]
            code = frame.bytecode
            position = frame.pc    # <-- just after the insn that raised
            if position < len(code):
                opcode = ord(code[position])
                if opcode == self.staticdata.op_catch_exception:
                    # found a 'catch_exception' instruction;
                    # jump to the handler
                    target = ord(code[position+1]) | (ord(code[position+2])<<8)
                    frame.pc = target
                    raise ChangeFrame
            self.popframe()
        if not self.is_blackholing():
            try:
                self.compile_exit_frame_with_exception(excvaluebox)
            except GiveUp:
                self.switch_to_blackhole(ABORT_BRIDGE)
        raise self.staticdata.ExitFrameWithExceptionRef(self.cpu, excvaluebox.getref_base())

    def check_recursion_invariant(self):
        in_recursion = -1
        for frame in self.framestack:
            jitcode = frame.jitcode
            if jitcode is self.staticdata.portal_code:
                in_recursion += 1
        if in_recursion != self.in_recursion:
            print "in_recursion problem!!!"
            print in_recursion, self.in_recursion
            for frame in self.framestack:
                jitcode = frame.jitcode
                if jitcode is self.staticdata.portal_code:
                    print "P",
                else:
                    print " ",
                print jitcode.name
            raise Exception

    def create_empty_history(self):
        warmrunnerstate = self.staticdata.state
        self.history = history.History()
        self.staticdata.stats.set_history(self.history)

    def _all_constants(self, *boxes):
        if len(boxes) == 0:
            return True
        return isinstance(boxes[0], Const) and self._all_constants(*boxes[1:])

    def _all_constants_varargs(self, boxes):
        for box in boxes:
            if not isinstance(box, Const):
                return False
        return True

    @specialize.arg(1)
    def execute_and_record(self, opnum, descr, *argboxes):
        history.check_descr(descr)
        assert not (rop._CANRAISE_FIRST <= opnum <= rop._CANRAISE_LAST)
        # execute the operation
        profiler = self.staticdata.profiler
        profiler.count_ops(opnum)
        resbox = executor.execute(self.cpu, self, opnum, descr, *argboxes)
        if rop._ALWAYS_PURE_FIRST <= opnum <= rop._ALWAYS_PURE_LAST:
            return self._record_helper_pure(opnum, resbox, descr, *argboxes)
        else:
            return self._record_helper_nonpure_varargs(opnum, resbox, descr,
                                                       list(argboxes))

    @specialize.arg(1)
    def execute_and_record_varargs(self, opnum, argboxes, descr=None):
        history.check_descr(descr)
        # execute the operation
        profiler = self.staticdata.profiler
        profiler.count_ops(opnum)
        resbox = executor.execute_varargs(self.cpu, self,
                                          opnum, argboxes, descr)
        if self.is_blackholing():
            profiler.count_ops(opnum, BLACKHOLED_OPS)
        else:
            # check if the operation can be constant-folded away
            argboxes = list(argboxes)
            if rop._ALWAYS_PURE_FIRST <= opnum <= rop._ALWAYS_PURE_LAST:
                resbox = self._record_helper_pure_varargs(opnum, resbox, descr, argboxes)
            else:
                resbox = self._record_helper_nonpure_varargs(opnum, resbox, descr, argboxes)
        return resbox

    def _record_helper_pure(self, opnum, resbox, descr, *argboxes): 
        canfold = self._all_constants(*argboxes)
        if canfold:
            resbox = resbox.constbox()       # ensure it is a Const
            return resbox
        else:
            resbox = resbox.nonconstbox()    # ensure it is a Box
            return self._record_helper_nonpure_varargs(opnum, resbox, descr, list(argboxes))

    def _record_helper_pure_varargs(self, opnum, resbox, descr, argboxes): 
        canfold = self._all_constants_varargs(argboxes)
        if canfold:
            resbox = resbox.constbox()       # ensure it is a Const
            return resbox
        else:
            resbox = resbox.nonconstbox()    # ensure it is a Box
            return self._record_helper_nonpure_varargs(opnum, resbox, descr, argboxes)

    def _record_helper_nonpure_varargs(self, opnum, resbox, descr, argboxes):
        assert resbox is None or isinstance(resbox, Box)
        # record the operation
        profiler = self.staticdata.profiler
        profiler.count_ops(opnum, RECORDED_OPS)        
        op = self.history.record(opnum, argboxes, resbox, descr)
        self.attach_debug_info(op)
        return resbox

    def attach_debug_info(self, op):
        if (not we_are_translated() and op is not None
            and getattr(self, 'framestack', None)):
            op.pc = self.framestack[-1].pc
            op.name = self.framestack[-1].jitcode.name

    def execute_raised(self, exception, constant=False):
        # Exception handling: when execute.do_call() gets an exception it
        # calls metainterp.execute_raised(), which puts it into
        # 'self.last_exc_value_box'.  This is used shortly afterwards
        # to generate either GUARD_EXCEPTION or GUARD_NO_EXCEPTION, and also
        # to handle the following opcodes 'goto_if_exception_mismatch'.
        llexception = get_llexception(self.cpu, exception)
        llexception = self.cpu.ts.cast_to_ref(llexception)
        exc_value_box = self.cpu.ts.get_exc_value_box(llexception)
        if constant:
            exc_value_box = exc_value_box.constbox()
        self.last_exc_value_box = exc_value_box
        self.class_of_last_exc_is_const = constant
        # 'class_of_last_exc_is_const' means that the class of the value
        # stored in the exc_value Box can be assumed to be a Const.  This
        # is only True after a GUARD_EXCEPTION or GUARD_CLASS.

    def execute_did_not_raise(self):
        self.last_exc_value_box = None

    def switch_to_blackhole(self, reason):
        XXX
        self.staticdata.profiler.count(reason)
        debug_print('~~~ ABORTING TRACING')
        debug_stop('jit-tracing')
        debug_start('jit-blackhole')
        self.history = None   # start blackholing
        self.staticdata.stats.aborted()
        self.staticdata.profiler.end_tracing()
        self.staticdata.profiler.start_blackhole()
    switch_to_blackhole._dont_inline_ = True

    def switch_to_blackhole_if_trace_too_long(self):
        if not self.is_blackholing():
            warmrunnerstate = self.staticdata.state
            if len(self.history.operations) > warmrunnerstate.trace_limit:
                self.greenkey_of_huge_function = self.find_biggest_function()
                self.portal_trace_positions = None
                self.switch_to_blackhole(ABORT_TOO_LONG)

    def _interpret(self):
        # Execute the frames forward until we raise a DoneWithThisFrame,
        # a ContinueRunningNormally, or a GenerateMergePoint exception.
        self.staticdata.stats.entered()
        try:
            while True:
                self.framestack[-1].run_one_step()
                self.switch_to_blackhole_if_trace_too_long()
                if not we_are_translated():
                    self.check_recursion_invariant()
        finally:
            if self.is_blackholing():
                self.staticdata.profiler.end_blackhole()
            else:
                self.staticdata.profiler.end_tracing()

    def interpret(self):
        if we_are_translated():
            self._interpret()
        else:
            try:
                self._interpret()
            except:
                import sys
                if sys.exc_info()[0] is not None:
                    self.staticdata.log(sys.exc_info()[0].__name__)
                raise

    def compile_and_run_once(self, *args):
        debug_start('jit-tracing')
        self.staticdata._setup_once()
        self.create_empty_history()
        try:
            return self._compile_and_run_once(*args)
        finally:
            debug_stop('jit-tracing')

    def _compile_and_run_once(self, *args):
        original_boxes = self.initialize_state_from_start(*args)
        self.current_merge_points = [(original_boxes, 0)]
        num_green_args = self.staticdata.num_green_args
        original_greenkey = original_boxes[:num_green_args]
        redkey = original_boxes[num_green_args:]
        self.resumekey = compile.ResumeFromInterpDescr(original_greenkey,
                                                       redkey)
        self.seen_can_enter_jit = False
        try:
            self.interpret()
            assert False, "should always raise"
        except GenerateMergePoint, gmp:
            return self.designate_target_loop(gmp)

    def handle_guard_failure(self, key):
        assert isinstance(key, compile.ResumeGuardDescr)
        self.initialize_state_from_guard_failure(key)
        try:
            return self._handle_guard_failure(key)
        finally:
            debug_stop('jit-tracing')

    def _handle_guard_failure(self, key):
        from pypy.jit.metainterp.warmspot import ContinueRunningNormallyBase
        original_greenkey = key.original_greenkey
        # notice that here we just put the greenkey
        # use -1 to mark that we will have to give up
        # because we cannot reconstruct the beginning of the proper loop
        self.current_merge_points = [(original_greenkey, -1)]
        self.resumekey = key
        self.seen_can_enter_jit = False
        try:
            self.prepare_resume_from_failure(key.guard_opnum)
            self.interpret()
            assert False, "should always raise"
        except GenerateMergePoint, gmp:
            return self.designate_target_loop(gmp)
        except ContinueRunningNormallyBase:
            key.reset_counter_from_failure(self)
            raise

    def remove_consts_and_duplicates(self, boxes, endindex, duplicates):
        for i in range(endindex):
            box = boxes[i]
            if isinstance(box, Const) or box in duplicates:
                oldbox = box
                box = oldbox.clonebox()
                boxes[i] = box
                self.history.record(rop.SAME_AS, [oldbox], box)
            else:
                duplicates[box] = None

    def reached_can_enter_jit(self, greenboxes, redboxes):
        duplicates = {}
        self.remove_consts_and_duplicates(redboxes, len(redboxes),
                                          duplicates)
        live_arg_boxes = greenboxes + redboxes
        if self.staticdata.virtualizable_info is not None:
            # we use pop() to remove the last item, which is the virtualizable
            # itself
            self.remove_consts_and_duplicates(self.virtualizable_boxes,
                                              len(self.virtualizable_boxes)-1,
                                              duplicates)
            live_arg_boxes += self.virtualizable_boxes
            live_arg_boxes.pop()
        assert len(self.virtualref_boxes) == 0, "missing virtual_ref_finish()?"
        # Called whenever we reach the 'can_enter_jit' hint.
        # First, attempt to make a bridge:
        # - if self.resumekey is a ResumeGuardDescr, it starts from a guard
        #   that failed;
        # - if self.resumekey is a ResumeFromInterpDescr, it starts directly
        #   from the interpreter.
        self.compile_bridge(live_arg_boxes)
        # raises in case it works -- which is the common case, hopefully,
        # at least for bridges starting from a guard.

        # Search in current_merge_points for original_boxes with compatible
        # green keys, representing the beginning of the same loop as the one
        # we end now. 
       
        for j in range(len(self.current_merge_points)-1, -1, -1):
            original_boxes, start = self.current_merge_points[j]
            assert len(original_boxes) == len(live_arg_boxes) or start < 0
            for i in range(self.staticdata.num_green_args):
                box1 = original_boxes[i]
                box2 = live_arg_boxes[i]
                assert isinstance(box1, Const)
                if not box1.same_constant(box2):
                    break
            else:
                # Found!  Compile it as a loop.
                if start < 0:
                    # we cannot reconstruct the beginning of the proper loop
                    raise GiveUp

                # raises in case it works -- which is the common case
                self.compile(original_boxes, live_arg_boxes, start)
                # creation of the loop was cancelled!
                self.staticdata.log('cancelled, going on...')

        # Otherwise, no loop found so far, so continue tracing.
        start = len(self.history.operations)
        self.current_merge_points.append((live_arg_boxes, start))

    def designate_target_loop(self, gmp):
        loop_token = gmp.target_loop_token
        num_green_args = self.staticdata.num_green_args
        residual_args = self.get_residual_args(loop_token.specnodes,
                                               gmp.argboxes[num_green_args:])
        history.set_future_values(self.cpu, residual_args)
        return loop_token

    def prepare_resume_from_failure(self, opnum):
        frame = self.framestack[-1]
        if opnum == rop.GUARD_TRUE:     # a goto_if_not that jumps only now
            frame.pc = frame.jitcode.follow_jump(frame.pc)
        elif opnum == rop.GUARD_FALSE:     # a goto_if_not that stops jumping
            pass
        elif opnum == rop.GUARD_VALUE or opnum == rop.GUARD_CLASS:
            pass        # the pc is already set to the *start* of the opcode
        elif (opnum == rop.GUARD_NONNULL or
              opnum == rop.GUARD_ISNULL or
              opnum == rop.GUARD_NONNULL_CLASS):
            pass        # the pc is already set to the *start* of the opcode
        elif opnum == rop.GUARD_NO_EXCEPTION or opnum == rop.GUARD_EXCEPTION:
            exception = self.cpu.grab_exc_value()
            if exception:
                if not we_are_translated():
                    from pypy.rpython.lltypesystem import rclass
                    etype = rclass.ll_type(exception)
                    exception = LLException(etype, exception)
                self.execute_raised(exception)
            else:
                self.execute_did_not_raise()
            try:
                self.handle_possible_exception()
            except ChangeFrame:
                pass
        elif opnum == rop.GUARD_NO_OVERFLOW:   # an overflow now detected
            self.execute_raised(OverflowError(), constant=True)
            try:
                self.finishframe_exception()
            except ChangeFrame:
                pass
        elif opnum == rop.GUARD_OVERFLOW:      # no longer overflowing
            self.execute_did_not_raise()
        else:
            from pypy.jit.metainterp.resoperation import opname
            raise NotImplementedError(opname[opnum])

    def compile(self, original_boxes, live_arg_boxes, start):
        num_green_args = self.staticdata.num_green_args
        self.history.inputargs = original_boxes[num_green_args:]
        greenkey = original_boxes[:num_green_args]
        glob = self.staticdata.globaldata
        old_loop_tokens = glob.get_compiled_merge_points(greenkey)
        self.history.record(rop.JUMP, live_arg_boxes[num_green_args:], None)
        loop_token = compile.compile_new_loop(self, old_loop_tokens,
                                              greenkey, start)
        if loop_token is not None: # raise if it *worked* correctly
            raise GenerateMergePoint(live_arg_boxes, loop_token)
        self.history.operations.pop()     # remove the JUMP

    def compile_bridge(self, live_arg_boxes):
        num_green_args = self.staticdata.num_green_args
        greenkey = live_arg_boxes[:num_green_args]
        glob = self.staticdata.globaldata
        old_loop_tokens = glob.get_compiled_merge_points(greenkey)
        if len(old_loop_tokens) == 0:
            return
        self.history.record(rop.JUMP, live_arg_boxes[num_green_args:], None)
        target_loop_token = compile.compile_new_bridge(self, old_loop_tokens,
                                                       self.resumekey)
        if target_loop_token is not None:   # raise if it *worked* correctly
            raise GenerateMergePoint(live_arg_boxes, target_loop_token)
        self.history.operations.pop()     # remove the JUMP

    def compile_done_with_this_frame(self, exitbox):
        self.gen_store_back_in_virtualizable()
        # temporarily put a JUMP to a pseudo-loop
        sd = self.staticdata
        if sd.result_type == 'void':
            assert exitbox is None
            exits = []
            loop_tokens = sd.loop_tokens_done_with_this_frame_void
        elif sd.result_type == 'int':
            exits = [exitbox]
            loop_tokens = sd.loop_tokens_done_with_this_frame_int
        elif sd.result_type == 'ref':
            exits = [exitbox]
            loop_tokens = sd.loop_tokens_done_with_this_frame_ref
        elif sd.result_type == 'float':
            exits = [exitbox]
            loop_tokens = sd.loop_tokens_done_with_this_frame_float
        else:
            assert False
        self.history.record(rop.JUMP, exits, None)
        target_loop_token = compile.compile_new_bridge(self, loop_tokens,
                                                       self.resumekey)
        assert target_loop_token is loop_tokens[0]

    def compile_exit_frame_with_exception(self, valuebox):
        self.gen_store_back_in_virtualizable()
        # temporarily put a JUMP to a pseudo-loop
        self.history.record(rop.JUMP, [valuebox], None)
        sd = self.staticdata
        loop_tokens = sd.loop_tokens_exit_frame_with_exception_ref
        target_loop_token = compile.compile_new_bridge(self, loop_tokens,
                                                       self.resumekey)
        assert target_loop_token is loop_tokens[0]

    def get_residual_args(self, specnodes, args):
        if specnodes is None:     # it is None only for tests
            return args
        assert len(specnodes) == len(args)
        expanded_args = []
        for i in range(len(specnodes)):
            specnode = specnodes[i]
            specnode.extract_runtime_data(self.cpu, args[i], expanded_args)
        return expanded_args

    def _initialize_from_start(self, original_boxes, num_green_args, *args):
        if args:
            from pypy.jit.metainterp.warmstate import wrap
            box = wrap(self.cpu, args[0], num_green_args > 0)
            original_boxes.append(box)
            self._initialize_from_start(original_boxes, num_green_args-1,
                                        *args[1:])

    def initialize_state_from_start(self, *args):
        self.in_recursion = -1 # always one portal around
        self.staticdata.profiler.start_tracing()
        num_green_args = self.staticdata.num_green_args
        original_boxes = []
        self._initialize_from_start(original_boxes, num_green_args, *args)
        # ----- make a new frame -----
        self.framestack = []
        f = self.newframe(self.staticdata.portal_code)
        f.setup_call(original_boxes)
        self.virtualref_boxes = []
        self.initialize_virtualizable(original_boxes)
        return original_boxes

    def initialize_state_from_guard_failure(self, resumedescr):
        # guard failure: rebuild a complete MIFrame stack
        debug_start('jit-tracing')
        self.staticdata.profiler.start_tracing()
        self.in_recursion = -1 # always one portal around
        self.history = history.History()
        inputargs_and_holes = self.rebuild_state_after_failure(resumedescr)
        self.history.inputargs = [box for box in inputargs_and_holes if box]

    def initialize_virtualizable(self, original_boxes):
        vinfo = self.staticdata.virtualizable_info
        if vinfo is not None:
            virtualizable_box = original_boxes[vinfo.index_of_virtualizable]
            virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
            # The field 'virtualizable_boxes' is not even present
            # if 'virtualizable_info' is None.  Check for that first.
            self.virtualizable_boxes = vinfo.read_boxes(self.cpu,
                                                        virtualizable)
            original_boxes += self.virtualizable_boxes
            self.virtualizable_boxes.append(virtualizable_box)
            self.initialize_virtualizable_enter()

    def initialize_virtualizable_enter(self):
        vinfo = self.staticdata.virtualizable_info
        virtualizable_box = self.virtualizable_boxes[-1]
        virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
        vinfo.clear_vable_token(virtualizable)

    def vable_and_vrefs_before_residual_call(self):
        if self.is_blackholing():
            return
        #
        vrefinfo = self.staticdata.virtualref_info
        for i in range(1, len(self.virtualref_boxes), 2):
            vrefbox = self.virtualref_boxes[i]
            vref = vrefbox.getref_base()
            vrefinfo.tracing_before_residual_call(vref)
            # the FORCE_TOKEN is already set at runtime in each vref when
            # it is created, by optimizeopt.py.
        #
        vinfo = self.staticdata.virtualizable_info
        if vinfo is not None:
            virtualizable_box = self.virtualizable_boxes[-1]
            virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
            vinfo.tracing_before_residual_call(virtualizable)
            #
            force_token_box = history.BoxInt()
            self.history.record(rop.FORCE_TOKEN, [], force_token_box)
            self.history.record(rop.SETFIELD_GC, [virtualizable_box,
                                                  force_token_box],
                                None, descr=vinfo.vable_token_descr)

    def vable_and_vrefs_after_residual_call(self):
        if self.is_blackholing():
            escapes = True
        else:
            escapes = False
            #
            vrefinfo = self.staticdata.virtualref_info
            for i in range(0, len(self.virtualref_boxes), 2):
                virtualbox = self.virtualref_boxes[i]
                vrefbox = self.virtualref_boxes[i+1]
                vref = vrefbox.getref_base()
                if vrefinfo.tracing_after_residual_call(vref):
                    # this vref was really a virtual_ref, but it escaped
                    # during this CALL_MAY_FORCE.  Mark this fact by
                    # generating a VIRTUAL_REF_FINISH on it and replacing
                    # it by ConstPtr(NULL).
                    self.stop_tracking_virtualref(i)
            #
            vinfo = self.staticdata.virtualizable_info
            if vinfo is not None:
                virtualizable_box = self.virtualizable_boxes[-1]
                virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
                if vinfo.tracing_after_residual_call(virtualizable):
                    # the virtualizable escaped during CALL_MAY_FORCE.
                    escapes = True
            #
            if escapes:
                self.switch_to_blackhole(ABORT_ESCAPE)
        #
        if escapes:
            self.load_fields_from_virtualizable()

    def stop_tracking_virtualref(self, i):
        virtualbox = self.virtualref_boxes[i]
        vrefbox = self.virtualref_boxes[i+1]
        # record VIRTUAL_REF_FINISH just before the current CALL_MAY_FORCE
        call_may_force_op = self.history.operations.pop()
        assert call_may_force_op.opnum == rop.CALL_MAY_FORCE
        self.history.record(rop.VIRTUAL_REF_FINISH,
                            [vrefbox, virtualbox], None)
        self.history.operations.append(call_may_force_op)
        # mark by replacing it with ConstPtr(NULL)
        self.virtualref_boxes[i+1] = self.cpu.ts.CONST_NULL

    def handle_possible_exception(self):
        frame = self.framestack[-1]
        if self.last_exc_value_box is not None:
            exception_box = self.cpu.ts.cls_of_box(self.last_exc_value_box)
            op = frame.generate_guard(rop.GUARD_EXCEPTION,
                                      None, [exception_box])
            assert op is not None
            op.result = self.last_exc_value_box
            self.class_of_last_exc_is_const = True
            self.finishframe_exception()
        else:
            frame.generate_guard(rop.GUARD_NO_EXCEPTION, None, [])

    def handle_possible_overflow_error(self):
        frame = self.framestack[-1]
        if self.last_exc_value_box is not None:
            frame.generate_guard(rop.GUARD_OVERFLOW, None)
            assert isinstance(self.last_exc_value_box, Const)
            assert self.class_of_last_exc_is_const
            self.finishframe_exception()
        else:
            frame.generate_guard(rop.GUARD_NO_OVERFLOW, None)

    def assert_no_exception(self):
        assert self.last_exc_value_box is None

    def rebuild_state_after_failure(self, resumedescr):
        vinfo = self.staticdata.virtualizable_info
        self.framestack = []
        expect_virtualizable = vinfo is not None
        boxlists = resume.rebuild_from_resumedata(self, resumedescr,
                                                  expect_virtualizable)
        #inputargs_and_holes, virtualizable_boxes, virtualref_boxes = boxlists
        inputargs_and_holes = boxlists    # XXX
        virtualref_boxes = []             # XXX
        #
        # virtual refs: make the vrefs point to the freshly allocated virtuals
        self.virtualref_boxes = virtualref_boxes
        vrefinfo = self.staticdata.virtualref_info
        for i in range(0, len(virtualref_boxes), 2):
            virtualbox = virtualref_boxes[i]
            vrefbox = virtualref_boxes[i+1]
            vrefinfo.continue_tracing(vrefbox.getref_base(),
                                      virtualbox.getref_base())
        #
        # virtualizable: synchronize the real virtualizable and the local
        # boxes, in whichever direction is appropriate
        if expect_virtualizable:
            self.virtualizable_boxes = virtualizable_boxes
            if 0:  ## self._already_allocated_resume_virtuals is not None:
                # resuming from a ResumeGuardForcedDescr: load the new values
                # currently stored on the virtualizable fields
                self.load_fields_from_virtualizable()
                return
            # just jumped away from assembler (case 4 in the comment in
            # virtualizable.py) into tracing (case 2); check that vable_token
            # is and stays 0.  Note the call to reset_vable_token() in
            # warmstate.py.
            virtualizable_box = self.virtualizable_boxes[-1]
            virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
            assert not virtualizable.vable_token
            if 0:  ## self._already_allocated_resume_virtuals is not None:
                # resuming from a ResumeGuardForcedDescr: load the new values
                # currently stored on the virtualizable fields
                self.load_fields_from_virtualizable()
            else:
                # normal case: fill the virtualizable with the local boxes
                self.synchronize_virtualizable()
        return inputargs_and_holes

    def check_synchronized_virtualizable(self):
        if not we_are_translated():
            vinfo = self.staticdata.virtualizable_info
            virtualizable_box = self.virtualizable_boxes[-1]
            virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
            vinfo.check_boxes(virtualizable, self.virtualizable_boxes)

    def synchronize_virtualizable(self):
        vinfo = self.staticdata.virtualizable_info
        virtualizable_box = self.virtualizable_boxes[-1]
        virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
        vinfo.write_boxes(virtualizable, self.virtualizable_boxes)

    def load_fields_from_virtualizable(self):
        # Force a reload of the virtualizable fields into the local
        # boxes (called only in escaping cases)
        assert self.is_blackholing()
        vinfo = self.staticdata.virtualizable_info
        if vinfo is not None:
            virtualizable_box = self.virtualizable_boxes[-1]
            virtualizable = vinfo.unwrap_virtualizable_box(virtualizable_box)
            self.virtualizable_boxes = vinfo.read_boxes(self.cpu,
                                                        virtualizable)
            self.virtualizable_boxes.append(virtualizable_box)

    def gen_store_back_in_virtualizable(self):
        vinfo = self.staticdata.virtualizable_info
        if vinfo is not None:
            # xxx only write back the fields really modified
            vbox = self.virtualizable_boxes[-1]
            for i in range(vinfo.num_static_extra_boxes):
                fieldbox = self.virtualizable_boxes[i]
                descr = vinfo.static_field_descrs[i]
                self.execute_and_record(rop.SETFIELD_GC, descr, vbox, fieldbox)
            i = vinfo.num_static_extra_boxes
            virtualizable = vinfo.unwrap_virtualizable_box(vbox)
            for k in range(vinfo.num_arrays):
                descr = vinfo.array_field_descrs[k]
                abox = self.execute_and_record(rop.GETFIELD_GC, descr, vbox)
                descr = vinfo.array_descrs[k]
                for j in range(vinfo.get_array_length(virtualizable, k)):
                    itembox = self.virtualizable_boxes[i]
                    i += 1
                    self.execute_and_record(rop.SETARRAYITEM_GC, descr,
                                            abox, ConstInt(j), itembox)
            assert i + 1 == len(self.virtualizable_boxes)

    def gen_load_from_other_virtualizable(self, vbox):
        vinfo = self.staticdata.virtualizable_info
        boxes = []
        assert vinfo is not None
        for i in range(vinfo.num_static_extra_boxes):
            descr = vinfo.static_field_descrs[i]
            boxes.append(self.execute_and_record(rop.GETFIELD_GC, descr, vbox))
        virtualizable = vinfo.unwrap_virtualizable_box(vbox)
        for k in range(vinfo.num_arrays):
            descr = vinfo.array_field_descrs[k]
            abox = self.execute_and_record(rop.GETFIELD_GC, descr, vbox)
            descr = vinfo.array_descrs[k]
            for j in range(vinfo.get_array_length(virtualizable, k)):
                boxes.append(self.execute_and_record(rop.GETARRAYITEM_GC, descr,
                                                     abox, ConstInt(j)))
        return boxes

    def replace_box(self, oldbox, newbox):
        assert isinstance(oldbox, Box)
        for frame in self.framestack:
            frame.replace_active_box_in_frame(oldbox, newbox)
        boxes = self.virtualref_boxes
        for i in range(len(boxes)):
            if boxes[i] is oldbox:
                boxes[i] = newbox
        if self.staticdata.virtualizable_info is not None:
            boxes = self.virtualizable_boxes
            for i in range(len(boxes)):
                if boxes[i] is oldbox:
                    boxes[i] = newbox

    def find_biggest_function(self):
        assert not self.is_blackholing()

        start_stack = []
        max_size = 0
        max_key = None
        for pair in self.portal_trace_positions:
            key, pos = pair
            if key is not None:
                start_stack.append(pair)
            else:
                greenkey, startpos = start_stack.pop()
                size = pos - startpos
                if size > max_size:
                    max_size = size
                    max_key = greenkey
        if start_stack:
            key, pos = start_stack[0]
            size = len(self.history.operations) - pos
            if size > max_size:
                max_size = size
                max_key = key
        return max_key

    def direct_assembler_call(self, pc, varargs, token, call_position):
        """ Generate a direct call to assembler for portal entry point.
        """
        assert not self.is_blackholing() # XXX
        num_green_args = self.staticdata.num_green_args
        args = varargs[num_green_args + 1:]
        resbox = self.history.operations[call_position].result
        rest = self.history.slice_history_at(call_position)
        if self.staticdata.virtualizable_info is not None:
            vindex = self.staticdata.virtualizable_info.index_of_virtualizable
            vbox = args[vindex - num_green_args]
            args += self.gen_load_from_other_virtualizable(vbox)
        self.history.record(rop.CALL_ASSEMBLER, args[:], resbox, descr=token)
        self.history.operations += rest

class GenerateMergePoint(Exception):
    def __init__(self, args, target_loop_token):
        assert target_loop_token is not None
        self.argboxes = args
        self.target_loop_token = target_loop_token

# ____________________________________________________________

class ChangeFrame(Exception):
    """Raised after we mutated metainterp.framestack, in order to force
    it to reload the current top-of-stack frame that gets interpreted."""

def _get_opimpl_method(name, argcodes):
    from pypy.jit.metainterp.blackhole import signedord
    #
    def handler(self, position):
        assert position >= 0
        args = ()
        next_argcode = 0
        code = self.bytecode
        orgpc = position
        position += 1
        for argtype in argtypes:
            if argtype == "box":     # a box, of whatever type
                argcode = argcodes[next_argcode]
                next_argcode = next_argcode + 1
                if argcode == 'i':
                    value = self.registers_i[ord(code[position])]
                elif argcode == 'c':
                    value = ConstInt(signedord(code[position]))
                elif argcode == 'r':
                    value = self.registers_r[ord(code[position])]
                elif argcode == 'f':
                    value = self.registers_f[ord(code[position])]
                else:
                    raise AssertionError("bad argcode")
                position += 1
            elif argtype == "descr" or argtype == "jitcode":
                assert argcodes[next_argcode] == 'd'
                next_argcode = next_argcode + 1
                index = ord(code[position]) | (ord(code[position+1])<<8)
                value = self.metainterp.staticdata.opcode_descrs[index]
                if argtype == "jitcode":
                    assert isinstance(value, JitCode)
                position += 2
            elif argtype == "label":
                assert argcodes[next_argcode] == 'L'
                next_argcode = next_argcode + 1
                value = ord(code[position]) | (ord(code[position+1])<<8)
                position += 2
            elif argtype == "boxes":     # a list of boxes of some type
                length = ord(code[position])
                value = [None] * length
                self.prepare_list_of_boxes(value, 0, position,
                                           argcodes[next_argcode])
                next_argcode = next_argcode + 1
                position += 1 + length
            elif argtype == "boxes2":     # two lists of boxes merged into one
                length1 = ord(code[position])
                position2 = position + 1 + length1
                length2 = ord(code[position2])
                value = [None] * (length1 + length2)
                self.prepare_list_of_boxes(value, 0, position,
                                           argcodes[next_argcode])
                self.prepare_list_of_boxes(value, length1, position2,
                                           argcodes[next_argcode + 1])
                next_argcode = next_argcode + 2
                position = position2 + 1 + length2
            elif argtype == "boxes3":    # three lists of boxes merged into one
                length1 = ord(code[position])
                position2 = position + 1 + length1
                length2 = ord(code[position2])
                position3 = position2 + 1 + length2
                length3 = ord(code[position3])
                value = [None] * (length1 + length2 + length3)
                self.prepare_list_of_boxes(value, 0, position,
                                           argcodes[next_argcode])
                self.prepare_list_of_boxes(value, length1, position2,
                                           argcodes[next_argcode + 1])
                self.prepare_list_of_boxes(value, length1 + length2, position3,
                                           argcodes[next_argcode + 2])
                next_argcode = next_argcode + 3
                position = position3 + 1 + length3
            elif argtype == "orgpc":
                value = orgpc
            else:
                raise AssertionError("bad argtype: %r" % (argtype,))
            args += (value,)
        #
        num_return_args = len(argcodes) - next_argcode
        assert num_return_args == 0 or num_return_args == 2
        if num_return_args:
            # Save the type of the resulting box.  This is needed if there is
            # a get_list_of_active_boxes().  See comments there.
            self._result_argcode = argcodes[next_argcode + 1]
            position += 1
        else:
            self._result_argcode = 'v'
        self.pc = position
        #
        if not we_are_translated():
            print '\tpyjitpl: %s(%s)' % (name, ', '.join(map(repr, args))),
            try:
                resultbox = unboundmethod(self, *args)
            except Exception, e:
                print '-> %s!' % e.__class__.__name__
                raise
            if num_return_args == 0:
                print
                assert resultbox is None
            else:
                print '-> %r' % (resultbox,)
                assert argcodes[next_argcode] == '>'
                result_argcode = argcodes[next_argcode + 1]
                assert resultbox.type == {'i': history.INT,
                                          'r': history.REF,
                                          'f': history.FLOAT}[result_argcode]
        else:
            resultbox = unboundmethod(self, *args)
        #
        self.make_result_of_lastop(resultbox)
    #
    unboundmethod = getattr(MIFrame, 'opimpl_' + name).im_func
    argtypes = unrolling_iterable(unboundmethod.argtypes)
    handler.func_name = 'handler_' + name
    return handler
