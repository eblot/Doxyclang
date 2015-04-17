#!/usr/bin/env python3

import re
import os
import sublime, sublime_plugin
import sys

from collections import Counter, defaultdict, deque
from pprint import pformat, pprint
from subprocess import Popen, PIPE
from tempfile import mkdtemp, mkstemp


# -----------------------------------------------------------------------------
# Clang check parsing
# -----------------------------------------------------------------------------

def _rx(rexp, count):
    """Rename (suffix with index) a RE group name"""
    return re.sub(r'\(\?P\<(\w+)\>', '(?P<\\g<1>%d>' % count, rexp)


def _alt(*res):
    return r'(?:' + r'|'.join(res) + r')'


class Parser(object):
    """Parse clang-check AST dump to extract useful hints for Doxygen
    """

    DEPTH_RE = r'^(?P<depth>(?:[| ]*)[|`]-)?'
    DEF_RE = r'(?P<stmt>[A-Za-z]+)\s(?P<ref>0x[0-9a-f]+)\s'
    BACKREF_RE = r'(?:(parent|prev)\s(?P<bref>0x[0-9a-f]+)\s)?'
    SCRATCH_RE = r'(?:<scratch space>:(?P<scratch>\d+:\d+))'
    NULL_RE = r'(?P<null><<<NULL>>>)'
    FIELD_RE = r"'(?P<fld>\w*)'"
    PATH_RE = r'(?P<path>/[\w/\.\-]+:\d+:\d+)'
    LINE_RE = r'(?:line:(?P<line>\d+:\d+))'
    COL_RE = r'(?:col:(?P<col>\d+))'
    ISLOC_RE = r'<invalid sloc>'
    LOC1_RE = _alt(PATH_RE, LINE_RE, COL_RE, ISLOC_RE, SCRATCH_RE)
    LOC_RE = _rx(LOC1_RE, 1) + r'(,\s' + _rx(LOC1_RE, 2) + r')?'
    RANGE_RE = r'<' + _alt(SCRATCH_RE, LOC_RE) + r'>'
    XRANGE_RE = r'(?:\s' + _rx(LOC1_RE, 3) + ')?'
    RIGHT_RE = r'(?:\s(?P<right>.*)|)$'
    ANSI_CRE = re.compile(r'\x1b[^m]*m')
    LONGDEF_RE = BACKREF_RE + RANGE_RE + XRANGE_RE
    FULDEF_RE = _alt(LONGDEF_RE, FIELD_RE)
    LEFT_RE = _alt(DEF_RE + FULDEF_RE, NULL_RE)
    LINE_CRE = re.compile(DEPTH_RE + LEFT_RE + RIGHT_RE)
    RANGE_CRE = re.compile(RANGE_RE)

    CMD_JSON_NAME = 'compile_commands.json'

    def __init__(self, clang_check, build_path, debug=False):
        self._debug = debug
        self._mainfile = None
        self._files = {}
        self._root = {}
        self._clang_check = clang_check
        self._build_path = build_path
        self._parameters = {}

    def parse(self, filename, cmddir):
        with self._exec_clang_check(filename, cmddir) as fp:
            self.build_tree(fp)
        if filename not in self._files:
            return
        self._mainfile = self._files[filename]

    def parse_buffer(self, srcname, buf):
        dname = mkdtemp()
        cmdname = os.path.join(dname, self.CMD_JSON_NAME)
        try:
            fname = os.path.join(os.path.dirname(srcname),
                                 '.%s' % os.path.basename(srcname))
            self._build_cmd_file(cmdname, srcname, fname)
            with open(fname, 'wt') as fp:
                fp.write(buf)
            self.parse(fname, dname)
        finally:
            try:
                # may have not been actually created
                os.unlink(fname)
            except:
                pass
            os.unlink(cmdname)
            os.rmdir(dname)

    def get_func(self, line):
        if not self._mainfile:
            return None
        return self._mainfile.get_at_line(line)

    @staticmethod
    def get_clang_class(name):
        try:
            return getattr(sys.modules[__name__], 'Clang%s' % name)
        except AttributeError:
            return None

    def build_tree(self, fp, show_tree=False):
        stack = deque()
        filename = ''
        for n, l, m, d in self._get_next_line(fp):
            filename = self._extract_filename(m, filename)
            stmt = m.group('stmt')
            # there might be a clever way to avoid creating a default object
            # each time we do not care about the statement. Using None could
            # be a solution, although the tree would be broken as the stack
            # would lose track of useful objects
            cls = self.get_clang_class(stmt) or ClangDefaultObject
            obj = cls(self, m, filename)
            depth = len(stack)
            if not depth:
                stack.append(obj)
                if show_tree:
                    print("%06d %2d %s" % (d, 0, l.split('0')[0]))
                continue
            d += 1  # stack is always one step deeper than the parsed value
            move = d-depth
            if show_tree:
                print("%06d %2d %s" % (d, move, l.split('0')[0]))
                print (" w/ stack:")
                for n, s in enumerate(stack):
                    print(' ' * (4+2*n), s)
            if move > 0:
                # create a child
                if move > 1:
                    print("ERROR: too deep")
                    break
            else:
                # want to retrieve the parent
                while move < 0:
                    stack.pop()
                    move += 1
                # we want to be a sibling, so get our parent, and 
                # add a new child
                stack.pop()
            # the parent of the child is the deepest element on the stack
            parent = stack[-1]
            parent.add_child(obj)
            # the new deepest element of the stack is now the new child
            stack.append(obj)
            if show_tree:
                print("-------------- B:%s Child:%s" % (parent, obj))
        # stack[0].dump(0)
        self._root = stack[0]

    def register_function(self, clobj, filename):
        if filename not in self._files:
            self._files[filename] = FileContainer(filename)
        self._files[filename].add_function(clobj)

    def collect_parameters(self, all=False, filename=None):
        if not all and not filename:
            filename = self._mainfile.name
        parameters = self._root.collect_parameters(filename)
        return self._reduce_parameters(parameters)

    @property
    def parameters(self):
        if not self._parameters:
            self._parameters = self.collect_parameters()
        return self._parameters

    def _get_next_line(self, fp):
        for n, l in enumerate(fp, start=1):
            # Python3, a byte stream is received but we need to handle strings
            # get rid of trailing space and line feed chars
            l = l.decode('utf8').rstrip()
            # get rid of ANSI color markers
            l = self.ANSI_CRE.sub('', l)
            # compute the depth of a statement
            mo = self.LINE_CRE.match(l)
            if not mo:
                if self._debug:
                    print("Wrong format: %s" % l,
                          file=sys.stderr)
                continue
            depthstr = mo.group('depth')
            if depthstr:
                depthstr.replace('`', '|').count('|'), l
                depth = len(mo.group('depth')) // 2
            else:
                depth = 0
            stmt = mo.group('stmt')
            yield n, l, mo, depth

    def _exec_clang_check(self, filename, cmddir):
        args = [self._clang_check, '--ast-dump', '-p', cmddir, filename]
        if self._debug:
            print(' '.join(args))
        return Popen(args, stdout=PIPE, stderr=PIPE, bufsize=-1).stdout

    @classmethod
    def _build_json(cls, json, orig, tmp):
        """A real JSON parser would be nice, but try to keep this simple for
           now"""
        cre = re.compile(r'(?m)\{([^}]+)\}')
        match = None
        desc = None
        for mo in cre.finditer(json):
            newdesc = {}
            for kv in mo.group(1).split(','):
                k, v = kv.strip('\r\n').split(':')
                k = k.strip().strip('"')
                if k == 'command':
                    v = v.strip().strip('"').replace(orig, tmp)
                    # Hack: to not execute post commands
                    # TODO: should be a setting
                    v = v.split('&&')[0].strip()
                    v = '"%s"' % v
                if k == 'file':
                    sv = v.strip().strip('"')
                    if sv == orig:
                        v = '"%s"' % tmp
                        match = True
                newdesc[k] = v
            if match:
                desc = newdesc
                break
        if desc:
            lines = []
            for k in desc:
                lines.append('  "%s": %s' % (k, desc[k]))
            return '[\n{\n%s\n}\n]' % ',\n'.join(lines)

    def _build_cmd_file(self, jsondst, srcname, tmpname):
        jsonsrc = os.path.join(self._build_path, self.CMD_JSON_NAME)
        with open(jsonsrc, 'rt') as json:
            data = json.read()
        newdata = self._build_json(data, srcname, tmpname)
        with open(jsondst, 'wt') as json:
            json.write(newdata)

    @classmethod
    def _extract_filename(cls, mo, default):
        # Ugly heuristic, there should be a better way to find the exact
        # path where a function belongs
        for x in reversed(range(3)):
            kp = 'path%d' % (x+1)
            kv = mo.group(kp)
            if kv:
                return kv.split(':')[0]
        return default

    @classmethod
    def _reduce_parameters(self, params):
        rparams = {}
        for k in params:
            rparams[k] = tuple(x[0] for x in Counter(params[k]).most_common())
        return rparams


