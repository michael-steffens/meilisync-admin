from typing import Any, Dict, List, Optional

from meilisync.discover import get_source
from meilisync.enums import EventType, SourceType
from tortoise import Model, fields


class BaseModel(Model):
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        abstract = True


class Source(BaseModel):
    label = fields.CharField(max_length=255)
    type = fields.CharEnumField(enum_type=SourceType)
    connection = fields.JSONField()

    def get_source(
        self, progress: Optional[Dict[str, Any]] = None, tables: Optional[List[str]] = None
    ):
        source_cls = get_source(self.type)
        source_obj = source_cls(
            progress=progress,
            tables=tables,
            **self.connection,
        )
        return source_obj


class Sync(BaseModel):
    label = fields.CharField(max_length=255)
    source: fields.ForeignKeyRelation[Source] = fields.ForeignKeyField("models.Source")
    full_sync = fields.BooleanField(default=False)
    table = fields.CharField(max_length=255)
    index = fields.CharField(max_length=255)
    primary_key = fields.CharField(max_length=255, default="id")
    enabled = fields.BooleanField(default=True)
    fields = fields.JSONField(null=True)

    class Meta:
        unique_together = [("source", "table")]


class SyncLog(BaseModel):
    sync: fields.ForeignKeyRelation[Sync] = fields.ForeignKeyField("models.Sync")
    type = fields.CharEnumField(enum_type=EventType, default=EventType.create)
    count = fields.IntField(default=0)
