# doorzo-watcher

Checks Doorzo for new listings that match saved keywords and sends a push
notification through [ntfy](https://ntfy.sh) for each new item.

- Keywords live in `config.json`. Each entry can set:
  - `note`: a reminder shown in alerts, never searched.
  - `sites`: limit to some marketplaces, for example `["mercari"]`. Leave it out to search all of them.
  - `category`: a Doorzo category code, for example `"1172"` for Sports & Outdoors > Fishing.
  - `title_must_include`: alert only when the title contains one of these words.
  - `title_must_also_include`: a second list, one of which must also be in the title
    (for example a size such as `"200"`; numbers match only as whole numbers).
  - `max_price` and `min_price` in yen, checked by the watcher against the listed
    price or current auction bid. A listing whose price later drops into range alerts once.
  - `max_alerts_per_check`.
- The ntfy topic is the `NTFY_TOPIC` repository secret.
- cron-job.org starts the check every 2 minutes, all day, through the workflow_dispatch API.
  GitHub's own schedule (every 5 minutes) is only a backup, because it often runs late.
- Run locally with `NTFY_TOPIC=<topic> python3 watcher.py` (`--dry-run` sends nothing).

## Inbox alerts (optional)

Doorzo does not push inbox messages. If the `DOORZO_COOKIE` repository secret
holds a signed-in Doorzo session, the watcher checks the unread counts on the
profile icon about once an hour and alerts when messages, notices or
support replies go up. These alerts go to a separate ntfy topic, the main
topic name plus `-inbox`, so they can have their own sound or be muted. Doorzo's login needs a reCAPTCHA, so the watcher never
signs in by itself. When the session expires it sends one "Doorzo login
expired" alert; sign in again in a browser and replace the secret. The cookie
gives full access to the account, so keep it only in the GitHub secret.

