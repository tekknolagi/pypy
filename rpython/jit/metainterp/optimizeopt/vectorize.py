import sys
import py
from rpython.rtyper.lltypesystem import lltype, rffi
from rpython.jit.metainterp.optimizeopt.optimizer import Optimizer, Optimization
from rpython.jit.metainterp.optimizeopt.util import make_dispatcher_method
from rpython.jit.metainterp.optimizeopt.dependency import DependencyGraph
from rpython.jit.metainterp.resoperation import rop
from rpython.jit.metainterp.resume import Snapshot
from rpython.rlib.debug import debug_print, debug_start, debug_stop
from rpython.jit.metainterp.jitexc import JitException

class NotAVectorizeableLoop(JitException):
    def __str__(self):
        return 'NotAVectorizeableLoop()'

def optimize_vector(metainterp_sd, jitdriver_sd, loop, optimizations):
    opt = OptVectorize(metainterp_sd, jitdriver_sd, loop, optimizations)
    try:
        opt.propagate_all_forward()
    except NotAVectorizeableLoop:
        # vectorization is not possible, propagate only normal optimizations
        def_opt = Optimizer(metainterp_sd, jitdriver_sd, loop, optimizations)
        def_opt.propagate_all_forward()

class VectorizeOptimizer(Optimizer):
    def setup(self):
        pass

class OptVectorize(Optimization):
    """ Try to unroll the loop and find instructions to group """

    def __init__(self, metainterp_sd, jitdriver_sd, loop, optimizations):
        self.optimizer = VectorizeOptimizer(metainterp_sd, jitdriver_sd,
                                             loop, optimizations)
        self.vec_info = LoopVectorizeInfo()
        self.memory_refs = []
        self.dependency_graph = None

    def _rename_arguments_ssa(self, rename_map, label_args, jump_args):
        # fill the map with the renaming boxes. keys are boxes from the label
        # values are the target boxes.

        # it is assumed that #label_args == #jump_args
        for i in range(len(label_args)):
            la = label_args[i]
            ja = jump_args[i]
            if la != ja:
                rename_map[la] = ja

    def unroll_loop_iterations(self, loop, unroll_factor):
        label_op = loop.operations[0]
        jump_op = loop.operations[-1]
        operations = [loop.operations[i].clone() for i in range(1,len(loop.operations)-1)]
        loop.operations = []

        op_index = len(operations) + 1

        iterations = [operations]
        label_op_args = [self.getvalue(box).get_key_box() for box in label_op.getarglist()]
        values = [self.getvalue(box) for box in label_op.getarglist()]
        #values[0].make_nonnull(self.optimizer)

        jump_op_args = jump_op.getarglist()

        rename_map = {}
        for unroll_i in range(2, unroll_factor+1):
            # for each unrolling factor the boxes are renamed.
            self._rename_arguments_ssa(rename_map, label_op_args, jump_op_args)
            iteration_ops = []
            for op in operations:
                copied_op = op.clone()

                if copied_op.result is not None:
                    # every result assigns a new box, thus creates an entry
                    # to the rename map.
                    new_assigned_box = copied_op.result.clonebox()
                    rename_map[copied_op.result] = new_assigned_box
                    copied_op.result = new_assigned_box

                args = copied_op.getarglist()
                for i, arg in enumerate(args):
                    try:
                        value = rename_map[arg]
                        copied_op.setarg(i, value)
                    except KeyError:
                        pass

                self.vec_info._op_index = op_index
                iteration_ops.append(copied_op)
                op_index += 1
                self.vec_info.inspect_operation(copied_op)

            # the jump arguments have been changed
            # if label(iX) ... jump(i(X+1)) is called, at the next unrolled loop
            # must look like this: label(i(X+1)) ... jump(i(X+2))

            args = jump_op.getarglist()
            for i, arg in enumerate(args):
                try:
                    value = rename_map[arg]
                    jump_op.setarg(i, value)
                except KeyError:
                    pass
            # map will be rebuilt, the jump operation has been updated already
            rename_map.clear()

            iterations.append(iteration_ops)

        # unwrap the loop nesting.
        loop.operations.append(label_op)
        for iteration in iterations:
            for op in iteration:
                loop.operations.append(op)
        loop.operations.append(jump_op)

    def _gather_trace_information(self, loop):
        for i,op in enumerate(loop.operations):
            self.vec_info._op_index = i
            self.vec_info.inspect_operation(op)

    def get_estimated_unroll_factor(self, force_reg_bytes = -1):
        """ force_reg_bytes used for testing """
        # this optimization is not opaque, and needs info about the CPU
        byte_count = self.vec_info.smallest_type_bytes
        if byte_count == 0:
            return 0
        simd_vec_reg_bytes = 16 # TODO get from cpu
        if force_reg_bytes > 0:
            simd_vec_reg_bytes = force_reg_bytes
        unroll_factor = simd_vec_reg_bytes // byte_count
        return unroll_factor

    def propagate_all_forward(self):

        loop = self.optimizer.loop
        self.optimizer.clear_newoperations()

        self._gather_trace_information(loop)

        byte_count = self.vec_info.smallest_type_bytes
        if byte_count == 0:
            # stop, there is no chance to vectorize this trace
            raise NotAVectorizeableLoop()

        unroll_factor = self.get_estimated_unroll_factor()

        self.unroll_loop_iterations(loop, unroll_factor)

        self.build_dependencies()

    def build_dependency_graph(self):
        self.dependency_graph = DependencyGraph(self.optimizer,
                                                self.optimizer.loop)

    def find_adjacent_memory_refs(self):
        """ the pre pass already builds a hash of memory references and the
        operations. Since it is in SSA form there is no array index. Indices
        are flattend. If there are two array accesses in the unrolled loop
        i0,i1 and i1 = int_add(i0,c), then i0 = i0 + 0, i1 = i0 + 1 """
        loop = self.optimizer.loop
        operations = loop.operations
        integral_mod = IntegralMod(self.optimizer)
        for opidx,memref in self.vec_info.memory_refs.items():
            integral_mod.reset()
            while True:

                for dep in self.dependency_graph.instr_dependencies(opidx):
                    # this is a use, thus if dep is not a defintion
                    # it points back to the definition
                    if memref.origin == dep.defined_arg and not dep.is_definition:
                        # if is_definition is false the params is swapped
                        # idx_to attributes points to definer
                        def_op = operations[dep.idx_to]
                        opidx = dep.idx_to
                        break
                else:
                    # this is an error in the dependency graph
                    raise RuntimeError("a variable usage does not have a " +
                             " definition. Cannot continue!")

                op = operations[opidx]
                if op.getopnum() == rop.LABEL:
                    break

                integral_mod.inspect_operation(def_op)
                if integral_mod.is_const_mod:
                    integral_mod.update_memory_ref(memref)
                else:
                    break

    def vectorize_trace(self, loop):
        """ Implementation of the algorithm introduced by Larsen. Refer to
              '''Exploiting Superword Level Parallelism
                 with Multimedia Instruction Sets'''
            for more details.
        """

        for i,operation in enumerate(loop.operations):

            if operation.getopnum() == rop.RAW_LOAD:
                # TODO while the loop is unrolled, build memory accesses
                pass


        # was not able to vectorize
        return False

