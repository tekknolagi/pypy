
"""Python's standard exception class hierarchy.

Before Python 1.5, the standard exceptions were all simple string objects.
In Python 1.5, the standard exceptions were converted to classes organized
into a relatively flat hierarchy.  String-based standard exceptions were
optional, or used as a fallback if some problem occurred while importing
the exception module.  With Python 1.6, optional string-based standard
exceptions were removed (along with the -X command line flag).

The class exceptions were implemented in such a way as to be almost
completely backward compatible.  Some tricky uses of IOError could
potentially have broken, but by Python 1.6, all of these should have
been fixed.  As of Python 1.6, the class-based standard exceptions are
now implemented in C, and are guaranteed to exist in the Python
interpreter.

Here is a rundown of the class hierarchy.  The classes found here are
inserted into both the exceptions module and the `built-in' module.  It is
recommended that user defined class based exceptions be derived from the
`Exception' class, although this is currently not enforced.

BaseException
 +-- SystemExit
 +-- KeyboardInterrupt
 +-- Exception
      +-- GeneratorExit
      +-- StopIteration
      +-- StandardError
      |    +-- ArithmeticError
      |    |    +-- FloatingPointError
      |    |    +-- OverflowError
      |    |    +-- ZeroDivisionError
      |    +-- AssertionError
      |    +-- AttributeError
      |    +-- EnvironmentError
      |    |    +-- IOError
      |    |    +-- OSError
      |    |         +-- WindowsError (Windows)
      |    |         +-- VMSError (VMS)
      |    +-- EOFError
      |    +-- ImportError
      |    +-- LookupError
      |    |    +-- IndexError
      |    |    +-- KeyError
      |    +-- MemoryError
      |    +-- NameError
      |    |    +-- UnboundLocalError
      |    +-- ReferenceError
      |    +-- RuntimeError
      |    |    +-- NotImplementedError
      |    +-- SyntaxError
      |    |    +-- IndentationError
      |    |         +-- TabError
      |    +-- SystemError
      |    +-- TypeError
      |    +-- ValueError
      |    |    +-- UnicodeError
      |    |         +-- UnicodeDecodeError
      |    |         +-- UnicodeEncodeError
      |    |         +-- UnicodeTranslateError
      +-- Warning
           +-- DeprecationWarning
           +-- PendingDeprecationWarning
           +-- RuntimeWarning
           +-- SyntaxWarning
           +-- UserWarning
           +-- FutureWarning
           +-- ImportWarning
           +-- UnicodeWarning
"""

from pypy.interpreter.baseobjspace import ObjSpace, Wrappable, W_Root
from pypy.interpreter.typedef import TypeDef, interp_attrproperty_w,\
     GetSetProperty, interp_attrproperty, descr_get_dict, descr_set_dict
from pypy.interpreter.gateway import interp2app

def readwrite_attrproperty(name, cls, unwrapname):
    def fget(space, obj):
        return space.wrap(getattr(obj, name))
    def fset(space, obj, w_val):
        setattr(obj, name, getattr(space, unwrapname)(w_val))
    return GetSetProperty(fget, fset, cls=cls)

def readwrite_attrproperty_w(name, cls):
    def fget(space, obj):
        return getattr(obj, name)
    def fset(space, obj, w_val):
        setattr(obj, name, w_val)
    return GetSetProperty(fget, fset, cls=cls)

class W_BaseException(Wrappable):
    """Superclass representing the base of the exception hierarchy.

    The __getitem__ method is provided for backwards-compatibility
    and will be deprecated at some point. 
    """
    w_dict = None

    def __init__(self, space, args_w):
        self.args_w = args_w
        self.space = space
        if len(args_w) == 1:
            self.w_message = args_w[0]
        else:
            self.w_message = space.wrap("")

    def descr_str(self, space):
        lgt = len(self.args_w)
        if lgt == 0:
            return space.wrap('')
        elif lgt == 1:
            return space.str(self.w_message)
        else:
            return space.str(space.newtuple(self.args_w))
    descr_str.unwrap_spec = ['self', ObjSpace]

    def descr_repr(self, space):
        if self.args_w:
            args_repr = space.str_w(space.repr(space.newtuple(self.args_w)))
        else:
            args_repr = "()"
        clsname = self.getclass(space).getname(space, '?')
        return space.wrap(clsname + args_repr)
    descr_repr.unwrap_spec = ['self', ObjSpace]

    def descr_getargs(space, self):
        return space.newtuple(self.args_w)

    def getdict(self):
        if self.w_dict is None:
            self.w_dict = self.space.newdict()
        return self.w_dict

    def setdict(self, space, w_dict):
        if not space.is_true(space.isinstance( w_dict, space.w_dict )):
            raise OperationError( space.w_TypeError, space.wrap("setting exceptions's dictionary to a non-dict") )
        self.w_dict = w_dict


def _new(cls):
    def descr_new_base_exception(space, w_subtype, args_w):
        exc = space.allocate_instance(cls, w_subtype)
        cls.__init__(exc, space, args_w)
        return space.wrap(exc)
    descr_new_base_exception.unwrap_spec = [ObjSpace, W_Root, 'args_w']
    descr_new_base_exception.func_name = 'descr_new_' + cls.__name__
    return interp2app(descr_new_base_exception)

