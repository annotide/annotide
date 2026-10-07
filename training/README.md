# Reference training pipeline

The platform never trains a model itself (ML-9). It freezes a snapshot,
emits `retrain.requested` to the project's webhooks, and records whatever
version comes back with its lineage (EXP-8). This package is the other end:
a pipeline a customer runs next to their own compute.

```
retrain.requested ─▶ prepare ─▶ split ─▶ train ─▶ evaluate ─▶ register
  (webhook)          export      EXP-3    Trainer   box P/R/F1   POST /models/{id}/versions
                     + digest                                     snapshot_id + digest + training_run
```

1. **prepare** — reads the snapshot row, refuses a digest that is not the
   snapshot's, queues a `native` export of it, downloads the archive through
   its signed URL (the API key never goes to the storage host) and checks
   the archive's manifest names the same snapshot and digest.
2. **split** — uses the snapshot's own train / val / test partition when it
   has one; otherwise splits with the platform's rule
   (`sha256("{seed}:item:{id}")`), so the same seed gives the same sets.
3. **train** — a pluggable `Trainer` (`annotide_training/trainers.py`).
4. **evaluate** — box precision / recall / F1 at IoU 0.5, per class and
   overall, on `test` (or `val` when `test` is empty). The same scorer for
   every trainer.
5. **register** — adds a model version with `snapshot_id`,
   `snapshot_digest`, the metrics and a `training_run` record (run id,
   trainer, params, timings, export job, split counts, artifact location,
   webhook delivery id). The platform re-checks the digest (409 otherwise).

Artifacts and a `run.json` land in `runs/{run_id}/`. Where the artifact goes
next — an ONNX file for `model-service`'s `onnx` backend, a model registry —
is the trainer's business; the platform only stores the lineage.

## Run it

```sh
cd training
python3.12 -m venv .venv
.venv/bin/pip install -e ../sdk     # first: the local SDK, not the PyPI release
.venv/bin/pip install -e '.[dev]'

export ANNOTIDE_API_URL=http://localhost:8000   # site root, not /api/v1
export ANNOTIDE_API_KEY=...                     # an API key with the `write` scope (AUTH-4)

# One snapshot, now. --no-register trains and evaluates without adding a version.
.venv/bin/annotide-train --model <model-id> run --project <project-id> --snapshot <snapshot-id>

# Or receive webhooks: subscribe http://<host>:8088/ to retrain.requested
export ANNOTIDE_WEBHOOK_SECRET=...              # the webhook's signing secret
.venv/bin/annotide-train --model <fallback-model-id> serve --host 0.0.0.0 --port 8088
```

`serve` verifies `X-Annotation-Signature` (5 min tolerance, constant-time),
answers `202` at once and trains on a thread (deliveries time out), skips a
repeated `X-Annotation-Delivery`, and acknowledges signed events it does not
act on (another event, no snapshot) with `200` so they are not retried.
The event's `model_id` wins over `--model`.

Options: `--trainer` (`baseline`, `fasterrcnn` or `package.module:ClassName`),
`--param key=value` (JSON values; repeatable, passed to the trainer),
`--split 0.8,0.1,0.1` (unsplit snapshots only), `--out runs`,
`--quantize int8` and `--teacher VERSION_ID` (below).

Every registered version carries the metrics the Models page compares
versions on: `precision` / `recall` / `f1` and `per_class` on the held-out
split, `size_bytes`, `latency_ms_p50` (median `predict` time per item on this
machine, `latency_device`) and `dtype`.

### Distillation and quantization

Both are recorded as the version's `derivation` and `parent_version_id`
(`docs/CONTRACTS.md`), which the Models page draws as the model's family.

**Distillation** here is the annotation loop with a big model as the
teacher: pre-label with the teacher's version (`POST /projects/{id}/prelabel`),
let people correct the drafts, take a snapshot, and train a smaller model on
it with `--teacher <the teacher's version id>`. The new version is registered
as `distilled` from the teacher, so its F1, size and latency sit next to the
teacher's. The teacher can be any model of the organisation, including one
the platform only calls through an endpoint.

