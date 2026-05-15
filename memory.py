"""
DynamoDB-backed Memory for Voice Bot.

Provides:
1. DynamoDBCheckpointer — LangGraph-compatible checkpointer (short-term memory).
   Implements BaseCheckpointSaver interface for LangGraph StateGraph.
   Stores graph state checkpoints keyed by (thread_id, checkpoint_id).

2. DynamoDBCheckpointerLite — Lightweight alternative (no langgraph dependency).
   Simple get/put for session state. Used when langgraph is not available.

3. DynamoDBLongTermMemory — Long-term memory (across calls for same user).
   Stores user preferences, past searches, and facts.

DynamoDB Table Schemas:
- Short-term: PK=thread_id, SK=checkpoint_ns#checkpoint_id (or "state")
- Long-term:  PK=user_id, SK=memory_key
"""
import json
import time
from typing import Any, Optional, Iterator, Sequence, Tuple

import boto3
from boto3.dynamodb.conditions import Key

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langchain_core.runnables import RunnableConfig


# =============================================================
# DYNAMODB CHECKPOINTER (LangGraph-compatible, Short-Term Memory)
# =============================================================

class DynamoDBCheckpointer(BaseCheckpointSaver):
    """
    Custom DynamoDB-based checkpointer for LangGraph.

    Table schema:
        PK: thread_id (String)
        SK: checkpoint_ns#checkpoint_id (String)
        Attributes: checkpoint (JSON), metadata (JSON), parent_id (String), ttl (Number)

    Writes table schema:
        PK: thread_id (String)
        SK: checkpoint_ns#checkpoint_id#task_id#idx (String)
        Attributes: channel (String), value (JSON), ttl (Number)
    """

    def __init__(
        self,
        table_name: str = "voicebot-checkpoints",
        writes_table_name: str = "voicebot-checkpoint-writes",
        region_name: str = "us-east-1",
        ttl_seconds: int = 86400,
        endpoint_url: Optional[str] = None,
    ):
        super().__init__()
        kwargs = {"region_name": region_name}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url

        dynamodb = boto3.resource("dynamodb", **kwargs)
        self.table = dynamodb.Table(table_name)
        self.writes_table = dynamodb.Table(writes_table_name)
        self.ttl_seconds = ttl_seconds

    def _make_sk(self, checkpoint_ns: str, checkpoint_id: str) -> str:
        ns = checkpoint_ns or ""
        return f"{ns}#{checkpoint_id}"

    def _ttl(self) -> int:
        return int(time.time()) + self.ttl_seconds

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id")

        try:
            if checkpoint_id:
                sk = self._make_sk(checkpoint_ns, checkpoint_id)
                response = self.table.get_item(Key={"thread_id": thread_id, "sk": sk})
                item = response.get("Item")
                if not item:
                    return None
            else:
                prefix = f"{checkpoint_ns}#"
                response = self.table.query(
                    KeyConditionExpression=(
                        Key("thread_id").eq(thread_id) &
                        Key("sk").begins_with(prefix)
                    ),
                    ScanIndexForward=False,
                    Limit=1,
                )
                items = response.get("Items", [])
                if not items:
                    return None
                item = items[0]

            checkpoint_data = json.loads(item["checkpoint"]) if isinstance(item["checkpoint"], str) else item["checkpoint"]
            metadata_data = json.loads(item.get("metadata", "{}")) if isinstance(item.get("metadata", "{}"), str) else item.get("metadata", {})
            parent_id = item.get("parent_id", "")

            cp_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_data.get("id", ""),
                }
            }

            parent_config = None
            if parent_id:
                parent_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }

            pending_writes = self._load_writes(thread_id, checkpoint_ns, checkpoint_data.get("id", ""))

            return CheckpointTuple(
                config=cp_config,
                checkpoint=checkpoint_data,
                metadata=metadata_data,
                parent_config=parent_config,
                pending_writes=pending_writes,
            )

        except Exception as e:
            print(f"[CHECKPOINTER] Error in get_tuple: {e}")
            return None

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        if not config:
            return

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        prefix = f"{checkpoint_ns}#"

        try:
            query_kwargs = {
                "KeyConditionExpression": (
                    Key("thread_id").eq(thread_id) &
                    Key("sk").begins_with(prefix)
                ),
                "ScanIndexForward": False,
            }
            if limit:
                query_kwargs["Limit"] = limit

            response = self.table.query(**query_kwargs)

            for item in response.get("Items", []):
                checkpoint_data = json.loads(item["checkpoint"]) if isinstance(item["checkpoint"], str) else item["checkpoint"]
                metadata_data = json.loads(item.get("metadata", "{}")) if isinstance(item.get("metadata", "{}"), str) else item.get("metadata", {})
                parent_id = item.get("parent_id", "")

                cp_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_data.get("id", ""),
                    }
                }

                parent_config = None
                if parent_id:
                    parent_config = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": parent_id,
                        }
                    }

                yield CheckpointTuple(
                    config=cp_config,
                    checkpoint=checkpoint_data,
                    metadata=metadata_data,
                    parent_config=parent_config,
                )

        except Exception as e:
            print(f"[CHECKPOINTER] Error in list: {e}")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Optional[dict[str, Any]] = None,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        parent_id = config["configurable"].get("checkpoint_id", "")
        checkpoint_id = checkpoint["id"]

        sk = self._make_sk(checkpoint_ns, checkpoint_id)

        try:
            self.table.put_item(Item={
                "thread_id": thread_id,
                "sk": sk,
                "checkpoint": json.dumps(checkpoint, default=str),
                "metadata": json.dumps(metadata, default=str),
                "parent_id": parent_id or "",
                "ttl": self._ttl(),
            })
        except Exception as e:
            print(f"[CHECKPOINTER] Error in put: {e}")

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id", "")

        try:
            for idx, (channel, value) in enumerate(writes):
                sk = f"{checkpoint_ns}#{checkpoint_id}#{task_id}#{idx}"
                self.writes_table.put_item(Item={
                    "thread_id": thread_id,
                    "sk": sk,
                    "channel": channel,
                    "value": json.dumps(value, default=str),
                    "ttl": self._ttl(),
                })
        except Exception as e:
            print(f"[CHECKPOINTER] Error in put_writes: {e}")

    def _load_writes(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> list:
        prefix = f"{checkpoint_ns}#{checkpoint_id}#"
        try:
            response = self.writes_table.query(
                KeyConditionExpression=(
                    Key("thread_id").eq(thread_id) &
                    Key("sk").begins_with(prefix)
                )
            )
            writes = []
            for item in response.get("Items", []):
                channel = item.get("channel", "")
                value = json.loads(item["value"]) if isinstance(item["value"], str) else item["value"]
                parts = item["sk"].split("#")
                task_id = parts[2] if len(parts) >= 4 else ""
                writes.append((task_id, channel, value))
            return writes
        except Exception as e:
            print(f"[CHECKPOINTER] Error loading writes: {e}")
            return []


# =============================================================
# DYNAMODB CHECKPOINTER LITE (No langgraph dependency)
# =============================================================

class DynamoDBCheckpointerLite:
    """Lightweight checkpointer — simple get/put without langgraph."""

    def __init__(self, table_name="voicebot-checkpoints", region_name="us-east-1", ttl_seconds=86400):
        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self.table = dynamodb.Table(table_name)
        self.ttl_seconds = ttl_seconds

    def load(self, thread_id):
        if not thread_id:
            return None
        try:
            response = self.table.get_item(Key={"thread_id": thread_id, "sk": "state"})
            item = response.get("Item")
            if not item:
                return None
            data = item.get("state_data", {})
            if isinstance(data, str):
                data = json.loads(data)
            return data
        except Exception as e:
            print(f"[CHECKPOINTER] Load error: {e}")
            return None

    def save(self, thread_id, session_attributes):
        if not thread_id:
            return
        try:
            clean_data = {k: str(v) for k, v in session_attributes.items() if v is not None}
            self.table.put_item(Item={
                "thread_id": thread_id,
                "sk": "state",
                "state_data": clean_data,
                "updated_at": int(time.time()),
                "ttl": int(time.time()) + self.ttl_seconds,
            })
        except Exception as e:
            print(f"[CHECKPOINTER] Save error: {e}")

    def delete(self, thread_id):
        if not thread_id:
            return
        try:
            self.table.delete_item(Key={"thread_id": thread_id, "sk": "state"})
        except Exception as e:
            print(f"[CHECKPOINTER] Delete error: {e}")


# =============================================================
# DYNAMODB LONG-TERM MEMORY STORE
# =============================================================

class DynamoDBLongTermMemory:
    """
    Long-term memory store backed by DynamoDB.
    Persists user preferences, search history, and facts across calls.

    Table schema:
        PK: user_id (String) — phone number
        SK: memory_key (String) — "preferences", "search#timestamp", "fact#key"
    """

    def __init__(self, table_name="voicebot-long-term-memory", region_name="us-east-1",
                 ttl_days=90, endpoint_url=None):
        kwargs = {"region_name": region_name}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        dynamodb = boto3.resource("dynamodb", **kwargs)
        self.table = dynamodb.Table(table_name)
        self.ttl_days = ttl_days

    def _ttl(self):
        return int(time.time()) + (self.ttl_days * 86400)

    # ── READ ──────────────────────────────────────────────────

    def get_user_profile(self, user_id):
        try:
            response = self.table.get_item(Key={"user_id": user_id, "memory_key": "profile"})
            return response.get("Item", {}).get("data", {})
        except Exception as e:
            print(f"[LONG-TERM] Error getting profile: {e}")
            return {}

    def get_preferences(self, user_id):
        try:
            response = self.table.get_item(Key={"user_id": user_id, "memory_key": "preferences"})
            return response.get("Item", {}).get("data", {})
        except Exception as e:
            print(f"[LONG-TERM] Error getting preferences: {e}")
            return {}

    def get_search_history(self, user_id, limit=5):
        try:
            response = self.table.query(
                KeyConditionExpression=(
                    Key("user_id").eq(user_id) &
                    Key("memory_key").begins_with("search#")
                ),
                ScanIndexForward=False,
                Limit=limit,
            )
            return [item.get("data", {}) for item in response.get("Items", [])]
        except Exception as e:
            print(f"[LONG-TERM] Error getting history: {e}")
            return []

    def get_all_memories(self, user_id):
        try:
            response = self.table.query(KeyConditionExpression=Key("user_id").eq(user_id))
            return {item["memory_key"]: item.get("data", {}) for item in response.get("Items", [])}
        except Exception as e:
            print(f"[LONG-TERM] Error getting all memories: {e}")
            return {}

    def get_context_for_session(self, user_id):
        if not user_id or user_id == "unknown":
            return {}
        context = {}
        prefs = self.get_preferences(user_id)
        if prefs:
            context["preferences"] = prefs
        history = self.get_search_history(user_id, limit=3)
        if history:
            context["recent_searches"] = history
        profile = self.get_user_profile(user_id)
        if profile:
            context["profile"] = profile
        return context

    # ── WRITE ─────────────────────────────────────────────────

    def save_user_profile(self, user_id, profile):
        try:
            self.table.put_item(Item={
                "user_id": user_id, "memory_key": "profile",
                "data": profile, "updated_at": int(time.time()), "ttl": self._ttl(),
            })
        except Exception as e:
            print(f"[LONG-TERM] Error saving profile: {e}")

    def save_preferences(self, user_id, preferences):
        try:
            existing = self.get_preferences(user_id)
            existing.update(preferences)
            self.table.put_item(Item={
                "user_id": user_id, "memory_key": "preferences",
                "data": existing, "updated_at": int(time.time()), "ttl": self._ttl(),
            })
        except Exception as e:
            print(f"[LONG-TERM] Error saving preferences: {e}")

    def save_search(self, user_id, search_params, results_count=0):
        try:
            timestamp = int(time.time())
            self.table.put_item(Item={
                "user_id": user_id, "memory_key": f"search#{timestamp}",
                "data": {"params": search_params, "results_count": results_count, "timestamp": timestamp},
                "updated_at": timestamp, "ttl": self._ttl(),
            })
        except Exception as e:
            print(f"[LONG-TERM] Error saving search: {e}")

    def save_fact(self, user_id, fact_key, fact_value):
        try:
            self.table.put_item(Item={
                "user_id": user_id, "memory_key": f"fact#{fact_key}",
                "data": {"key": fact_key, "value": fact_value},
                "updated_at": int(time.time()), "ttl": self._ttl(),
            })
        except Exception as e:
            print(f"[LONG-TERM] Error saving fact: {e}")

    # ── SESSION UPDATE ────────────────────────────────────────

    def update_from_session(self, user_id, session_attributes):
        if not user_id or user_id == "unknown":
            return
        preferences = {}
        if session_attributes.get("location"):
            preferences["last_city"] = session_attributes["location"]
        if session_attributes.get("budget"):
            preferences["last_budget"] = session_attributes["budget"]
        if session_attributes.get("amenities") and session_attributes["amenities"] != "No specific preference":
            preferences["preferred_amenities"] = session_attributes["amenities"]
        if session_attributes.get("property_type"):
            preferences["last_property_type"] = session_attributes["property_type"]
        if session_attributes.get("configuration"):
            preferences["last_configuration"] = session_attributes["configuration"]
        if preferences:
            self.save_preferences(user_id, preferences)
        if session_attributes.get("step") in ("results", "done"):
            search_params = {
                "property_type": session_attributes.get("property_type", ""),
                "configuration": session_attributes.get("configuration", ""),
                "location": session_attributes.get("location", ""),
                "budget": session_attributes.get("budget", ""),
                "amenities": session_attributes.get("amenities", ""),
            }
            self.save_search(user_id, search_params, 0)
