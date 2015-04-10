#!/usr/bin/env python3

import sublime, sublime_plugin

import re
import os
import sys

from collections import defaultdict
from pprint import pformat, pprint
from subprocess import Popen, PIPE
from tempfile import mkdtemp, mkstemp



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
    for n, l, m, d in dc._get_next_line(sys.stdin): 
        print (n, d, l)
