"""支持 `python -m clients.cli` 直接运行。"""
from .main import main

raise SystemExit(main())
