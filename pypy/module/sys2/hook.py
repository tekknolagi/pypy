from pypy.interpreter.pyopcode import print_item_to, print_newline_to, sys_stdout 

def displayhook(space, w_obj):
    if not space.is_w(w_obj, space.w_None): 
        space.setitem(space.builtin.w_dict, space.wrap('_'), w_obj)
        # NB. this is slightly more complicated in CPython,
        # see e.g. the difference with  >>> print 5,; 8
        print_item_to(space, w_obj, sys_stdout(space))
        print_newline_to(space, sys_stdout(space))

__displayhook__ = displayhook  # this is exactly like in CPython

