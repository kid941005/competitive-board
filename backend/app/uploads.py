"""上传文件的安全读取边界。"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

CHUNK_SIZE = 64 * 1024


async def read_upload_limited(file: UploadFile, max_bytes: int) -> bytes:
    """分块读取上传文件，超过上限立即失败。

    UploadFile 后面可能接 SpooledTemporaryFile，但直接 await file.read() 仍会
    构造一份完整 bytes。这里逐块检查，累计最多 max_bytes；返回时仍需完整 bytes。
    """
    if max_bytes <= 0:
        raise HTTPException(500, "上传大小上限配置无效")

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"文件超过 {max_bytes // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)