class FileContainer(object):
    """Container for functions in a single source file
    """

    MAX_SEEK_LINE = 4

    def __init__(self, name):
        self._functions = {}
        self.name = name

    def add_function(self, clfunc):
        self._functions[clfunc.line] = clfunc

    def get_at_line(self, line):
        if line in self._functions:
            return self._functions[line]
        for l in sorted(self._functions):
            if l < line:
                continue
            if l > line + self.MAX_SEEK_LINE:
                break
            return self._functions[l]
        return ''


# -----------------------------------------------------------------------------
# Clang-check mapped objects
# -----------------------------------------------------------------------------

class ClangObject(object):
    """Base class
    """

    DUMP_INDENT = 4

    def __init__(self, parser, mo, filename):
        self._children = []
        self.parser = parser
        self.filename = filename
        try:
            self.uid = int(mo.group('ref'), 16)
        except TypeError:
            self.uid = 0

    def add_child(self, child):
        assert(isinstance(child, ClangObject))
        self._children.append(child)

    def dump(self, depth):
        print(' ' * depth, self)
        self._dump_children(depth + self.DUMP_INDENT)

    def collect_parameters(self, filename):
        params = {}
        for c in self._children:
            cparams = c.collect_parameters(filename)
            self._update_parameters(params, cparams)
        return params

    def _dump_children(self, depth):
        for c in self._children:
            c.dump(depth)

    def _update_parameters(self, params, nparams):
        for k in nparams:
            descs = nparams[k]
            if k not in params:
                params[k] = []
            if isinstance(descs, list):
                params[k].extend(descs)
            else:
                params[k].append(descs)

    def __str__(self):
        return '[%x]-%s' % (self.uid & ((1 << 24)-1), self.__class__.__name__)


