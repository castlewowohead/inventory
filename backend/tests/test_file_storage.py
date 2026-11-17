"""本地文件存储适配器的测试（步骤 02）：往返读写与路径边界。"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from src.errors import NotFoundError, ValidationError
from src.integrations.file_storage import (
    LocalFileStorage,
    StoredFile,
    ensure_storage_dir,
)

# 一张最小的 PNG，用来验证二进制内容原样往返（不是文本，含 0x00 字节）。
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
)


# --- 往返读写 ---


def test_save_then_read_returns_the_same_bytes(storage: LocalFileStorage) -> None:
    stored = storage.save(PNG_BYTES, original_name="invoice.png", content_type="image/png")

    assert storage.read(stored.key) == PNG_BYTES


def test_save_records_metadata_for_traceability(storage: LocalFileStorage) -> None:
    """元数据是日后由采购记录回溯到原始单据的依据。"""
    stored = storage.save(b"hello", original_name="2026-11-09 送货单.JPG")

    assert isinstance(stored, StoredFile)
    assert stored.size == 5
    assert stored.sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert stored.original_name == "2026-11-09 送货单.JPG"
    assert stored.stored_at.utcoffset() is not None, "时间要带时区，否则跨机器无法比较"


def test_original_name_keeps_chinese_characters(storage: LocalFileStorage) -> None:
    stored = storage.save(b"x", original_name="供应商发票 0001.pdf")

    assert stored.original_name == "供应商发票 0001.pdf"


def test_stream_input_is_supported(storage: LocalFileStorage) -> None:
    """上传对象是流（Flask 的 FileStorage），不必先整份读进内存。"""
    stored = storage.save(io.BytesIO(PNG_BYTES), original_name="invoice.png")

    assert storage.read(stored.key) == PNG_BYTES
    assert stored.size == len(PNG_BYTES)


def test_open_reads_in_binary_mode(storage: LocalFileStorage) -> None:
    stored = storage.save(PNG_BYTES, original_name="a.png")

    with storage.open(stored.key) as handle:
        assert handle.read() == PNG_BYTES


def test_each_save_gets_its_own_key(storage: LocalFileStorage) -> None:
    """同名文件不互相覆盖：原始单据是证据，不能被后一次上传顶掉。"""
    first = storage.save(b"first", original_name="invoice.pdf")
    second = storage.save(b"second", original_name="invoice.pdf")

    assert first.key != second.key
    assert storage.read(first.key) == b"first"
    assert storage.read(second.key) == b"second"


def test_identical_content_is_stored_twice_with_distinct_keys(
    storage: LocalFileStorage,
) -> None:
    """内容相同不等于是同一次上传；是否算重复由步骤 05 按业务口径判断。"""
    first = storage.save(b"same", original_name="a.pdf")
    second = storage.save(b"same", original_name="a.pdf")

    assert first.sha256 == second.sha256
    assert first.key != second.key


def test_empty_content_is_still_stored(storage: LocalFileStorage) -> None:
    """空文件要留痕：它说明上传出了问题，静默丢弃反而查不出原因。"""
    stored = storage.save(b"", original_name="empty.pdf")

    assert stored.size == 0
    assert storage.read(stored.key) == b""
    assert storage.exists(stored.key) is True


def test_size_of_matches_saved_size(storage: LocalFileStorage) -> None:
    stored = storage.save(PNG_BYTES, original_name="a.png")

    assert storage.size_of(stored.key) == stored.size


def test_root_directory_is_created_on_first_save(tmp_path: Path) -> None:
    """构造适配器不产生副作用，目录在首次保存时才建。"""
    root = tmp_path / "later" / "uploads"
    storage = LocalFileStorage(root)
    assert not root.exists()

    storage.save(b"x", original_name="a.pdf")

    assert root.is_dir()


def test_ensure_storage_dir_creates_the_root(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "uploads")

    created = ensure_storage_dir(storage)

    assert created.is_dir()


# --- 存储键 ---


def test_key_is_date_partitioned_and_not_derived_from_the_filename(
    storage: LocalFileStorage,
) -> None:
    """文件名不参与路径拼接，因此用户传什么名字都影响不到落盘位置。"""
    stored = storage.save(b"x", original_name="../../etc/passwd.pdf")

    assert "etc" not in stored.key
    assert "passwd" not in stored.key
    assert ".." not in stored.key
    year, month, day, name = stored.key.split("/")
    assert (len(year), len(month), len(day)) == (4, 2, 2)
    assert name.endswith(".pdf")


@pytest.mark.parametrize(
    ("original_name", "expected_suffix"),
    [
        ("invoice.PDF", ".pdf"),
        ("invoice.jpeg", ".jpeg"),
        ("archive.tar.gz", ".gz"),
        ("no-extension", ""),
        ("trailing.", ""),
        ("weird.p h p", ""),
        ("too-long.extensionname", ""),
        ("中文.发票", ""),
        ("path/like/name.png", ".png"),
        ("windows\\path\\name.png", ".png"),
        ("  spaced.png  ", ".png"),
    ],
)
def test_extension_is_sanitised(
    storage: LocalFileStorage, original_name: str, expected_suffix: str
) -> None:
    stored = storage.save(b"x", original_name=original_name)

    assert Path(stored.key).suffix == expected_suffix
    assert storage.read(stored.key) == b"x"


def test_missing_original_name_is_rejected(storage: LocalFileStorage) -> None:
    with pytest.raises(ValidationError) as excinfo:
        storage.save(b"x", original_name="   ")

    assert excinfo.value.details == {"field": "original_name"}


# --- 路径边界 ---


@pytest.mark.parametrize(
    "key",
    [
        "../outside.pdf",
        "..\\outside.pdf",
        "2026/../../outside.pdf",
        "2026/11/../../../outside.pdf",
        "/etc/passwd",
        "/abs/path.pdf",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "\\\\server\\share\\x.pdf",
        "2026\\11\\09\\a.pdf",
        "./a.pdf",
        "2026//11/a.pdf",
        "2026/11/a.pdf/",
        " 2026/11/a.pdf",
        "2026/11/a\x00.pdf",
        "",
        "   ",
    ],
)
def test_keys_outside_the_storage_root_are_rejected(
    storage: LocalFileStorage, key: str
) -> None:
    with pytest.raises(ValidationError):
        storage.read(key)


@pytest.mark.parametrize(
    "key",
    ["../outside.pdf", "/etc/passwd", "2026/../../x.pdf", "a\x00b"],
)
def test_boundary_checks_apply_to_every_entry_point(
    storage: LocalFileStorage, key: str
) -> None:
    """exists() 也要拒绝越界键，而不是当成「不存在」静默返回 False。"""
    for call in (
        lambda: storage.read(key),
        lambda: storage.exists(key),
        lambda: storage.open(key),
        lambda: storage.delete(key),
        lambda: storage.path_for(key),
        lambda: storage.size_of(key),
    ):
        with pytest.raises(ValidationError):
            call()


def test_traversal_does_not_reach_a_real_file_outside_the_root(
    tmp_path: Path,
) -> None:
    """用真实存在的外部文件验证边界：拒绝发生在能读到它之前。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("不该被读到", encoding="utf-8")
    storage = LocalFileStorage(tmp_path / "uploads")

    with pytest.raises(ValidationError):
        storage.read("../secret.txt")


