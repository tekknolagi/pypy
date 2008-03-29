from pypy.objspace.flow.model import checkgraph, copygraph, Constant
from pypy.objspace.flow.model import Block, Link, SpaceOperation, Variable
from pypy.translator.unsimplify import split_block, varoftype
from pypy.translator.simplify import join_blocks
from pypy.jit.hintannotator.annotator import HintAnnotator
from pypy.jit.hintannotator.model import SomeLLAbstractConstant, OriginFlags
from pypy.annotation import model as annmodel
from pypy.rpython.rmodel import inputconst
from pypy.rpython.rtyper import LowLevelOpList
from pypy.rpython.lltypesystem import lltype
from pypy.rlib.jit import JitHintError


class HotPathHintAnnotator(HintAnnotator):

    def build_hotpath_types(self):
        self.jitdrivers = {}
        self.prepare_portal_graphs()
        graph = self.portalgraph_with_on_enter_jit
        input_args_hs = [SomeLLAbstractConstant(v.concretetype,
                                                {OriginFlags(): True})
                         for v in graph.getargs()]
        return self.build_types(graph, input_args_hs)

    def prepare_portal_graphs(self):
        # find the graph with the jit_merge_point()
        found_at = []
        for graph in self.base_translator.graphs:
            place = find_jit_merge_point(graph)
            if place is not None:
                found_at.append(place)
        if len(found_at) != 1:
            raise JitHintError("found %d graphs with a jit_merge_point(),"
                               " expected 1 (for now)" % len(found_at))
        origportalgraph, _, origportalop = found_at[0]
        jitdriver = origportalop.args[1].value
        self.jitdrivers[jitdriver] = True
        #
        # We make a copy of origportalgraph and mutate it to make it
        # the portal.  The portal really starts at the jit_merge_point()
        # without any block or operation before it.
        #
        portalgraph = copygraph(origportalgraph)
        block = split_before_jit_merge_point(None, portalgraph)
        assert block is not None
        # rewire the graph to start at the global_merge_point
        portalgraph.startblock.isstartblock = False
        portalgraph.startblock = block
        portalgraph.startblock.isstartblock = True
        self.portalgraph = portalgraph
        self.origportalgraph = origportalgraph
        # check the new graph: errors mean some live vars have not
        # been listed in the jit_merge_point()
        # (XXX should give an explicit JitHintError explaining the problem)
        checkgraph(portalgraph)
        # insert the on_enter_jit() logic before the jit_merge_point()
        # in a copy of the graph which will be the one that gets hint-annotated
        # and turned into rainbow bytecode.  On the other hand, the
        # 'self.portalgraph' is the copy that will run directly, in
        # non-JITting mode, so it should not contain the on_enter_jit() call.
        if hasattr(jitdriver, 'on_enter_jit'):
            anothercopy = copygraph(portalgraph)
            anothercopy.tag = 'portal'
            insert_on_enter_jit_handling(self.base_translator.rtyper,
                                         anothercopy,
                                         jitdriver)
            self.portalgraph_with_on_enter_jit = anothercopy
        else:
            self.portalgraph_with_on_enter_jit = portalgraph  # same is ok
        # put the new graph back in the base_translator
        portalgraph.tag = 'portal'
        self.base_translator.graphs.append(portalgraph)

# ____________________________________________________________

def find_jit_merge_point(graph):
    found_at = []
    for block in graph.iterblocks():
        for op in block.operations:
            if (op.opname == 'jit_marker' and
                op.args[0].value == 'jit_merge_point'):
                found_at.append((graph, block, op))
    if len(found_at) > 1:
        raise JitHintError("multiple jit_merge_point() not supported")
    if found_at:
        return found_at[0]
    else:
        return None

