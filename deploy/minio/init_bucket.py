"""Create the local stack's private bucket in MinIO, waiting for MinIO to start.

Local and CI only. In production the bucket is made once by hand in Cloudflare R2
(docs/deploy/railway.md), and the app's token can't create buckets at all.
"""
import os
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], region_name=os.environ.get("S3_REGION", "us-east-1"),
                      aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                      config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))
bucket = os.environ["S3_BUCKET"]
for attempt in range(60):
    try:
        client.head_bucket(Bucket=bucket)
        print(f"bucket {bucket} already exists")
        break
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            client.create_bucket(Bucket=bucket)
            print(f"created private bucket {bucket}")
            break
        print(f"waiting for the object store ({error.response['Error'].get('Code')})", file=sys.stderr)
    except BotoCoreError:
        print("waiting for the object store", file=sys.stderr)
    time.sleep(2)
else:
    sys.exit("the object store never answered")
# New buckets are private. Make sure nobody added a policy that says otherwise.
try:
    client.get_bucket_policy(Bucket=bucket)
    sys.exit("the bucket has a policy; it must have none")
except ClientError as error:
    if error.response["Error"]["Code"] not in ("NoSuchBucketPolicy",):
        raise