class IntegralMod(object):
    """ Calculates integral modifications on an integer object.
    The operations must be provided in backwards direction and of one
    variable only. Call reset() to reuse this object for other variables.
    """

    def __init__(self, optimizer):
        self.optimizer = optimizer
        self.reset()

    def reset(self):
        self.is_const_mod = False
        self.coefficient_mul = 1
        self.coefficient_div = 1
        self.constant = 0
        self.used_box = None

    def operation_INT_SUB(self, op):
        box_a0 = op.getarg(0)
        box_a1 = op.getarg(1)
        a0 = self.optimizer.getvalue(box_a0)
        a1 = self.optimizer.getvalue(box_a1)
        self.is_const_mod = True
        if a0.is_constant() and a1.is_constant():
            raise NotImplementedError()
        elif a0.is_constant():
            self.constant -= box_a0.getint() * self.coefficient_mul
            self.used_box = box_a1
        elif a1.is_constant():
            self.constant -= box_a1.getint() * self.coefficient_mul
            self.used_box = box_a0
        else:
            self.is_const_mod = False

    def _update_additive(self, i):
        return (i * self.coefficient_mul) / self.coefficient_div
    
    additive_func_source = """
    def operation_{name}(self, op):
        box_a0 = op.getarg(0)
        box_a1 = op.getarg(1)
        a0 = self.optimizer.getvalue(box_a0)
        a1 = self.optimizer.getvalue(box_a1)
        self.is_const_mod = True
        if a0.is_constant() and a1.is_constant():
            self.used_box = None
            self.constant += self._update_additive(box_a0.getint() {op} \
                                                      box_a1.getint())
        elif a0.is_constant():
            self.constant {op}= self._update_additive(box_a0.getint())
            self.used_box = box_a1
        elif a1.is_constant():
            self.constant {op}= self._update_additive(box_a1.getint())
            self.used_box = box_a0
        else:
            self.is_const_mod = False
    """
    exec py.code.Source(additive_func_source.format(name='INT_ADD', 
                                                    op='+')).compile()
    exec py.code.Source(additive_func_source.format(name='INT_SUB', 
                                                    op='-')).compile()
    del additive_func_source

    multiplicative_func_source = """
    def operation_{name}(self, op):
        box_a0 = op.getarg(0)
        box_a1 = op.getarg(1)
        a0 = self.optimizer.getvalue(box_a0)
        a1 = self.optimizer.getvalue(box_a1)
        self.is_const_mod = True
        if a0.is_constant() and a1.is_constant():
            # here these factor becomes a constant, thus it is
            # handled like any other additive operation
            self.used_box = None
            self.constant += self._update_additive(box_a0.getint() {cop} \
                                                      box_a1.getint())
        elif a0.is_constant():
            self.coefficient_{tgt} {op}= box_a0.getint()
            self.used_box = box_a1
        elif a1.is_constant():
            self.coefficient_{tgt} {op}= box_a1.getint()
            self.used_box = box_a0
        else:
            self.is_const_mod = False
    """
    exec py.code.Source(multiplicative_func_source.format(name='INT_MUL', 
                                                 op='*', tgt='mul',
                                                 cop='*')).compile()
    exec py.code.Source(multiplicative_func_source.format(name='INT_FLOORDIV',
                                                 op='*', tgt='div',
                                                 cop='/')).compile()
    exec py.code.Source(multiplicative_func_source.format(name='UINT_FLOORDIV',
                                                 op='*', tgt='div',
                                                 cop='/')).compile()
    del multiplicative_func_source
    

    def update_memory_ref(self, memref):
        #print("updating memory ref pre: ", memref)
        memref.constant = self.constant
        memref.coefficient_mul = self.coefficient_mul
        memref.coefficient_div = self.coefficient_div
        memref.origin = self.used_box
        #print("updating memory ref post: ", memref)

    def default_operation(self, operation):
        pass
