# Pydantic request/response models shared across the API routes.
from pydantic import BaseModel, Field

class CompareRequest(BaseModel):
    a: str
    b: str

class ExchangeIn(BaseModel):
    user_text: str
    assistant_text: str

class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(4, ge=1, le=20)
    min_similarity: float = Field(0.0, ge=-1.0, le=1.0)

class PushKeys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)

class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(max_length=1000, pattern=r"^https://")
    keys: PushKeys

class PushUnsubscribe(BaseModel):
    endpoint: str = Field(max_length=1000)

class TitleUpdate(BaseModel):
    title: str = Field(max_length=100)

class ChatRequest(BaseModel):
    message: str = Field(min_length=1)

class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
