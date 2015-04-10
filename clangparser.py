#!/usr/bin/env python3

import re
import os
import sys

from collections import defaultdict, deque
from pprint import pformat, pprint
from subprocess import Popen, PIPE
from tempfile import mkdtemp, mkstemp


def RX(rexp, count):
    """Rename (suffix with index) a RE group name"""
    return re.sub(r'\(\?P\<(\w+)\>', '(?P<\\g<1>%d>' % count, rexp)


def ALT(*res):
    return r'(?:' + r'|'.join(res) + r')'


class ClangParser(object):
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
    LOC1_RE = ALT(PATH_RE, LINE_RE, COL_RE, ISLOC_RE)
    LOC_RE = RX(LOC1_RE, 1) + r'(,\s' + RX(LOC1_RE, 2) + r')?'
    RANGE_RE = r'<' + ALT(SCRATCH_RE, LOC_RE) + r'>'
    XRANGE_RE = r'(?:\s' + RX(LOC1_RE, 3) + ')?'
    RIGHT_RE = r'(?:\s(?P<right>.*)|)$'
    ANSI_CRE = re.compile(r'\x1b[^m]*m')
    LONGDEF_RE = BACKREF_RE + RANGE_RE + XRANGE_RE
    FULDEF_RE = ALT(LONGDEF_RE, FIELD_RE)
    LEFT_RE = ALT(DEF_RE + FULDEF_RE, NULL_RE)
    LINE_CRE = re.compile(DEPTH_RE + LEFT_RE + RIGHT_RE)
    RANGE_CRE = re.compile(RANGE_RE)

    CMD_JSON_NAME = 'compile_commands.json'

    def __init__(self, debug=False):
        self._debug = debug
        # self._doxyfile = None
        self._files = {}
        self._clang_path = '/usr/local/bin/arm-elf32-minix-clang-check'
        self._build_path = '/Users/eblot/Sources/Neotion/ndk/sandboxes/t29/build/minix'

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
                    print("NO MATCH", l, file=sys.stderr)
                continue
            depthstr = mo.group('depth')
            if depthstr:
                depthstr.replace('`', '|').count('|'), l
                depth = len(mo.group('depth')) // 2
            else:
                depth = 0
            stmt = mo.group('stmt')
            yield n, l, mo, depth

    def _clang_check(self, filename, cmddir):
        args = [self._clang_path, '--ast-dump', '-p', cmddir, filename]
        print (' '.join(args))
        return Popen(args, stdout=PIPE, stderr=PIPE).stdout

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
                    v = v.replace(orig, tmp)
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

    def parse(self, filename, cmddir):
        with self._clang_check(filename, cmddir) as fp:
            files = self._parse(fp)
        if filename not in files:
            return
        self._doxyfile = files[filename]

    def parse_buffer(self, srcname, buf):
        dname = mkdtemp()
        cmdname = os.path.join(dname, self.CMD_JSON_NAME)
        try:
            fname = os.path.join(os.path.dirname(srcname),
                                 '.%s' % os.path.basename(srcname))
            self._build_cmd_file(cmdname, srcname, fname)
            fp = open(fname, 'wt')
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
        if not self._doxyfile:
            return None
        return self._doxyfile.get_at_line(line)

    @staticmethod
    def get_clang_class(name):
        try:
            return getattr(sys.modules[__name__], 'Clang%s' % name)
        except AttributeError:
            return None

    def build_tree(self, fp):
        stack = deque()
        filename = ''
        for n, l, m, d in self._get_next_line(fp):
            filename = self._extract_filename(m, filename)
            stmt = m.group('stmt')
            cls = self.get_clang_class(stmt) or ClangDefaultObject
            obj = cls(self, m, filename)
            depth = len(stack)
            move = d-depth
            if not depth:
                stack.append(obj)
                continue
            if move > 0:
                if move > 1: 
                    print("ERROR: too deep")
                    break
                stack[-1].add_child(obj)
                stack.append(obj)
                continue
            while move < 0:
                stack.pop()
                move += 1
            parent = stack[-1]
            parent.add_child(obj)
        print(stack)
        stack[0].dump(0)

    def register_function(self, clobj, filename):
        if filename not in self._files:
            self._files[filename] = FileContainer(filename)
        self._files[filename].add_function(clobj)

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


