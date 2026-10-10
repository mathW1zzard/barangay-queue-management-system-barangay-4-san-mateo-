"""Lists functions that open a database session but do not guarantee closing it.

Safe patterns:  `with SessionLocal() as db:`   or   `db = SessionLocal()` + `try: ... finally: db.close()`
Usage:  python check_sessions.py main.py
"""
import ast
import sys

source = open(sys.argv[1] if len(sys.argv) > 1 else "main.py", encoding="utf-8").read()
tree = ast.parse(source)


def is_session_call(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SessionLocal"


def closes_in_finally(func):
    for node in ast.walk(func):
        if isinstance(node, ast.Try):
            for stmt in node.finalbody:
                for sub in ast.walk(stmt):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "close"):
                        return True
    return False


problems = []
for func in ast.walk(tree):
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    with_items = {id(item.context_expr) for n in ast.walk(func) if isinstance(n, ast.With) for item in n.items}
    manual = [n for n in ast.walk(func) if is_session_call(n) and id(n) not in with_items]
    if manual and not closes_in_finally(func):
        problems.append((manual[0].lineno, func.name))

for lineno, name in sorted(problems):
    print(f"line {lineno:4}: {name}()  opens a session without a guaranteed close")
print(f"\n{len(problems)} unsafe function(s)." if problems else "OK: every session is closed safely.")