class ClangDefaultObject(ClangObject):
    """A clang object that is not parsed"""

    def __init__(self, parser, mo, filename):
        super(ClangDefaultObject, self).__init__(parser, mo, filename)
        self.kind = mo.group('stmt')


class ClangTranslationUnitDecl(ClangObject):
    """Root object"""

    def __init__(self, parser, mo, filename):
        super(ClangTranslationUnitDecl, self).__init__(parser, mo, filename)


class ClangTypedefDecl(ClangObject):
    """Type declaration"""


class ClangFunctionDecl(ClangObject):
    """Function declaration"""

    SCRATCH_RE = r'(?:<scratch space>:(?P<scratch>\d+:\d+))'
    SCRATCH_CRE = re.compile(SCRATCH_RE)
    FUNC_MODS_RE = r'(?P<fmods>(?:implicit|used|referenced) )*'
    FUNC_CRE = re.compile(FUNC_MODS_RE + r"(?P<fname>\w+)\s'(?P<fsig>[^']+)'")

    def __init__(self, parser, mo, filename, debug=False):
        super(ClangFunctionDecl, self).__init__(parser, mo, filename)
        self.name = ''
        self.line = 0
        right = mo.group('right')
        smo = self.SCRATCH_CRE.match(right)
        if smo:
            if debug:
                print("Ignore %s" % right, file=sys.stderr)
            return
        fmo = self.FUNC_CRE.match(right)
        if not fmo:
            print("Error %s" % right, file=sys.stderr)
        fname = fmo.group('fname')
        line = 0
        linedef = mo.group('line1')
        if linedef:
            line = int(linedef.split(':')[0])
        else:
            linedef = mo.group('path1')
            if linedef:
                line = int(linedef.split(':')[1])
        if not line:
            return
        self.name = fname
        self.line = line
        self.ret = fmo.group('fsig').split('(')[0].strip()
        self.parser.register_function(self, filename)

    @property
    def args(self):
        return [c for c in self._children if isinstance(c, ClangParmVarDecl)]

    def __str__(self):
        return '%s: %s @ %s:%d' % (super(ClangFunctionDecl, self).__str__(),
                                   self.name, os.path.basename(self.filename),
                                   self.line)


