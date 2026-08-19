import ast
import sys
with open(sys.argv[1]) as f:
    ast.parse(f.read())
    print("OK")
    sys.exit(0)