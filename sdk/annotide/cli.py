"""`annotation` — command line over the SDK (API-3).

Reads the key from `ANNOTIDE_API_KEY` only (a flag would end up in shell
history and `ps`); the site from `--url` or `ANNOTIDE_URL`. Single objects
print as indented JSON, lists as JSON Lines, so output pipes into `jq`.

    annotide projects list
    annotide snapshots create <project> "2026-q3" --split 0.8,0.1,0.1
    annotide export <project> --format coco --snapshot <id> -o dataset.zip
    annotide import <project> labels.json --format coco --dry-run
    annotide mcp            # MCP server for AI agents over stdio (API-8)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable
from typing import Any

from annotide import __version__
from annotide.client import URL_ENV, Client
from annotide.errors import AnnotationError

ClientFactory = Callable[[str | None], Client]


def _print_one(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _print_many(values: Iterable[Any]) -> None:
    for value in values:
        print(json.dumps(value, ensure_ascii=False))


def _split(text: str) -> dict[str, float]:
    try:
        train, val, test = (float(part) for part in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected three ratios, e.g. 0.8,0.1,0.1") from exc
    return {"train": train, "val": val, "test": test}


def _class_map(text: str) -> dict[str, str | None]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError('expected a JSON object, e.g. {"car": "vehicle"}')
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="annotide",
        description="Annotide command line. The API key is read from ANNOTIDE_API_KEY.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--url", help=f"site root, e.g. https://annotate.example.com (${URL_ENV})")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("whoami", help="the user or service account the key acts as")
    commands.add_parser(
        "mcp",
        help="serve the Model Context Protocol over stdio for AI agents (needs the mcp extra)",
    )

    projects = commands.add_parser("projects", help="projects").add_subparsers(
        dest="action", required=True
    )
    projects.add_parser("list", help="projects you are a member of")
    stats = projects.add_parser("stats", help="dashboard numbers of one project")
    stats.add_argument("project")

    items = commands.add_parser("items", help="items").add_subparsers(dest="action", required=True)
    items_list = items.add_parser("list", help="a project's items")
    items_list.add_argument("project")
    items_list.add_argument("--status")
    items_list.add_argument("--media-type")
    items_list.add_argument("--q", help="path substring")

    snapshots = commands.add_parser("snapshots", help="dataset snapshots").add_subparsers(
        dest="action", required=True
    )
    snapshots_list = snapshots.add_parser("list", help="a project's snapshots")
    snapshots_list.add_argument("project")
    create = snapshots.add_parser("create", help="freeze a snapshot and wait for it")
    create.add_argument("project")
    create.add_argument("name")
    create.add_argument("--split", type=_split, help="train,val,test ratios, e.g. 0.8,0.1,0.1")
    create.add_argument("--seed", type=int, default=0, help="split seed (default 0)")
    create.add_argument("--group-by", help='keep groups together: "folder" or "meta.<key>"')
    create.add_argument("--timeout", type=float, default=600.0)

    export = commands.add_parser("export", help="export and download an archive")
    export.add_argument("project")
    export.add_argument("--format", required=True, help="coco, yolo or native")
    export.add_argument("-o", "--output", required=True, help="file or directory to write")
    export.add_argument("--snapshot", help="snapshot id (recommended: reproducible)")
    export.add_argument("--split", choices=["train", "val", "test"])
    export.add_argument("--timeout", type=float, default=1800.0)

    imp = commands.add_parser("import", help="upload an annotation file and import it")
    imp.add_argument("project")
    imp.add_argument("file")
    imp.add_argument("--format", required=True, help="coco, yolo, voc, cvat or label_studio")
    imp.add_argument("--status", choices=["submitted", "draft"])
    imp.add_argument("--class-map", type=_class_map, help='JSON, e.g. {"car": "vehicle"}')
    imp.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    imp.add_argument("--timeout", type=float, default=1800.0)

    jobs = commands.add_parser("jobs", help="background jobs").add_subparsers(
        dest="action", required=True
    )
    jobs_list = jobs.add_parser("list", help="a project's jobs")
    jobs_list.add_argument("project")
    jobs_list.add_argument("--status")
    jobs_list.add_argument("--type")
    for name, text in (("get", "one job"), ("wait", "wait until a job finishes")):
        sub = jobs.add_parser(name, help=text)
        sub.add_argument("job")
        if name == "wait":
            sub.add_argument("--timeout", type=float, default=600.0)
    return parser


def run(args: argparse.Namespace, client: Client) -> None:
    command, action = args.command, getattr(args, "action", None)
    if command == "whoami":
        _print_one(client.me())
    elif command == "projects" and action == "list":
        _print_many(client.list_projects())
    elif command == "projects" and action == "stats":
        _print_one(client.get_stats(args.project))
    elif command == "items":
        _print_many(
            client.list_items(
                args.project, status=args.status, media_type=args.media_type, q=args.q
            )
        )
    elif command == "snapshots" and action == "list":
        _print_many(client.list_snapshots(args.project))
    elif command == "snapshots" and action == "create":
        split = None
        if args.split is not None:
            split = {**args.split, "seed": args.seed, "group_by": args.group_by}
        _print_one(client.take_snapshot(args.project, args.name, split=split, timeout=args.timeout))
    elif command == "export":
        path = client.export(
            args.project,
            args.format,
            args.output,
            snapshot_id=args.snapshot,
            split=args.split,
            timeout=args.timeout,
        )
        print(path)
    elif command == "import":
        _print_one(
            client.import_file(
                args.project,
                args.file,
                args.format,
                class_mapping=args.class_map,
                status=args.status,
                dry_run=args.dry_run,
                timeout=args.timeout,
            )
        )
    elif command == "jobs" and action == "list":
        _print_many(client.list_jobs(args.project, status=args.status, type=args.type))
    elif command == "jobs" and action == "get":
        _print_one(client.get_job(args.job))
    elif command == "jobs" and action == "wait":
        _print_one(client.wait_for_job(args.job, timeout=args.timeout))
    else:  # pragma: no cover - argparse rejects anything else
        raise AnnotationError(f"unknown command {command} {action or ''}".rstrip())


def _serve_mcp(url: str | None) -> int:
    """`annotide mcp`: the MCP server needs the `mcp` extra and owns stdout."""
    try:
        from annotide import mcp_server
    except ImportError:
        print(
            'annotation: the MCP server needs the extra: pip install "annotide[mcp]"',
            file=sys.stderr,
        )
        return 2
    mcp_server.build_server(lambda: Client(url)).run("stdio")
    return 0


def main(argv: list[str] | None = None, client_factory: ClientFactory | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        return _serve_mcp(args.url)
    factory: ClientFactory = client_factory or (lambda url: Client(url))
    try:
        with factory(args.url) as client:
            run(args, client)
    except AnnotationError as exc:
        print(f"annotation: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