W_BaseException.typedef = TypeDef(
    'BaseException',
    __doc__ = W_BaseException.__doc__,
    __new__ = _new(W_BaseException),
    __str__ = interp2app(W_BaseException.descr_str),
    __repr__ = interp2app(W_BaseException.descr_repr),
    __dict__ = GetSetProperty(descr_get_dict, descr_set_dict,
                              cls=W_BaseException),
    message = interp_attrproperty_w('w_message', W_BaseException),
    args = GetSetProperty(W_BaseException.descr_getargs),
)

def _new_exception(name, base, docstring, **kwargs):
    class W_Exception(base):
        __doc__ = docstring

    W_Exception.__name__ = 'W_' + name

    for k, v in kwargs.items():
        kwargs[k] = interp2app(v.__get__(None, W_Exception))
    W_Exception.typedef = TypeDef(
        name,
        base.typedef,
        __doc__ = W_Exception.__doc__,
        __new__ = _new(W_Exception),
        **kwargs
    )
    return W_Exception

W_Exception = _new_exception('Exception', W_BaseException,
                         """Common base class for all non-exit exceptions.""")

W_GeneratorExit = _new_exception('GeneratorExit', W_Exception,
                          """Request that a generator exit.""")

W_StandardError = _new_exception('StandardError', W_Exception,
                         """Base class for all standard Python exceptions.""")

W_ValueError = _new_exception('ValueError', W_StandardError,
                         """Inappropriate argument value (of correct type).""")

W_ImportError = _new_exception('ImportError', W_StandardError,
                  """Import can't find module, or can't find name in module.""")

W_RuntimeError = _new_exception('RuntimeError', W_StandardError,
                     """Unspecified run-time error.""")

W_UnicodeError = _new_exception('UnicodeError', W_ValueError,
                          """Unicode related error.""")


class W_UnicodeTranslateError(W_UnicodeError):
    """Unicode translation error."""
    def __init__(self, space, w_obj, w_start, w_end, w_reason):
        self.object = space.unicode_w(w_obj)
        self.start = space.int_w(w_start)
        self.end = space.int_w(w_end)
        self.reason = space.str_w(w_reason)
        W_BaseException.__init__(self, space, [w_obj, w_start, w_end, w_reason])

    def descr_str(self, space):
        return space.appexec([space.wrap(self)], """(self):
            if self.end == self.start + 1:
                badchar = ord(self.object[self.start])
                if badchar <= 0xff:
                    return "can't translate character u'\\\\x%02x' in position %d: %s" % (badchar, self.start, self.reason)
                if badchar <= 0xffff:
                    return "can't translate character u'\\\\u%04x' in position %d: %s"%(badchar, self.start, self.reason)
                return "can't translate character u'\\\\U%08x' in position %d: %s"%(badchar, self.start, self.reason)
            return "can't translate characters in position %d-%d: %s" % (self.start, self.end - 1, self.reason)
        """)
    descr_str.unwrap_spec = ['self', ObjSpace]

def descr_new_unicode_translate_error(space, w_subtype, w_obj, w_start, w_end,
                                      w_reason):
    exc = space.allocate_instance(W_UnicodeTranslateError, w_subtype)
    W_UnicodeTranslateError.__init__(exc, space, w_obj, w_start,
                                     w_end, w_reason)
    return space.wrap(exc)

W_UnicodeTranslateError.typedef = TypeDef(
    'UnicodeTranslateError',
    W_UnicodeError.typedef,
    __doc__ = W_UnicodeTranslateError.__doc__,
    __new__ = interp2app(descr_new_unicode_translate_error),
    __str__ = interp2app(W_UnicodeTranslateError.descr_str),
    object = readwrite_attrproperty('object', W_UnicodeTranslateError, 'unicode_w'),
    start  = readwrite_attrproperty('start', W_UnicodeTranslateError, 'int_w'),
    end    = readwrite_attrproperty('end', W_UnicodeTranslateError, 'int_w'),
    reason = readwrite_attrproperty('reason', W_UnicodeTranslateError, 'str_w'),
)

W_LookupError = _new_exception('LookupError', W_StandardError,
                               """Base class for lookup errors.""")

def key_error_str(self, space):
    if len(self.args_w) == 0:
        return space.wrap('')
    elif len(self.args_w) == 1:
        return space.repr(self.args_w[0])
    else:
        return space.str(space.newtuple(self.args_w))
key_error_str.unwrap_spec = ['self', ObjSpace]
    
W_KeyError = _new_exception('KeyError', W_LookupError,
                            """Mapping key not found.""",
                            __str__ = key_error_str)

W_StopIteration = _new_exception('StopIteration', W_Exception,
                                 """Signal the end from iterator.next().""")

W_Warning = _new_exception('Warning', W_Exception,
                           """Base class for warning categories.""")

W_PendingDeprecationWarning = _new_exception('PendingDeprecationWarning',
                                             W_Warning,
       """Base class for warnings about features which will be deprecated in the future.""")

