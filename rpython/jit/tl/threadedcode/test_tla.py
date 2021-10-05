import py
import pytest

from rpython.jit.tl.threadedcode import tla
from rpython.jit.tl.threadedcode.tla import \
    W_Object, W_IntObject, W_StringObject, Frame

def assemble(mylist):
    return ''.join([chr(x) for x in mylist])

def interp(mylist, w_arg):
    bytecode = assemble(mylist)
    return tla.run(bytecode, w_arg)

class TestFrame:

    def test_add(self):
        code = [
            tla.CONST_INT, 123,
            tla.ADD,
            tla.EXIT
        ]
        res = interp(code, W_IntObject(123))
        assert res.intvalue == 123 + 123

    def test_sub(self):
        code = [
            tla.CONST_INT, 123,
            tla.SUB,
            tla.EXIT
        ]
        res = interp(code, W_IntObject(234))
        assert res.intvalue == 234 - 123

    def test_mul(self):
        code = [
            tla.CONST_INT, 123,
            tla.MUL,
            tla.EXIT
        ]
        res = interp(code, W_IntObject(234))
        assert res.intvalue == 234 * 123

    def test_div(self):
        code = [
            tla.CONST_INT, 123,
            tla.DIV,
            tla.EXIT
        ]
        res = interp(code, W_IntObject(234))
        assert res.intvalue == 234 / 123

    def test_mod(self):
        code = [
            tla.CONST_INT, 2,
            tla.MOD,
            tla.EXIT
        ]
        res = interp(code, W_IntObject(10))
        assert res.intvalue == 0
        res = interp(code, W_IntObject(13))
        assert res.intvalue == 1

    def test_jump(self):
        code = [
            tla.JUMP, 3,
            tla.ADD,
            tla.EXIT
        ]
        res = interp(code, W_IntObject(234))
        assert res.intvalue == 234

    def test_call(self):
        code = [
            tla.CALL, 3,
            tla.EXIT,
            tla.CONST_INT, 12,
            tla.ADD,
            tla.RET, 1
        ]
        res = interp(code, W_IntObject(34))
        assert res.intvalue == 34 + 12

    def test_simple_loop(self):
        code = [
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 11,
            tla.CONST_INT, 1,
            tla.SUB,
            tla.JUMP, 0,
            tla.EXIT,
        ]
        res = interp(code, W_IntObject(100))
        assert res.intvalue == 0

    def test_double_loop(self):
        code = [
            tla.DUP,
            tla.CONST_INT, 1,
            tla.SUB,
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 12,
            tla.JUMP, 1,
            tla.POP,
            tla.CONST_INT, 1,
            tla.SUB,
            tla.DUP,
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 25,
            tla.JUMP, 1,
            tla.EXIT
        ]
        res = interp(code, W_IntObject(3))
        assert res.intvalue == 0

    def test_call(self):
        code = [
            tla.CALL, 11,
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 10,
            tla.JUMP, 0,
            tla.EXIT,
            # function sub_1(x)
            tla.CONST_INT, 1,
            tla.SUB,
            tla.RET, 1
        ]
        res = interp(code, W_IntObject(10))
        assert res.intvalue == 0


from rpython.jit.metainterp.test.support import LLJitMixin

class TestLLType(LLJitMixin):

    def test_jit_call(self):
        code = [
            tla.CALL, 11,
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 10,
            tla.JUMP, 0,
            tla.EXIT,
            # function sub_1(x)
            tla.CONST_INT, 1,
            tla.SUB,
            tla.RET, 1
        ]
        def interp_w(intvalue):
            w_result = interp(code, W_IntObject(intvalue))
            assert isinstance(w_result, W_IntObject)
            return w_result.intvalue
        res = self.meta_interp(interp_w, [42])

    def test_jit_loop(self):
        code = [
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 11,
            tla.CONST_INT, 1,
            tla.SUB,
            tla.JUMP, 0,
            tla.CONST_INT, 10,
            tla.SUB,
            tla.EXIT,
        ]
        def interp_w(intvalue):
            w_result = interp(code, W_IntObject(intvalue))
            assert isinstance(w_result, W_IntObject)
            return w_result.intvalue
        res = self.meta_interp(interp_w, [42])
        assert res == -10

    def test_jit_double_loop(self):
        code = [
            tla.DUP,
            tla.CONST_INT, 1,
            tla.SUB,
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 12,
            tla.JUMP, 1,
            tla.POP,
            tla.CONST_INT, 1,
            tla.SUB,
            tla.DUP,
            tla.DUP,
            tla.CONST_INT, 1,
            tla.LT,
            tla.JUMP_IF, 25,
            tla.JUMP, 1,
            tla.EXIT
        ]
        def interp_w(intvalue):
            w_result = interp(code, W_IntObject(intvalue))
            assert isinstance(w_result, W_IntObject)
            return w_result.intvalue
        res = self.meta_interp(interp_w, [42])
        assert res == 0
