import ast
from pathlib import Path

for name in ("app.py", "netflix_ai_agent.py"):
    ast.parse(Path(name).read_text(encoding="utf-8"), filename=name)

print("Python syntax OK")
