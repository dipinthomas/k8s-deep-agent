"""
CloudWatch tools for the K8s agent.
These wrap boto3 CloudWatch and CloudWatch Logs Insights calls with
LangChain-compatible tool definitions.
"""

import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from langchain_core.tools import tool
import boto3

_cw = None
_logs = None


def _cloudwatch():
    global _cw
    if _cw is None:
        _cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
    return _cw


def _logs_client():
    global _logs
    if _logs is None:
        _logs = boto3.client("logs", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
    return _logs


@tool
def cloudwatch_get_metric(
    metric_name: str,
    namespace: str,
    dimensions: str,
    period: int = 60,
    minutes_back: int = 15,
    stat: str = "Average",
) -> str:
    """
    Get a CloudWatch metric time series.

    Args:
        metric_name:  CloudWatch metric name (e.g. node_filesystem_utilization)
        namespace:    CloudWatch namespace (e.g. ContainerInsights)
        dimensions:   JSON string of dimension name/value pairs, e.g.
                      '[{"Name":"ClusterName","Value":"otel-demo-prod"}]'
        period:       Aggregation period in seconds (default 60)
        minutes_back: How many minutes of history to fetch (default 15)
        stat:         Statistic to return: Average, Maximum, Sum, etc.
    """
    try:
        dims = json.loads(dimensions)
    except json.JSONDecodeError:
        return f"ERROR: dimensions must be valid JSON, got: {dimensions}"

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes_back)

    response = _cloudwatch().get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dims,
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=[stat],
    )

    datapoints = sorted(response.get("Datapoints", []), key=lambda x: x["Timestamp"])
    if not datapoints:
        return f"No data found for {namespace}/{metric_name} in last {minutes_back} minutes"

    results = []
    for dp in datapoints[-10:]:  # last 10 datapoints
        ts = dp["Timestamp"].strftime("%H:%M:%S")
        val = dp.get(stat, 0)
        unit = dp.get("Unit", "")
        results.append(f"  {ts}  {val:.2f} {unit}")

    return f"{namespace}/{metric_name} ({stat}, last {minutes_back}m):\n" + "\n".join(results)


@tool
def cloudwatch_logs_insights(
    log_group: str,
    query: str,
    minutes_back: int = 15,
    limit: int = 20,
) -> str:
    """
    Run a CloudWatch Logs Insights query.

    Args:
        log_group:    Log group name (e.g. /aws/containerinsights/otel-demo-prod/performance)
        query:        Logs Insights query string
        minutes_back: Time window in minutes (default 15)
        limit:        Max number of result rows (default 20)
    """
    client = _logs_client()
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - (minutes_back * 60)

    response = client.start_query(
        logGroupName=log_group,
        startTime=start,
        endTime=end,
        queryString=query,
        limit=limit,
    )
    query_id = response["queryId"]

    import time
    for _ in range(30):
        result = client.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(1)

    if result["status"] != "Complete":
        return f"Query {result['status']}: {query_id}"

    rows = result.get("results", [])
    if not rows:
        return f"No results for query on {log_group}"

    lines = []
    for row in rows:
        fields = {f["field"]: f["value"] for f in row}
        lines.append("  " + " | ".join(f"{k}={v}" for k, v in fields.items()))

    return f"Logs Insights ({log_group}, last {minutes_back}m):\n" + "\n".join(lines)


@tool
def cloudwatch_describe_alarms(alarm_name_prefix: str = "EKS") -> str:
    """
    List CloudWatch alarms matching a name prefix and their current state.

    Args:
        alarm_name_prefix: Filter alarms whose names start with this (default: EKS)
    """
    response = _cloudwatch().describe_alarms(
        AlarmNamePrefix=alarm_name_prefix,
        StateValue="ALARM",
    )

    alarms = response.get("MetricAlarms", [])
    if not alarms:
        return f"No alarms in ALARM state with prefix '{alarm_name_prefix}'"

    lines = []
    for alarm in alarms:
        lines.append(
            f"  {alarm['AlarmName']}: {alarm['StateValue']} — {alarm.get('StateReason', '')}"
        )

    return "Active alarms:\n" + "\n".join(lines)


@tool
def cloudwatch_get_traces(
    service_name: str = "checkoutservice",
    minutes_back: int = 15,
) -> str:
    """
    Get X-Ray trace statistics for a service (latency, error rate).
    Use this to check application-level health from distributed traces.

    Args:
        service_name: Service name to filter (e.g. checkoutservice, paymentservice)
        minutes_back: Time window in minutes (default 15)
    """
    import boto3
    from datetime import datetime, timedelta, timezone as tz

    client = boto3.client("xray", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
    end = datetime.now(tz.utc)
    start = end - timedelta(minutes=minutes_back)

    try:
        response = client.get_service_graph(StartTime=start, EndTime=end)
        services = response.get("Services", [])

        lines = []
        for svc in services:
            name = svc.get("Name", "")
            if service_name.lower() not in name.lower():
                continue
            stats = svc.get("SummaryStatistics", {})
            total = stats.get("TotalCount", 0)
            errors = stats.get("ErrorStatistics", {}).get("TotalCount", 0)
            faults = stats.get("FaultStatistics", {}).get("TotalCount", 0)
            resp_time = stats.get("TotalResponseTime", 0)
            avg_ms = (resp_time / total * 1000) if total > 0 else 0
            lines.append(
                f"  {name}: {total} requests | avg={avg_ms:.0f}ms | errors={errors} | faults={faults}"
            )

        if not lines:
            return f"No X-Ray trace data found for '{service_name}' in last {minutes_back} minutes"
        return f"X-Ray traces ({service_name}, last {minutes_back}m):\n" + "\n".join(lines)
    except Exception as e:
        return f"X-Ray query failed: {e}"


@tool
def cloudwatch_get_metric_data(
    metric_queries: str,
    minutes_back: int = 15,
) -> str:
    """
    Get multiple CloudWatch metrics in a single API call (more efficient).

    Args:
        metric_queries: JSON array of MetricDataQuery objects.
                        Example:
                        [{"Id":"m1","MetricStat":{"Metric":{"Namespace":"ContainerInsights",
                        "MetricName":"container_fs_usage_bytes",
                        "Dimensions":[{"Name":"ClusterName","Value":"otel-demo-prod"},
                        {"Name":"PodName","Value":"imageprovider"}]},
                        "Period":60,"Stat":"Average"}}]
        minutes_back:   Time window in minutes (default 15)
    """
    try:
        queries = json.loads(metric_queries)
    except json.JSONDecodeError:
        return f"ERROR: metric_queries must be valid JSON"

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes_back)

    response = _cloudwatch().get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
    )

    results = []
    for result in response.get("MetricDataResults", []):
        label = result.get("Label", result.get("Id"))
        values = result.get("Values", [])
        timestamps = result.get("Timestamps", [])
        if values:
            latest_val = values[0]
            latest_ts = timestamps[0].strftime("%H:%M:%S") if timestamps else "?"
            results.append(f"  {label}: {latest_val:.2f} at {latest_ts}")
        else:
            results.append(f"  {label}: no data")

    if not results:
        return "No metric data returned"

    return "Metric data:\n" + "\n".join(results)
