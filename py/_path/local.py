"""
local path implementation.
"""
from contextlib import contextmanager
import sys, os, re, atexit
import py
from py._path import common
from stat import S_ISLNK, S_ISDIR, S_ISREG

from os.path import abspath, normpath, isabs, exists, isdir, isfile, islink

iswin32 = sys.platform == "win32" or (getattr(os, '_name', False) == 'nt')

if sys.version_info > (3,0):
    def map_as_list(func, iter):
        return list(map(func, iter))
else:
    map_as_list = map

class Stat(object):
    def __getattr__(self, name):
        return getattr(self._osstatresult, "st_" + name)

    def __init__(self, path, osstatresult):
        self.path = path
        self._osstatresult = osstatresult

    @property
    def owner(self):
        if iswin32:
            raise NotImplementedError("XXX win32")
        import pwd
        entry = py.error.checked_call(pwd.getpwuid, self.uid)
        return entry[0]

    @property
    def group(self):
        """ return group name of file. """
        if iswin32:
            raise NotImplementedError("XXX win32")
        import grp
        entry = py.error.checked_call(grp.getgrgid, self.gid)
        return entry[0]

    def isdir(self):
        return S_ISDIR(self._osstatresult.st_mode)

    def isfile(self):
        return S_ISREG(self._osstatresult.st_mode)

    def islink(self):
        st = self.path.lstat()
        return S_ISLNK(self._osstatresult.st_mode)

class PosixPath(common.PathBase):
    def chown(self, user, group, rec=0):
        """ change ownership to the given user and group.
            user and group may be specified by a number or
            by a name.  if rec is True change ownership
            recursively.
        """
        uid = getuserid(user)
        gid = getgroupid(group)
        if rec:
            for x in self.visit(rec=lambda x: x.check(link=0)):
                if x.check(link=0):
                    py.error.checked_call(os.chown, str(x), uid, gid)
        py.error.checked_call(os.chown, str(self), uid, gid)

    def readlink(self):
        """ return value of a symbolic link. """
        return py.error.checked_call(os.readlink, self.strpath)

    def mklinkto(self, oldname):
        """ posix style hard link to another name. """
        py.error.checked_call(os.link, str(oldname), str(self))

    def mksymlinkto(self, value, absolute=1):
        """ create a symbolic link with the given value (pointing to another name). """
        if absolute:
            py.error.checked_call(os.symlink, str(value), self.strpath)
        else:
            base = self.common(value)
            # with posix local paths '/' is always a common base
            relsource = self.__class__(value).relto(base)
            reldest = self.relto(base)
            n = reldest.count(self.sep)
            target = self.sep.join(('..', )*n + (relsource, ))
            py.error.checked_call(os.symlink, target, self.strpath)

def getuserid(user):
    import pwd
    if not isinstance(user, int):
        user = pwd.getpwnam(user)[2]
    return user

def getgroupid(group):
    import grp
    if not isinstance(group, int):
        group = grp.getgrnam(group)[2]
    return group

FSBase = not iswin32 and PosixPath or common.PathBase

