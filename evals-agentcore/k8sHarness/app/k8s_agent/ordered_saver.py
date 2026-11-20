"""AgentCoreMemorySaver with a correct, fast get_delta_channel_history.

langgraph 1.2 stores `messages` as a (beta) DeltaChannel: the checkpoint keeps a
sentinel, and on resume the history is rebuilt by replaying ancestor writes via
`get_delta_channel_history`. AgentCoreMemorySaver (langgraph-checkpoint-aws 1.2.7)
does not override it, so the base default runs, which:

  * calls get_tuple once per ancestor, and every get_tuple re-reads the whole
    session from ListEvents -> O(n^2); a 149-checkpoint incident took ~165 s, and
  * replays each checkpoint's writes in ListEvents order, while the contract
    requires `writes_sort_key` order (task_path, task_id, idx). The rebuilt
    history then differs from the one the model saw, and Bedrock rejects the
    signed thinking blocks ("bound to a different conversation").

This override reads the session once, walks the parent chain in memory, and
sorts each checkpoint's writes by writes_sort_key. Return shape is the base one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langgraph.checkpoint.base import WRITES_IDX_MAP, writes_sort_key
from langgraph_checkpoint_aws import AgentCoreMemorySaver
from langgraph_checkpoint_aws.checkpoint.agentcore.models import (
    ChannelDataEvent,
    CheckpointerConfig,
    CheckpointEvent,
    WriteItem,
    WritesEvent,
)

try:  # snapshot client exists only in newer releases
    from langgraph_checkpoint_aws.checkpoint.agentcore.snapshot import AgentCoreSnapshotClient
except Exception:  # pragma: no cover
    AgentCoreSnapshotClient = ()  # type: ignore[assignment]

EMPTY = getattr(__import__("langgraph_checkpoint_aws.checkpoint.agentcore.constants",
                           fromlist=["EMPTY_CHANNEL_VALUE"]), "EMPTY_CHANNEL_VALUE", object())


def _sorted_writes(events_for_ckpt: list[list[WriteItem]]) -> list[WriteItem]:
    """Order one checkpoint's writes by writes_sort_key; later duplicates of (task_id, idx) win."""
    keyed: dict[tuple[str, int], tuple[tuple, WriteItem]] = {}
    for items in events_for_ckpt:
        for pos, w in enumerate(items):
            idx = WRITES_IDX_MAP.get(w.channel, pos)
            keyed[(w.task_id, idx)] = (writes_sort_key(w.task_path, w.task_id, idx), w)
    return [w for _, w in sorted(keyed.values(), key=lambda kv: kv[0])]


class OrderedAgentCoreMemorySaver(AgentCoreMemorySaver):
    def get_delta_channel_history(self, *, config, channels: Sequence[str]) -> Mapping[str, Any]:
        if not channels:
            return {}
        if AgentCoreSnapshotClient and isinstance(self.checkpoint_event_client, AgentCoreSnapshotClient):
            return super().get_delta_channel_history(config=config, channels=channels)

        cc = CheckpointerConfig.from_runnable_config(dict(config), default_actor_id=self._resolve_actor_id(config))
        events = self.checkpoint_event_client.get_events(cc.session_id, cc.actor_id, max_results=self.max_results)

        checkpoints: dict[str, CheckpointEvent] = {}
        writes: dict[str, list[list[WriteItem]]] = {}
        channel_data: dict[tuple[str, str], Any] = {}
        for ev in events:
            if isinstance(ev, CheckpointEvent):
                checkpoints[ev.checkpoint_id] = ev
            elif isinstance(ev, WritesEvent):
                writes.setdefault(ev.checkpoint_id, []).append(ev.writes)
            elif isinstance(ev, ChannelDataEvent) and ev.value != EMPTY:
                channel_data[(ev.channel, ev.version)] = ev.value

        target_id = cc.checkpoint_id or (max(checkpoints) if checkpoints else None)
        target = checkpoints.get(target_id) if target_id else None
        collected: dict[str, list] = {c: [] for c in channels}
        seed: dict[str, Any] = {}
        remaining = set(channels)
        cursor = target.parent_checkpoint_id if target else None
        while cursor and remaining:
            ev = checkpoints.get(cursor)
            if ev is None:
                break
            tup = self.processor.build_checkpoint_tuple(ev, _sorted_writes(writes.get(cursor, [])), channel_data, cc)
            for w in reversed(tup.pending_writes or []):
                if w[1] in remaining:
                    collected[w[1]].append(w)
            for ch in list(remaining):
                if ch in tup.checkpoint["channel_values"]:
                    seed[ch] = tup.checkpoint["channel_values"][ch]
                    remaining.discard(ch)
            cursor = ev.parent_checkpoint_id

        out: dict[str, Any] = {}
        for ch in channels:
            entry: dict[str, Any] = {"writes": list(reversed(collected[ch]))}
            if ch in seed:
                entry["seed"] = seed[ch]
            out[ch] = entry
        return out

    async def aget_delta_channel_history(self, *, config, channels: Sequence[str]) -> Mapping[str, Any]:
        import asyncio

        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self.get_delta_channel_history(config=config, channels=channels))