class ClangObject(object):
    """Base class
    """

    DUMP_INDENT = 4

    def __init__(self, parser, mo, filename):
        self._children = []
        self.parser = parser
        self.filename = filename

    def add_child(self, child):
        assert(isinstance(child, ClangObject))
        self._children.append(child)

    def dump(self, depth):
        print(' ' * depth, self)
        self._dump_children(depth + self.DUMP_INDENT)

    def _dump_children(self, depth):
        for c in self._children:
            c.dump(depth)

    def __str__(self):
        return self.__class__.__name__


class ClangDefaultObject(ClangObject):
    """A clang object that is not parsed"""

    def __init__(self, parser, mo, filename):
        super(ClangDefaultObject, self).__init__(parser, mo, filename)
        self.kind = mo.group('stmt')


class ClangTranslationUnitDecl(ClangObject):
    """Root object"""

    def __init__(self, parser, mo, filename):
        super(ClangTranslationUnitDecl, self).__init__(parser, mo, filename)


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
        #print(fname, line)
        #func = DoxyFunc(fname, line)
        #func.set_return(fret)
        #srcfile.add_function(func, line)
                
    def __str__(self):
        return '%s: %s @ %s:%d' % (self.__class__.__name__, 
                                   self.name, os.path.basename(self.filename),
                                   self.line)


class ClangParmVarDecl(ClangObject):
    """Function parameter variable"""

    PARMVAR_MODS_RE = r'(?P<pvmods>(?:used) )*'
    PARMVAR_CRE = re.compile(PARMVAR_MODS_RE + 
                             r"(?:(?P<pvname>\w+)\s)?'(?P<pvsig>[^']+)'")

    def __init__(self, parser, mo, filename):
        super(ClangParmVarDecl, self).__init__(parser, mo, filename)
        pvmo = self.PARMVAR_CRE.match(mo.group('right'))
        if not pvmo:
            print(n, l, file=sys.stderr)
            self.name = ''
            self.signature = ''
        else:
            self.name = pvmo.group('pvname')
            self.signature = pvmo.group('pvsig')

    def __str__(self):
        return '%s: %s %s' % (self.__class__.__name__, 
                              self.signature, self.name)


class Function(object):
    """A C function
    """

    BOOL_KIND_CRE = re.compile(r'\w+_(is|has)_\w+')

    def __init__(self, name, line):
        self.name = name
        self.line = line
        self.ret = 'void'
        self.args = []

    def set_arg(self, argtype, argname=None):
        self.args.append((argtype, argname))

    def set_return(self, rettype):
        self.ret = rettype

    def to_doxygen(self):
        doc = []
        doc.append("/**")
        doc.append(" * %s" % self.name)
        doc.append(" *")
        for argtype, argname in self.args:
            if argtype.endswith('*'):
                argdir = argtype.startswith('const ') and 'in' or 'in,out'
            else:
                argdir = 'in'
            doc.append(" * @param[%s] %s" % (argdir, argname))
        if self.ret != 'void':
            doc.append(" * @return %s" % self._get_default_return_doc())
        doc.append(" */")
        return '\n'.join(doc)

    def _get_default_return_doc(self):
        if self.BOOL_KIND_CRE.match(self.name) and \
           self.ret in ('bool', 'unsigned int', 'int', '_Bool'):
                return '@c true if condition matches or @c false otherwise'
        if self.ret == 'int':
            return '@c OK or a negative POSIX error code on error'
        if self.ret.endswith('*'):
            return 'an instance of %s' % self.ret.rstrip(' *')
        return ''

    def __str__(self):
        return self.to_doxygen()


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
            # print (l, self._functions[l].name)
            if l < line:
                continue
            if l > line + self.MAX_SEEK_LINE:
                break
            return self._functions[l]
        return ''


if __name__ == '__main__':
    cp = ClangParser(True)
    cp.build_tree(sys.stdin.buffer)
