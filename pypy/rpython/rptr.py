from pypy.annotation.pairtype import pairtype
from pypy.annotation import model as annmodel
from pypy.objspace.flow import model as flowmodel
from pypy.rpython.lltypesystem.lltype import \
     Ptr, ContainerType, Void, Signed, Bool, FuncType, typeOf, FixedSizeArray, \
     InteriorPtr, OpaqueType
from pypy.rpython.error import TyperError
from pypy.rpython.rmodel import Repr, IntegerRepr


class __extend__(annmodel.SomePtr):
    def rtyper_makerepr(self, rtyper):
##        if self.is_constant() and not self.const:   # constant NULL
##            return nullptr_repr
##        else:
        return PtrRepr(self.ll_ptrtype)
    def rtyper_makekey(self):
##        if self.is_constant() and not self.const:
##            return None
##        else:
        return self.__class__, self.ll_ptrtype

class __extend__(annmodel.SomeInteriorPtr):
    def rtyper_makerepr(self, rtyper):
        return InteriorPtrRepr(self.ll_ptrtype)

class PtrRepr(Repr):

    def __init__(self, ptrtype):
        assert isinstance(ptrtype, Ptr)
        self.lowleveltype = ptrtype

    def rtype_getattr(self, hop):
        attr = hop.args_s[1].const
        if isinstance(hop.s_result, annmodel.SomeLLADTMeth):
            return hop.inputarg(hop.r_result, arg=0)
        FIELD_TYPE = getattr(self.lowleveltype.TO, attr)
        if isinstance(FIELD_TYPE, ContainerType):
            if isinstance(hop.r_result, InteriorPtrRepr):
                return hop.genop('same_as', [hop.inputarg(self, 0)],
                                 resulttype=self.lowleveltype)
            else:
                newopname = 'getsubstruct'
        else:
            newopname = 'getfield'
        vlist = hop.inputargs(self, Void)
        return hop.genop(newopname, vlist,
                         resulttype = hop.r_result.lowleveltype)

    def rtype_setattr(self, hop):
        attr = hop.args_s[1].const
        FIELD_TYPE = getattr(self.lowleveltype.TO, attr)
        assert not isinstance(FIELD_TYPE, ContainerType)
        vlist = hop.inputargs(self, Void, hop.args_r[2])
        hop.genop('setfield', vlist)

    def rtype_len(self, hop):
        ARRAY = hop.args_r[0].lowleveltype.TO
        if isinstance(ARRAY, FixedSizeArray):
            return hop.inputconst(Signed, ARRAY.length)
        else:
            vlist = hop.inputargs(self)
            return hop.genop('getarraysize', vlist,
                             resulttype = hop.r_result.lowleveltype)

    def rtype_is_true(self, hop):
        vlist = hop.inputargs(self)
        return hop.genop('ptr_nonzero', vlist, resulttype=Bool)

    def rtype_simple_call(self, hop):
        if not isinstance(self.lowleveltype.TO, FuncType):
            raise TyperError("calling a non-function %r", self.lowleveltype.TO)
        vlist = hop.inputargs(*hop.args_r)
        nexpected = len(self.lowleveltype.TO.ARGS)
        nactual = len(vlist)-1
        if nactual != nexpected: 
            raise TyperError("argcount mismatch:  expected %d got %d" %
                            (nexpected, nactual))
        if isinstance(vlist[0], flowmodel.Constant):
            if hasattr(vlist[0].value, 'graph'):
                hop.llops.record_extra_call(vlist[0].value.graph)
            opname = 'direct_call'
        else:
            opname = 'indirect_call'
            vlist.append(hop.inputconst(Void, None))
        hop.exception_is_here()
        return hop.genop(opname, vlist,
                         resulttype = self.lowleveltype.TO.RESULT)

    def rtype_call_args(self, hop):
        from pypy.rpython.rbuiltin import call_args_expand
        hop, _ = call_args_expand(hop, takes_kwds=False)
        hop.swap_fst_snd_args()
        hop.r_s_popfirstarg()
        return self.rtype_simple_call(hop)
        

