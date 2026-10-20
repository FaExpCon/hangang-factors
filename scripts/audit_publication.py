"""Read-only publication audit. Prints paths/counts only, never secret values.

Checks tracked and untracked nonignored files plus blobs reachable from local
Git refs. Does not modify index/history, read provider credentials over network,
or certify legal rights to redistribution. A zero finding is not a guarantee.
"""

from collections import Counter
from pathlib import Path
import json
import re
import subprocess

from dotenv import dotenv_values


PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws_access_id": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "literal_api_key_parameter": re.compile(rb"(?:crtfc_key|AUTH_KEY)[=:\"'\s]+[A-Fa-f0-9]{32,40}\b"),
}


def git(*args):
    return subprocess.check_output(["git",*args])


def raw_path(path):
    return path.startswith(("cache/","data/cache/","data/raw/","raw/"))


def main():
    tracked = [p for p in git("ls-files","-z").decode().split("\0") if p]
    working = [p for p in git("ls-files","--cached","--others","--exclude-standard","-z").decode().split("\0") if p]
    # Compare exact local credential values without printing them or a digest.
    secret_values = [str(v).encode() for k,v in dotenv_values(".env").items()
                     if v and len(str(v)) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|KRX_PW|KRX_ID",k,re.I)]
    def findings(raw):
        result = [name for name,p in PATTERNS.items() if p.search(raw)]
        if any(value in raw for value in secret_values):
            result.append("exact_local_credential")
        return result
    local_findings = []
    for path in sorted(set(working)):
        file = Path(path)
        if file.is_file():
            rules = findings(file.read_bytes())
            if rules: local_findings.append({"path":path,"rules":rules})
    objects = []
    for line in git("rev-list","--objects","--all").decode().splitlines():
        oid, _, path = line.partition(" ")
        objects.append((oid,path))
    history_findings, history_raw, history_sensitive = [], set(), set()
    process = subprocess.Popen(["git","cat-file","--batch"],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    blobs = 0
    try:
        for oid,path in objects:
            process.stdin.write((oid+"\n").encode());process.stdin.flush()
            header = process.stdout.readline().decode().split()
            if len(header)!=3: raise RuntimeError("Unexpected git object header")
            size = int(header[2]);raw=process.stdout.read(size);process.stdout.read(1)
            if header[1]!="blob":continue
            blobs+=1
            if raw_path(path):history_raw.add(path)
            if (Path(path).name.startswith(".env") and Path(path).name!=".env.example") or Path(path).suffix in (".pem", ".key", ".p12", ".pfx"):
                history_sensitive.add(path)
            rules=findings(raw)
            if rules:history_findings.append({"path":path,"object":oid,"rules":rules})
    finally:
        process.stdin.close();process.stdout.close();process.wait()
    raw = [p for p in tracked if raw_path(p)]
    output = [p for p in tracked if p.startswith("outputs/")]
    credentials = [p for p in tracked if Path(p).name.startswith(".env") and Path(p).name!=".env.example"]
    result={"tracked_files":len(tracked),"tracked_raw_cache_files":len(raw),
            "tracked_raw_cache_groups":dict(Counter(str(Path(p).parent).replace('\\','/') for p in raw)),
            "tracked_output_files":len(output),"tracked_credential_files":credentials,
            "local_nonignored_secret_findings":local_findings,
            "reachable_history_blobs_scanned":blobs,"reachable_history_raw_cache_paths":len(history_raw),
            "reachable_history_secret_findings":history_findings,
            "reachable_history_sensitive_paths":sorted(history_sensitive),
            "credential_check":"Exact configured values (>=8 chars) and limited token/key patterns; values never emitted",
            "history_scope":"Locally reachable refs only; not deleted remote refs, reflogs or hosting backups"}
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