class ClangParmVarDecl(ClangObject):
    """Function parameter variable"""

    CRE = re.compile(r"(?P<pvmods>(?:used) )*" +
                     r"(?:(?P<pvname>\w+)\s)?'(?P<pvsig>[^']+)'")

    def __init__(self, parser, mo, filename):
        super(ClangParmVarDecl, self).__init__(parser, mo, filename)
        pvmo = self.CRE.match(mo.group('right'))
        if not pvmo:
            print(n, l, file=sys.stderr)
            self.name = ''
            self.signature = ''
        else:
            self.name = pvmo.group('pvname')
            self.signature = pvmo.group('pvsig')

    def __str__(self):
        return '%s: %s %s' % (super(ClangParmVarDecl, self).__str__(),
                              self.signature, self.name)


class ClangFullComment(ClangObject):
    """A Doxygen comment block"""

    def collect_parameters(self, filename):
        if filename is not None:
            if self.filename != filename:
                return {}
        params = super(ClangFullComment, self).collect_parameters(filename)
        self._update_parameters(params, self.get_parameters())
        return params

    def get_parameters(self):
        parameters = {}
        for c in self._children:
            if isinstance(c, ClangParamCommandComment):
                parameters[c.name] = c.description
        return parameters


class ClangParagraphComment(ClangObject):
    """A Doxygen comment paragraph"""

    @property
    def text(self):
        try:
            # space or EOL to join?
            return ' '.join([c.text for c in self._children])
        except AttributeError as e:
            raise ValueError("Paragraph %x as issue %s" % (self.uid, e))


class ClangTextComment(ClangObject):
    """A Doxygen text paragraph"""

    CRE = re.compile(r'^Text="(.*)"$')

    def __init__(self, parser, mo, filename):
        super(ClangTextComment, self).__init__(parser, mo, filename)
        tmo = self.CRE.match(mo.group('right'))
        if not tmo:
            print("TMO error %s" % mo.group('right'), file=sys.stderr)
            self.text = ''
        self.text = tmo.group(1).strip()


class ClangInlineCommandComment(ClangObject):
    """A Doxygen command comment (@c)"""

    RE = r'Name="(?P<name>\w+)"(?:\sRender(?P<style>Monospaced|Normal))' + \
         r'(?:\sArg\[(?P<pos>\d+)\]="(?P<value>\w+)")?'
    CRE = re.compile(RE)

    def __init__(self, parser, mo, filename):
        super(ClangInlineCommandComment, self).__init__(parser, mo, filename)
        icmo = self.CRE.match(mo.group('right'))
        if not icmo:
            print("ICMO error %s" % mo.group('right'), file=sys.stderr)
            self.text = ''
            return
        text = icmo.group('value') or ''
        self.text = text.strip()


class ClangParamCommandComment(ClangObject):
    """A Doxygen-commented function parameter"""

    RE = r'(?:\[(?P<dir>in|out|in,out)\]\s)?(?:(explicitly|implicitly)\s)?' + \
         r'Param="(?P<name>\w+)"(?:\sParamIndex=(?P<pos>\d+))?'
    CRE = re.compile(RE)

    def __init__(self, parser, mo, filename):
        super(ClangParamCommandComment, self).__init__(parser, mo, filename)
        pcmo = self.CRE.match(mo.group('right'))
        if not pcmo:
            self.name = ''
            self.pos = -1
            self.dir = ''
            return
        self.name = pcmo.group('name')
        pos = pcmo.group('pos')
        if pos is not None:
            self.pos = int(pos)
        else:
            self.pos = -1
        self.dir = pcmo.group('dir')

    @property
    def description(self):
        d = ' '.join([c.text for c in self._children])
        return d.strip()


# -----------------------------------------------------------------------------
# Doxygen generation
# -----------------------------------------------------------------------------

