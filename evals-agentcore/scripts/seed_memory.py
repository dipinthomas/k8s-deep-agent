"""Put one past incident straight into AgentCore Memory, so the memory demo works now.

  python scripts/seed_memory.py
  python scripts/run_incident.py --runs 1 --tag memory-demo --use-memory

Normally the EPISODIC strategy turns a finished session into a long-term record
about 15 minutes later. This writes a long-term record directly (no waiting),
in the namespace the agent recalls from (/episodes/<actor>/...).
"""
import datetime
import os
import uuid

import boto3

REGION = os.environ.get("AWS_REGION") or boto3.Session().region_name
if not REGION:
    raise SystemExit("Set AWS_REGION (or a region in your AWS profile)")
ACTOR_ID = "k8s-agent-retail-prod"   # same as k8sHarness/app/k8s_agent/main.py
EPISODE = (
    "Past incident (seeded): checkoutservice p99 latency alarm on retail-prod. Root cause: "
    "batch/image-resizer (Deployment, no CPU limit, priority background) used about 3.7 cores on "
    "the same node as checkoutservice. Deleting the pod did not help because its ReplicaSet "
    "recreated it. Fix that worked, after human approval: kubectl scale deployment/image-resizer "
    "-n batch --replicas=0. Checkout p99 went from about 1500 ms to about 240 ms within minutes. "
    "Follow-up: add a CPU limit to image-resizer."
)

sess = boto3.Session(region_name=REGION)
ctl, data = sess.client("bedrock-agentcore-control"), sess.client("bedrock-agentcore")
memories, kw = [], {}
while True:
    page = ctl.list_memories(**kw)
    memories += page.get("memories", [])
    if not page.get("nextToken"):
        break
    kw = {"nextToken": page["nextToken"]}
mem = next((m for m in memories if m["id"].startswith("k8sHarness_") and "incidentMemory" in m["id"]), None)
if not mem:
    raise SystemExit("incident memory not found: deploy the lab first (agentcore deploy)")

ns = f"/episodes/{ACTOR_ID}/seeded-{uuid.uuid4().hex[:8]}"
resp = data.batch_create_memory_records(memoryId=mem["id"], records=[{
    "requestIdentifier": uuid.uuid4().hex, "namespaces": [ns], "content": {"text": EPISODE},
    "timestamp": datetime.datetime.now(datetime.timezone.utc)}])
failed = resp.get("failedRecords") or []
if failed:
    raise SystemExit(f"seeding failed: {failed}")
print(f"seeded 1 past incident into {mem['id']} at {ns}")
