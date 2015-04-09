import sublime, sublime_plugin

import re
import os
import sys

from collections import defaultdict
from pprint import pformat, pprint
from subprocess import Popen, PIPE
from tempfile import mkdtemp, mkstemp


def RX(rexp, count):
    """Rename (suffix with index) a RE group name"""
    return re.sub(r'\(\?P\<(\w+)\>', '(?P<\\g<1>%d>' % count, rexp)


class DoxyFunc(object):
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


class DoxyFile(object):
    """Container for functions in a single source file
    """

    MAX_SEEK_LINE = 4

    def __init__(self, name):
        self._functions = {}
        self.name = name

    def add_function(self, doxyfunc, line):
        # print("Add %s %s %d" % (self.name, doxyfunc.name, line))
        self._functions[line] = doxyfunc

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


class DoxyClang(object):
    """Parse clang-check AST dump to extract useful hints for Doxygen
    """

    DEPTH_RE = r'^(?P<depth>(?:[| ]*)[|`]-)'
    DEF_RE = r'(?P<stmt>[A-Za-z]+)\s(?P<ref>0x[0-9a-f]+)\s'
    BACKREF_RE = r'(?:(parent|prev)\s(?P<bref>0x[0-9a-f]+)\s)?'
    SCRATCH_RE = r'(?:<scratch space>:(?P<scratch>\d+:\d+))'
    PATH_RE = r'(?P<path>/[\w/\.]+:\d+:\d+)'
    LINE_RE = r'(?:line:(?P<line>\d+:\d+))'
    COL_RE = r'(?:col:(?P<col>\d+))'
    LOC1_RE = r'(?:' + r'|'.join((PATH_RE, LINE_RE, COL_RE)) + r')'
    LOC_RE = RX(LOC1_RE, 1) + r'(,\s' + RX(LOC1_RE, 2) + r')?'
    RANGE_RE = r'<(?:' + r'|'.join((SCRATCH_RE, LOC_RE)) + ')>\s'
    XRANGE_RE = r'(?:' + RX(LOC1_RE, 3) + '\s)?'
    RIGHT_RE = r'(?P<right>.*)$'
    ANSI_CRE = re.compile(r'\x1b[^m]*m')
    LEFT_CRE = re.compile(DEPTH_RE + DEF_RE + BACKREF_RE + RANGE_RE +
                          XRANGE_RE + RIGHT_RE)
    RANGE_CRE = re.compile(RANGE_RE)
    SCRATCH_CRE = re.compile(SCRATCH_RE)
    FUNC_MODS_RE = r'(?P<fmods>(?:implicit|used|referenced) )*'
    FUNC_CRE = re.compile(FUNC_MODS_RE + r"(?P<fname>\w+)\s'(?P<fsig>[^']+)'")
    PARMVAR_MODS_RE = r'(?P<pvmods>(?:used) )*'
    PARMVAR_CRE = re.compile(PARMVAR_MODS_RE + 
                             r"(?:(?P<pvname>\w+)\s)?'(?P<pvsig>[^']+)'")

    CMD_JSON_NAME = 'compile_commands.json'

    def __init__(self, debug=False):
        self._debug = debug
        self._doxyfile = None
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
            mo = self.LEFT_CRE.match(l)
            if not mo:
                if self._debug:
                    print(l, file=sys.stderr)
                continue
            #  mo.group('depth').replace('`', '|').count('|'), l
            #  depth = len(mo.group('depth')) / 2
            stmt = mo.group('stmt')
            if stmt.endswith('Expr'):
                continue
            if stmt.endswith('Stmt'):
                continue
            if stmt.endswith('Operator'):
                continue

            yield n, l, mo

    def _parse(self, fp):
        srcfiles = defaultdict(DoxyFile)
        srcfile = None
        func = None
        # get_parm_var = False
        for n, l, mo in self._get_next_line(fp):
            right = mo.group('right')
            stmt = mo.group('stmt')
            if stmt.endswith('FunctionDecl'):
                for x in reversed(range(3)):
                    kp = 'path%d' % (x+1)
                    kv = mo.group(kp)
                    if kv:
                        filename = kv.split(':')[0]
                        if filename not in srcfiles:
                            srcfiles[filename] = DoxyFile(filename)
                        srcfile = srcfiles[filename]
                        break
            if stmt == 'FunctionDecl':
                # ignore scratch definitions
                func = None
                smo = self.SCRATCH_CRE.match(right)
                if smo:
                    if self._debug:
                        print("Ignore %s" % right, file=sys.stderr)
                    continue
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
                    continue
                func = DoxyFunc(fname, line)
                fret = fmo.group('fsig').split('(')[0].strip()
                func.set_return(fret)
                srcfile.add_function(func, line)
                continue
            if stmt == 'ParmVarDecl':
                pvmo = self.PARMVAR_CRE.match(right)
                if not pvmo:
                    print(n, l, file=sys.stderr)
                if not func:
                    if self._debug:
                        print("Orpheanous param @ %d: %s" % (n, l), 
                              file=sys.stderr)
                    continue
                func.set_arg(pvmo.group('pvsig'), pvmo.group('pvname'))
                #if fname.find('seqfs_') >= 0:
            else:
                # stop function argument retrieval
                func = None
        return srcfiles

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
            pass
            #os.unlink(fname)
            #os.unlink(cmdname)
            #os.rmdir(dname)

    def get_func(self, line):
        if not self._doxyfile:
            return None
        return self._doxyfile.get_at_line(line)


class DoxyclangCommand(sublime_plugin.TextCommand):

    def _read_line(self, point):
        if (point >= self.view.size()):
            return
        return self.view.substr(self.view.line(point))

    def _get_document_text(self, point):
        before_insert = self.view.substr(sublime.Region(0, point-3))
        after_insert = self.view.substr(sublime.Region(point, self.view.size()))
        return ''.join((before_insert, after_insert))

    def run(self, edit, mode=None):
        extension = self.view.window().extract_variables()['file_extension']
        if extension not in ('c',):
            return '\n'
        point = self.view.sel()[0].begin()
        linestr = self._read_line(point)
        filename = self.view.file_name()
        line, col = self.view.rowcol(point)
        line += 1 # first line starts at 0
        # print("%04d: %s" % (line, linestr))
        dc = DoxyClang(False)
        # dc.parse(filename)
        buf = self._get_document_text(point)
        #for n, x in enumerate(buf.split('\n'), 1):
        #    print ("%04d: %s" % (n, x))
        #return
        dc.parse_buffer(filename, buf)
        func = dc.get_func(line)
        if func:
            doc = str(func)[len(linestr):]
            self.view.insert(edit, point, doc)
        else:
            self.view.insert(edit, point, '\n *\n */')


if __name__ == '__main__':
    dc = DoxyClang(False)
    dc.main()