class DoxygenFunction(object):
    """A C function
    """

    BOOL_KIND_CRE = re.compile(r'\w+_(is|has)_\w+')

    def __init__(self, clangfunc):
        self.cfunc = clangfunc

    def to_dox(self, start=0):
        doc = []
        func = self.cfunc
        doc.append("/**")
        doc.append(" * %s" % func.name)
        doc.append(" *")
        for arg in func.args:
            sig = arg.signature
            if sig.endswith('*'):
                argdir = sig.startswith('const ') and 'in' or 'in,out'
            else:
                argdir = 'in'
            doc.append(" * @param[%s] %s" % (argdir, arg.name))
        if func.ret != 'void':
            doc.append(" * @return %s" % self._get_default_return_doc())
        doc.append(" */")
        return '\n'.join(doc)[start:]

    def _get_default_return_doc(self):
        func = self.cfunc
        if self.BOOL_KIND_CRE.match(func.name) and \
           func.ret in ('bool', 'unsigned int', 'int', '_Bool'):
                return '@c true if condition matches or @c false otherwise'
        if func.ret == 'int':
            return '@c OK or a negative POSIX error code on error'
        if func.ret.endswith('*'):
            return 'an instance of %s' % cfunc.ret.rstrip(' *')
        return ''


# -----------------------------------------------------------------------------
# Sublime Text plugin commands
# -----------------------------------------------------------------------------

class DoxyclangContext(object):
    """Maintain context across calls, there should be a cleaner way to 
       implement this"""

    def __init__(self):
        self.filename = ''
        self.line = 0
        self.cp = None
        self.choice = -1
        if self.build_path:
            self.buildpaths = self.build_path
        else:
            self.buildpaths = {}

    def get_build_path(self, filename):
        if isinstance(self.buildpaths, str):
            return self.buildpaths
        return self.buildpaths.get(filename, None)

    def set_build_path(self, filename, path):
        if isinstance(self.buildpaths, str):
            return
        self.buildpaths[filename] = path

    def _get_settings(self):
        settings = sublime.load_settings("Doxyclang.sublime-settings")
        return settings

    def __getattr__(self, name):
        if name.startswith('_'):
            return self.__getattribute__(name)
        return self._get_settings().get(name)

_context = DoxyclangContext()

