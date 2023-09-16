import asyncio
from functools import partial
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Type
from zipfile import BadZipFile

import faiss
import numpy as np
import orjson
from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import ORJSONResponse
from onnxruntime.capi.onnxruntime_pybind11_state import InvalidProtobuf, NoSuchFile  # type: ignore
from starlette.formparsers import MultiPartParser

from app.models.base import InferenceModel

from .config import log, settings
from .models.cache import ModelCache
from .schemas import (
    MessageResponse,
    ModelType,
    TextResponse,
)


MultiPartParser.max_file_size = 2**24  # spools to disk if payload is 16 MiB or larger
app = FastAPI()


class VectorStore:
    def __init__(self, dims: int, index: Type[faiss.Index] = faiss.IndexHNSWFlat) -> None:
        self.index = index(dims, 32, faiss.METRIC_INNER_PRODUCT)
        self.key_to_id: dict[int, Any] = {}

    def search(self, embeddings: np.ndarray[int, np.dtype[Any]], k: int) -> list[Any]:
        keys: np.ndarray[int, np.dtype[np.int64]] = self.index.assign(embeddings, k)  # type: ignore
        return [self.key_to_id[idx] for row in keys.tolist() for idx in row if not idx == -1]

    def add_with_ids(self, embeddings: np.ndarray[int, np.dtype[Any]], embedding_ids: list[Any]) -> None:
        cur_total = self.index.ntotal
        self.index.add(embeddings)  # type: ignore
        new_total = self.index.ntotal
        self.key_to_id |= {key: id for key, id in zip(range(cur_total, new_total), embedding_ids)}

    @property
    def dims(self) -> int:
        return self.index.d


vector_stores: dict[str, VectorStore] = {}


def validate_embeddings(embeddings: list[float]) -> Any:
    np_embeddings = np.array(embeddings)
    if len(np_embeddings.shape) == 1:
        np_embeddings = np.expand_dims(np_embeddings, 0)
    elif len(np_embeddings.shape) != 2:
        raise HTTPException(400, f"Expected one or two axes for embeddings; got {len(np_embeddings.shape)}")
    if np_embeddings.shape[1] < 10:
        raise HTTPException(400, f"Dimension size must be at least 10; got {np_embeddings.shape[1]}")
    return np_embeddings


async def validate_payload(image: UploadFile | None, text: str | None, options: str) -> tuple[str | bytes, dict[str, Any]]:
    if image is not None:
        inputs: str | bytes = await image.read()
    elif text is not None:
        inputs = text
    else:
        raise HTTPException(400, "Either image or text must be provided")
    try:
        kwargs = orjson.loads(options)
    except orjson.JSONDecodeError:
        raise HTTPException(400, f"Invalid options JSON: {options}")

    return inputs, kwargs


def init_state() -> None:
    app.state.model_cache = ModelCache(ttl=settings.model_ttl, revalidate=settings.model_ttl > 0)
    log.info(
        (
            "Created in-memory cache with unloading "
            f"{f'after {settings.model_ttl}s of inactivity' if settings.model_ttl > 0 else 'disabled'}."
        )
    )
    # asyncio is a huge bottleneck for performance, so we use a thread pool to run blocking code
    app.state.thread_pool = ThreadPoolExecutor(settings.request_threads) if settings.request_threads > 0 else None
    app.state.model_locks = {model_type: threading.Lock() for model_type in ModelType}
    app.state.index_lock = threading.Lock()
    log.info(f"Initialized request thread pool with {settings.request_threads} threads.")


@app.on_event("startup")
async def startup_event() -> None:
    init_state()


@app.get("/", response_model=MessageResponse)
async def root() -> dict[str, str]:
    return {"message": "Immich ML"}


@app.get("/ping", response_model=TextResponse)
def ping() -> str:
    return "pong"


