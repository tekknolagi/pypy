from pypy.rpython.annlowlevel import base_ptr_lltype, base_obj_ootype
from pypy.rpython.annlowlevel import cast_instance_to_base_ptr
from pypy.rpython.annlowlevel import cast_instance_to_base_obj
from pypy.rpython.lltypesystem import lltype, llmemory
from pypy.rpython.ootypesystem import ootype

def deref(T):
    if isinstance(T, lltype.Ptr):
        return T.TO
    assert isinstance(T, ootype.OOType)
    return T

def fieldType(T, name):
    if isinstance(T, lltype.Struct):
        return getattr(T, name)
    elif isinstance(T, (ootype.Instance, ootype.Record)):
        _, FIELD = T._lookup_field(name)
        return FIELD
    else:
        assert False

class TypeSystemHelper(object):

    def _freeze_(self):
        return True

class LLTypeHelper(TypeSystemHelper):

    name = 'lltype'
    ROOT_TYPE = llmemory.Address
    BASE_OBJ_TYPE = base_ptr_lltype()
    NULL_OBJECT = base_ptr_lltype()._defl()
    cast_instance_to_base_ptr = staticmethod(cast_instance_to_base_ptr)

    def get_typeptr(self, obj):
        return obj.typeptr

    def genop_malloc_fixedsize(self, builder, alloctoken):
        return builder.genop_malloc_fixedsize(alloctoken)

    def genop_ptr_iszero(self, builder, argbox, gv_addr):
        return builder.genop1("ptr_iszero", gv_addr)

    def genop_ptr_nonzero(self, builder, argbox, gv_addr):
        return builder.genop1("ptr_nonzero", gv_addr)

    def genop_ptr_eq(self, builder, gv_addr0, gv_addr1):
        return builder.genop2("ptr_eq", gv_addr0, gv_addr1)

    def genop_ptr_ne(self, builder, gv_addr0, gv_addr1):
        return builder.genop2("ptr_ne", gv_addr0, gv_addr1)

    def get_FuncType(self, ARGS, RESULT):
        FUNCTYPE = lltype.FuncType(ARGS, RESULT)
        FUNCPTRTYPE = lltype.Ptr(FUNCTYPE)
        return FUNCTYPE, FUNCPTRTYPE

    def PromotionPoint(self, flexswitch, incoming_gv, promotion_path):
        from pypy.jit.timeshifter.rtimeshift import PromotionPointLLType
        return PromotionPointLLType(flexswitch, incoming_gv, promotion_path)

class OOTypeHelper(TypeSystemHelper):

    name = 'ootype'
    ROOT_TYPE = ootype.Object
    BASE_OBJ_TYPE = base_obj_ootype()
    NULL_OBJECT = base_obj_ootype()._defl()
    cast_instance_to_base_ptr = staticmethod(cast_instance_to_base_obj)

    def get_typeptr(self, obj):
        return obj.meta

    def genop_malloc_fixedsize(self, builder, alloctoken):
        return builder.genop_new(alloctoken)

    def genop_ptr_iszero(self, builder, argbox, gv_addr):
        return builder.genop_ooisnull(gv_addr)

    def genop_ptr_nonzero(self, builder, argbox, gv_addr):
        return builder.genop_oononnull(gv_addr)

    def genop_ptr_eq(self, builder, gv_addr0, gv_addr1):
        return builder.genop2("oois", gv_addr0, gv_addr1)

    def genop_ptr_ne(self, builder, gv_addr0, gv_addr1):
        assert False, 'TODO'
        #return builder.genop2("ptr_ne", gv_addr0, gv_addr1)

    def get_FuncType(self, ARGS, RESULT):
        FUNCTYPE = ootype.StaticMethod(ARGS, RESULT)
        return FUNCTYPE, FUNCTYPE

    def PromotionPoint(self, flexswitch, incoming_gv, promotion_path):
        from pypy.jit.timeshifter.rtimeshift import PromotionPointOOType
        return PromotionPointOOType(flexswitch, incoming_gv, promotion_path)


llhelper = LLTypeHelper()
oohelper = OOTypeHelper()