class DoxyclangCommand(sublime_plugin.TextCommand):

    RE = r'^\s*(?:(?P<start>\/\*{2})$|' + \
         r'(?:\*\s+@(?P<def>return|param)(?:\[(?:in|out|in,out)\])?\s(?P<arg>\w+)))'
    CRE = re.compile(RE)

    def is_enabled(self):
        global _context
        if not bool(_context.enabled):
            return False
        extension = self.view.window().extract_variables()['file_extension']
        return extension in ('c','h')

    def run(self, edit):
        if not self.is_enabled():
            return '\n'
        # wish to find a better way to maintain a context than an ugly global
        # object
        global _context
        point = self.view.sel()[0].begin()
        linestr = self._read_line(point)
        mo = self.CRE.match(linestr)
        if not mo:
            return
        filename = self.view.file_name()
        line, col = self.view.rowcol(point)
        line += 1  # first line starts at 0
        buf = self._get_document_text(point)
        if _context.line != line or _context.filename != filename:
            # maybe this can be optimized: there is no point reparsing
            # a whole file if the very same comment block is being edited
            # check start/end lines of the comment block, and detect if it is
            # worth spawning a new parser at it.
            build_path = _context.get_build_path(filename)
            if not build_path:
                build_path = self._find_build_command_dir(
                    _context.build_path_comp, Parser.CMD_JSON_NAME,
                    int(_context.build_path_up), int(_context.build_path_down),
                    _context.debug)
                if _context.debug:
                    print("Build path for %s is %s" % (filename, build_path))
                if build_path:
                    _context.set_build_path(filename, build_path)
            if not build_path:
                print("Cannot find clang build path", file=sys.stderr)
                return
            clang_check = _context.clang_check
            if not os.path.isfile(clang_check):
                print("Invalid clang-check tool %s" % clang_check,
                      file=sys.stderr)
                return
            cp = Parser(clang_check, build_path, _context.debug)
            cp.parse_buffer(filename, buf)
            _context.cp = cp
            _context.line = line
            _context.filename = filename
        else:
            cp = _context.cp
        if mo.group('start'):
            func = cp.get_func(line)
            if func:
                doc = DoxygenFunction(func).to_dox(len(linestr))
                self.view.insert(edit, point, doc)
            else:
                self.view.insert(edit, point, '\n *\n */')
        elif mo.group('def'):
            arg = mo.group('arg')
            params = cp.parameters
            candidates = params.get(arg, {})
            if not candidates:
                return
            else:
                count = len(candidates)
                if count == 1:
                    hint = candidates[0]
                else:
                    _context.choice = (_context.choice+1) % count
                    hint = candidates[_context.choice]
            newline = ' '.join((mo.string[:mo.end('arg')], hint))
            region = self.view.line(point)
            self.view.replace(edit, region, newline)

    @staticmethod
    def enumerate_dir_candidates(topdir, dircomp, depth):
        """Find all directories whose last component matches"""
        sdepth = topdir.count(os.sep)
        for dirpath, dirnames, filenames in os.walk(topdir):
            if dircomp in dirnames:
                yield os.path.join(dirpath, dircomp)
            ddepth = dirpath.count(os.sep)
            if (ddepth-sdepth) >= depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames
                               if not d.startswith('.')]

    @staticmethod
    def enumerate_file_candidates(topdir, filecomp, depth):
        """Find all directories that contain a specified file"""
        sdepth = topdir.count(os.sep)
        for dirpath, dirnames, filenames in os.walk(topdir):
            if filecomp in filenames:
                yield dirpath
            ddepth = dirpath.count(os.sep)
            if (ddepth-sdepth) >= depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames
                               if not d.startswith('.')]

    @staticmethod
    def common_path(directories):
        """commonprefix working on directory names, not on chars"""
        norm_paths = [os.path.abspath(p) + os.sep for p in directories]
        return os.path.dirname(os.path.commonprefix(norm_paths))

    def _read_line(self, point):
        if (point >= self.view.size()):
            return
        return self.view.substr(self.view.line(point))

    def _get_document_text(self, point):
        before_insert = self.view.substr(sublime.Region(0, point-3))
        after_insert = self.view.substr(sublime.Region(point,
                                                       self.view.size()))
        return ''.join((before_insert, after_insert))

    def _find_build_command_dir(self, dircomp, bldfile, maxup, maxdown, debug):
        # start from the current ST folder
        folder = self.view.window().extract_variables()['folder']
        current = folder

        if debug:
            print("bld: folder %s" % folder)

        # move up to the maximum defined level to search top-down 
        while maxup > 0:
            parent = os.path.join(current, os.pardir)
            if os.path.isdir(parent):
                current = parent
            maxup -= 1
        current = os.path.normpath(current)

        if debug:
            print("bld: start from %s" % current)

        # get all directories that contains the specified dircomp
        dcompref = [(d, len(os.path.commonprefix((folder, d)))) for d in
                    self.enumerate_dir_candidates(current, dircomp, maxdown)]
        # select the one that closely looks like the original folder, so
        # that candidates for other build component are not considered
        dbest = sorted(dcompref, key=lambda x: -x[1])[0][0]
        if not dbest:
            return None

        if debug:
            print("bld: dcompref %s" % dcompref)
            print("bld: dbest %s" % dbest)

        # find all clang build files within the selected directory
        dref = list(self.enumerate_file_candidates(dbest, bldfile, maxdown))
        if not dref:
            return None

        if debug:
            print("bld: dref %s" % dref)

        # remove common part from candidates
        common = self.common_path(dref)
        cmnlen = len(common)
        dist = ['%s' % d[cmnlen:] for d in dref]

        # reverse path order for all candidates
        rdist = [os.sep.join(reversed(d.split(os.sep))) for d in dist]

        # reverse path order for folder
        rfolder = os.sep.join(reversed(folder.split(os.sep)))

        # weigth each candidate, based on how close it is to the folder
        weights = [(d, rfolder.find(d)) for d in rdist]

        # find the best candidate
        dirs = [d[0] for d in sorted(weights, key=lambda x: x[1]) if d[1] >= 0]
        if not dirs:
            return None
        best = os.path.join(common,dirs[0])

        if debug:
            print("bld: dirs %s" % dirs)
            print("bld: best %s" % best)

        # hope the heuristic is fine :-)
        return best


#if __name__ == '__main__':
#    dc = DoxyClang(False)
#    for n, l, m, d in dc._get_next_line(sys.stdin): 
#        print (n, d, l)