class W_EnvironmentError(W_StandardError):
    """Base class for I/O related errors."""

    def __init__(self, space, args_w):
        W_BaseException.__init__(self, space, args_w)
        self.w_errno = space.w_None
        self.w_strerror = space.w_None
        self.w_filename = space.w_None
        if 2 <= len(args_w) <= 3:
            self.w_errno = args_w[0]
            self.w_strerror = args_w[1]
        if len(args_w) == 3:
            self.w_filename = args_w[2]
            self.args_w = [args_w[0], args_w[1]]

    def descr_str(self, space):
        if not space.is_w(self.w_filename, space.w_None):
            return space.wrap("[Errno %d] %s: %s" % (space.int_w(self.w_errno),
                                                     space.str_w(self.w_strerror),
                                                     space.str_w(self.w_filename)))
        if (not space.is_w(self.w_errno, space.w_None) and
            not space.is_w(self.w_errno, space.w_None)):
            return space.wrap("[Errno %d] %s" % (space.int_w(self.w_errno),
                                                 space.str_w(self.w_strerror)))
        return W_BaseException.__str__(self, space)
    descr_str.unwrap_spec = ['self', ObjSpace]

W_EnvironmentError.typedef = TypeDef(
    'EnvironmentError',
    W_StandardError.typedef,
    __new__ = _new(W_EnvironmentError),
    __str__ = interp2app(W_EnvironmentError.descr_str)
    )

W_OSError = _new_exception('OSError', W_EnvironmentError,
                           """OS system call failed.""")

W_DeprecationWarning = _new_exception('DeprecationWarning', W_Warning,
                        """Base class for warnings about deprecated features.""")

W_ArithmeticError = _new_exception('ArithmeticError', W_StandardError,
                         """Base class for arithmetic errors.""")

W_FloatingPointError = _new_exception('FloatingPointError', W_ArithmeticError,
                                      """Floating point operation failed.""")

W_ReferenceError = _new_exception('ReferenceError', W_StandardError,
                           """Weak ref proxy used after referent went away.""")

W_NameError = _new_exception('NameError', W_StandardError,
                             """Name not found globally.""")

W_IOError = _new_exception('IOError', W_EnvironmentError,
                           """I/O operation failed.""")


class W_SyntaxError(W_StandardError):
    """Invalid syntax."""

    def __init__(self, space, args_w):
        W_BaseException.__init__(self, space, args_w)
        # that's not a self.w_message!!!
        if len(args_w) > 0:
            self.w_msg = args_w[0]
        else:
            self.w_msg = space.wrap('')
        if len(args_w) == 2:
            values_w = space.viewiterable(args_w[1], 4)
            self.w_filename = values_w[0]
            self.w_lineno   = values_w[1]
            self.w_offset   = values_w[2]
            self.w_text     = values_w[3]
        else:
            self.w_filename = space.w_None
            self.w_lineno   = space.w_None
            self.w_offset   = space.w_None
            self.w_text     = space.w_None

    def descr_str(self, space):
        return space.appexec([self], """(self):
            if type(self.msg) is not str:
                return str(self.msg)

            buffer = self.msg
            have_filename = type(self.filename) is str
            have_lineno = type(self.lineno) is int
            if have_filename:
                import os
                fname = os.path.basename(self.filename or "???")
                if have_lineno:
                    buffer = "%s (%s, line %ld)" % (self.msg, fname, self.lineno)
                else:
                    buffer ="%s (%s)" % (self.msg, fname)
            elif have_lineno:
                buffer = "%s (line %ld)" % (self.msg, self.lineno)
            return buffer
        """)

    descr_str.unwrap_spec = ['self', ObjSpace]

W_SyntaxError.typedef = TypeDef(
    'SyntaxError',
    W_StandardError.typedef,
    __new__ = _new(W_SyntaxError),
    __str__ = interp2app(W_SyntaxError.descr_str),
    __doc__ = W_SyntaxError.__doc__,
    msg      = readwrite_attrproperty_w('w_msg', W_SyntaxError),
    filename = readwrite_attrproperty_w('w_filename', W_SyntaxError),
    lineno   = readwrite_attrproperty_w('w_lineno', W_SyntaxError),
    offset   = readwrite_attrproperty_w('w_offset', W_SyntaxError),
    text     = readwrite_attrproperty_w('w_text', W_SyntaxError),
)

W_FutureWarning = _new_exception('FutureWarning', W_Warning,
    """Base class for warnings about constructs that will change semantically in the future.""")

class W_SystemExit(W_BaseException):
    """Request to exit from the interpreter."""
    
    def __init__(self, space, args_w):
        W_BaseException.__init__(self, space, args_w)
        if len(args_w) == 0:
            self.w_code = space.w_None
        elif len(args_w) == 1:
            self.w_code = args_w[0]
        else:
            self.w_code = space.newtuple(args_w)

W_SystemExit.typedef = TypeDef(
    'SystemExit',
    W_BaseException.typedef,
    __new__ = _new(W_SystemExit),
    __doc__ = W_SystemExit.__doc__,
    code    = readwrite_attrproperty_w('w_code', W_SystemExit)
)


