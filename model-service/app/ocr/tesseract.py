"""The Tesseract engine: the `tesseract` binary, run locally per page.

Called as a subprocess with TSV output rather than through a Python binding,
so the only dependency is the binary itself (`tesseract-ocr` in the image,
plus a `tesseract-ocr-<lang>` package per extra language).
"""

from __future__ import annotations

import asyncio
import io
import os

from PIL import Image

from app.ocr import OcrError, OcrWord

#: TSV `level` of a word row (1 page, 2 block, 3 paragraph, 4 line, 5 word).
_WORD_LEVEL = "5"
_COLUMNS = ("level", "left", "top", "width", "height", "conf", "text")


def parse_tsv(tsv: str, min_confidence: float = 0.0) -> list[OcrWord]:
    """Word rows of Tesseract's TSV output, in reading order."""
    lines = tsv.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    try:
        col = {name: header.index(name) for name in _COLUMNS}
    except ValueError as exc:
        raise OcrError(f"unexpected tesseract output: {lines[0][:80]!r}") from exc
    words: list[OcrWord] = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) < len(header) or fields[col["level"]] != _WORD_LEVEL:
            continue
        text = fields[col["text"]].strip()
        try:
            confidence = float(fields[col["conf"]]) / 100
            left, top = float(fields[col["left"]]), float(fields[col["top"]])
            width, height = float(fields[col["width"]]), float(fields[col["height"]])
        except ValueError:
            continue
        if not text or confidence < max(0.0, min_confidence):
            continue
        words.append(OcrWord(text, (left, top, left + width, top + height), round(confidence, 4)))
    return words


class TesseractEngine:
    name = "tesseract"

    def __init__(
        self,
        *,
        languages: str = "eng",
        binary: str = "tesseract",
        min_confidence: float = 0.3,
        timeout: float = 60.0,
        dpi: int = 300,
        max_side: int = 4200,
    ) -> None:
        self.languages = languages
        self.binary = binary
        self.min_confidence = min_confidence
        self.timeout = timeout
        self.dpi = dpi
        self.max_side = max_side

    @classmethod
    def from_env(cls) -> TesseractEngine:
        return cls(
            languages=os.environ.get("OCR_LANGUAGES", "eng"),
            binary=os.environ.get("TESSERACT_BIN", "tesseract"),
            min_confidence=float(os.environ.get("OCR_MIN_CONFIDENCE", "0.3")),
            timeout=float(os.environ.get("OCR_TIMEOUT", "60")),
        )

    async def read(self, image: Image.Image) -> list[OcrWord]:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "stdin",
                "stdout",
                "-l",
                self.languages,
                "--psm",
                "3",
                "--dpi",
                str(self.dpi),
                "tsv",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise OcrError(f"cannot run {self.binary}: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(
                process.communicate(buffer.getvalue()), timeout=self.timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise OcrError(f"tesseract took longer than {self.timeout:g} s") from exc
        if process.returncode != 0:
            message = err.decode(errors="replace").strip().splitlines()
            raise OcrError(f"tesseract failed: {message[-1] if message else process.returncode}")
        return parse_tsv(out.decode(errors="replace"), self.min_confidence)
