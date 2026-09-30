# doorzo-watcher

Checks Doorzo for new listings that match saved keywords and sends a push
notification through [ntfy](https://ntfy.sh) for each new item.

- Keywords live in `config.json`. Each entry can set `note` (a reminder shown
  in alerts, never searched), `sites` (for example
  `["mercari"]`), `max_price`, `min_price` and `max_alerts_per_check`.
- The ntfy topic is the `NTFY_TOPIC` repository secret.
- GitHub Actions runs the check every 5 minutes from 6 AM to midnight Japan time.
- Run locally with `NTFY_TOPIC=<topic> python3 watcher.py` (`--dry-run` sends nothing).