def link_directory(link: Path, target: Path) -> str:
    """在 `link` 处建一个指向 `target` 目录的重解析点，返回实际用的类型。

    Windows 上 `os.symlink` 需要管理员特权或本地安全策略授权，普通用户会拿到
    `WinError 1314`；但**目录联接（junction）不需要任何特权**，而 `Path.resolve()`
    同样会跟随它。被测的防线是「resolve() 之后再判断归属」，对两种重解析点一视同仁，
    所以退回 junction 不削弱这个用例。

    反过来说，junction 才是 Windows 上真正要防的那一种：普通用户**建得出** junction、
    **建不出** symlink。原先只测 symlink 等于在这台机器上恰好跳过了唯一能实际发生的情形。
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not link.exists():
            pytest.skip(
                "当前环境既不能建符号链接也不能建目录联接："
                f"{completed.stdout.strip()} {completed.stderr.strip()}"
            )
        return "junction"


def test_link_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    """根目录内的链接指向外部，同样越不过边界（校验在 resolve() 之后）。

    POSIX 上用符号链接，Windows 上退回不需要特权的目录联接，于是这个用例在两边
    都真的执行，而不是在 Windows 上被跳过。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("不该被读到", encoding="utf-8")
    root = tmp_path / "uploads"
    root.mkdir()

    kind = link_directory(root / "escape", outside)

    # 先确认链接真的能跨出根目录——否则这个用例会在「链接没生效」时假装通过。
    escaped = (root / "escape" / "secret.txt").resolve()
    assert root.resolve() not in escaped.parents, f"{kind} 没有指向根目录之外"

    with pytest.raises(ValidationError, match="越出存储目录"):
        LocalFileStorage(root).read("escape/secret.txt")


def test_containment_check_is_a_second_line_of_defence(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """即使键的形状校验被绕过，解析后的归属判断仍然拦得住越界。

    上面的符号链接用例在没有相应权限的 Windows 上会跳过，这个用例不依赖权限，
    直接验证 `_resolve()` 里基于 `Path.resolve()` 的归属判断本身。
    """
    monkeypatch.setattr(
        LocalFileStorage, "_validate_key", staticmethod(lambda key: None)
    )

    with pytest.raises(ValidationError, match="越出存储目录"):
        storage.read("../outside.pdf")


def test_key_pointing_inside_the_root_is_accepted(storage: LocalFileStorage) -> None:
    """合法键不受影响：边界检查不能把正常引用也拒掉。"""
    stored = storage.save(b"x", original_name="a.pdf")

    assert storage.exists(stored.key) is True
    assert storage.path_for(stored.key).is_file()
    assert storage.root.resolve() in storage.path_for(stored.key).parents


# --- 不存在与删除 ---


def test_reading_an_unknown_key_raises_not_found(storage: LocalFileStorage) -> None:
    with pytest.raises(NotFoundError):
        storage.read("2026/11/09/does-not-exist.pdf")


def test_exists_is_false_for_unknown_key(storage: LocalFileStorage) -> None:
    assert storage.exists("2026/11/09/does-not-exist.pdf") is False


def test_delete_removes_the_file(storage: LocalFileStorage) -> None:
    stored = storage.save(b"x", original_name="a.pdf")

    storage.delete(stored.key)

    assert storage.exists(stored.key) is False


def test_delete_is_idempotent(storage: LocalFileStorage) -> None:
    storage.delete("2026/11/09/never-existed.pdf")


def test_failed_write_leaves_no_partial_file(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入中断不留残片：半份文件会被后续流程当成完整单据。"""

    class Failing(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            raise OSError("模拟读取中断")

    with pytest.raises(OSError, match="模拟读取中断"):
        storage.save(Failing(), original_name="broken.pdf")

    leftovers = [path for path in storage.root.rglob("*") if path.is_file()]
    assert leftovers == []