integral_dispatch_opt = make_dispatcher_method(IntegralMod, 'operation_',
        default=IntegralMod.default_operation)
IntegralMod.inspect_operation = integral_dispatch_opt

class LoopVectorizeInfo(object):

    def __init__(self):
        self.smallest_type_bytes = 0
        self._op_index = 0
        self.memory_refs = {}
        self.label_op = None

    def operation_RAW_LOAD(self, op):
        descr = op.getdescr()
        self.memory_refs[self._op_index] = \
                MemoryRef(op.getarg(0), op.getarg(1), op.getdescr())
        if not descr.is_array_of_pointers():
            byte_count = descr.get_item_size_in_bytes()
            if self.smallest_type_bytes == 0 \
               or byte_count < self.smallest_type_bytes:
                self.smallest_type_bytes = byte_count

    def default_operation(self, operation):
        pass
dispatch_opt = make_dispatcher_method(LoopVectorizeInfo, 'operation_',
        default=LoopVectorizeInfo.default_operation)
LoopVectorizeInfo.inspect_operation = dispatch_opt

class Pack(object):
    """ A pack is a set of n statements that are:
        * isomorphic
        * independant
        Statements are named operations in the code.
    """
    def __init__(self, ops):
        self.operations = ops

class Pair(Pack):
    """ A special Pack object with only two statements. """
    def __init__(self, left_op, right_op):
        assert isinstance(left_op, rop.ResOperation)
        assert isinstance(right_op, rop.ResOperation)
        self.left_op = left_op
        self.right_op = right_op
        Pack.__init__(self, [left_op, right_op])


class MemoryRef(object):
    def __init__(self, array, origin, descr):
        self.array = array
        self.origin = origin
        self.descr = descr
        self.coefficient_mul = 1
        self.coefficient_div = 1
        self.constant = 0

    def is_adjacent_to(self, other):
        """ this is a symmetric relation """
        match, off = self.calc_difference(other)
        if match:
            return off == 1 or off == -1
        return False

    def is_adjacent_after(self, other):
        """ the asymetric relation to is_adjacent_to """
        match, off = self.calc_difference(other)
        if match:
            return off == 1
        return False

    def __eq__(self, other):
        match, off = self.calc_difference(other)
        if match:
            return off == 0
        return False

    def __ne__(self, other):
        return not self.__eq__(other)


    def calc_difference(self, other):
        if self.array == other.array \
            and self.origin == other.origin:
            mycoeff = self.coefficient_mul // self.coefficient_div
            othercoeff = other.coefficient_mul // other.coefficient_div
            diff = other.constant - self.constant
            return mycoeff == othercoeff, diff
        return False, 0

    def __repr__(self):
        return 'MemoryRef(%s*(%s/%s)+%s)' % (self.origin, self.coefficient_mul,
                                            self.coefficient_div, self.constant)