class LocalPath(FSBase):
    """ object oriented interface to os.path and other local filesystem
        related information.
    """
    class ImportMismatchError(ImportError):
        """ raised on pyimport() if there is a mismatch of __file__'s"""

    sep = os.sep
    class Checkers(common.Checkers):
        def _stat(self):
            try:
                return self._statcache
            except AttributeError:
                try:
                    self._statcache = self.path.stat()
                except py.error.ELOOP:
                    self._statcache = self.path.lstat()
                return self._statcache

        def dir(self):
            return S_ISDIR(self._stat().mode)

        def file(self):
            return S_ISREG(self._stat().mode)

        def exists(self):
            return self._stat()

        def link(self):
            st = self.path.lstat()
            return S_ISLNK(st.mode)

    def __init__(self, path=None, expanduser=False):
        """ Initialize and return a local Path instance.

        Path can be relative to the current directory.
        If path is None it defaults to the current working directory.
        If expanduser is True, tilde-expansion is performed.
        Note that Path instances always carry an absolute path.
        Note also that passing in a local path object will simply return
        the exact same path object. Use new() to get a new copy.
        """
        if path is None:
            self.strpath = py.error.checked_call(os.getcwd)
        elif isinstance(path, common.PathBase):
            self.strpath = path.strpath
        elif isinstance(path, py.builtin._basestring):
            if expanduser:
                path = os.path.expanduser(path)
            self.strpath = abspath(normpath(path))
        else:
            raise ValueError("can only pass None, Path instances "
                             "or non-empty strings to LocalPath")

    def __hash__(self):
        return hash(self.strpath)

    def __eq__(self, other):
        s1 = self.strpath
        s2 = getattr(other, "strpath", other)
        if iswin32:
            s1 = s1.lower()
            try:
                s2 = s2.lower()
            except AttributeError:
                return False
        return s1 == s2

    def __ne__(self, other):
        return not (self == other)

    def __lt__(self, other):
        return self.strpath < getattr(other, "strpath", other)

    def __gt__(self, other):
        return self.strpath > getattr(other, "strpath", other)

    def samefile(self, other):
        """ return True if 'other' references the same file as 'self'.
        """
        other = getattr(other, "strpath", other)
        if not isabs(other):
            other = abspath(other)
        if self == other:
            return True
        if iswin32:
            return False # there is no samefile
        return py.error.checked_call(
                os.path.samefile, self.strpath, other)

    def remove(self, rec=1, ignore_errors=False):
        """ remove a file or directory (or a directory tree if rec=1).
        if ignore_errors is True, errors while removing directories will
        be ignored.
        """
        if self.check(dir=1, link=0):
            if rec:
                # force remove of readonly files on windows
                if iswin32:
                    self.chmod(448, rec=1) # octcal 0700
                py.error.checked_call(py.std.shutil.rmtree, self.strpath,
                    ignore_errors=ignore_errors)
            else:
                py.error.checked_call(os.rmdir, self.strpath)
        else:
            if iswin32:
                self.chmod(448) # octcal 0700
            py.error.checked_call(os.remove, self.strpath)

    def computehash(self, hashtype="md5", chunksize=524288):
        """ return hexdigest of hashvalue for this file. """
        try:
            try:
                import hashlib as mod
            except ImportError:
                if hashtype == "sha1":
                    hashtype = "sha"
                mod = __import__(hashtype)
            hash = getattr(mod, hashtype)()
        except (AttributeError, ImportError):
            raise ValueError("Don't know how to compute %r hash" %(hashtype,))
        f = self.open('rb')
        try:
            while 1:
                buf = f.read(chunksize)
                if not buf:
                    return hash.hexdigest()
                hash.update(buf)
        finally:
            f.close()

    def new(self, **kw):
        """ create a modified version of this path.
            the following keyword arguments modify various path parts::

              a:/some/path/to/a/file.ext
              xx                           drive
              xxxxxxxxxxxxxxxxx            dirname
                                xxxxxxxx   basename
                                xxxx       purebasename
                                     xxx   ext
        """
        obj = object.__new__(self.__class__)
        if not kw:
            obj.strpath = self.strpath
            return obj
        drive, dirname, basename, purebasename,ext = self._getbyspec(
             "drive,dirname,basename,purebasename,ext")
        if 'basename' in kw:
            if 'purebasename' in kw or 'ext' in kw:
                raise ValueError("invalid specification %r" % kw)
        else:
            pb = kw.setdefault('purebasename', purebasename)
            try:
                ext = kw['ext']
            except KeyError:
                pass
            else:
                if ext and not ext.startswith('.'):
                    ext = '.' + ext
            kw['basename'] = pb + ext

        if ('dirname' in kw and not kw['dirname']):
            kw['dirname'] = drive
        else:
            kw.setdefault('dirname', dirname)
        kw.setdefault('sep', self.sep)
        obj.strpath = normpath(
            "%(dirname)s%(sep)s%(basename)s" % kw)
        return obj

    def _getbyspec(self, spec):
        """ see new for what 'spec' can be. """
        res = []
        parts = self.strpath.split(self.sep)

        args = filter(None, spec.split(',') )
        append = res.append
        for name in args:
            if name == 'drive':
                append(parts[0])
            elif name == 'dirname':
                append(self.sep.join(parts[:-1]))
            else:
                basename = parts[-1]
                if name == 'basename':
                    append(basename)
                else:
                    i = basename.rfind('.')
                    if i == -1:
                        purebasename, ext = basename, ''
                    else:
                        purebasename, ext = basename[:i], basename[i:]
                    if name == 'purebasename':
                        append(purebasename)
                    elif name == 'ext':
                        append(ext)
                    else:
                        raise ValueError("invalid part specification %r" % name)
        return res

    def join(self, *args, **kwargs):
        """ return a new path by appending all 'args' as path
        components.  if abs=1 is used restart from root if any
        of the args is an absolute path.
        """
        sep = self.sep
        strargs = [getattr(arg, "strpath", arg) for arg in args]
        strpath = self.strpath
        if kwargs.get('abs'):
            newargs = []
            for arg in reversed(strargs):
                if isabs(arg):
                    strpath = arg
                    strargs = newargs
                    break
                newargs.insert(0, arg)
        for arg in strargs:
            arg = arg.strip(sep)
            if iswin32:
                # allow unix style paths even on windows.
                arg = arg.strip('/')
                arg = arg.replace('/', sep)
            strpath = strpath + sep + arg
        obj = object.__new__(self.__class__)
        obj.strpath = normpath(strpath)
        return obj

    def open(self, mode='r', ensure=False):
        """ return an opened file with the given mode.

        If ensure is True, create parent directories if needed.
        """
        if ensure:
            self.dirpath().ensure(dir=1)
        return py.error.checked_call(open, self.strpath, mode)

    def _fastjoin(self, name):
        child = object.__new__(self.__class__)
        child.strpath = self.strpath + self.sep + name
        return child

    def islink(self):
        return islink(self.strpath)

    def check(self, **kw):
        if not kw:
            return exists(self.strpath)
        if len(kw) == 1:
            if "dir" in kw:
                return not kw["dir"] ^ isdir(self.strpath)
            if "file" in kw:
                return not kw["file"] ^ isfile(self.strpath)
        return super(LocalPath, self).check(**kw)

    _patternchars = set("*?[" + os.path.sep)
    def listdir(self, fil=None, sort=None):
        """ list directory contents, possibly filter by the given fil func
            and possibly sorted.
        """
        if fil is None and sort is None:
            names = py.error.checked_call(os.listdir, self.strpath)
            return map_as_list(self._fastjoin, names)
        if isinstance(fil, py.builtin._basestring):
            if not self._patternchars.intersection(fil):
                child = self._fastjoin(fil)
                if exists(child.strpath):
                    return [child]
                return []
            fil = common.FNMatcher(fil)
        names = py.error.checked_call(os.listdir, self.strpath)
        res = []
        for name in names:
            child = self._fastjoin(name)
            if fil is None or fil(child):
                res.append(child)
        self._sortlist(res, sort)
        return res

    def size(self):
        """ return size of the underlying file object """
        return self.stat().size

    def mtime(self):
        """ return last modification time of the path. """
        return self.stat().mtime

    def copy(self, target, mode=False):
        """ copy path to target."""
        if self.check(file=1):
            if target.check(dir=1):
                target = target.join(self.basename)
            assert self!=target
            copychunked(self, target)
            if mode:
                copymode(self, target)
        else:
            def rec(p):
                return p.check(link=0)
            for x in self.visit(rec=rec):
                relpath = x.relto(self)
                newx = target.join(relpath)
                newx.dirpath().ensure(dir=1)
                if x.check(link=1):
                    newx.mksymlinkto(x.readlink())
                    continue
                elif x.check(file=1):
                    copychunked(x, newx)
                elif x.check(dir=1):
                    newx.ensure(dir=1)
                if mode:
                    copymode(x, newx)

    def rename(self, target):
        """ rename this path to target. """
        target = getattr(target, "strpath", target)
        return py.error.checked_call(os.rename, self.strpath, target)

    def dump(self, obj, bin=1):
        """ pickle object into path location"""
        f = self.open('wb')
        try:
            py.error.checked_call(py.std.pickle.dump, obj, f, bin)
        finally:
            f.close()

    def mkdir(self, *args):
        """ create & return the directory joined with args. """
        p = self.join(*args)
        py.error.checked_call(os.mkdir, getattr(p, "strpath", p))
        return p

    def write(self, data, mode='w', ensure=False):
        """ write data into path.   If ensure is True create
        missing parent directories.
        """
        if ensure:
            self.dirpath().ensure(dir=1)
        if 'b' in mode:
            if not py.builtin._isbytes(data):
                raise ValueError("can only process bytes")
        else:
            if not py.builtin._istext(data):
                if not py.builtin._isbytes(data):
                    data = str(data)
                else:
                    data = py.builtin._totext(data, sys.getdefaultencoding())
        f = self.open(mode)
        try:
            f.write(data)
        finally:
            f.close()

    def _ensuredirs(self):
        parent = self.dirpath()
        if parent == self:
            return self
        if parent.check(dir=0):
            parent._ensuredirs()
        if self.check(dir=0):
            try:
                self.mkdir()
            except py.error.EEXIST:
                # race condition: file/dir created by another thread/process.
                # complain if it is not a dir
                if self.check(dir=0):
                    raise
        return self

    def ensure(self, *args, **kwargs):
        """ ensure that an args-joined path exists (by default as
            a file). if you specify a keyword argument 'dir=True'
            then the path is forced to be a directory path.
        """
        p = self.join(*args)
        if kwargs.get('dir', 0):
            return p._ensuredirs()
        else:
            p.dirpath()._ensuredirs()
            if not p.check(file=1):
                p.open('w').close()
            return p

    def stat(self, raising=True):
        """ Return an os.stat() tuple. """
        if raising == True:
            return Stat(self, py.error.checked_call(os.stat, self.strpath))
        try:
            return Stat(self, os.stat(self.strpath))
        except KeyboardInterrupt:
            raise
        except Exception:
            return None

    def lstat(self):
        """ Return an os.lstat() tuple. """
        return Stat(self, py.error.checked_call(os.lstat, self.strpath))

    def setmtime(self, mtime=None):
        """ set modification time for the given path.  if 'mtime' is None
        (the default) then the file's mtime is set to current time.

        Note that the resolution for 'mtime' is platform dependent.
        """
        if mtime is None:
            return py.error.checked_call(os.utime, self.strpath, mtime)
        try:
            return py.error.checked_call(os.utime, self.strpath, (-1, mtime))
        except py.error.EINVAL:
            return py.error.checked_call(os.utime, self.strpath, (self.atime(), mtime))

    def chdir(self):
        """ change directory to self and return old current directory """
        try:
            old = self.__class__()
        except py.error.ENOENT:
            old = None
        py.error.checked_call(os.chdir, self.strpath)
        return old


    @contextmanager
    def as_cwd(self):
        """ return context manager which changes to current dir during the
        managed "with" context. On __enter__ it returns the old dir.
        """
        old = self.chdir()
        try:
            yield old
        finally:
            old.chdir()

    def realpath(self):
        """ return a new path which contains no symbolic links."""
        return self.__class__(os.path.realpath(self.strpath))

    def atime(self):
        """ return last access time of the path. """
        return self.stat().atime

    def __repr__(self):
        return 'local(%r)' % self.strpath

    def __str__(self):
        """ return string representation of the Path. """
        return self.strpath

    def pypkgpath(self, pkgname=None):
        """ return the Python package path by looking for a
            pkgname.  If pkgname is None look for the last
            directory upwards which still contains an __init__.py
            and whose basename is python-importable.
            Return None if a pkgpath can not be determined.
        """
        pkgpath = None
        for parent in self.parts(reverse=True):
            if pkgname is None:
                if parent.check(file=1):
                    continue
                if not isimportable(parent.basename):
                    break
                if parent.join('__init__.py').check():
                    pkgpath = parent
                    continue
                return pkgpath
            else:
                if parent.basename == pkgname:
                    return parent
        return pkgpath

    def _prependsyspath(self, path):
        s = str(path)
        if s != sys.path[0]:
            #print "prepending to sys.path", s
            sys.path.insert(0, s)

    def chmod(self, mode, rec=0):
        """ change permissions to the given mode. If mode is an
            integer it directly encodes the os-specific modes.
            if rec is True perform recursively.
        """
        if not isinstance(mode, int):
            raise TypeError("mode %r must be an integer" % (mode,))
        if rec:
            for x in self.visit(rec=rec):
                py.error.checked_call(os.chmod, str(x), mode)
        py.error.checked_call(os.chmod, str(self), mode)

    def pyimport(self, modname=None, ensuresyspath=True):
        """ return path as an imported python module.
            if modname is None, look for the containing package
            and construct an according module name.
            The module will be put/looked up in sys.modules.
        """
        if not self.check():
            raise py.error.ENOENT(self)
        #print "trying to import", self
        pkgpath = None
        if modname is None:
            pkgpath = self.pypkgpath()
            if pkgpath is not None:
                if ensuresyspath:
                    self._prependsyspath(pkgpath.dirpath())
                __import__(pkgpath.basename)
                pkg = sys.modules[pkgpath.basename]
                names = self.new(ext='').relto(pkgpath.dirpath())
                names = names.split(self.sep)
                if names and names[-1] == "__init__":
                    names.pop()
                modname = ".".join(names)
            else:
                # no package scope, still make it possible
                if ensuresyspath:
                    self._prependsyspath(self.dirpath())
                modname = self.purebasename
            __import__(modname)
            mod = sys.modules[modname]
            if self.basename == "__init__.py":
                return mod # we don't check anything as we might
                       # we in a namespace package ... too icky to check
            modfile = mod.__file__
            if modfile[-4:] in ('.pyc', '.pyo'):
                modfile = modfile[:-1]
            elif modfile.endswith('$py.class'):
                modfile = modfile[:-9] + '.py'
            if modfile.endswith(os.path.sep + "__init__.py"):
                if self.basename != "__init__.py":
                    modfile = modfile[:-12]

            try:
                issame = self.samefile(modfile)
            except py.error.ENOENT:
                issame = False
            if not issame:
                raise self.ImportMismatchError(modname, modfile, self)
            return mod
        else:
            try:
                return sys.modules[modname]
            except KeyError:
                # we have a custom modname, do a pseudo-import
                mod = py.std.types.ModuleType(modname)
                mod.__file__ = str(self)
                sys.modules[modname] = mod
                try:
                    py.builtin.execfile(str(self), mod.__dict__)
                except:
                    del sys.modules[modname]
                    raise
                return mod

    def sysexec(self, *argv, **popen_opts):
        """ return stdout text from executing a system child process,
            where the 'self' path points to executable.
            The process is directly invoked and not through a system shell.
        """
        from subprocess import Popen, PIPE
        argv = map_as_list(str, argv)
        popen_opts['stdout'] = popen_opts['stderr'] = PIPE
        proc = Popen([str(self)] + argv, **popen_opts)
        stdout, stderr = proc.communicate()
        ret = proc.wait()
        if py.builtin._isbytes(stdout):
            stdout = py.builtin._totext(stdout, sys.getdefaultencoding())
        if ret != 0:
            if py.builtin._isbytes(stderr):
                stderr = py.builtin._totext(stderr, sys.getdefaultencoding())
            raise py.process.cmdexec.Error(ret, ret, str(self),
                                           stdout, stderr,)
        return stdout

    def sysfind(cls, name, checker=None, paths=None):
        """ return a path object found by looking at the systems
            underlying PATH specification. If the checker is not None
            it will be invoked to filter matching paths.  If a binary
            cannot be found, None is returned
            Note: This is probably not working on plain win32 systems
            but may work on cygwin.
        """
        if isabs(name):
            p = py.path.local(name)
            if p.check(file=1):
                return p
        else:
            if paths is None:
                if iswin32:
                    paths = py.std.os.environ['Path'].split(';')
                    if '' not in paths and '.' not in paths:
                        paths.append('.')
                    try:
                        systemroot = os.environ['SYSTEMROOT']
                    except KeyError:
                        pass
                    else:
                        paths = [re.sub('%SystemRoot%', systemroot, path)
                                 for path in paths]
                else:
                    paths = py.std.os.environ['PATH'].split(':')
            tryadd = ['']
            if iswin32:
                tryadd += os.environ['PATHEXT'].split(os.pathsep)

            for x in paths:
                for addext in tryadd:
                    p = py.path.local(x).join(name, abs=True) + addext
                    try:
                        if p.check(file=1):
                            if checker:
                                if not checker(p):
                                    continue
                            return p
                    except py.error.EACCES:
                        pass
        return None
    sysfind = classmethod(sysfind)

    def _gethomedir(cls):
        try:
            x = os.environ['HOME']
        except KeyError:
            try:
                x = os.environ["HOMEDRIVE"] + os.environ['HOMEPATH']
            except KeyError:
                return None
        return cls(x)
    _gethomedir = classmethod(_gethomedir)

    #"""
    #special class constructors for local filesystem paths
    #"""
    def get_temproot(cls):
        """ return the system's temporary directory
            (where tempfiles are usually created in)
        """
        return py.path.local(py.std.tempfile.gettempdir())
    get_temproot = classmethod(get_temproot)

    def mkdtemp(cls, rootdir=None):
        """ return a Path object pointing to a fresh new temporary directory
            (which we created ourself).
        """
        import tempfile
        if rootdir is None:
            rootdir = cls.get_temproot()
        return cls(py.error.checked_call(tempfile.mkdtemp, dir=str(rootdir)))
    mkdtemp = classmethod(mkdtemp)

    def make_numbered_dir(cls, prefix='session-', rootdir=None, keep=3,
                          lock_timeout = 172800,   # two days
                          min_timeout = 300):      # five minutes
        """ return unique directory with a number greater than the current
            maximum one.  The number is assumed to start directly after prefix.
            if keep is true directories with a number less than (maxnum-keep)
            will be removed.
        """
        if rootdir is None:
            rootdir = cls.get_temproot()

        def parse_num(path):
            """ parse the number out of a path (if it matches the prefix) """
            bn = path.basename
            if bn.startswith(prefix):
                try:
                    return int(bn[len(prefix):])
                except ValueError:
                    pass

        # compute the maximum number currently in use with the
        # prefix
        lastmax = None
        while True:
            maxnum = -1
            for path in rootdir.listdir():
                num = parse_num(path)
                if num is not None:
                    maxnum = max(maxnum, num)

            # make the new directory
            try:
                udir = rootdir.mkdir(prefix + str(maxnum+1))
            except py.error.EEXIST:
                # race condition: another thread/process created the dir
                # in the meantime.  Try counting again
                if lastmax == maxnum:
                    raise
                lastmax = maxnum
                continue
            break

        # put a .lock file in the new directory that will be removed at
        # process exit
        if lock_timeout:
            lockfile = udir.join('.lock')
            mypid = os.getpid()
            if hasattr(lockfile, 'mksymlinkto'):
                lockfile.mksymlinkto(str(mypid))
            else:
                lockfile.write(str(mypid))
            def try_remove_lockfile():
                # in a fork() situation, only the last process should
                # remove the .lock, otherwise the other processes run the
                # risk of seeing their temporary dir disappear.  For now
                # we remove the .lock in the parent only (i.e. we assume
                # that the children finish before the parent).
                if os.getpid() != mypid:
                    return
                try:
                    lockfile.remove()
                except py.error.Error:
                    pass
            atexit.register(try_remove_lockfile)

        # prune old directories
        if keep:
            for path in rootdir.listdir():
                num = parse_num(path)
                if num is not None and num <= (maxnum - keep):
                    if min_timeout:
                        # NB: doing this is needed to prevent (or reduce
                        # a lot the chance of) the following situation:
                        # 'keep+1' processes call make_numbered_dir() at
                        # the same time, they create dirs, but then the
                        # last process notices the first dir doesn't have
                        # (yet) a .lock in it and kills it.
                        try:
                            t1 = path.lstat().mtime
                            t2 = lockfile.lstat().mtime
                            if abs(t2-t1) < min_timeout:
                                continue   # skip directories too recent
                        except py.error.Error:
                            continue   # failure to get a time, better skip
                    lf = path.join('.lock')
                    try:
                        t1 = lf.lstat().mtime
                        t2 = lockfile.lstat().mtime
                        if not lock_timeout or abs(t2-t1) < lock_timeout:
                            continue   # skip directories still locked
                    except py.error.Error:
                        pass   # assume that it means that there is no 'lf'
                    try:
                        path.remove(rec=1)
                    except KeyboardInterrupt:
                        raise
                    except: # this might be py.error.Error, WindowsError ...
                        pass

        # make link...
        try:
            username = os.environ['USER']           #linux, et al
        except KeyError:
            try:
                username = os.environ['USERNAME']   #windows
            except KeyError:
                username = 'current'

        src  = str(udir)
        dest = src[:src.rfind('-')] + '-' + username
        try:
            os.unlink(dest)
        except OSError:
            pass
        try:
            os.symlink(src, dest)
        except (OSError, AttributeError, NotImplementedError):
            pass

        return udir
    make_numbered_dir = classmethod(make_numbered_dir)

def copymode(src, dest):
    py.std.shutil.copymode(str(src), str(dest))

def copychunked(src, dest):
    chunksize = 524288 # half a meg of bytes
    fsrc = src.open('rb')
    try:
        fdest = dest.open('wb')
        try:
            while 1:
                buf = fsrc.read(chunksize)
                if not buf:
                    break
                fdest.write(buf)
        finally:
            fdest.close()
    finally:
        fsrc.close()

def isimportable(name):
    if name:
        if not (name[0].isalpha() or name[0] == '_'):
            return False
        name= name.replace("_", '')
        return not name or name.isalnum()