def split_before_jit_merge_point(hannotator, graph):
    """Find the block with 'jit_merge_point' and split just before,
    making sure the input args are in the canonical order.  If
    hannotator is not None, preserve the hint-annotations while doing so
    (used by codewriter.py).
    """
    found_at = find_jit_merge_point(graph)
    if found_at is not None:
        _, portalblock, portalop = found_at
        portalopindex = portalblock.operations.index(portalop)
        # split the block just before the jit_merge_point()
        if portalopindex > 0:
            link = split_block(hannotator, portalblock, portalopindex)
            portalblock = link.target
            portalop = portalblock.operations[0]
        # split again, this time enforcing the order of the live vars
        # specified by the user in the jit_merge_point() call
        assert portalop.opname == 'jit_marker'
        assert portalop.args[0].value == 'jit_merge_point'
        livevars = [v for v in portalop.args[2:]
                      if v.concretetype is not lltype.Void]
        link = split_block(hannotator, portalblock, 0, livevars)
        return link.target
    else:
        return None

def insert_on_enter_jit_handling(rtyper, graph, jitdriver):
    vars = [varoftype(v.concretetype, name=v) for v in graph.getargs()]
    newblock = Block(vars)

    op = graph.startblock.operations[0]
    assert op.opname == 'jit_marker'
    assert op.args[0].value == 'jit_merge_point'
    assert op.args[1].value is jitdriver
    allvars = []
    i = 0
    for v in op.args[2:]:
        if v.concretetype is lltype.Void:
            allvars.append(Constant(None, concretetype=lltype.Void))
        else:
            allvars.append(vars[i])
            i += 1
    assert i == len(vars)

    # six lines just to get at the INVARIANTS type...
    compute_invariants_func = jitdriver.compute_invariants.im_func
    bk = rtyper.annotator.bookkeeper
    s_func = bk.immutablevalue(compute_invariants_func)
    r_func = rtyper.getrepr(s_func)
    c_func = r_func.get_unique_llfn()
    INVARIANTS = c_func.concretetype.TO.RESULT

    llops = LowLevelOpList(rtyper)
    # generate ops to make an instance of RedVarsHolder
    RedVarsHolder = jitdriver._RedVarsHolder
    classdef = rtyper.annotator.bookkeeper.getuniqueclassdef(RedVarsHolder)
    s_instance = annmodel.SomeInstance(classdef)
    r_instance = rtyper.getrepr(s_instance)
    v_reds = r_instance.new_instance(llops)
    # generate ops to store the 'reds' variables on the RedVarsHolder
    num_greens = len(jitdriver.greens)
    num_reds = len(jitdriver.reds)
    assert len(allvars) == num_greens + num_reds
    for name, v_value in zip(jitdriver.reds, allvars[num_greens:]):
        r_instance.setfield(v_reds, name, v_value, llops)
    # generate a call to on_enter_jit(self, reds, invariants, *greens)
    on_enter_jit_func = jitdriver.on_enter_jit.im_func
    s_func = rtyper.annotator.bookkeeper.immutablevalue(on_enter_jit_func)
    r_func = rtyper.getrepr(s_func)
    c_func = r_func.get_unique_llfn()
    ON_ENTER_JIT = c_func.concretetype.TO
    assert ON_ENTER_JIT.ARGS[0] is lltype.Void
    assert ON_ENTER_JIT.ARGS[1] == INVARIANTS
    assert ON_ENTER_JIT.ARGS[2] == r_instance.lowleveltype
    c_self = inputconst(lltype.Void, jitdriver)
    v_invariants = varoftype(INVARIANTS, 'invariants')
    c_hint = inputconst(lltype.Void, {'concrete': True})
    llops.genop('hint', [v_invariants, c_hint], resulttype=INVARIANTS)
    vlist = allvars[:num_greens]
    llops.genop('direct_call', [c_func, c_self, v_invariants, v_reds] + vlist)
    # generate ops to reload the 'reds' variables from the RedVarsHolder
    newvars = allvars[:num_greens]
    for name, v_value in zip(jitdriver.reds, allvars[num_greens:]):
        v_value = r_instance.getfield(v_reds, name, llops)
        newvars.append(v_value)
    newvars = [v for v in newvars if v.concretetype is not lltype.Void]
    # done, fill the block and link it to make it the startblock
    newblock.inputargs.append(v_invariants)
    newblock.operations[:] = llops
    newblock.closeblock(Link(newvars, graph.startblock))
    graph.startblock.isstartblock = False
    graph.startblock = newblock
    graph.startblock.isstartblock = True
    checkgraph(graph)
