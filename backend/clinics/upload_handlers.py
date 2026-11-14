from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class PrivateImageUploadLimitHandler(FileUploadHandler):
    """Reject oversized image bodies as multipart bytes arrive, before temp staging."""

    chunk_size = 64 * 1024

    def __init__(self, request=None):
        super().__init__(request)
        self.total_received = 0
        self.file_received = 0
        self.limit_exceeded = False

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.file_received = 0

    def receive_data_chunk(self, raw_data, start):
        self.file_received += len(raw_data)
        self.total_received += len(raw_data)
        if self.file_received > settings.IMAGE_UPLOAD_MAX_BYTES or self.total_received > settings.IMAGE_UPLOAD_BATCH_MAX_BYTES:
            self.limit_exceeded = True
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None
