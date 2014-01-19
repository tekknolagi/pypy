from rpython.annotator import model as annmodel
from rpython.flowspace.model import FunctionGraph
from rpython.rtyper.lltypesystem import lltype
from rpython.rtyper.lltypesystem.rstr import STR
from rpython.translator.c.support import cdecl


def predeclare_common_types(db, rtyper):
    # Common types
    yield ('RPyString', STR)

def predeclare_exception_data(db, rtyper):
    # Exception-related types and constants
    exceptiondata = rtyper.exceptiondata
    exctransformer = db.exctransformer

    yield ('RPYTHON_EXCEPTION',        exceptiondata.lltype_of_exception_value)
    yield ('RPYTHON_TYPE_OF_EXC_INST', exceptiondata.fn_type_of_exc_inst)

    yield ('RPyExceptionOccurred1',    exctransformer.rpyexc_occured_ptr.value)
    yield ('RPyFetchExceptionType',    exctransformer.rpyexc_fetch_type_ptr.value)
    yield ('RPyFetchExceptionValue',   exctransformer.rpyexc_fetch_value_ptr.value)
    yield ('RPyClearException',        exctransformer.rpyexc_clear_ptr.value)
    yield ('RPyRaiseException',        exctransformer.rpyexc_raise_ptr.value)

    for exccls in exceptiondata.standardexceptions:
        exc_llvalue = exceptiondata.get_standard_ll_exc_instance_by_class(
            exccls)
        rtyper.getrepr(annmodel.lltype_to_annotation(lltype.typeOf(exc_llvalue)))
        # strange naming here because the macro name must be
        # a substring of PyExc_%s
        name = exccls.__name__
        if exccls.__module__ != 'exceptions':
            name = '%s_%s' % (exccls.__module__.replace('.', '__'), name)
        yield ('RPyExc_%s' % name, exc_llvalue)
    rtyper.call_all_setups()


def predeclare_all(db, rtyper):
    for fn in [predeclare_common_types,
               predeclare_exception_data,
               ]:
        for t in fn(db, rtyper):
            yield t


def get_all(db, rtyper):
    for fn in [predeclare_common_types,
               predeclare_exception_data,
               ]:
        for t in fn(db, rtyper):
            yield t[1]

# ____________________________________________________________

def do_the_getting(db, rtyper):

    decls = list(get_all(db, rtyper))
    rtyper.specialize_more_blocks()

    for obj in decls:
        if isinstance(obj, lltype.LowLevelType):
            db.gettype(obj)
        elif isinstance(obj, FunctionGraph):
            db.get(rtyper.getcallable(obj))
        else:
            db.get(obj)


def pre_include_code_lines(db, rtyper):
    # generate some #defines that go before the #include to provide
    # predeclared well-known names for constant objects, functions and
    # types.  These names are then used by the #included files, like
    # g_exception.h.

    def predeclare(c_name, lowlevelobj):
        llname = db.get(lowlevelobj)
        assert '\n' not in llname
        return '#define\t%s\t%s' % (c_name, llname)

    def predeclaretype(c_typename, lowleveltype):
        typename = db.gettype(lowleveltype)
        return 'typedef %s;' % cdecl(typename, c_typename)

    decls = list(predeclare_all(db, rtyper))

    for c_name, obj in decls:
        if isinstance(obj, lltype.LowLevelType):
            yield predeclaretype(c_name, obj)
        elif isinstance(obj, FunctionGraph):
            yield predeclare(c_name, rtyper.getcallable(obj))
        else:
            yield predeclare(c_name, obj)
