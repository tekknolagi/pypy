from pypy.interpreter.typedef import TypeDef
from pypy.interpreter.gateway import interp2app
from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.error import OperationError

import prolog.interpreter.continuation as pcont
import prolog.interpreter.term as pterm
import prolog.interpreter.error as perr
import prolog.interpreter.parsing as ppars
import pypy.module.unipycation.conversion as conv
import pypy.module.unipycation.util as util

class W_SolutionIterator(W_Root):
    """
    An interface that allows retreival of multiple solutions
    """

    def __init__(self, space, var_to_pos, goals, w_engine):
        print("CONSTRUCTOR")
        self.w_engine = w_engine
        self.var_to_pos = var_to_pos

        assert len(goals) == 1 # XXX
        self.goal = goals[0]

        self.space = space
        self.d_result = None
        self.first_sol = True

        self.fcont = None
        self.heap = None

    def populate_result(self, var_to_pos, fcont, heap):

        for var, real_var in var_to_pos.iteritems():
            print("VAR: %s" % var)
            if var.startswith("_"): continue

            w_var = self.space.wrap(var)
            w_val = conv.w_of_p(self.space, real_var.dereference(heap))
            self.space.setitem(self.d_result, w_var, w_val)

        self.fcont = fcont
        self.heap = heap

    def iter_w(self): return self.space.wrap(self)

    def next_w(self):
        """ Obtain the next solution (if there is one) """

        self.d_result = self.space.newdict()
        cur_mod = self.w_engine.engine.modulewrapper.current_module

        if self.first_sol:
            cont = UnipycationContinuation2(
                    self.w_engine, self.var_to_pos, self.space.wrap(self))
            self.first_sol = False

            try:
                r = self.w_engine.engine.run(self.goal, cur_mod, cont)
            except perr.UnificationFailed:
                self.d_result = None
        else:
            try:
                pcont.driver(*self.fcont.fail(self.heap))
            except perr.UnificationFailed:
                raise OperationError(self.space.w_StopIteration, None)

        return self.d_result

W_SolutionIterator.typedef = TypeDef("SolutionIterator",
    __iter__ = interp2app(W_SolutionIterator.iter_w),
    next = interp2app(W_SolutionIterator.next_w),
)

W_SolutionIterator.typedef.acceptable_as_base_class = False

# ---


# XXX this temproary variation is for the iterator interface
class UnipycationContinuation2(pcont.Continuation):
    def __init__(self, w_engine, var_to_pos, w_solution_iter):
        engine = w_engine.engine

        print("CONSTRUCT CONTINUATION")
        pcont.Continuation.__init__(self, engine, pcont.DoneSuccessContinuation(engine))

        # stash
        self.var_to_pos = var_to_pos
        self.w_engine = w_engine
        self.w_solution_iter = w_solution_iter

    def activate(self, fcont, heap):
        self.w_solution_iter.populate_result(self.var_to_pos, fcont, heap)
        return pcont.DoneSuccessContinuation(self.engine), fcont, heap


class UnipycationContinuation(pcont.Continuation):
    def __init__(self, engine, var_to_pos, w_engine):
        pcont.Continuation.__init__(self, engine, pcont.DoneSuccessContinuation(engine))
        self.var_to_pos = var_to_pos
        self.w_engine = w_engine

    def activate(self, fcont, heap):
        self.w_engine.populate_result(self.var_to_pos, heap)
        return pcont.DoneSuccessContinuation(self.engine), fcont, heap

# ---

def engine_new__(space, w_subtype, __args__):
    w_anything = __args__.firstarg()
    e = W_Engine(space, w_anything)
    return space.wrap(e)

class W_Engine(W_Root):
    def __init__(self, space, w_anything):
        self.space = space                      # Stash space
        self.engine = e = pcont.Engine()        # We embed an instance of prolog
        self.d_result = None                    # When we have a result, we will stash it here

        try:
            e.runstring(space.str_w(w_anything))    # Load the database with the first arg
        except ppars.ParseError as e:
            w_ParseError = util.get_from_module(self.space, "unipycation", "ParseError")
            raise OperationError(w_ParseError, self.space.wrap(e.nice_error_message()))

    def query(self, w_anything):
        query_raw = self.space.str_w(w_anything)

        try:
            goals, var_to_pos = self.engine.parse(query_raw)
        except ppars.ParseError as e:
            w_ParseError = util.get_from_module(self.space, "unipycation", "ParseError")
            raise OperationError(w_ParseError, self.space.wrap(e.nice_error_message()))

        cont = UnipycationContinuation(self.engine, var_to_pos, self)
        self.d_result = self.space.newdict()
        for g in goals:
            try:
                self.engine.run(g, self.engine.modulewrapper.current_module, cont)
            except perr.UnificationFailed:
                self.d_result = None
                break

        return self.d_result

    def query_iter(self, w_query_str):
        """ Returns an iterator by which to acquire multiple solutions """
        query_raw = self.space.str_w(w_query_str)

        try:
            goals, var_to_pos = self.engine.parse(query_raw)
        except ppars.ParseError as e:
            w_ParseError = util.get_from_module(self.space, "unipycation", "ParseError")
            raise OperationError(w_ParseError, self.space.wrap(e.nice_error_message()))

        w_engine = self.space.wrap(self)
        w_solution_iter = W_SolutionIterator(self.space, var_to_pos, goals, w_engine)

        return w_solution_iter

    def populate_result(self, var_to_pos, heap):

        for var, real_var in var_to_pos.iteritems():
            print("VAR: %s" % var)
            if var.startswith("_"): continue

            w_var = self.space.wrap(var)
            w_val = conv.w_of_p(self.space, real_var.dereference(heap))
            self.space.setitem(self.d_result, w_var, w_val)

    def print_last_result(self):
        print(self.result)

W_Engine.typedef = TypeDef("Engine",
    __new__ = interp2app(engine_new__),
    query = interp2app(W_Engine.query),
    query_iter = interp2app(W_Engine.query_iter),
    print_last_result = interp2app(W_Engine.print_last_result),
)

W_Engine.typedef.acceptable_as_base_class = False
