"""One-shot AWS setup: create the two DynamoDB tables + the S3 plot bucket.

Run this once before `chalice deploy`. Idempotent — re-runs are a no-op.

Usage:
    python scripts/setup_aws.py --bucket my-card-plots-bucket [--region us-east-1]
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

# Local import path so this can run as a standalone script.
import logging

log = logging.getLogger("setup_aws")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def ensure_dynamodb_table(
    ddb,
    table_name: str,
    key_schema: list,
    attribute_definitions: list,
) -> None:
    log.info("Ensuring DynamoDB table %s", table_name)
    try:
        ddb.meta.client.describe_table(TableName=table_name)
        log.info("Table %s already exists; skipping", table_name)
        return
    except ddb.meta.client.exceptions.ResourceNotFoundException:
        pass
    except ClientError:
        log.exception("describe_table failed for %s", table_name)
        raise

    try:
        ddb.create_table(
            TableName=table_name,
            KeySchema=key_schema,
            AttributeDefinitions=attribute_definitions,
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError:
        log.exception("create_table failed for %s", table_name)
        raise

    log.info("Waiting for %s to become ACTIVE...", table_name)
    try:
        ddb.meta.client.get_waiter("table_exists").wait(TableName=table_name)
    except ClientError:
        log.exception("Waiter failed for %s", table_name)
        raise
    log.info("Table %s is ACTIVE", table_name)


def ensure_s3_bucket(s3, bucket: str, region: str) -> None:
    log.info("Ensuring S3 bucket %s in %s", bucket, region)
    try:
        s3.head_bucket(Bucket=bucket)
        log.info("Bucket %s already exists; skipping creation", bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchBucket"):
            log.info("Bucket missing; creating %s", bucket)
            kwargs = {"Bucket": bucket}
            if region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            try:
                s3.create_bucket(**kwargs)
            except ClientError:
                log.exception("create_bucket failed for %s", bucket)
                raise
        else:
            log.exception("head_bucket failed for %s", bucket)
            raise

    # Allow public ACLs / public bucket policies on this bucket. The Lambda
    # uses ACL=public-read on uploaded plot objects.
    try:
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
    except ClientError:
        log.exception("put_public_access_block failed for %s", bucket)
        raise

    # Bucket policy: allow public GetObject under the plots/ prefix only.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadPlots",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/plots/*",
            }
        ],
    }
    try:
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    except ClientError:
        log.exception("put_bucket_policy failed for %s", bucket)
        raise
    log.info("Bucket %s configured for public-read on plots/*", bucket)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="S3 bucket name for plots")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument(
        "--prices-table", default="CardPrices", help="DynamoDB prices table name"
    )
    parser.add_argument(
        "--watchlist-table",
        default="CardWatchlist",
        help="DynamoDB watchlist table name",
    )
    args = parser.parse_args(argv)

    session = boto3.Session(region_name=args.region)
    ddb = session.resource("dynamodb")
    s3 = session.client("s3")

    try:
        ensure_dynamodb_table(
            ddb,
            args.prices_table,
            key_schema=[
                {"AttributeName": "card_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            attribute_definitions=[
                {"AttributeName": "card_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "N"},
            ],
        )
        ensure_dynamodb_table(
            ddb,
            args.watchlist_table,
            key_schema=[{"AttributeName": "card_id", "KeyType": "HASH"}],
            attribute_definitions=[
                {"AttributeName": "card_id", "AttributeType": "S"}
            ],
        )
        ensure_s3_bucket(s3, args.bucket, args.region)
    except Exception:
        log.exception("Setup failed")
        return 1

    log.info("All resources ready.")
    log.info(
        "Next: edit .chalice/config.json (PLOT_BUCKET=%s, POKEMONTCG_API_KEY=...) "
        "and run `chalice deploy`.",
        args.bucket,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
