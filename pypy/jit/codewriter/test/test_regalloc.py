import py
from pypy.jit.codewriter import support
from pypy.jit.codewriter.regalloc import perform_register_allocation
from pypy.jit.codewriter.flatten import flatten_graph
from pypy.jit.codewriter.format import format_assembler


class TestRegAlloc:

    def make_graphs(self, func, values, type_system='lltype'):
        self.rtyper = support.annotate(func, values, type_system=type_system,
                                       backendoptimize=False)
        return self.rtyper.annotator.translator.graphs

    def check_assembler(self, graph, expected):
        """Only for simple graphs.  More complex graphs must first be
        transformed by jitter.py before they can be subjected to
        register allocation and flattening."""
        regalloc = perform_register_allocation(graph, 'int')
        ssarepr = flatten_graph(graph, {'int': regalloc})
        asm = format_assembler(ssarepr)
        assert asm == str(py.code.Source(expected).strip()) + '\n'

    def test_regalloc_simple(self):
        def f(a, b):
            return a + b
        graph = self.make_graphs(f, [5, 6])[0]
        regalloc = perform_register_allocation(graph, 'int')
        va, vb = graph.startblock.inputargs
        vc = graph.startblock.operations[0].result
        assert regalloc.getcolor(va) == 0
        assert regalloc.getcolor(vb) == 1
        assert regalloc.getcolor(vc) == 0

    def test_regalloc_void(self):
        def f(a, b):
            while a > 0:
                b += a
                a -= 1
            return b
        graph = self.make_graphs(f, [5, 6])[0]
        regalloc = perform_register_allocation(graph, 'float')
        # assert did not crash

    def test_regalloc_loop(self):
        def f(a, b):
            while a > 0:
                b += a
                a -= 1
            return b
        graph = self.make_graphs(f, [5, 6])[0]
        self.check_assembler(graph, """
            L1:
            int_gt %i0, $0, %i2
            goto_if_not L2, %i2
            int_add %i1, %i0, %i1
            int_sub %i0, $1, %i0
            goto L1
            L2:
            int_return %i1
        """)

    def test_regalloc_loop_swap(self):
        def f(a, b):
            while a > 0:
                a, b = b, a
            return b
        graph = self.make_graphs(f, [5, 6])[0]
        self.check_assembler(graph, """
            L1:
            int_gt %i0, $0, %i2
            goto_if_not L2, %i2
            int_rename i[%i1, %i0], i[%i0, %i1]
            goto L1
            L2:
            int_return %i1
        """)

    def test_regalloc_loop_constant(self):
        def f(a, b):
            while a > 0:
                a, b = b, 2
            return b
        graph = self.make_graphs(f, [5, 6])[0]
        self.check_assembler(graph, """
            L1:
            int_gt %i0, $0, %i0
            goto_if_not L2, %i0
            int_copy %i1, %i0
            int_copy $2, %i1
            goto L1
            L2:
            int_return %i1
        """)

    def test_regalloc_cycle(self):
        def f(a, b, c):
            while a > 0:
                a, b, c = b, c, a
            return b
        graph = self.make_graphs(f, [5, 6, 7])[0]
        self.check_assembler(graph, """
            L1:
            int_gt %i0, $0, %i3
            goto_if_not L2, %i3
            int_rename i[%i1, %i2, %i0], i[%i0, %i1, %i2]
            goto L1
            L2:
            int_return %i1
        """)

    def test_regalloc_same_as_var(self):
        def f(a, b, c):
            while a > 0:
                b = c
            return b
        graph = self.make_graphs(f, [5, 6, 7])[0]
        self.check_assembler(graph, """
            L1:
            int_gt %i0, $0, %i3
            goto_if_not L2, %i3
            int_copy %i2, %i1
            goto L1
            L2:
            int_return %i1
        """)
