# Security

Report suspected vulnerabilities privately to keypoints.motion@gmail.com. Include
the affected version, reproduction steps, and impact. Do not include live tokens,
passwords, or data from a real farm in a public issue.

Use the latest version on `main`; older revisions do not receive security fixes.

## Deployment

- Keep the bot token, `ENCRYPTION_KEY`, and `DEADLINE_EVENT_SECRET` in `.env` or
  your deployment secret store. Never commit them.
- Require authentication on the Deadline Web Service. The bot signs people in by
  calling the Deadline REST API with their user name and password, and has no
  user list of its own: a server that accepts any credentials lets anyone who
  finds the bot use the farm.
- `data/app.db` holds every user's Deadline password, encrypted with
  `ENCRYPTION_KEY`. Keep both private and back them up together.
- The preview upload server speaks plain HTTP and accepts only one-time tokens
  issued for a preview job. Expose its port to the render network only, or put
  it behind a TLS reverse proxy when workers reach it over the internet.
- Store deployment credentials in GitHub Actions secrets, using a dedicated SSH
  account with only the access needed to deploy the bot.
- Run the secret and dependency checks described in `CONTRIBUTING.md` before
  publishing changes. Automated scans reduce risk but cannot prove no secret exists.

If a credential is committed, revoke or rotate it first. Removing a file or
rewriting Git history does not invalidate an exposed credential. Follow
[GitHub's sensitive-data removal guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
for history, cached references, forks, and other clones.
