"""Check the Git index (or HEAD) without printing potential secret values."""

import re
import subprocess
import sys
from pathlib import PurePosixPath

RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider-token": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
    ),
    "personal-path": re.compile(r"(?i)(?:[a-z]:[\\/]Users[\\/][^\s/\\]+|/(?:home|Users)/[^\s/]+)"),
    "email": re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "credential-url": re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@"),
    "literal-secret": re.compile(
        r"""(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)\s*[:=]\s*["']([A-Za-z0-9_./+\-=]{16,})["']"""
    ),
}
FORBIDDEN_PARTS = {
    ".venv",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".agents",
    ".codex",
    "dist",
    "build",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".pyc", ".log", ".db", ".sqlite"}


def main():
    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    names = result.stdout.decode("utf-8").split("\0")
    findings = []
    count = 0
    for name in filter(None, names):
        count += 1
        path = PurePosixPath(name)
        if (
            set(path.parts) & FORBIDDEN_PARTS
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or (path.name.startswith(".env") and path.name != ".env.example")
        ):
            findings.append((name, 0, "forbidden-file"))
            continue
        data = subprocess.run(["git", "show", ":" + name], check=True, capture_output=True).stdout
        if len(data) > 2_000_000:
            findings.append((name, 0, "oversized-file"))
            continue
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            findings.append((name, 0, "non-text-file-review-required"))
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            for kind, rule in RULES.items():
                for match in rule.finditer(line):
                    value = match.group(0)
                    if kind == "email" and value.endswith("@users.noreply.github.com"):
                        continue
                    if (
                        kind == "credential-url"
                        and name
                        in {
                            "tests/unit/test_config.py",
                            "tests/unit/test_merchant_api.py",
                        }
                        and match.group(1) in {"p", "x", "demo", "admin", "secret"}
                    ):
                        continue
                    if kind == "credential-url" and (
                        match.group(1).endswith("-demo")
                        or match.group(1) in {"password", "pass"}
                        or "${" in match.group(1)
                    ):
                        continue
                    if kind == "literal-secret" and match.group(1).startswith(
                        ("local-", "test-", "mock-")
                    ):
                        continue
                    findings.append((name, lineno, kind))
    for name, lineno, kind in findings:
        print(f"{name}:{lineno}: {kind}")
    print(f"Scanned {count} indexed files; {len(findings)} findings.")
    return 1 if findings or not count else 0


if __name__ == "__main__":
    sys.exit(main())
