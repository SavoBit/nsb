#!/bin/bash

set -x
set -e

test -d autom4te.cache && rm -rf autom4te.cache
aclocal
autoconf
automake --add-missing --copy