class __extend__(pairtype(PtrRepr, IntegerRepr)):

    def rtype_getitem((r_ptr, r_int), hop):
        ARRAY = r_ptr.lowleveltype.TO
        ITEM_TYPE = ARRAY.OF
        if isinstance(ITEM_TYPE, ContainerType):
            if isinstance(hop.r_result, InteriorPtrRepr):
                v_array, v_index = hop.inputargs(r_ptr, Signed)
                INTERIOR_PTR_TYPE = r_ptr.lowleveltype._interior_ptr_type_with_index(ITEM_TYPE)
                v_interior_ptr = hop.genop('malloc', [flowmodel.Constant(INTERIOR_PTR_TYPE, Void)],
                                           resulttype = Ptr(INTERIOR_PTR_TYPE))
                hop.genop('setfield',
                          [v_interior_ptr, flowmodel.Constant('ptr', Void), v_array])
                hop.genop('setfield',
                          [v_interior_ptr, flowmodel.Constant('index', Void), v_index])
                return v_interior_ptr
            else:
                newopname = 'getarraysubstruct'
        else:
            newopname = 'getarrayitem'
        vlist = hop.inputargs(r_ptr, Signed)
        return hop.genop(newopname, vlist,
                         resulttype = hop.r_result.lowleveltype)

    def rtype_setitem((r_ptr, r_int), hop):
        ARRAY = r_ptr.lowleveltype.TO
        ITEM_TYPE = ARRAY.OF
        assert not isinstance(ITEM_TYPE, ContainerType)
        vlist = hop.inputargs(r_ptr, Signed, hop.args_r[2])
        hop.genop('setarrayitem', vlist)

# ____________________________________________________________
#
#  Null Pointers

##class NullPtrRepr(Repr):
##    lowleveltype = Void

##    def rtype_is_true(self, hop):
##        return hop.inputconst(Bool, False)

##nullptr_repr = NullPtrRepr()

##class __extend__(pairtype(NullPtrRepr, PtrRepr)):
##    def convert_from_to((r_null, r_ptr), v, llops):
##        # nullptr to general pointer
##        return inputconst(r_ptr, _ptr(r_ptr.lowleveltype, None))

# ____________________________________________________________
#
#  Comparisons

class __extend__(pairtype(PtrRepr, Repr)):

    def rtype_eq((r_ptr, r_any), hop):
        vlist = hop.inputargs(r_ptr, r_ptr)
        return hop.genop('ptr_eq', vlist, resulttype=Bool)

    def rtype_ne((r_ptr, r_any), hop):
        vlist = hop.inputargs(r_ptr, r_ptr)
        return hop.genop('ptr_ne', vlist, resulttype=Bool)


class __extend__(pairtype(Repr, PtrRepr)):

    def rtype_eq((r_any, r_ptr), hop):
        vlist = hop.inputargs(r_ptr, r_ptr)
        return hop.genop('ptr_eq', vlist, resulttype=Bool)

    def rtype_ne((r_any, r_ptr), hop):
        vlist = hop.inputargs(r_ptr, r_ptr)
        return hop.genop('ptr_ne', vlist, resulttype=Bool)

# ________________________________________________________________
# ADT  methods

class __extend__(annmodel.SomeLLADTMeth):
    def rtyper_makerepr(self, rtyper):
        return LLADTMethRepr(self, rtyper)
    def rtyper_makekey(self):
        return self.__class__, self.ll_ptrtype, self.func

class LLADTMethRepr(Repr):

    def __init__(self, adtmeth, rtyper):
        self.func = adtmeth.func
        self.ll_ptrtype = adtmeth.ll_ptrtype
        self.lowleveltype = rtyper.getrepr(annmodel.lltype_to_annotation(adtmeth.ll_ptrtype)).lowleveltype

    def rtype_simple_call(self, hop):
        hop2 = hop.copy()
        func = self.func
        s_func = hop.rtyper.annotator.bookkeeper.immutablevalue(func)
        v_ptr = hop2.args_v[0]
        hop2.r_s_popfirstarg()
        hop2.v_s_insertfirstarg(v_ptr, annmodel.lltype_to_annotation(self.ll_ptrtype))
        hop2.v_s_insertfirstarg(flowmodel.Constant(func), s_func)
        return hop2.dispatch()

class __extend__(pairtype(PtrRepr, LLADTMethRepr)):

    def convert_from_to((r_from, r_to), v, llops):
        if r_from.lowleveltype == r_to.lowleveltype:
            return v
        return NotImplemented


