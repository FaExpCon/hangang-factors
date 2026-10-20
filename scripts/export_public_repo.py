"""Copy a reviewed code/docs-only snapshot; never initialize Git or copy history.

The explicit manifest is intersected with hard path/content rules. All files
are scanned before the destination is created. Existing destinations, links,
raw provider payloads and suspected credentials fail closed. No network/Git
commands, dependency installation, deletion or overwrite operations.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat


MANIFEST = "docs/PUBLIC_EXPORT_MANIFEST.txt"
DEFAULT_DESTINATION = Path(r"C:\Users\user\hangang-factors")
ROOT_FILES = {".gitignore", ".env.example", "pyproject.toml", "AGENTS.md", "README.md", "README.ko.md"}
DOC_FILES = {"docs/RESEARCH_REPORT.md", "docs/RESEARCH_REPORT.ko.md", "docs/experiments.md",
             "docs/PUBLICATION.md", MANIFEST}
IMAGES = {"docs/images/design_equity.png", "docs/images/validation_equity.png", "docs/images/yearly_returns.png"}
BLOCKED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".unlazy",
                 "data", "cache", "outputs", "raw", "agent_reports", "recovery", "incident_evidence"}
PATTERNS = {
    "private key": rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----",
    "GitHub token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "AWS access ID": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "API key literal": rb"(?:crtfc_key|AUTH_KEY|DART_API_KEY|KRX_API_KEY)[\s:=\"']+[A-Fa-f0-9]{32,40}\b",
    "credential-bearing URL": rb"[?&](?:crtfc_key|api_key|token|password)=[A-Za-z0-9_-]{16,}",
    "OpenDART corporation XML": rb"\A\s*(?:<\?xml[^>]*>\s*)?<(?:result|list)>[\s\S]*<corp_code>\s*[0-9]{8}\s*</corp_code>",
    "KRX raw response": rb'\A\s*\{[\s\S]{0,300}"OutBlock_1"\s*:\s*\[\s*\{\s*"BAS_DD"\s*:\s*"[0-9]{8}"',
    "OpenDART raw response": rb'\A\s*\{[\s\S]{0,100}"status"\s*:\s*"000"[\s\S]{0,500}"list"\s*:\s*\[[\s\S]{0,300}"rcept_no"\s*:\s*"[0-9]{14}"',
}
ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def is_link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def checked_file(root, relative):
    p = PurePosixPath(relative)
    if (not relative or "\\" in relative or ":" in relative or p.is_absolute()
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ValueError("Invalid manifest path")
    if any(part.casefold() in BLOCKED_PARTS for part in p.parts):
        raise ValueError(f"Private/raw directory is forbidden: {relative}")
    if re.search(r"(?:raw[_-]?(?:data|response)|credentials|secrets|private[_-]?key)", p.name, re.I):
        raise ValueError(f"Raw/credential file name is forbidden: {relative}")
    allowed = (relative in ROOT_FILES or relative in DOC_FILES or relative in IMAGES
               or (len(p.parts) == 3 and p.parts[:2] == ("src", "kr_quant") and p.suffix == ".py")
               or (len(p.parts) == 2 and p.parts[0] in ("scripts", "tests") and p.suffix == ".py"))
    if not allowed:
        raise ValueError(f"Not a publishable path/type: {relative}")
    current = root
    for part in p.parts:
        current = current / part
        if is_link(current):
            raise ValueError(f"Links/reparse points are forbidden: {relative}")
    if not current.is_file():
        raise ValueError(f"Manifest entry is not a file: {relative}")
    return current


def local_credential_values(root):
    """Only local .env literals; never emit values, hashes, or copy this file."""
    env = root / ".env"
    if not env.exists():
        return []
    if is_link(env):
        raise ValueError("Linked credential file is not permitted")
    values = []
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        match = ENV_ASSIGNMENT.match(line)
        if not match or not re.search(r"KEY|TOKEN|SECRET|PASS|KRX_ID", match[1], re.I):
            continue
        value = match[2].split(" #", 1)[0].strip().strip("\"'")
        if len(value) >= 8 and not value.startswith(("YOUR_", "PLACEHOLDER")):
            values.append(value.encode())
    return values


def inspect_content(relative, raw, credentials):
    if any(value in raw for value in credentials):
        raise ValueError(f"Configured credential found in {relative}; value withheld")
    for label, pattern in PATTERNS.items():
        if re.search(pattern, raw, re.I):
            raise ValueError(f"Refusing {relative}: {label} pattern; matched content withheld")
    if relative not in IMAGES:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError(f"Non-text payload in public text file: {relative}") from None
        if "\x00" in text:
            raise ValueError(f"Binary payload in public text file: {relative}")
        if relative.endswith(".py"):
            try:
                module = ast.parse(text)
            except SyntaxError:
                raise ValueError(f"Not Python source: {relative}") from None
            if len(module.body) == 1 and isinstance(module.body[0], ast.Expr) and isinstance(module.body[0].value, (ast.Dict, ast.List)):
                raise ValueError(f"Data document disguised as Python: {relative}")
        elif re.search(r'<corp_code>\s*[0-9]{8}\s*</corp_code>', text):
            raise ValueError(f"Raw provider XML embedded in {relative}")
        if re.search(r'(?m)^\s*(?:BAS_DD,.*ISU_CD|rcept_no,.*(?:corp_code|bsns_year))', text):
            raise ValueError(f"Raw provider CSV in {relative}")
        if not relative.endswith(".py"):
            for block in re.findall(r"```(?:json|xml|csv)?\s*\n(.*?)\n```", text, re.DOTALL):
                for label, pattern in PATTERNS.items():
                    if re.search(pattern, block.encode(), re.I):
                        raise ValueError(f"Forbidden fenced payload in {relative}: {label}")
    if relative == ".env.example":
        assignments = {}
        for line in raw.decode("utf-8").splitlines():
            m = ENV_ASSIGNMENT.match(line)
            if m: assignments[m[1]] = m[2]
        if assignments != {"KRX_API_KEY": "YOUR_KRX_OPEN_API_KEY", "DART_API_KEY": "YOUR_OPENDART_API_KEY"}:
            raise ValueError(".env.example must contain the two approved placeholders only")
    if relative in IMAGES:
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Approved figure is not PNG")
        # Figures must not carry arbitrary text/attached source payload chunks.
        offset = 8
        while offset < len(raw):
            size = int.from_bytes(raw[offset:offset+4], "big")
            kind = raw[offset+4:offset+8]
            if kind not in {b"IHDR", b"IDAT", b"IEND", b"pHYs", b"tEXt"}:
                raise ValueError("Unapproved figure metadata chunk")
            if kind == b"tEXt":
                content = raw[offset+8:offset+8+size]
                if not content.startswith((b"Software\x00Matplotlib", b"Description\x00Derived")):
                    raise ValueError("Unapproved PNG text metadata")
            offset += 12 + size
        if offset != len(raw):
            raise ValueError("Malformed or attached PNG data")


def plan_export(source):
    source = Path(source).absolute()
    if is_link(source):
        raise ValueError("Source root must not be a link/reparse point")
    manifest = checked_file(source, MANIFEST)
    names = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if len(names) != len(set(n.casefold() for n in names)):
        raise ValueError("Duplicate manifest paths")
    if not (ROOT_FILES | DOC_FILES | IMAGES).issubset(names):
        raise ValueError("Required public front pages/configuration/docs/figures missing from manifest")
    credentials = local_credential_values(source)
    files = []
    for name in names:
        path = checked_file(source, name)
        raw = path.read_bytes()
        inspect_content(name, raw, credentials)
        files.append((name, hashlib.sha256(raw).hexdigest()))
    return source.resolve(), files


def validate_destination(source, destination):
    destination = Path(destination).absolute()
    for current in [destination, *destination.parents]:
        if current.exists() and is_link(current):
            raise ValueError("Destination ancestry cannot contain links/reparse points")
    resolved = destination.resolve()
    if resolved == source or resolved.is_relative_to(source) or source.is_relative_to(resolved):
        raise ValueError("Destination must be separate and outside the source repository")
    if destination.exists():
        raise FileExistsError("Destination already exists; no merging or overwrite allowed")
    if not destination.parent.is_dir():
        raise ValueError("Destination parent must already exist")
    return destination


def export(source, destination, *, list_only=False):
    source, plan = plan_export(source)
    destination = validate_destination(source, destination)
    for name, _ in plan:
        print(name)
    print(f"{len(plan)} approved files; no Git metadata/history included")
    if list_only:
        print("LIST ONLY: destination not created")
        return [name for name, _ in plan]
    destination.mkdir(exist_ok=False)
    credentials = local_credential_values(source)
    for name, digest in plan:
        path = checked_file(source, name)
        raw = path.read_bytes()
        inspect_content(name, raw, credentials)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"Source changed after preflight: {name}; incomplete export, do not publish")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    actual = sorted(p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file())
    if actual != sorted(name for name, _ in plan):
        raise ValueError("Unexpected destination files; incomplete/modified export, do not publish")
    print("Snapshot copied. No repository created, no Git commands run.")
    return [name for name, _ in plan]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    p.add_argument("--list", action="store_true", dest="list_only", help="Validate and list only; do not create destination")
    return p.parse_args(argv)


def main():
    args = parse_args()
    export(args.source, args.destination, list_only=args.list_only)


if __name__ == "__main__":
    main()
