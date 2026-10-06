"""The arq background worker (ARC-4).

Same package as the API, different process: `arq app.worker.main.WorkerSettings`.
It shares the ORM, connectors, exporters and services with the API rather
than re-implementing them over raw SQL — see the layout in docs/CONTRACTS.md.
"""