class InteriorPtrRepr(Repr):
    def __init__(self, ptrtype):
        assert isinstance(ptrtype, InteriorPtr)
        self.v_offsets = []
        numitemoffsets = 0
        for i, offset in enumerate(ptrtype.offsets):
            if isinstance(offset, int):
                numitemoffsets += 1
                self.v_offsets.append(None)
            else:
                assert isinstance(offset, str)
                self.v_offsets.append(flowmodel.Constant(offset, Void))
        self.parentptrtype = Ptr(ptrtype.PARENTTYPE)
        self.resulttype = Ptr(ptrtype.TO)
        assert numitemoffsets <= 1
        if numitemoffsets > 0:
            self.lowleveltype = Ptr(self.parentptrtype._interior_ptr_type_with_index(self.resulttype.TO))
        else:
            self.lowleveltype = self.parentptrtype            

    def getinteriorfieldargs(self, hop, v_self):
        vlist = []
        if None in self.v_offsets:
            INTERIOR_TYPE = v_self.concretetype.TO
            nameiter = iter(INTERIOR_TYPE._names)
            name = nameiter.next()
            vlist.append(
                hop.genop('getfield',
                          [v_self, flowmodel.Constant(name, Void)],
                          resulttype=INTERIOR_TYPE._flds[name]))
        else:
            vlist.append(v_self)
        for v_offset in self.v_offsets:
            if v_offset is None:
                name = nameiter.next()
                vlist.append(
                    hop.genop('getfield',
                              [v_self, flowmodel.Constant(name, Void)],
                              resulttype=INTERIOR_TYPE._flds[name]))
            else:
                vlist.append(v_offset)
        if None in self.v_offsets:
            try:
                nameiter.next()
            except StopIteration:
                pass
            else:
                assert False
        return vlist

    def rtype_len(self, hop):
        v_self, = hop.inputargs(self)
        vlist = self.getinteriorfieldargs(hop, v_self)
        return hop.genop('getinteriorarraysize', vlist,
                         resulttype=Signed)

    def rtype_getattr(self, hop):
        attr = hop.args_s[1].const
        if isinstance(hop.s_result, annmodel.SomeLLADTMeth):
            return hop.inputarg(hop.r_result, arg=0)
        FIELD_TYPE = getattr(self.resulttype.TO, attr)
        if isinstance(FIELD_TYPE, ContainerType):
            return hop.genop('same_as', [hop.inputarg(self, 0)],
                             resulttype=self.lowleveltype)
        else:
            v_self, v_attr = hop.inputargs(self, Void)
            vlist = self.getinteriorfieldargs(hop, v_self) + [v_attr]
            return hop.genop('getinteriorfield', vlist,
                             resulttype=hop.r_result.lowleveltype)

    def rtype_setattr(self, hop):
        attr = hop.args_s[1].const
        FIELD_TYPE = getattr(self.resulttype.TO, attr)
        assert not isinstance(FIELD_TYPE, ContainerType)
        v_self, v_fieldname, v_value = hop.inputargs(self, Void, hop.args_r[2])
        vlist = self.getinteriorfieldargs(hop, v_self) + [v_fieldname, v_value]
        return hop.genop('setinteriorfield', vlist)




class __extend__(pairtype(InteriorPtrRepr, IntegerRepr)):
    def rtype_getitem((r_ptr, r_item), hop): 
        ARRAY = r_ptr.resulttype.TO
        ITEM_TYPE = ARRAY.OF
        if isinstance(ITEM_TYPE, ContainerType):
            v_array, v_index = hop.inputargs(r_ptr, Signed)
            INTERIOR_PTR_TYPE = r_ptr.lowleveltype._interior_ptr_type_with_index(ITEM_TYPE)
            v_interior_ptr = hop.genop('malloc', [flowmodel.Constant(INTERIOR_PTR_TYPE, Void)],
                                       resulttype = Ptr(INTERIOR_PTR_TYPE))
            hop.genop('setfield',
                      [v_interior_ptr, flowmodel.Constant('ptr', Void), v_array])
            hop.genop('setfield',
                      [v_interior_ptr, flowmodel.Constant('index', Void), v_index])
            return v_interior_ptr
        else:
            v_self, v_index = hop.inputargs(r_ptr, Signed)
            vlist = r_ptr.getinteriorfieldargs(hop, v_self) + [v_index]
            return hop.genop('getinteriorfield', vlist,
                             resulttype=ITEM_TYPE)
        
    def rtype_setitem((r_ptr, r_index), hop):
        ARRAY = r_ptr.resulttype.TO
        ITEM_TYPE = ARRAY.OF
        assert not isinstance(ITEM_TYPE, ContainerType)
        v_self, v_index, v_value = hop.inputargs(r_ptr, Signed, hop.args_r[2])
        vlist = r_ptr.getinteriorfieldargs(hop, v_self) + [v_index, v_value]
        hop.genop('setinteriorfield', vlist)
            
class __extend__(pairtype(InteriorPtrRepr, LLADTMethRepr)):

    def convert_from_to((r_from, r_to), v, llops):
        if r_from.lowleveltype == r_to.lowleveltype:
            return v
        return NotImplemented
