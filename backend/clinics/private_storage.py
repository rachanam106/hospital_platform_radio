"""Private local blob storage. Keys are opaque and never map to public URLs."""
import hashlib
import os
import tempfile
import uuid
from datetime import timedelta
from django.utils import timezone
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError


class PrivateImageStorage:
    def __init__(self, root=None):
        self.root = Path(root or settings.PRIVATE_IMAGE_STORAGE_ROOT).resolve()
        self.staging = self.root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    def stage(self, uploaded_file, *, max_bytes=None):
        limit = max_bytes or settings.IMAGE_UPLOAD_MAX_BYTES
        fd, filename = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=self.staging)
        path = Path(filename)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as target:
                chunks = uploaded_file.chunks() if hasattr(uploaded_file, "chunks") else iter(lambda: uploaded_file.read(1024 * 1024), b"")
                for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise ValidationError(f"Each imaging file must be at most {limit} bytes.")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if not size:
                raise ValidationError("Empty imaging files are not accepted.")
            return StagedImage(path=path, byte_size=size, checksum_sha256=digest.hexdigest())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def promote(self, staged):
        key = f"images/{uuid.uuid4().hex}"
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.path, destination)
        return key

    def open(self, key, mode="rb"):
        return self._path(key).open(mode)

    def exists(self, key):
        return self._path(key).is_file()

    def verify(self, key, *, checksum_sha256, byte_size):
        try:
            with self.open(key) as stream:
                digest = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
        except FileNotFoundError:
            return False
        return size == byte_size and digest.hexdigest() == checksum_sha256

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)

    def cleanup_staging(self, *, older_than=timedelta(hours=24)):
        """Remove stale, unreferenced staging chunks left by a hard process stop."""
        cutoff = timezone.now().timestamp() - older_than.total_seconds()
        removed = 0
        for path in self.staging.glob("upload-*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed

    def cleanup_orphans(self, *, older_than=timedelta(hours=24)):
        """Remove promoted blobs not referenced by a committed imaging row."""
        from .models import ImagingInstance

        referenced = set(ImagingInstance.objects.values_list("storage_key", flat=True))
        cutoff = timezone.now().timestamp() - older_than.total_seconds()
        removed = 0
        image_root = self.root / "images"
        if not image_root.exists():
            return 0
        for path in image_root.glob("*"):
            key = path.relative_to(self.root).as_posix()
            try:
                if path.is_file() and key not in referenced and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed

    def _path(self, key):
        path = (self.root / key).resolve()
        if path == self.root or self.root not in path.parents or key.startswith(("/", "\\")):
            raise ValidationError("Invalid private storage key.")
        return path


class StagedImage:
    def __init__(self, *, path, byte_size, checksum_sha256):
        self.path = path
        self.byte_size = byte_size
        self.checksum_sha256 = checksum_sha256

    def cleanup(self):
        self.path.unlink(missing_ok=True)
