#!/usr/bin/env python3
"""YouTube Subscription Pruner — identify and remove stale subscriptions."""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypedDict

from dateutil.parser import isoparse
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

SCOPES: list[str] = ["https://www.googleapis.com/auth/youtube"]
LOG: logging.Logger = logging.getLogger("youtube-pruner")

Status = Literal["active", "stale", "no_uploads"]


class Subscription(TypedDict):
    subscription_id: str
    channel_id: str
    channel_name: str


class ChannelResult(TypedDict):
    subscription_id: str
    channel_id: str
    channel_name: str
    channel_url: str
    last_upload_date: str
    status: Status
    deleted: bool


def authenticate(credentials_path: str, token_path: str) -> Credentials:
    """Authenticate via OAuth 2.0, caching tokens to disk."""
    creds: Credentials | None = None
    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except (FileNotFoundError, ValueError):
        pass

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            LOG.warning("Refresh token revoked — re-running OAuth flow")
            creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(token_path, "w") as f:
        f.write(creds.to_json())

    return creds


def api_call_with_retry(request: Any, max_retries: int = 3) -> dict[str, Any]:
    """Execute an API request with exponential backoff on transient errors."""
    for attempt in range(max_retries + 1):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e):
                raise
            if attempt == max_retries:
                raise
            wait: int = 2 ** attempt
            LOG.warning("Retryable error (attempt %d/%d), waiting %ds: %s",
                        attempt + 1, max_retries, wait, e)
            time.sleep(wait)
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            LOG.warning("Network error (attempt %d/%d), waiting %ds: %s",
                        attempt + 1, max_retries, wait, e)
            time.sleep(wait)
    raise RuntimeError("Unreachable: all retries exhausted without raising")


def fetch_subscriptions(youtube: Any) -> list[Subscription]:
    """Paginate subscriptions.list and return all subscriptions."""
    subs: list[Subscription] = []
    page_token: str | None = None
    while True:
        request = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        )
        response: dict[str, Any] = api_call_with_retry(request)
        for item in response.get("items", []):
            subs.append(Subscription(
                subscription_id=item["id"],
                channel_id=item["snippet"]["resourceId"]["channelId"],
                channel_name=item["snippet"]["title"],
            ))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return subs


def get_latest_upload_date(youtube: Any, channel_id: str) -> datetime | None:
    """Return the latest upload date for a channel, or None if no uploads."""
    request = youtube.channels().list(part="contentDetails", id=channel_id)
    response: dict[str, Any] = api_call_with_retry(request)
    items: list[dict[str, Any]] = response.get("items", [])
    if not items:
        LOG.warning("Channel %s not found or inaccessible", channel_id)
        return None

    uploads_playlist: str = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    request = youtube.playlistItems().list(
        part="contentDetails",
        playlistId=uploads_playlist,
        maxResults=1,
    )
    response = api_call_with_retry(request)
    items = response.get("items", [])
    if not items:
        return None

    date_str: str = items[0]["contentDetails"]["videoPublishedAt"]
    return isoparse(date_str)


def classify_subscriptions(
    youtube: Any, subs: list[Subscription], threshold_days: int
) -> list[ChannelResult]:
    """Resolve upload dates and classify each subscription."""
    cutoff: datetime = datetime.now(timezone.utc) - timedelta(days=threshold_days)
    results: list[ChannelResult] = []
    total: int = len(subs)
    for i, sub in enumerate(subs, 1):
        channel_id: str = sub["channel_id"]
        LOG.info("Checking %d/%d: %s", i, total, sub["channel_name"])
        try:
            last_upload: datetime | None = get_latest_upload_date(youtube, channel_id)
        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e):
                LOG.error("Quota exceeded after processing %d/%d channels", i - 1, total)
                LOG.error("Re-run the script later to continue.")
                raise
            LOG.warning("Failed to fetch channel %s (%s): %s",
                        sub["channel_name"], channel_id, e)
            last_upload = None
        except Exception as e:
            LOG.warning("Error fetching channel %s (%s): %s",
                        sub["channel_name"], channel_id, e)
            last_upload = None

        status: Status
        if last_upload is None:
            status = "no_uploads"
        elif last_upload < cutoff:
            status = "stale"
        else:
            status = "active"

        results.append(ChannelResult(
            subscription_id=sub["subscription_id"],
            channel_id=channel_id,
            channel_name=sub["channel_name"],
            channel_url=f"https://www.youtube.com/channel/{channel_id}",
            last_upload_date=last_upload.strftime("%Y-%m-%d") if last_upload else "",
            status=status,
            deleted=False,
        ))
    return results


