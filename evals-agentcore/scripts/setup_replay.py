"""Create the replay Lambda (Gateway target) and wire it into agentcore.json.

  python scripts/setup_replay.py            # uses AWS_PROFILE / AWS_REGION from the env

Creates: IAM role k8s-harness-replay-role, Lambda k8s-harness-replay, SSM parameter
/k8s-harness-lab/state. Then sets aws-targets.json (account, region) and adds the
Lambda as target "k8s-replay" on gateway "k8s-tools" in agentcore.json.
"""
import io
import json
import os
import pathlib
import time
import zipfile

import boto3

LAB = pathlib.Path(__file__).resolve().parents[1]
PROJECT = LAB / "k8sHarness"
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
FN, ROLE, PARAM = "k8s-harness-replay", "k8s-harness-replay-role", "/k8s-harness-lab/state"

sess = boto3.Session(region_name=REGION)
account = sess.client("sts").get_caller_identity()["Account"]
iam, lam, ssm = sess.client("iam"), sess.client("lambda"), sess.client("ssm")

trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
try:
    role_arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                               Description="k8s harness lab: replay tools for AgentCore Gateway")["Role"]["Arn"]
    time.sleep(10)
except iam.exceptions.EntityAlreadyExistsException:
    role_arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
iam.attach_role_policy(RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
iam.put_role_policy(RoleName=ROLE, PolicyName="lab-state", PolicyDocument=json.dumps({
    "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"],
                                            "Resource": f"arn:aws:ssm:{REGION}:{account}:parameter{PARAM}"}]}))

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    for f in ("handler.py", "replay_core.py"):
        z.write(LAB / "replay_lambda" / f, f)
code = buf.getvalue()
for attempt in range(6):
    try:
        try:
            arn = lam.create_function(FunctionName=FN, Runtime="python3.13", Role=role_arn, Handler="handler.handler",
                                      Code={"ZipFile": code}, Timeout=30, MemorySize=256,
                                      Environment={"Variables": {"STATE_PARAM": PARAM}},
                                      Description="Replays the retail-prod noisy-neighbour incident as kubectl/CloudWatch tools")["FunctionArn"]
        except lam.exceptions.ResourceConflictException:
            arn = lam.update_function_code(FunctionName=FN, ZipFile=code)["FunctionArn"]
        break
    except lam.exceptions.InvalidParameterValueException:
        time.sleep(10)  # new role not yet assumable
lam.get_waiter("function_active_v2").wait(FunctionName=FN)
ssm.put_parameter(Name=PARAM, Value=json.dumps({"deployments": {}, "deleted_pods": []}), Type="String", Overwrite=True)

(PROJECT / "agentcore" / "aws-targets.json").write_text(json.dumps([{"name": "default", "account": account, "region": REGION}], indent=2))
spec_path = PROJECT / "agentcore" / "agentcore.json"
spec = json.loads(spec_path.read_text())
gw = next(g for g in spec["agentCoreGateways"] if g["name"] == "k8s-tools")
gw["targets"] = [{"name": "k8s-replay", "targetType": "lambdaFunctionArn",
                  "lambdaFunctionArn": {"lambdaArn": arn, "toolSchemaFile": "../replay_lambda/tool-schema.json"}}]
spec_path.write_text(json.dumps(spec, indent=2))
print(json.dumps({"account": account, "region": REGION, "lambda": arn}, indent=2))
