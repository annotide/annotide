"""`annotide-train run …` for one snapshot, `annotide-train serve` for the webhook.

Configuration comes from flags or the environment:
`ANNOTIDE_API_URL` (or the SDK's `ANNOTIDE_URL`), `ANNOTIDE_API_KEY`
(a `write`-scoped API key),
`ANNOTIDE_WEBHOOK_SECRET` (the webhook's signing secret, `serve` only).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from annotide import Client

from annotide_training.dataset import SplitConfig
from annotide_training.pipeline import run_pipeline
from annotide_training.tracking import MlflowSettings
from annotide_training.trainers import QUANTIZE_DTYPES, load_trainer
from annotide_training.webhook import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    RetrainRequest,
    WebhookError,
    parse_retrain,
    verify,
)

log = logging.getLogger("annotide_training")

#: Bodies are small JSON events; anything larger is not a delivery.
MAX_BODY_BYTES = 256 * 1024


def _param(value: str) -> tuple[str, Any]:
    key, sep, raw = value.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"--param {value!r}: use key=value")
    try:
        return key, json.loads(raw)
    except json.JSONDecodeError:
        return key, raw


def _split(value: str) -> SplitConfig:
    try:
        train, val, test = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--split: use train,val,test e.g. 0.8,0.1,0.1") from exc
    return SplitConfig(train=train, val=val, test=test)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="annotide-train", description=__doc__)
    parser.add_argument(
        "--url", default=os.environ.get("ANNOTIDE_API_URL") or os.environ.get("ANNOTIDE_URL")
    )
    parser.add_argument("--api-key", default=os.environ.get("ANNOTIDE_API_KEY"))
    parser.add_argument(
        "--trainer", default="baseline", help="baseline, fasterrcnn, or module:Class"
    )
    parser.add_argument("--out", type=Path, default=Path("runs"), help="where runs are written")
    parser.add_argument("--param", type=_param, action="append", default=[], metavar="K=V")
    parser.add_argument(
        "--split", type=_split, default=None, help="for unsplit snapshots: train,val,test"
    )
    parser.add_argument("--model", help="model to register into when the event names none")
    parser.add_argument(
        "--quantize",
        choices=QUANTIZE_DTYPES,
        help="also register a copy at this precision, a child of the trained version",
    )
    parser.add_argument(
        "--teacher",
        metavar="VERSION_ID",
        help="register as distilled from this model version (its pre-labels, corrected, "
        "are the snapshot)",
    )
    parser.add_argument(
        "--mlflow-experiment",
        help="also record the run in this MLflow experiment (needs the mlflow extra)",
    )
    parser.add_argument(
        "--mlflow-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI"),
        help="MLflow tracking URI (default: MLFLOW_TRACKING_URI)",
    )
    parser.add_argument(
        "--mlflow-register", metavar="NAME", help="register the run as a version of this model"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="train on one snapshot now")
    run.add_argument("--project", required=True)
    run.add_argument("--snapshot", required=True)
    run.add_argument("--no-register", action="store_true", help="train and evaluate only")

    serve = commands.add_parser("serve", help="receive retrain.requested webhooks")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8088)
    serve.add_argument("--secret", default=os.environ.get("ANNOTIDE_WEBHOOK_SECRET"))
    return parser


def make_handler(
    secret: str, on_request: Callable[[RetrainRequest], None]
) -> type[BaseHTTPRequestHandler]:
    """A handler that verifies, de-duplicates and hands off; it never trains inline.

    The platform times out deliveries (`APP_WEBHOOK_TIMEOUT`), so the answer
    is 202 as soon as the event is accepted and the run happens on a thread.
    `X-Annotation-Delivery` de-duplicates retried deliveries.
    """
    seen: set[str] = set()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: HTTPStatus, message: str) -> None:
            body = json.dumps({"detail": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(HTTPStatus.BAD_REQUEST, "missing or oversized body")
                return
            body = self.rfile.read(length)
            if not verify(secret, self.headers.get(SIGNATURE_HEADER, ""), body):
                self._reply(HTTPStatus.UNAUTHORIZED, "bad or stale signature")
                return
            try:
                request = parse_retrain(body)
            except WebhookError as exc:
                # Signed but not for us (another event, no snapshot): accept so
                # the platform does not retry it for hours.
                self._reply(HTTPStatus.OK, f"ignored: {exc}")
                return
            delivery = self.headers.get(DELIVERY_HEADER) or request.delivery_id
            with lock:
                if delivery and delivery in seen:
                    self._reply(HTTPStatus.OK, "duplicate delivery")
                    return
                if delivery:
                    seen.add(delivery)
            threading.Thread(target=on_request, args=(request,), daemon=True).start()
            self._reply(HTTPStatus.ACCEPTED, "training started")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
            log.info("webhook %s", format % args)

    return Handler


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parser().parse_args(argv)
    if not args.url or not args.api_key:
        print(
            "--url/ANNOTIDE_API_URL and --api-key/ANNOTIDE_API_KEY are required",
            file=sys.stderr,
        )
        return 2
    trainer = load_trainer(args.trainer)
    params = dict(args.param)
    mlflow_settings = (
        MlflowSettings(
            experiment=args.mlflow_experiment,
            tracking_uri=args.mlflow_uri,
            registered_model=args.mlflow_register,
        )
        if args.mlflow_experiment
        else None
    )

    def train(request: RetrainRequest, *, register: bool = True) -> None:
        with Client(args.url, args.api_key) as client:
            result = run_pipeline(
                request,
                client,
                trainer,
                out_dir=args.out,
                model_id=args.model,
                params=params,
                split=args.split,
                register=register,
                mlflow=mlflow_settings,
                quantize=args.quantize,
                teacher_version_id=args.teacher,
            )
        summary: dict[str, Any] = {
            "run_id": result.run_id,
            "metrics": result.metrics,
            "version": result.version,
        }
        if result.quantized is not None:
            summary["quantized"] = {
                "metrics": result.quantized.metrics,
                "version": result.quantized.version,
            }
        print(json.dumps(summary, indent=2))

    if args.command == "run":
        with Client(args.url, args.api_key) as client:
            snapshot = client.get_snapshot(args.project, args.snapshot)
        request = RetrainRequest(
            project_id=args.project,
            snapshot_id=args.snapshot,
            snapshot_digest=str(snapshot["digest"]),
            model_id=None,
            note=None,
            delivery_id=None,
        )
        train(request, register=not args.no_register)
        return 0

    if not args.secret:
        print("--secret/ANNOTIDE_WEBHOOK_SECRET is required for serve", file=sys.stderr)
        return 2

    def on_request(request: RetrainRequest) -> None:
        try:
            train(request)
        except Exception:  # a failed run must not take the receiver down
            log.exception("training run for snapshot %s failed", request.snapshot_id)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.secret, on_request))
    log.info("listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
