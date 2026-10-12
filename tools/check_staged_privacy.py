"""Inspect staged blobs without printing matched secrets. Python standard library only."""
from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAMES = {'settings.json', 'server_safety.json', 'image_library.json',
                 'VALIDIERUNG.json', 'MANIFEST.json'}
PRIVATE_DIRS = {'runs', 'logs', 'backups', 'work', 'input', 'output', 'outputs', 'data', 'downloads',
                '__pycache__', '.venv', '.idea', '.vscode'}
ALLOWED_SUFFIXES = {'.py', '.js', '.html', '.json', '.md', '.txt', '.sh', '.cmd'}
SPECIAL_NAMES = {'.gitignore', '.gitattributes', 'LICENSE', 'Dockerfile', '.dockerignore', '.env.example'}
DEPLOY_FILES = {'compose.yaml', 'deploy/h3-models', 'deploy/requirements.in', 'deploy/requirements.lock'}
PATTERNS = {
    'private key': r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'GitHub credential': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'AWS access key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'API credential': r'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,})\b',
    'JWT': r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b',
    'Hugging Face token': r'\bhf_[A-Za-z0-9]{20,}\b',
    'URL credentials': r'https?://[^\s/<>"\x27]+:[^\s/<>"\x27]+@',
    'personal home path': r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+[^\s/\\]+|/(?:home|Users)/[^\s/]+)',
    'private tailnet hostname': r'(?i)\b[\w-]+(?:\.[\w-]+)*\.(?:ts\.net|tail\.[\w.-]+)\b',
    'literal credential assignment': r'(?i)["\x27]?(?:api[_-]?key|access[_-]?token|password|secret)["\x27]?\s*[:=]\s*["\x27][A-Za-z0-9+/=_-]{24,}["\x27]',
}


def content_issues(text, requirement_versions=False):
    issues = {label for label, pattern in PATTERNS.items() if re.search(pattern, text)}
    for match in re.finditer(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', text):
        if requirement_versions:
            prefix = text[:match.start()].rsplit('\n', 1)[-1]
            suffix = text[match.end():].split('\n', 1)[0]
            if re.fullmatch(r'[A-Za-z0-9_.-]+==', prefix) and re.fullmatch(r'\s*\\?\s*', suffix):
                continue  # A pinned four-component version, never a URL or comment.
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if not address.is_loopback and not address.is_global:
            issues.add('non-public IP address')
    return sorted(issues)


def path_issues(name):
    path = PurePosixPath(name)
    if (path.name in PRIVATE_NAMES or any(p in PRIVATE_DIRS for p in path.parts)
            or path.name.startswith('.env') and path.name != '.env.example'):
        return ['private runtime file']
    if path.name not in SPECIAL_NAMES and name not in DEPLOY_FILES and path.suffix.lower() not in ALLOWED_SUFFIXES:
        return ['unexpected file type; review before publication']
    return []


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def main():
    failures = []
    entries = git('ls-files', '--stage', '-z').split(b'\0')
    count = 0
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, oid, stage = metadata.decode('ascii').split()
        name = raw_name.decode('utf-8')
        count += 1
        issues = path_issues(name)
        if mode not in ('100644', '100755') or stage != '0':
            issues.append('symlink, submodule or unresolved entry')
        else:
            blob = git('cat-file', 'blob', oid)
            try:
                text = blob.decode('utf-8')
            except UnicodeDecodeError:
                issues.append('binary or non-UTF-8 content')
            else:
                if '\x00' in text:
                    issues.append('binary content')
                issues.extend(content_issues(text, requirement_versions=name == 'deploy/requirements.lock'))
        failures.extend(f'{name}: {issue}' for issue in issues)
    if not count:
        raise SystemExit('Nothing staged. Stage the intended source files first.')
    for failure in failures:
        print(failure)
    print(f'{count} staged files checked; {len(failures)} findings. Matched values are never printed.')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
