# Doxyclang

Experimental Doxygen function doc generator from clang AST for Sublime Text 3

## Motivation

The idea behind Doxyclang is to use the clang capabilities to extract function 
prototypes from C files to create function documentation skeletons. 

LLVM/clang tools provide accurate Abstract Syntax Trees that can be used to 
extract the information that is required to build up documentation blocks,
using the exact same options used to build the project.

This Sublime Text 3 plugin should be seen as a highly experimental prototype - 
not a supported tool - to learn ST3 plugin features and clang tooling 
capabilities.

## Features

* Extract prototypes of function from the current edit window, and create a
  Doxygen comment blocks, autocompleted with the parameter names.
* Extract documentation info from documented blocks to provide autocompletion
  for function parameters that have already been commented in other functions. 
* Experimental retrieval of the path to the `compile_commands.json` file that
  clang-check requires. This feature avoids to define a project to edit file
  documentation.


## Usage

* Edit `Preferences/Package Settings/Doxy Clang/Settings - User`

  * `Enabled` can be set to false to disable the plugin, w/o uninstalling it
  * `debug` can be enabled to obtain various debug information within the 
    ST3 embedded Python console.
  * `clang_check` specifies the path the clang-check executable (tested with
    clang-check v3.5)
  * `build_path`: specifies the directory where to find the 
   `compile_commands.json` file required to run clang-check. Can be left empty
   so that the experimental/heuristic search feature kicks in, see below. If
   not empty, the following options are ignored.
  * `build_path_comp` specifies a remarkable path component (directory) that 
   should be seek in the nearby directory trees. Out-of-source builds usually
   define a "build" directory where all intermediate and binary files are 
   generated. The build system is likely to store the `compile_commands.json`
   generated file into this directory tree. The heuristic search needs an 
   anchor to locate this file, which can be specified with this option.
  * `build_path_up` specifies how many directories up from the currently edited
   file should be discarded to start looking for the `build_path_comp` 
   directory tree.
  * `build_path_down` specifies how deep the search for the `build_path_comp` 
   directory tree should go.

## Caveats

* Use clang-check AST output. A far cleaner implementation would use the native
libtooling library, however no Python wrapper exists at this time, and it would
likely be not trivial to write as clang is written in C++. Parsing the AST 
output is enough to extract the required information for this plugin to work,
although parsing error and subtile changes from one clang version to another
could easily break the plugin's parser.

## Missing features

Most of them :-)

* proper documentation
* return type autocompletion
* versatile project configuration support
* custom Doxygen block style / templating system
* doc extraction from sibling source files
* C++ support
