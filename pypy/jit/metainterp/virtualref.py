from pypy.rpython.rmodel import inputconst, log
from pypy.rpython.lltypesystem import lltype, llmemory, rclass
from pypy.jit.metainterp import history, jitframe
from pypy.jit.codewriter import heaptracker
from pypy.rlib.jit import InvalidVirtualRef

class VirtualRefInfo:

    def __init__(self, warmrunnerdesc):
        self.warmrunnerdesc = warmrunnerdesc
        self.cpu = warmrunnerdesc.cpu
        self.TOKEN_TRACING_RESCALL = self.cpu.TOKEN_TRACING_RESCALL
        # we make the low-level type of an RPython class directly
        self.JIT_VIRTUAL_REF = lltype.GcStruct('JitVirtualRef',
            ('super', rclass.OBJECT),
            ('jit_frame', llmemory.GCREF),
            ('forced', rclass.OBJECTPTR))
        self.jit_virtual_ref_vtable = lltype.malloc(rclass.OBJECT_VTABLE,
                                                    zero=True, flavor='raw',
                                                    immortal=True)
        self.jit_virtual_ref_vtable.name = rclass.alloc_array_name(
            'jit_virtual_ref')
        # build some constants
        adr = llmemory.cast_ptr_to_adr(self.jit_virtual_ref_vtable)
        adr = heaptracker.adr2int(adr)
        self.jit_virtual_ref_const_class = history.ConstInt(adr)
        fielddescrof = self.cpu.fielddescrof
        self.descr_jit_frame = fielddescrof(self.JIT_VIRTUAL_REF, 'jit_frame')
        self.descr_forced = fielddescrof(self.JIT_VIRTUAL_REF, 'forced')
        #
        # record the type JIT_VIRTUAL_REF explicitly in the rtyper, too
        if hasattr(self.warmrunnerdesc, 'rtyper'):    # <-- for tests
            self.warmrunnerdesc.rtyper.set_type_for_typeptr(
                self.jit_virtual_ref_vtable, self.JIT_VIRTUAL_REF)

    def _freeze_(self):
        return True

    def replace_force_virtual_with_call(self, graphs):
        # similar to rvirtualizable2.replace_force_virtualizable_with_call().
        c_force_virtual_ptr = None
        c_is_virtual_ptr = None
        force_virtual_count = 0
        for graph in graphs:
            for block in graph.iterblocks():
                for op in block.operations:
                    if op.opname == 'jit_force_virtual':
                        # first compute c_funcptr, but only if there is any
                        # 'jit_force_virtual' around
                        if c_force_virtual_ptr is None:
                            c_force_virtual_ptr = self.get_force_virtual_fnptr()
                        #
                        op.opname = 'direct_call'
                        op.args = [c_force_virtual_ptr, op.args[0]]
                        force_virtual_count += 1
                    #
                    if op.opname == 'jit_is_virtual':
                        if c_is_virtual_ptr is None:
                            c_is_virtual_ptr = self.get_is_virtual_fnptr()
                        #
                        op.opname = 'direct_call'
                        op.args = [c_is_virtual_ptr, op.args[0]]
        #
        if c_force_virtual_ptr is not None:
            log("replaced %d 'jit_force_virtual' with %r" % (force_virtual_count,
                                                             c_force_virtual_ptr.value))

    # ____________________________________________________________

    def virtual_ref_during_tracing(self, real_object):
        assert real_object
        vref = lltype.malloc(self.JIT_VIRTUAL_REF)
        p = lltype.cast_pointer(rclass.OBJECTPTR, vref)
        p.typeptr = self.jit_virtual_ref_vtable
        vref.jit_frame = jitframe.TOKEN_NONE
        vref.forced = lltype.cast_opaque_ptr(rclass.OBJECTPTR, real_object)
        return lltype.cast_opaque_ptr(llmemory.GCREF, vref)

    def is_virtual_ref(self, gcref):
        if not gcref:
            return False
        inst = lltype.cast_opaque_ptr(rclass.OBJECTPTR, gcref)
        return inst.typeptr == self.jit_virtual_ref_vtable

    def tracing_before_residual_call(self, gcref):
        if not self.is_virtual_ref(gcref):
            return
        vref = lltype.cast_opaque_ptr(lltype.Ptr(self.JIT_VIRTUAL_REF), gcref)
        assert vref.jit_frame == jitframe.TOKEN_NONE
        vref.jit_frame = self.TOKEN_TRACING_RESCALL

    def tracing_after_residual_call(self, gcref):
        if not self.is_virtual_ref(gcref):
            return False
        vref = lltype.cast_opaque_ptr(lltype.Ptr(self.JIT_VIRTUAL_REF), gcref)
        assert vref.forced
        if vref.jit_frame != jitframe.TOKEN_NONE:
            # not modified by the residual call; assert that it is still
            # set to TOKEN_TRACING_RESCALL and clear it.
            assert vref.jit_frame == self.TOKEN_TRACING_RESCALL
            vref.jit_frame = jitframe.TOKEN_NONE
            return False
        else:
            # marker "modified during residual call" set.
            return True

    def continue_tracing(self, gcref, real_object):
        if not self.is_virtual_ref(gcref):
            return
        assert real_object
        vref = lltype.cast_opaque_ptr(lltype.Ptr(self.JIT_VIRTUAL_REF), gcref)
        assert vref.jit_frame != self.TOKEN_TRACING_RESCALL
        vref.jit_frame = jitframe.TOKEN_NONE
        vref.forced = lltype.cast_opaque_ptr(rclass.OBJECTPTR, real_object)

    # ____________________________________________________________

    def get_force_virtual_fnptr(self):
        #
        def force_virtual_if_necessary(inst):
            if not inst or inst.typeptr != self.jit_virtual_ref_vtable:
                return inst    # common, fast case
            return self.force_virtual(inst)
        #
        FUNC = lltype.FuncType([rclass.OBJECTPTR], rclass.OBJECTPTR)
        funcptr = self.warmrunnerdesc.helper_func(
            lltype.Ptr(FUNC),
            force_virtual_if_necessary)
        return inputconst(lltype.typeOf(funcptr), funcptr)

    def get_is_virtual_fnptr(self):
        #
        def is_virtual(inst):
            if not inst:
                return False
            return inst.typeptr == self.jit_virtual_ref_vtable
        #
        FUNC = lltype.FuncType([rclass.OBJECTPTR], lltype.Bool)
        funcptr = self.warmrunnerdesc.helper_func(lltype.Ptr(FUNC), is_virtual)
        return inputconst(lltype.typeOf(funcptr), funcptr)

    def force_virtual(self, inst):
        vref = lltype.cast_pointer(lltype.Ptr(self.JIT_VIRTUAL_REF), inst)
        token = vref.jit_frame
        if token != jitframe.TOKEN_NONE:
            if token == self.TOKEN_TRACING_RESCALL:
                # The "virtual" is not a virtual at all during tracing.
                # We only need to reset jit_frame to TOKEN_NONE
                # as a marker for the tracing, to tell it that this
                # "virtual" escapes.
                assert vref.forced
                vref.jit_frame = jitframe.TOKEN_NONE
            else:
                assert not vref.forced
                from pypy.jit.metainterp.compile import ResumeGuardForcedDescr
                ResumeGuardForcedDescr.force_now(self.cpu, token)
                assert vref.jit_frame == jitframe.TOKEN_NONE
                assert vref.forced
        elif not vref.forced:
            # token == TOKEN_NONE and the vref was not forced: it's invalid
            raise InvalidVirtualRef
        return vref.forced
    force_virtual._dont_inline_ = True
