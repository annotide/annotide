"""Download SAM2 ONNX weights, pinned by revision and SHA-256 (ML-7).

    python tools/fetch_sam2.py tiny /models

writes `/models/sam2.1-hiera-tiny/` with the encoder and decoder that
`MODEL_BACKEND=sam2` loads. Standard library only, so it runs in a bare
Python image. A file whose digest does not match is deleted and the run
fails; a file already present with the right digest is not downloaded again.

The weights are Meta's SAM 2.1 (Apache-2.0), as exported to ONNX by
`onnx-community` on Hugging Face. Moving to a new revision means updating
the digests below from the Hub's `?blobs=true` listing and checking them
against a download.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path
from typing import Final

HUB: Final = "https://huggingface.co"

MODELS: Final[dict[str, tuple[str, str, dict[str, str]]]] = {
    "tiny": (
        "onnx-community/sam2.1-hiera-tiny-ONNX",
        "814a066640debee5a91e70aa401fb8e17e030503",
        {
            "vision_encoder.onnx": (
                "4f30aacd3aaefbca81a0b7fe4c1fc96345570ea0a6f80ced599493d1b3be2e8c"
            ),
            "vision_encoder.onnx_data": (
                "e83df9866a5afe68ea7f0f721f18f65137fc3acbf0da1c74e946d363e09c69cc"
            ),
            "prompt_encoder_mask_decoder.onnx": (
                "874414704c5d686db7d206a35f6e15d26563d50c8c4468fccc6739bd7e491dcf"
            ),
            "prompt_encoder_mask_decoder.onnx_data": (
                "e9874d900dd4134ed60eab1e97910327c2419e0b2954485d8fd6e7f1a1470f47"
            ),
        },
    ),
    "small": (
        "onnx-community/sam2.1-hiera-small-ONNX",
        "a7df49d8de14b9d2e4504d1687b0d568f905fd8d",
        {
            "vision_encoder.onnx": (
                "aacf1f7137bb6fffcf6bf166abcfabe28f57a76059254f3fb611c4a64a208119"
            ),
            "vision_encoder.onnx_data": (
                "260fd1f0a34e72a3dc79a739e563b4facc0ba75504818b433a1f808e66637456"
            ),
            "prompt_encoder_mask_decoder.onnx": (
                "079c59b261f723ff5c6a125e69b0170a957b21c58738c28d2b0394ecd0587d7f"
            ),
            "prompt_encoder_mask_decoder.onnx_data": (
                "f9e59a584ab8ced21fa812c211bc01084204db1c9e92a5ef4fb3a49972b4e864"
            ),
        },
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(size: str, root: Path) -> Path:
    """Download one model into `root/sam2.1-hiera-<size>`; returns that directory."""
    if size not in MODELS:
        raise SystemExit(f"unknown SAM2 size {size!r}: expected one of {sorted(MODELS)}")
    repo, revision, files = MODELS[size]
    target = root / f"sam2.1-hiera-{size}"
    target.mkdir(parents=True, exist_ok=True)
    for name, expected in files.items():
        path = target / name
        if path.is_file() and _sha256(path) == expected:
            continue
        url = f"{HUB}/{repo}/resolve/{revision}/onnx/{name}"
        print(f"fetching {url}", flush=True)
        urllib.request.urlretrieve(url, path)
        actual = _sha256(path)
        if actual != expected:
            path.unlink()
            raise SystemExit(f"{name}: SHA-256 {actual}, expected {expected}")
    return target


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit("usage: fetch_sam2.py tiny|small DIRECTORY")
    print(fetch(argv[0], Path(argv[1])))


if __name__ == "__main__":
    main(sys.argv[1:])
