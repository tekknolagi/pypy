import py
py.path.local(__file__)
from rpython.jit.tl.threadedcode import tla
from rpython.rlib import jit
from time import time

def entry_point(args):
    for i in range(len(args)):
        if args[i] == "--jit":
            if len(args) == i + 1:
                print "missing argument after --jit"
                return 2
            jitarg = args[i + 1]
            del args[i:i+2]
            jit.set_user_param(None, jitarg)
            break

    if len(args) < 3:
        print "Usage: %s filename x [iter (optional)]" % (args[0],)
        return 2
    filename = args[1]
    x = int(args[2])
    try:
        N = int(args[3])
    except IndexError:
        N = 100
    w_x = tla.W_IntObject(x)
    bytecode = load_bytecode(filename)
    times = []

    for i in range(N):
        s = time()
        w_res = tla.run(bytecode, w_x)
        e = time()
        times.append(e - s)
    # print w_res.getrepr()
    for t in times:
        print t
    return 0

def load_bytecode(filename):
    from rpython.rlib.streamio import open_file_as_stream
    f = open_file_as_stream(filename)
    bytecode = f.readall()
    f.close()
    return bytecode

def target(driver, args):
    return entry_point

# ____________________________________________________________


if __name__ == '__main__':
    import sys
    sys.exit(entry_point(sys.argv))