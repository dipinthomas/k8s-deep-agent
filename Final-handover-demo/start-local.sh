#!/bin/bash
# Start the full demo site locally (simulation mode — no real cluster needed)
# Usage: ./start-local.sh

set -e
cd "$(dirname "$0")"

echo "==> Starting FastAPI backend on :8000"
cd api
[ ! -d .venv ] && python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

# Simulation mode by default
export REAL_CLUSTER=false
uvicorn main:app --host 0.0.0.0 --port 8000 --reload &
API_PID=$!
cd ..

echo "==> Starting Next.js frontend on :3000"
cd demo-site
[ ! -d node_modules ] && npm install
npm run dev &
NEXT_PID=$!
cd ..

echo ""
echo "  Demo site:    http://localhost:3000"
echo "  Agent logs:   http://localhost:3000/agent"
echo "  Cluster view: http://localhost:3000/cluster"
echo "  API health:   http://localhost:8000/api/health"
echo ""
echo "  Press Ctrl+C to stop both servers."

trap "kill $API_PID $NEXT_PID 2>/dev/null; echo 'Stopped.'" EXIT INT TERM
wait