@app.post("/pipeline", response_class=ORJSONResponse)
async def pipeline(
    model_name: str = Form(alias="modelName"),
    model_type: ModelType = Form(alias="modelType"),
    options: str = Form(default="{}"),
    text: str | None = Form(default=None),
    image: UploadFile | None = None,
    index_name: str | None = Form(default=None),
    embedding_id: str | None = Form(default=None),
    k: int | None = Form(default=None),
) -> ORJSONResponse:
    inputs, kwargs = await validate_payload(image, text, options)
    model = await app.state.model_cache.get(model_name, model_type, **kwargs)
    outputs = await run(_predict, model, inputs, **kwargs)
    if index_name is not None:
        expanded = np.expand_dims(outputs, 0)
        if k is not None:
            if k < 1:
                raise HTTPException(400, f"k must be a positive integer; got {k}")
            if index_name not in vector_stores:
                raise HTTPException(404, f"Index '{index_name}' not found")
            outputs = await run(vector_stores[index_name].search, expanded, k)
        if embedding_id is not None:
            if index_name not in vector_stores:
                await create(index_name, [embedding_id], expanded)
            else:
                await add(index_name, [embedding_id], expanded)
    return ORJSONResponse(outputs)


@app.post("/predict", response_class=ORJSONResponse)
async def predict(
    model_name: str = Form(alias="modelName"),
    model_type: ModelType = Form(alias="modelType"),
    options: str = Form(default="{}"),
    text: str | None = Form(default=None),
    image: UploadFile | None = None,
) -> ORJSONResponse:
    inputs, kwargs = await validate_payload(image, text, options)
    model = await app.state.model_cache.get(model_name, model_type, **kwargs)
    outputs = await run(_predict, model, inputs, **kwargs)
    return ORJSONResponse(outputs)


@app.post("/index/{index_name}/search", response_class=ORJSONResponse)
async def search(index_name: str, embeddings: Any = Depends(validate_embeddings), k: int = 10) -> ORJSONResponse:
    if index_name not in vector_stores or vector_stores[index_name].dims != embeddings.shape[1]:
        raise HTTPException(404, f"Index '{index_name}' not found")
    outputs: np.ndarray[int, np.dtype[Any]] = await run(vector_stores[index_name].search, embeddings, k)
    return ORJSONResponse(outputs)


@app.post("/index/{index_name}/add")
async def add(
    index_name: str,
    embedding_ids: list[str],
    embeddings: Any = Depends(validate_embeddings),
) -> None:
    if index_name not in vector_stores or vector_stores[index_name].dims != embeddings.shape[1]:
        await create(index_name, embedding_ids, embeddings)
    else:
        log.info(f"Adding {len(embedding_ids)} embedding(s) to index '{index_name}'")
        await run(_add, vector_stores[index_name], embedding_ids, embeddings)


@app.post("/index/{index_name}/create")
async def create(
    index_name: str,
    embedding_ids: list[str],
    embeddings: Any = Depends(validate_embeddings),
) -> None:
    if embeddings.shape[0] != len(embedding_ids):
        raise HTTPException(
            400,
            f"Number of embedding IDs must match number of embeddings; got {len(embedding_ids)} ID(s) and {embeddings.shape[0]} embedding(s)",
        )
    if index_name in vector_stores:
        log.warn(f"Index '{index_name}' already exists. Overwriting.")
    log.info(f"Creating new index '{index_name}'")

    vector_stores[index_name] = await run(_create, embedding_ids, embeddings)


async def run(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if app.state.thread_pool is None:
        return func(*args, **kwargs)
    if kwargs:
        func = partial(func, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(app.state.thread_pool, func, *args)

def _load(model: InferenceModel) -> InferenceModel:
    if model.loaded:
        return model

    try:
        with app.state.model_locks[model.model_type]:
            if not model.loaded:
                model.load()
    except (OSError, InvalidProtobuf, BadZipFile, NoSuchFile):
        log.warn(
            (
                f"Failed to load {model.model_type.replace('_', ' ')} model '{model.model_name}'."
                "Clearing cache and retrying."
            )
        )
        model.clear_cache()
        model.load()
    return model


def _predict(model: InferenceModel, inputs: Any, **options: Any) -> np.ndarray[int, np.dtype[np.float32]]:
    if not model.loaded:
        _load(model)
    model.configure(**options)
    return model.predict(inputs)


def _create(
    embedding_ids: list[str],
    embeddings: np.ndarray[int, np.dtype[np.float32]],
) -> VectorStore:
    index = VectorStore(embeddings.shape[1])
    _add(index, embedding_ids, embeddings)
    return index


def _add(
    index: VectorStore,
    embedding_ids: list[str],
    embeddings: np.ndarray[int, np.dtype[np.float32]],
) -> None:
    with app.state.index_lock:
        index.add_with_ids(embeddings, embedding_ids)  # type: ignore
