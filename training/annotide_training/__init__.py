"""Reference training pipeline: prepare → split → train → evaluate → register (ML-9, EXP-8).

The platform emits `retrain.requested` and trains nothing itself. This
package is what a customer runs on the other end: it pulls the snapshot's
export through the REST API, checks it is the data the event named, trains
with a pluggable `Trainer`, and registers the result as a model version
carrying its lineage (`snapshot_id`, `snapshot_digest`, `training_run`).
"""

__version__ = "0.1.0"
