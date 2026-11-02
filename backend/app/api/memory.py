# Memory endpoints: hybrid search over past exchanges, and listing/deleting stored facts.
from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.api.auth import current_user
from app.api.schemas import SearchRequest
from app.llm.embeddings import embed
from app.memory import retrieval

router = APIRouter(prefix="/Assistant/api/memory", tags=["memory"])


@router.post("/search")
async def search(req: SearchRequest, user: dict = Depends(current_user)):
    query_vec = await embed(req.query, "query")
    return await retrieval.recall_exchanges(user["id"], req.query, query_vec, req.top_k, req.min_similarity)

@router.get("/facts")
async def get_facts(user: dict = Depends(current_user)):
    return await db.list_facts(user["id"])


@router.delete("/facts/{fact_id}", status_code=204)
async def remove_fact(fact_id: str, user: dict = Depends(current_user)):
    if not await db.delete_fact(user["id"], fact_id):
        raise HTTPException(status_code=404, detail="Fact not found")