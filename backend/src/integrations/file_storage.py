"""本地文件存储适配器：保存与读取原始单据。

职责是「把字节存下来，并给出一个以后还能取回同一份字节的引用」。业务层把引用
（`StoredFile.key`）写进数据库，于是任何一条采购记录都能回溯到它的原始图片或 PDF；
适配器自己不认识发票、不认识采购，也不解析文件内容。

三条规则：

1. **原始文件只写不改。** 保存后不覆盖、不压缩、不转格式，读回来的字节与存进去的
   完全一致——它是人工复核时的证据，改过就不再是证据。
2. **存储键由本模块生成，不取自用户输入。** 键形如
   `2026/11/09/3f6c…e1.jpg`：日期分目录便于按时间定位，文件名是随机值。
   上传时的原始文件名只作为元数据保留（`StoredFile.original_name`），
   从不参与路径拼接，因此用户传什么文件名都无法影响落盘位置。
3. **路径边界强制校验。** 每次按键定位文件都要确认解析后的真实路径仍在存储根目录
   之内（`_resolve()`），越界一律拒绝。符号链接也在这一步被拆解，因为校验发生在
   `Path.resolve()` 之后，根目录内指向外部的链接同样越不过去。

替换为对象存储时只需实现同一组方法；上层拿到的仍是 `StoredFile` 与 `key`。
文件大小上限与类型白名单属于**步骤 05 的上传通路**（见 `services/invoices.py` 与
配置项 `UPLOAD_MAX_BYTES`）：那里才知道业务上允许什么，本模块只管存字节。多页 PDF
的拆页与归并属于步骤 08。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from src.errors import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

# 键里允许出现的字符：日期目录、随机名与扩展名。刻意不含空格、中文与 "..".
KEY_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]*$")

# 从原始文件名里保留的扩展名形状。只认纯字母数字，长度有限：
# 扩展名只是为了之后好认文件，不能成为写入路径的注入点。
EXTENSION_PATTERN = re.compile(r"^[0-9a-z]{1,8}$")

# Windows 不允许出现在文件名中的字符，以及控制字符。
ILLEGAL_KEY_CHARS = frozenset('<>:"\\|?*') | {chr(code) for code in range(32)}

READ_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class StoredFile:
    """一份已保存文件的引用与元数据。字段由业务层写入数据库，用于日后追溯。"""

    key: str
    """存储内的相对引用。业务表保存这个值，不保存绝对路径——绝对路径随部署变化。"""

    original_name: str
    """上传时的文件名，原样保留（含中文）。只作记录，不参与路径拼接。"""

    size: int
    """字节数。"""

    sha256: str
    """内容摘要。用于校验读回的字节未被改动，也是重复上传判定的依据。

    步骤 05 定下的口径：摘要相同即同一份文件，直接拒绝并指回已有的那张单据
    （`invoice_files.sha256` 上有唯一约束）；补拍的照片字节不同，照样能传。
    """

    content_type: str | None
    """调用方声明的 MIME 类型；未声明就是 None，本模块不猜测。"""

    stored_at: datetime
    """保存时间（UTC）。"""


class LocalFileStorage:
    """把文件保存在本机目录下的存储适配器。"""

    def __init__(self, root: Path | str) -> None:
        # 不在构造时建目录：创建应用不应该产生副作用，目录在首次保存时才建。
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def save(
        self,
        content: bytes | BinaryIO,
        *,
        original_name: str,
        content_type: str | None = None,
    ) -> StoredFile:
        """保存一份文件，返回它的引用与元数据。

        content 可以是字节，也可以是可读的二进制流（如 Flask 的上传对象）。
        """
        if not original_name or not original_name.strip():
            raise ValidationError("缺少原始文件名", details={"field": "original_name"})

        original_name = original_name.strip()
        now = datetime.now(timezone.utc)
        key = self._new_key(original_name, now)
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("wb") as target:
                for chunk in _iter_chunks(content):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            # 写入中断就不留下半份文件：残片会被后续流程当成完整单据。
            self._discard(path)
            raise

        stored = StoredFile(
            key=key,
            original_name=original_name,
            size=size,
            sha256=digest.hexdigest(),
            content_type=content_type,
            stored_at=now,
        )
        logger.info("file stored key=%s size=%s sha256=%s", key, size, stored.sha256[:12])
        return stored

    def open(self, key: str) -> BinaryIO:
        """按键打开文件供读取。调用方负责关闭（建议用 with）。"""
        path = self._require(key)
        return path.open("rb")

    def read(self, key: str) -> bytes:
        """按键读出全部字节。大文件请改用 open() 逐块读。"""
        return self._require(key).read_bytes()

    def exists(self, key: str) -> bool:
        """文件是否存在。键越界时抛 ValidationError，而不是返回 False——
        越界是调用方的错误，静默当成「不存在」会掩盖问题。"""
        return self._resolve(key).is_file()

    def size_of(self, key: str) -> int:
        return self._require(key).stat().st_size

    def path_for(self, key: str) -> Path:
        """返回文件的绝对路径，供必须接收本地路径的第三方库使用。

        仅限本地适配器；需要跨存储实现的代码请用 open() / read()。
        """
        return self._require(key)

    def delete(self, key: str) -> None:
        """删除文件。不存在时静默返回（删除是幂等的）。

        原始单据是追溯证据，正常业务流程不删；这个方法用于清理上传失败或
        确认前被放弃的临时文件。
        """
        path = self._resolve(key)
        self._discard(path)

    # --- 内部实现 ---

    def _new_key(self, original_name: str, now: datetime) -> str:
        extension = self._safe_extension(original_name)
        name = uuid.uuid4().hex
        return f"{now:%Y/%m/%d}/{name}{extension}"

    @staticmethod
    def _safe_extension(original_name: str) -> str:
        """从原始文件名取扩展名，只接受短纯字母数字；否则不带扩展名。

        不接受的例子：`.tar.gz` 取 `gz`（够用）、`.ph p`、`.。jpg`、超长扩展名——
        这些都不会写进路径，文件只是少一个后缀，原始名仍完整保留在元数据里。
        """
        suffix = PurePosixPath(original_name.replace("\\", "/")).suffix.lower()
        candidate = suffix[1:] if suffix.startswith(".") else ""
        return f".{candidate}" if EXTENSION_PATTERN.match(candidate) else ""

    def _resolve(self, key: str) -> Path:
        """把键解析成绝对路径，并确认它没有越出存储根目录。"""
        self._validate_key(key)
        root = self._root.resolve()
        # resolve() 会展开 ".." 与符号链接，因此下面的归属判断对两者都有效。
        resolved = (root / key).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValidationError(
                "文件引用越出存储目录", details={"field": "key", "key": key}
            )
        return resolved

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("缺少文件引用", details={"field": "key"})
        problem = _key_problem(key)
        if problem is not None:
            raise ValidationError(
                f"文件引用不合法：{problem}", details={"field": "key", "key": key}
            )

    def _require(self, key: str) -> Path:
        path = self._resolve(key)
        if not path.is_file():
            raise NotFoundError("原始文件不存在", details={"key": key})
        return path

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _iter_chunks(content: bytes | BinaryIO) -> Iterator[bytes]:
    """把字节或二进制流统一成分块迭代，避免把大文件整份读进内存。"""
    if isinstance(content, (bytes, bytearray, memoryview)):
        data = bytes(content)
        if data:
            yield data
        return
    while True:
        chunk = content.read(READ_CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


def _key_problem(key: str) -> str | None:
    """返回键不合法的原因；合法则返回 None。

    逐项拒绝的都是能把写入或读取引出存储目录的形式，不是单纯的风格约束。
    """
    if "\x00" in key:
        return "包含空字符"
    illegal = sorted(ILLEGAL_KEY_CHARS & set(key))
    if illegal:
        # 反斜杠在这里一并拒绝：键是 POSIX 风格的相对路径，
        # 允许两种分隔符会让同一个文件有两种键，去重与比较都会出错。
        rendered = " ".join(repr(char) for char in illegal)
        return f"包含不允许的字符 {rendered}"
    if key != key.strip():
        return "首尾有空白"
    if key.startswith("/") or key.endswith("/"):
        return "不是相对路径或以分隔符结尾"
    if PurePosixPath(key).is_absolute() or re.match(r"^[A-Za-z]:", key):
        return "不是相对路径"
    parts = PurePosixPath(key).parts
    if any(part in ("..", ".") for part in parts):
        return "包含上级目录引用"
    if "//" in key:
        return "包含空目录名"
    if not KEY_PATTERN.match(key):
        return "只允许字母、数字与 . _ - /"
    return None


def ensure_storage_dir(storage: LocalFileStorage) -> Path:
    """建出存储根目录并返回它。给需要提前准备目录的脚本或测试使用。"""
    storage.root.mkdir(parents=True, exist_ok=True)
    return storage.root


__all__ = [
    "LocalFileStorage",
    "StoredFile",
    "ensure_storage_dir",
]
