# ContestDeck Data

ContestDeck Data is a dependency-free, automatically refreshed JSON feed of major competitive-programming contests. It uses [CList](https://clist.by) as its attributed upstream source and runs entirely on GitHub Actions.

## Endpoints

- Current and upcoming contests: [`data/contests.json`](https://raw.githubusercontent.com/MaskedCarrot/contestdeck-data/main/data/contests.json)
- Rolling archive index: [`data/archive/index.json`](https://raw.githubusercontent.com/MaskedCarrot/contestdeck-data/main/data/archive/index.json)
- Monthly archives: `data/archive/YYYY-MM.json`

The archive retains ended contests for 183 days. The current feed contains every running or future contest returned for the configured platforms; it is not capped at 300 records.

## Contract

```json
{
  "schemaVersion": 1,
  "updatedAt": "2026-08-23T10:00:00Z",
  "source": {"name": "CLIST", "url": "https://clist.by"},
  "retentionDays": 183,
  "contests": [
    {
      "id": "clist:123456",
      "platform": "codeforces",
      "name": "Codeforces Round 1000",
      "url": "https://codeforces.com/contest/1234",
      "startsAt": "2026-08-25T14:35:00Z",
      "endsAt": "2026-08-25T16:35:00Z"
    }
  ]
}
```

All timestamps are UTC ISO-8601 values. Records are sorted by `startsAt`, `platform`, and `id`. Supported platform slugs are `codeforces`, `codechef`, `atcoder`, `leetcode`, `hackerrank`, `hackerearth`, `topcoder`, `meta-hacker-cup`, `icpc`, `nowcoder`, `luogu`, and `cphof`.

This is a clean break from TruCoder's numeric platform IDs and millisecond timestamps.

## Run locally

Python 3.13 is recommended; no packages need to be installed.

```bash
export CLIST_USERNAME="your-clist-username"
export CLIST_API_KEY="your-new-clist-api-key"
python collector.py dry-run
python collector.py reconcile
python -m unittest discover -s tests -v
```

Modes:

- `incremental` refreshes all current/future contests and the last seven days, preserving older archive files until reconciliation.
- `reconcile` rebuilds the entire 183-day rolling window.
- `dry-run` performs reconciliation and validation without changing files.

## Deploy

1. Revoke the API key exposed in the legacy repository and generate a replacement in your [CList account](https://clist.by/api/v2/doc/).
2. In **Settings → Secrets and variables → Actions**, add `CLIST_USERNAME` and `CLIST_API_KEY` as repository secrets. Never commit either value. See [GitHub's encrypted-secrets guide](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions).
3. Open **Actions → Refresh contest data → Run workflow**, select `reconcile`, and run it.
4. Confirm the workflow commits populated `data/contests.json` and archive files.
5. Run an `incremental` workflow immediately afterward; it should succeed without a commit when CList data is unchanged.
6. Point ContestDeck clients at the raw `data/contests.json` endpoint above.

Incremental collection runs every three hours at minute 17. A full reconciliation runs Sundays at 02:47 UTC. GitHub notes that scheduled workflows can be delayed during high load, particularly near the start of an hour, which is why these schedules are offset. See [scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Standard GitHub-hosted runners are free for public repositories under [GitHub Actions billing](https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions).

## Data source and terms

Contest metadata is fetched from and attributed to [CList](https://clist.by). CList documents a standard limit of 10 API requests per minute; the collector spaces requests accordingly and caches the normalized result in this repository. Review the [CList API documentation](https://clist.by/api/v2/doc/) and [CList terms](https://clist.by/terms/) before reusing or republishing the data.

The MIT license covers this repository's source code. Upstream contest data remains subject to its source terms.
