# -*- coding: utf-8 -*-
"""临时脚本：提取 memory_tools.py 中指定函数的完整源码到文件"""
import ast

src_path = r"D:\anywhere\work\大眼\bigeye5\bigeye\tools\memory_tools.py"
out_path = r"D:\anywhere\work\大眼\bigeye5\bigeye\tools\_dump_out.txt"
with open(src_path, encoding="utf-8") as f:
    src = f.read()

tree = ast.parse(src)
targets = {"fold_message", "compress_context", "_fold_range_msgs", "_expand_by_node"}

lines = src.splitlines()
with open(out_path, "w", encoding="utf-8") as out:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
            out.write(f"\n{'='*70}\n### {node.name} (L{node.lineno}-{node.end_lineno})\n{'='*70}\n")
            out.write("\n".join(lines[node.lineno-1:node.end_lineno]))
            out.write("\n")

print("written:", out_path)
