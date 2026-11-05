"""P4-HTTP-Endpunkte zum Einbinden im FastAPI-Server des Game-Service."""

from fastapi import APIRouter

from .replication import CommitTimeout, NotLeader, Replication


def client_router(replication: Replication) -> APIRouter:
    router = APIRouter(prefix="/client")

    async def reply(operation) -> dict:
        try:
            return await operation
        except NotLeader as error:
            return {"ok": False, "error": "not_leader", "leader": error.leader}
        except CommitTimeout:
            return {"ok": False, "error": "timeout"}

    @router.post("/submit")
    async def submit(command: dict) -> dict:
        return await reply(replication.submit(command))

    @router.get("/views/{game_id}")
    async def views(game_id: str) -> dict:
        return await reply(replication.views(game_id))

    return router
