SELECT
  (SELECT count(*) FROM job WHERE status IN ('queued', 'running'))
  + (SELECT count(*) FROM outbox_event WHERE published_at IS NULL AND attempts < 10)
  + (SELECT count(*) FROM webhook_delivery WHERE status = 'pending' AND next_attempt_at <= CURRENT_TIMESTAMP)
