"""Private, content-verified file store; user filenames never form disk paths."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.shared.errors import Conflict, InvalidInput, NotFound
from app.shared.models import Asset


class FileStore:
    def __init__(self, root: str | Path, max_bytes: int = 10_000_000):
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise InvalidInput("Лимит размера файлов должен быть положительным")
        self.max_bytes = max_bytes
        self.root = Path(os.path.abspath(root))
        # Reject links/junctions in any existing component before mkdir or use.
        self._check_components(self.root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._check_components(self.root)
        if not self.root.is_dir():
            raise InvalidInput("Корень файлового хранилища не является каталогом")
        root_stat = self.root.stat()
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)

    @staticmethod
    def _is_link(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)

    @classmethod
    def _check_components(cls, path: Path) -> None:
        for part in [*reversed(path.parents), path]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if cls._is_link(info):
                raise InvalidInput("Ссылки и точки перенаправления в файловом хранилище запрещены")

    def _check_root(self) -> None:
        self._check_components(self.root)
        try:
            info = self.root.stat()
        except FileNotFoundError as exc:
            raise NotFound("Файловое хранилище отсутствует") from exc
        if (info.st_dev, info.st_ino) != self._root_identity or not stat.S_ISDIR(info.st_mode):
            raise Conflict("Корень файлового хранилища был заменён")

    def _path(self, asset_id: str) -> Path:
        try:
            parsed = UUID(asset_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise InvalidInput("Некорректный идентификатор файла") from exc
        if str(parsed) != asset_id:
            raise InvalidInput("Некорректный идентификатор файла")
        return self.root / f"{asset_id}.blob"

    @staticmethod
    def _filename(value: str) -> str:
        if not isinstance(value, str):
            raise InvalidInput("Некорректное имя файла")
        # Retain a display-only basename, not a path from another OS.
        value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not value or value in {".", ".."} or len(value) > 255 or any(ord(c) < 32 for c in value):
            raise InvalidInput("Некорректное имя файла")
        return value

    def write(
        self,
        business_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        now: datetime,
        is_demo: bool = False,
    ) -> Asset:
        self._check_root()
        if not isinstance(content, bytes) or not 1 <= len(content) <= self.max_bytes:
            raise InvalidInput("Файл пуст или превышает допустимый размер")
        if (
            not isinstance(mime_type, str)
            or not 1 <= len(mime_type) <= 200
            or any(ord(c) < 32 for c in mime_type)
        ):
            raise InvalidInput("Некорректный тип файла")
        if not isinstance(business_id, str) or not business_id:
            raise InvalidInput("Не указан бизнес")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise InvalidInput("Требуется дата с часовым поясом")
        asset = Asset(
            id=str(uuid4()),
            business_id=business_id,
            filename=self._filename(filename),
            mime_type=mime_type,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=now,
            is_demo=is_demo,
        )
        target = self._path(asset.id)
        temp_path: Path | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=self.root)
            temp_path = Path(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_root()
            if target.exists() or target.is_symlink():
                raise Conflict("Идентификатор файла уже используется")
            os.replace(temp_path, target)
            temp_path = None
            # Persist directory entry when the platform supports directory fsync.
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return asset

    def read(self, asset: Asset) -> bytes:
        self._check_root()
        if asset.size < 1 or asset.size > self.max_bytes:
            raise InvalidInput("Размер файла вне допустимых пределов")
        path = self._path(asset.id)
        try:
            before = path.lstat()
        except FileNotFoundError as exc:
            raise NotFound("Файл отсутствует в хранилище") from exc
        if self._is_link(before) or not stat.S_ISREG(before.st_mode):
            raise InvalidInput("Ожидался обычный файл внутри хранилища")
        if before.st_size != asset.size or before.st_size > self.max_bytes:
            raise Conflict("Размер файла не совпадает с сохранёнными метаданными")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise NotFound("Файл отсутствует в хранилище") from exc
        except OSError as exc:
            raise InvalidInput("Не удалось безопасно открыть файл") from exc
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                self._is_link(opened)
                or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise Conflict("Файл заменён во время чтения")
            self._check_root()
            content = stream.read(self.max_bytes + 1)
        if len(content) != asset.size or len(content) > self.max_bytes:
            raise Conflict("Размер файла изменился во время чтения")
        checksum = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(checksum, asset.sha256):
            raise Conflict("Контрольная сумма файла не совпадает")
        return content
