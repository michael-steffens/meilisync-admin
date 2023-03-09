from fastapi import APIRouter

from meilisync_admin.routers import meilisearch, source, sync

router = APIRouter()
router.include_router(source.router, prefix="/source", tags=["Source"])
router.include_router(sync.router, prefix="/sync", tags=["Sync"])
router.include_router(meilisearch.router, prefix="/meilisearch", tags=["Meilisearch"])
