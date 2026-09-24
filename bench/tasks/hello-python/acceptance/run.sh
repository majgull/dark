#!/bin/bash
# hidden acceptance for hello-python; runs at the repo root in the fresh judging VM
fail=0
out=$(python3 hello.py 2>/dev/null); rc=$?
if [ "$rc" = 0 ] && [ "$out" = "hello, dark" ]; then echo "CHECK output ok"; else echo "CHECK output fail (rc=$rc out=$out)"; fail=1; fi
lines=$(python3 hello.py 2>/dev/null | wc -l)
if [ "$lines" = 1 ]; then echo "CHECK one-line ok"; else echo "CHECK one-line fail ($lines lines)"; fail=1; fi
if [ -f test_hello.py ] && grep -q "subprocess" test_hello.py; then echo "CHECK test-file ok"; else echo "CHECK test-file fail"; fail=1; fi
if python3 -m unittest -q test_hello >/dev/null 2>&1; then echo "CHECK own-test-green ok"; else echo "CHECK own-test-green fail"; fail=1; fi
exit $fail
