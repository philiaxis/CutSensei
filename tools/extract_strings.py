import ast, pathlib, json, sys
strings = {}
def add(s, where):
    strings.setdefault(s, where)
for path in sorted(pathlib.Path('src/cutsensei').rglob('*.py')):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name in ('tr', 'trf', 'act') and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                add(node.args[0].value, f'{path}:{node.lineno}')
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            if n in ('REASONS', 'ENCODER_NAMES') and isinstance(node.value, ast.Dict):
                for v in node.value.values:
                    if isinstance(v, ast.Constant): add(v.value, f'{path}:{node.lineno}')
            if n in ('SHORTCUTS', 'rows') and isinstance(node.value, ast.List):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Tuple) and isinstance(elt.elts[1], ast.Constant):
                        add(elt.elts[1].value, f'{path}:{node.lineno}')
for s in sorted(strings):
    print(json.dumps(s, ensure_ascii=False))
