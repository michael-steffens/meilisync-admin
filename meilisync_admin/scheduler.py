import asyncio
from asyncio import Task
from typing import Dict, List, Tuple

from loguru import logger
from meilisync.discover import get_progress
from meilisync.enums import EventType, ProgressType
from meilisync.event import EventCollection
from meilisync.meili import Meili
from meilisync.schemas import Event
from meilisync.settings import Sync as SyncSettings

from meilisync_admin.models import Source, Sync, SyncLog
from meilisync_admin.settings import settings


class Runner:
    def __init__(self, source: Source, reset_progress: bool = False):
        self.current_progress = None
        self.reset_progress = reset_progress
        self.lock = None
        self.queue = None
        self.source = source
        self.source_obj = None
        self.progress = get_progress(ProgressType.redis)(
            dsn=settings.REDIS_URL, key=f"meilisync:progress:{source.pk}"
        )
        self.collections_map: Dict[SyncSettings, EventCollection] = {}
        self.tables_sync_settings_map: Dict[str, List[Tuple[SyncSettings, Sync]]] = {}
        self.tables_map_reverse: Dict[int, str] = {}
        self.meili_map: Dict[SyncSettings, Tuple[Meili, int]] = {}
        self.sync_settings: List[SyncSettings] = []

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def __aenter__(self):
        self.lock = asyncio.Lock()
        self.queue = asyncio.Queue()
        self.stats: Dict[int, Dict[EventType, int]] = {}
        if self.reset_progress:
            await self.progress.reset()
        self.current_progress = await self.progress.get()
        syncs = (
            await Sync.filter(enabled=True, source=self.source)
            .all()
            .select_related("meilisearch")
        )
        for sync in syncs:
            self.tables_map_reverse[sync.pk] = sync.table
            sync_setting = SyncSettings(
                table=sync.table,
                pk=sync.primary_key,
                full=sync.full_sync,
                index=sync.index,
                fields=sync.fields,
            )
            self.tables_sync_settings_map.setdefault(sync.table, []).append(
                (sync_setting, sync)
            )
            self.collections_map[sync_setting] = EventCollection()
            self.meili_map[sync_setting] = (
                sync.meili_client,
                sync.meilisearch.insert_interval,
            )
            self.sync_settings.append(
                sync_setting,
            )
        self.source_obj = self.source.get_source(
            self.current_progress, list(self.tables_sync_settings_map.keys())
        )

        for ss in self.sync_settings:
            meili, insert_interval = self.meili_map[ss]
            if ss.full and not await meili.index_exists(ss.index_name):
                count = 0
                async for items in self.source_obj.get_full_data(
                    ss, insert_interval or 10000
                ):
                    count += len(items)
                    await meili.add_full_data(ss.index_name, ss.pk, items)
                if count:
                    logger.info(
                        f'Full data sync for table "{self.source.label}.{ss.table}" '
                        f"done! {count} documents added."
                    )
                else:
                    logger.info(
                        f'Full data sync for table "{self.source.label}.{ss.table}" '
                        "done! No data found."
                    )
            if insert_interval:
                asyncio.ensure_future(
                    self.start_interval(
                        meili, self.collections_map[ss], insert_interval
                    )
                )
        return self

    async def save_stats(self):
        while True:
            await asyncio.sleep(60)
            async with self.lock:
                objs = []
                for sync_id, events in self.stats.items():
                    total = 0
                    for event_type, count in events.items():
                        total += count
                        objs.append(
                            SyncLog(sync_id=sync_id, count=count, type=event_type)
                        )
                    stats_str = ", ".join(
                        f"{event_type.name}: {count}"
                        for event_type, count in events.items()
                    )
                    logger.info(
                        f'Save {total} sync logs for table "{self.source.label}'
                        f'.{self.tables_map_reverse[sync_id]}", {stats_str}'
                    )
                if not objs:
                    continue
                await SyncLog.bulk_create(objs)
                self.stats.clear()

    async def _save_progress(self):
        await self.progress.set(**self.current_progress)

    async def sync_data(self):
        while True:
            event = await self.queue.get()
            self.current_progress = event.progress
            if not isinstance(event, Event):
                await self._save_progress()
                continue
            ss_list = self.tables_sync_settings_map.get(event.table)
            if not ss_list:
                continue
            async with self.lock:
                for setting, sync_model in ss_list:
                    m, _ = self.meili_map[setting]
                    meilisearch = sync_model.meilisearch
                    self.stats.setdefault(sync_model.pk, {}).setdefault(event.type, 0)
                    self.stats[sync_model.pk][event.type] += 1
                    if not meilisearch.insert_size and not meilisearch.insert_interval:
                        await m.handle_event(event, setting)
                        await self._save_progress()
                    else:
                        collection = self.collections_map[setting]
                        collection.add_event(setting, event)
                        if collection.size >= meilisearch.insert_size:
                            await m.handle_events(collection)
                            await self._save_progress()

    async def listen(self):
        logger.info(
            f'Start increment sync data from "{self.source.label}" to Meilisearch,'
            f' tables: {", ".join(self.tables_sync_settings_map.keys())}...'
        )
        async for event in self.source_obj:
            if settings.DEBUG:
                logger.debug(event)
            await self.queue.put(event)

    async def start_interval(self, m: Meili, c: EventCollection, interval: int):
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.lock:  # type: ignore
                    await m.handle_events(c)
                    await self._save_progress()
            except Exception as e:
                logger.error(f"Error when insert data to Meilisearch: {e}")

    async def run(self):
        await asyncio.gather(self.save_stats(), self.sync_data(), self.listen())


class Scheduler:
    _tasks: Dict[int, Task] = {}

    @classmethod
    async def startup(cls):
        sources = await Source.all()
        for source in sources:
            cls._tasks[source.pk] = asyncio.ensure_future(cls._start_source(source))

    @classmethod
    async def _start_source(cls, source: Source, reset_progress: bool = False):
        async with Runner(source, reset_progress) as runner:
            await runner.run()

    @classmethod
    def shutdown(cls):
        for task in cls._tasks.values():
            task.cancel()

    @classmethod
    def remove_source(cls, source_id: int):
        if source_id in cls._tasks:
            cls._tasks[source_id].cancel()
            del cls._tasks[source_id]

    @classmethod
    async def restart_source(cls, source: Source, reset_progress: bool = False):
        logger.info(f'Restart source "{source.label}"...')
        source_id = source.pk
        cls.remove_source(source_id)
        cls._tasks[source_id] = asyncio.ensure_future(
            cls._start_source(source, reset_progress)
        )
