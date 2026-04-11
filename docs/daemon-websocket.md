# Daemon WebSocket protocol

Endpoint: `GET /v1/ws?token=<BEARER_TOKEN>`

If the daemon is configured with a bearer token (`DAEMON_BEARER_TOKEN`), the same value must be passed as the `token` query parameter (browsers and some clients cannot set custom headers on WebSocket handshakes).

## Client → server messages

JSON text frames:

### `subscribe_job`

```json
{ "type": "subscribe_job", "job_id": "<uuid>" }
```

Subscribe to events for one job. Idempotent per connection.

### `unsubscribe_job`

```json
{ "type": "unsubscribe_job", "job_id": "<uuid>" }
```

### `ping`

```json
{ "type": "ping" }
```

Server responds with `{ "type": "pong" }`.

## Server → client messages

### `job_state_changed`

```json
{
  "type": "job_state_changed",
  "job_id": "...",
  "status": "queued|downloading|converting|done|failed|cancelled",
  "phase": "download|convert|null"
}
```

### `job_progress`

```json
{
  "type": "job_progress",
  "job_id": "...",
  "phase": "download|convert",
  "percent": 42.5,
  "speed": "1.2MiB/s",
  "eta": "00:15",
  "indeterminate": false
}
```

`percent` may be `-1` when unknown. `indeterminate` is used for ffmpeg when duration is unknown.

### `job_done`

```json
{
  "type": "job_done",
  "job_id": "...",
  "download_url": "/v1/jobs/<job_id>/download"
}
```

`download_url` is a path relative to the API base URL.

### `job_failed`

```json
{
  "type": "job_failed",
  "job_id": "...",
  "error": "human-readable message",
  "phase": "download|convert"
}
```

### `job_cancelled`

```json
{
  "type": "job_cancelled",
  "job_id": "...",
  "phase": "download|convert"
}
```

`phase` is the stage the job was in when cancellation completed (used by the GUI to update the correct tab).

### `error`

```json
{
  "type": "error",
  "message": "invalid message or unknown job_id"
}
```