def print_report(results: list[ChannelResult], threshold_days: int) -> None:
    """Print summary table to stdout."""
    active: list[ChannelResult] = [r for r in results if r["status"] == "active"]
    stale: list[ChannelResult] = [r for r in results if r["status"] == "stale"]
    no_uploads: list[ChannelResult] = [r for r in results if r["status"] == "no_uploads"]

    print(f"\nSubscriptions scanned: {len(results)}")
    print(f"Active:                {len(active)}")
    print(f"No uploads found:      {len(no_uploads)}")
    print(f"Stale (>{threshold_days} days):     {len(stale)}")

    if stale:
        print("\nStale channels:")
        print(f"  {'Channel Name':<26}{'Last Upload':<13}URL")
        print(f"  {'-'*26}{'-'*13}{'-'*41}")
        for r in sorted(stale, key=lambda x: x["last_upload_date"]):
            print(f"  {r['channel_name']:<26}{r['last_upload_date']:<13}{r['channel_url']}")
    print()


def write_csv(results: list[ChannelResult], output_path: str) -> None:
    """Write full results to CSV."""
    fieldnames: list[str] = [
        "subscription_id", "channel_id", "channel_name",
        "channel_url", "last_upload_date", "status", "deleted",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({**r, "deleted": str(r["deleted"]).lower()})
    LOG.info("CSV written to %s", output_path)


def delete_stale(youtube: Any, results: list[ChannelResult], dry_run: bool) -> int:
    """Delete stale subscriptions. Returns count of successful deletions."""
    stale: list[ChannelResult] = [r for r in results if r["status"] == "stale"]
    if not stale:
        print("No stale subscriptions to delete.")
        return 0

    deleted: int = 0
    for i, r in enumerate(stale, 1):
        if dry_run:
            LOG.info("[DRY RUN] Would delete: %s", r["channel_name"])
            continue
        try:
            request = youtube.subscriptions().delete(id=r["subscription_id"])
            api_call_with_retry(request)
            r["deleted"] = True
            deleted += 1
            LOG.info("Deleted %d/%d: %s", i, len(stale), r["channel_name"])
        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e):
                LOG.error("Quota exceeded after %d deletions. %d remaining.",
                          deleted, len(stale) - i)
                LOG.error("Each deletion costs 50 quota units (daily limit: 10,000).")
                LOG.error("Re-run the script tomorrow to continue.")
                break
            LOG.error("Failed to delete %s: %s", r["channel_name"], e)

    if not dry_run:
        print(f"\nDeleted {deleted}/{len(stale)} stale subscriptions.")
    return deleted


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Identify and remove stale YouTube subscriptions."
    )
    parser.add_argument("--days", type=int,
                        default=int(os.getenv("YOUTUBE_PRUNER_DAYS", "365")),
                        help="Staleness threshold in days (default: 365)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print deletions without executing")
    parser.add_argument("--output", type=str, default=None,
                        help="Save full report to CSV")
    parser.add_argument("--credentials", type=str,
                        default=os.getenv("YOUTUBE_PRUNER_CREDENTIALS", "client_secret.json"),
                        help="OAuth credentials file (default: client_secret.json)")
    parser.add_argument("--token-cache", type=str,
                        default=os.getenv("YOUTUBE_PRUNER_TOKEN_CACHE", "token.json"),
                        help="Cached OAuth token file (default: token.json)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    args: argparse.Namespace = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    creds: Credentials = authenticate(args.credentials, args.token_cache)
    youtube: Any = build("youtube", "v3", credentials=creds)

    LOG.info("Fetching subscriptions...")
    subs: list[Subscription] = fetch_subscriptions(youtube)
    if not subs:
        print("No subscriptions found.")
        return

    LOG.info("Found %d subscriptions, resolving upload dates...", len(subs))
    try:
        results: list[ChannelResult] = classify_subscriptions(youtube, subs, args.days)
    except HttpError as e:
        if e.resp.status == 403 and "quotaExceeded" in str(e):
            print("Quota exceeded during scanning. Try again later.", file=sys.stderr)
            sys.exit(1)
        raise

    print_report(results, args.days)

    if args.output:
        write_csv(results, args.output)

    stale: list[ChannelResult] = [r for r in results if r["status"] == "stale"]
    if not stale:
        return

    if args.dry_run:
        print("Dry run — no subscriptions will be deleted.")
        delete_stale(youtube, results, dry_run=True)
        return

    if not args.yes:
        answer: str = input("Proceed with deletion? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    delete_stale(youtube, results, dry_run=False)

    if args.output:
        write_csv(results, args.output)


if __name__ == "__main__":
    main()