**Quantization**: `--quantize int8` stores the trained model again at 8 bits,
scores it on the same held-out split and registers it as a `quantized` child
of the version this run registered, in `runs/{run_id}/int8/`. The trainer
implements `quantize(model, calibration, dtype)`; the calibration records are
a fixed-seed sample of the train split. `fasterrcnn` uses onnxruntime's static
QDQ quantization (Conv, Gemm, MatMul, per-channel weights); in a smoke test
the 76 MB model became 20 MB. How much faster it runs depends on the CPU:
x86 with VNNI / AMX gains most, Apple silicon little. Read the int8
version's F1 before switching prelabelling to it. `baseline` stores its
priors as 8-bit fractions, so the path runs without torch.

### Recording runs in MLflow (API-6)

With the `mlflow` extra (`pip install -e '.[dev,mlflow]'`), `--mlflow-experiment
NAME` also records each run in MLflow — a local server (`make mlflow` at the
repo root, `--mlflow-uri http://localhost:5001`), Databricks or Azure ML,
wherever `MLFLOW_TRACKING_URI` points and however that environment signs in.
The run gets the params, the numeric metrics, the artifacts under `model/`,
the tags `annotation.snapshot_id` / `annotation.snapshot_digest` and the
snapshot as its dataset input (`source_type` `annotation-snapshot`).
`--mlflow-register NAME` also registers it as a version of that registered
model. The platform's *Import from MLflow* (`POST /models/{id}/versions/import`)
reads the lineage back from the run or the registered version, so a version
imported that way is linked to its snapshot as one registered by this
pipeline is. `training_run.mlflow` names the run either way.

## The Faster R-CNN trainer

`--trainer fasterrcnn` fine-tunes torchvision's Faster R-CNN
(MobileNetV3-Large FPN) on the snapshot's boxes and exports it to ONNX in the
format `model-service`'s `onnx` backend loads:

```sh
pip install -e '.[torch]'   # torch, torchvision, onnx, onnxruntime (CPU wheels:
                            # --index-url https://download.pytorch.org/whl/cpu)
annotide-train run … --trainer fasterrcnn \
  --param image_root=/mnt/images --param epochs=20

# runs/{run_id}/fasterrcnn.onnx + fasterrcnn.names → the model service:
MODEL_BACKEND=onnx MODEL_PATH=runs/{run_id}/fasterrcnn.onnx uvicorn app.main:app
```

- **Images** come from `image_root`, a directory where the customer's own
  storage is mounted or synced (blobfuse2, gcsfuse, `aws s3 sync`): export
  records carry only each item's `path`, and the platform never serves media
  to the trainer. Missing files fail the run before training starts; a path
  that resolves outside `image_root` is refused.
- **Params**: `epochs` (10), `batch_size` (4), `lr` (0.01, SGD + cosine),
  `weights` (`coco` | `imagenet` | `none`), `hflip` (true), `seed` (0),
  `device` (`cuda` if available, else `cpu`), `score_threshold` (0.5).
- **Input** is letterboxed to 640 × 640 with grey padding, the same transform
  the model service applies, for training, export and evaluation alike.
- **Evaluation runs the exported ONNX file** through onnxruntime, not the
  torch model, so the registered metrics are for the artifact that ships.
  The export is also checked against torch on a few training images and a
  blank frame before it is accepted.
- **Licences**: torchvision, onnx and onnxruntime are BSD / Apache / MIT.
  `weights=coco` starts from torchvision's COCO checkpoint; check its terms
  for your use, or start from `imagenet` / `none`.
- **Cost**: the artifact is ~76 MB. On an Apple M-series CPU, 48 images ×
  6 epochs take about 90 s; use `device=cuda` for real datasets.

## Bring your own trainer

```python
from annotide_training.trainers import Prediction, TrainedModel


class MyDetector:
    name = "my-detector"

    def train(self, train, val, params) -> TrainedModel:
        # train: native export records — item_id, path, width, height, shapes[…]
        # Media is the customer's own storage: read it from there.
        ...
        return TrainedModel(artifact=onnx_bytes, filename="model.onnx", classes=[...])

    def predict(self, model, record) -> list[Prediction]: ...

    # Optional, for --quantize: the same model at lower precision, loadable
    # by predict above.
    def quantize(self, model, calibration, dtype) -> TrainedModel: ...
```

`annotide-train --trainer my_package.detector:MyDetector …`. The framework
it needs is its own dependency, never the platform's.

`BaselineTrainer` needs nothing: it learns how often each class appears and
where it usually sits, and predicts that. It exists to exercise every stage
end to end and to give a floor a real model has to beat.

## Develop

```sh
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy annotide_training tests && .venv/bin/pytest
```
