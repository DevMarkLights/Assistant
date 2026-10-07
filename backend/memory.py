# FastAPI router exposing a semantic memory search endpoint that queries past exchanges by vector similarity.
import os
from fastapi import APIRouter, Depends, HTTPException

import db2
from embeddings import embed
from models import SearchRequest
from config import EMBED_MODEL
from auth import current_user


router = APIRouter(prefix="/Assistant/api/memory", tags=["memory"])


@router.post("/search")
async def search(req: SearchRequest, user: dict = Depends(current_user)):
    query_vec = await embed(req.query, "query")
    return await db2.search_exchanges(user["id"], query_vec, EMBED_MODEL, req.top_k, req.min_similarity)

@router.get("/facts")
async def get_facts(user: dict = Depends(current_user)):
    return await db2.list_facts(user["id"])


@router.delete("/facts/{fact_id}", status_code=204)
async def remove_fact(fact_id: str, user: dict = Depends(current_user)):
    if not await db2.delete_fact(user["id"], fact_id):
        raise HTTPException(status_code=404, detail="Fact not found")