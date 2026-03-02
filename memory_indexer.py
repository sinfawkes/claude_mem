"""
Project file indexer that proposes memory entries for human review.

Scans a project directory, extracts code patterns, and proposes them
as memory entries with status='proposed'. Never auto-confirms.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".py", ".cpp", ".h", ".hpp", ".java", ".ts", ".tsx", ".js", ".jsx"}
_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", "target"}


@dataclass
class IndexedPattern:
    type: str
    content: str
    files: list[str]
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.8


class MemoryIndexer:
    """
    Scans project files and proposes memory entries.

    Rate-limited to avoid flooding: max `rate_limit` proposals per minute.
    """

    def __init__(
        self,
        project_path: str,
        git_root: Optional[str] = None,
        rate_limit: int = 10,
        dry_run: bool = False,
    ):
        self.project_path = os.path.expanduser(project_path)
        self.git_root = os.path.expanduser(git_root or project_path)
        self.rate_limit = rate_limit
        self.dry_run = dry_run
        self._proposal_times: list[float] = []

    def get_git_commit_hash(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.git_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    def get_git_branch(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.git_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    def _check_rate_limit(self) -> bool:
        """Return True if we can propose; False if rate limit exceeded."""
        now = time.monotonic()
        self._proposal_times = [t for t in self._proposal_times if now - t < 60]
        if len(self._proposal_times) >= self.rate_limit:
            return False
        self._proposal_times.append(now)
        return True

    def index_project(self, project_name: str) -> list[IndexedPattern]:
        """
        Scan the project and return all detected patterns.
        In non-dry-run mode, each pattern is proposed via propose_callback.
        """
        patterns: list[IndexedPattern] = []
        git_hash = self.get_git_commit_hash()
        git_branch = self.get_git_branch()

        logger.info(
            "Indexing project '%s' at %s (git: %s / %s, dry_run=%s)",
            project_name, self.project_path, git_hash, git_branch, self.dry_run,
        )

        for root, dirs, files in os.walk(self.project_path):
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
            for fname in files:
                ext = Path(fname).suffix
                if ext not in _SUPPORTED_EXTENSIONS:
                    continue
                file_path = os.path.join(root, fname)
                rel_path = os.path.relpath(file_path, self.project_path)
                found = self._extract_patterns(file_path, rel_path, ext)
                patterns.extend(found)

        logger.info("Indexer found %d patterns in project '%s'", len(patterns), project_name)
        return patterns

    def _extract_patterns(self, file_path: str, rel_path: str, ext: str) -> list[IndexedPattern]:
        try:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                source = f.read()
        except OSError:
            return []

        if ext == ".py":
            return self._extract_python_patterns(source, rel_path)
        else:
            return self._extract_generic_patterns(source, rel_path, ext)

    def _extract_python_patterns(self, source: str, rel_path: str) -> list[IndexedPattern]:
        patterns: list[IndexedPattern] = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                docstring = ast.get_docstring(node) or ""
                content = "Python class '" + node.name + "'"
                if bases:
                    content += " extends " + ", ".join(bases)
                if docstring:
                    content += ": " + docstring[:200]
                patterns.append(IndexedPattern(
                    type="code_pattern",
                    content=content,
                    files=[rel_path],
                    tags=["class", "python"],
                    confidence=0.75,
                ))
            elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                docstring = ast.get_docstring(node) or ""
                args = [a.arg for a in node.args.args]
                content = "Python function '" + node.name + "(" + ", ".join(args) + ")' in " + rel_path
                if docstring:
                    content += ": " + docstring[:200]
                patterns.append(IndexedPattern(
                    type="code_pattern",
                    content=content,
                    files=[rel_path],
                    tags=["function", "python"],
                    confidence=0.7,
                ))

        return patterns

    def _extract_generic_patterns(
        self, source: str, rel_path: str, ext: str
    ) -> list[IndexedPattern]:
        patterns: list[IndexedPattern] = []
        lang_tag = ext.lstrip(".")

        for match in re.finditer(r'\bclass\s+(\w+)(?:\s*:\s*[\w,\s]+)?', source):
            class_name = match.group(1)
            patterns.append(IndexedPattern(
                type="code_pattern",
                content=lang_tag + " class '" + class_name + "' in " + rel_path,
                files=[rel_path],
                tags=["class", lang_tag],
                confidence=0.7,
            ))

        for match in re.finditer(
            r'(?:public|protected|static)\s+\w[\w<>*&\s]*\s+(\w+)\s*\([^)]{0,100}\)',
            source,
        ):
            fn_name = match.group(1)
            if fn_name not in {"if", "while", "for", "switch"}:
                patterns.append(IndexedPattern(
                    type="code_pattern",
                    content=lang_tag + " function '" + fn_name + "' in " + rel_path,
                    files=[rel_path],
                    tags=["function", lang_tag],
                    confidence=0.65,
                ))

        return patterns
