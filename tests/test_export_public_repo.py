"""Public exporter tests: no real output trees, caches, Git index or network."""

import struct
import zlib
from pathlib import Path

import pytest

from scripts import export_public_repo as exporter


def test_public_destination_default_and_override():
    assert str(exporter.parse_args([]).destination).replace("\\", "/") == "C:/Users/user/hangang-factors"
    assert exporter.parse_args(["--destination", "another-new-folder"]).destination == Path("another-new-folder")


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B",1,1,8,2,0,0,0))
            + chunk(b"IDAT",zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND",b""))


@pytest.fixture
def tree(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    names = sorted(exporter.ROOT_FILES | exporter.DOC_FILES | exporter.IMAGES | {"src/kr_quant/example.py"})
    for name in names:
        p = source / name; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png() if name in exporter.IMAGES else b"# public example\n")
    (source/".env.example").write_text("KRX_API_KEY=YOUR_KRX_OPEN_API_KEY\nDART_API_KEY=YOUR_OPENDART_API_KEY\n")
    (source/exporter.MANIFEST).write_text("\n".join(names)+"\n")
    # Private files exist but are never candidates or copied by directory recursion.
    for name in (".env","cache/provider.json","data/raw.csv","outputs/result.json","docs/agent_reports/incident.md",".git/config"):
        p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("PRIVATE_NOT_FOR_EXPORT")
    return source, tmp_path/"public", names


def test_explicit_allowlist_only_no_git_and_source_unchanged(tree):
    source,dest,names=tree
    before={p:p.read_bytes() for p in source.rglob('*') if p.is_file()}
    assert exporter.export(source,dest)==names
    actual=sorted(p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file())
    assert actual==names
    assert not (dest/".git").exists() and not (dest/"cache").exists() and not (dest/".env").exists()
    assert all(p.read_bytes()==raw for p,raw in before.items())


def test_list_is_read_only(tree):
    source,dest,names=tree
    assert exporter.export(source,dest,list_only=True)==names
    assert not dest.exists()


@pytest.mark.parametrize("name",["cache/provider.json","data/raw.csv","outputs/result.json",".env",
    "docs/agent_reports/incident.md","docs/response.json","src/kr_quant/raw_response.py",
    "src/kr_quant/secrets.py","../outside.py","/tmp/outside.py","C:/secret.py"])
def test_forbidden_manifest_entry_fails_before_destination(tree,name):
    source,dest,names=tree
    with (source/exporter.MANIFEST).open('a') as f:f.write(name+'\n')
    with pytest.raises((ValueError,FileNotFoundError)):
        exporter.export(source,dest)
    assert not dest.exists()


@pytest.mark.parametrize("payload",[
    b'-----BEGIN ' + b'PRIVATE KEY-----\nnot-for-public',
    b'ghp_' + b'A'*36,
    b'AKIA' + b'A'*16,
    b'DART_API_KEY="' + b'a'*40 + b'"',
    b'https://example.invalid?crtfc_key=' + b'B'*40,
    b'<list><corp_code>00000001</corp_code></list>',
    b'{"OutBlock_1":[{"BAS_DD":"20240101","TDD_CLSPRC":"100"}]}',
    b'{"status":"000","list":[{"rcept_no":"20240101000001"}]}',
])
def test_raw_or_secret_content_refused_without_leaking_value(tree,payload):
    source,dest,_=tree
    (source/"src/kr_quant/example.py").write_bytes(payload)
    with pytest.raises(ValueError) as e:exporter.export(source,dest)
    assert payload.decode() not in str(e.value)
    assert not dest.exists()


def test_exact_local_secret_and_real_env_example_rejected(tree):
    source,dest,_=tree
    secret="unit-test-secret-not-for-public"
    (source/".env").write_text("DART_API_KEY="+secret)
    (source/"README.md").write_text(secret)
    with pytest.raises(ValueError,match="Configured credential") as e:exporter.export(source,dest)
    assert secret not in str(e.value)
    (source/"README.md").write_text("public")
    (source/".env.example").write_text("DART_API_KEY=real-key-not-allowed")
    with pytest.raises(ValueError,match="placeholders"):exporter.export(source,dest)


def test_existing_and_internal_destination_refused(tree):
    source,dest,_=tree
    dest.mkdir();(dest/"sentinel").write_text("keep")
    with pytest.raises(FileExistsError):exporter.export(source,dest)
    assert (dest/"sentinel").read_text()=="keep"
    with pytest.raises(ValueError,match="outside"):exporter.export(source,source/"public")


def test_symlink_source_rejected_where_os_allows_it(tree):
    source,dest,_=tree
    target=source/"src/kr_quant/example.py"
    original=source/"private.py";original.write_text("private")
    target.unlink()
    try:target.symlink_to(original)
    except OSError:pytest.skip("OS does not allow symlink creation for this user")
    with pytest.raises(ValueError,match="Links"):exporter.export(source,dest)
    assert not dest.exists()


def test_png_attachment_and_duplicates_rejected(tree):
    source,dest,names=tree
    (source/next(iter(exporter.IMAGES))).write_bytes(png()+b"attached raw data")
    with pytest.raises(ValueError):exporter.export(source,dest)
    (source/exporter.MANIFEST).write_text("\n".join(names+["README.md"])+"\n")
    with pytest.raises(ValueError,match="Duplicate"):exporter.export(source,dest)


def test_reparse_source_refused_without_os_symlink_privileges(tree,monkeypatch):
    source,dest,_=tree
    actual=exporter.is_link
    monkeypatch.setattr(exporter,"is_link",lambda p:p==source/"README.md" or actual(p))
    with pytest.raises(ValueError,match="Links"):exporter.export(source,dest)
    assert not dest.exists()


def test_nested_destination_link_and_binary_document_refused(tree,monkeypatch):
    source,dest,_=tree
    (source/"README.md").write_bytes(b'raw\x00binary')
    with pytest.raises(ValueError,match="Binary"):exporter.export(source,dest)
    assert not dest.exists()


def test_new_unlisted_source_not_copied(tree):
    source,dest,names=tree
    new=source/"src/kr_quant/not_yet_approved.py";new.write_text('print("not reviewed")')
    exporter.export(source,dest)
    assert not (dest/"src/kr_quant/not_yet_approved.py").exists()


def test_fenced_raw_payload_in_document_is_refused(tree):
    source,dest,_=tree
    (source/"README.md").write_text('notes\n```json\n{"OutBlock_1":[{"BAS_DD":"20240101"}]}\n```\n')
    with pytest.raises(ValueError,match="fenced payload"):exporter.export(source,dest)
    assert not dest.exists()
