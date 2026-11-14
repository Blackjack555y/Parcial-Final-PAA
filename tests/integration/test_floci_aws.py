"""Integración contra Floci: los servicios AWS que usa AegisVolt responden como AWS."""

import json

import boto3
import pytest

from aegis_common import Environment, resource_name

pytestmark = pytest.mark.integration


def test_dynamodb_conditional_write_is_idempotent(aws_endpoint, unique_suffix):
    """Base de la idempotencia de la SAGA: la 2.ª escritura condicional se rechaza."""
    ddb = boto3.client("dynamodb", endpoint_url=aws_endpoint)
    table = resource_name(f"it-{unique_suffix}", Environment.DEV)
    ddb.create_table(
        TableName=table,
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    try:
        item = {"pk": {"S": "saga-1#CHARGE"}, "amount": {"N": "10"}}
        ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        with pytest.raises(ddb.exceptions.ConditionalCheckFailedException):
            ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(pk)")
    finally:
        ddb.delete_table(TableName=table)


def test_s3_bucket_versioning_keeps_history(aws_endpoint, unique_suffix):
    """Base del respaldo del tfstate: cada escritura queda como versión recuperable."""
    s3 = boto3.client("s3", endpoint_url=aws_endpoint)
    bucket = f"aegisvolt-it-{unique_suffix}"
    key = "env:/dev/terraform.tfstate"
    s3.create_bucket(Bucket=bucket)
    try:
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        for serial in (1, 2):
            s3.put_object(Bucket=bucket, Key=key, Body=json.dumps({"serial": serial}).encode())
        versions = s3.list_object_versions(Bucket=bucket, Prefix=key)["Versions"]
        assert len(versions) == 2
        oldest = next(v for v in versions if not v["IsLatest"])  # la versión previa
        body = s3.get_object(Bucket=bucket, Key=key, VersionId=oldest["VersionId"])["Body"]
        assert json.loads(body.read())["serial"] == 1
    finally:
        for v in s3.list_object_versions(Bucket=bucket).get("Versions", []):
            s3.delete_object(Bucket=bucket, Key=v["Key"], VersionId=v["VersionId"])
        s3.delete_bucket(Bucket=bucket)


def test_cloudwatch_logs_filter_by_correlation_id(aws_endpoint, unique_suffix):
    """Base de la trazabilidad: un correlation_id se encuentra con un patrón JSON."""
    logs = boto3.client("logs", endpoint_url=aws_endpoint)
    group = f"/aegisvolt/dev/it-{unique_suffix}"
    logs.create_log_group(logGroupName=group)
    try:
        logs.create_log_stream(logGroupName=group, logStreamName="it")
        logs.put_log_events(
            logGroupName=group,
            logStreamName="it",
            logEvents=[
                {"timestamp": 1_700_000_000_000, "message": json.dumps({"correlation_id": "c-1"})},
                {"timestamp": 1_700_000_000_001, "message": json.dumps({"correlation_id": "c-2"})},
            ],
        )
        events = logs.filter_log_events(
            logGroupName=group, filterPattern='{ $.correlation_id = "c-1" }'
        )["events"]
        assert len(events) == 1
    finally:
        logs.delete_log_group(logGroupName=group)
