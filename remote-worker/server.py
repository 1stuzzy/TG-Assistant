"""
Мини-сервер модели. На удалённый ПК нужны только эта папка и файл .gguf —
весь TG-Assistant качать не обязательно.

  pip install -r requirements.txt
  python server.py --model model.gguf --host 0.0.0.0 --port 8088

С видеокартой (CUDA-сборка llama-cpp-python):
  python server.py --model model.gguf --host 0.0.0.0 --port 8088 --gpu

В панели TG-Assistant: Настройки → Удалённый ПК → http://IP:8088
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

llm = None
loaded_name = None
device_label = "cpu"


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: Optional[str] = "local"
    messages: list[ChatMessage]
    max_tokens: int = 64
    temperature: float = 0.86
    top_p: float = 0.9
    stream: bool = False


def load_model(path: str, n_ctx: int, n_gpu_layers: int) -> None:
    global llm, loaded_name, device_label
    from llama_cpp import Llama

    if not os.path.isfile(path):
        raise FileNotFoundError(f"Нет файла модели: {path}")

    n_threads = os.cpu_count() or 4
    kwargs = dict(
        model_path=path,
        n_ctx=n_ctx,
        n_threads=n_threads,
        n_gpu_layers=n_gpu_layers,
        n_batch=512,
        use_mmap=True,
        logits_all=False,
        embedding=False,
        chat_format="chatml" if "qwen" in path.lower() or "llama-3" not in path.lower() else "llama-3",
        verbose=False,
    )
    try:
        model = Llama(**kwargs, n_threads_batch=n_threads, n_ubatch=512)
    except TypeError:
        model = Llama(**kwargs)
    llm = model
    loaded_name = os.path.basename(path)
    device_label = "gpu" if n_gpu_layers else "cpu"
    log.info("Готово: %s (%s, ctx=%s)", loaded_name, device_label, n_ctx)


def create_app(api_key: str = "") -> FastAPI:
    app = FastAPI(title="TG worker", version="1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _auth(authorization: Optional[str]) -> None:
        if not api_key:
            return
        token = (authorization or "").replace("Bearer ", "", 1).strip()
        if token != api_key:
            raise HTTPException(status_code=401, detail="Неверный ключ")

    @app.get("/health")
    async def health(authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        return {"ok": True, "model": loaded_name, "device": device_label}

    @app.get("/v1/models")
    async def models(authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        return {"data": [{"id": loaded_name or "local", "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat(payload: ChatRequest, authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        if llm is None:
            raise HTTPException(status_code=503, detail="Модель не загружена")
        messages = [{"role": m.role, "content": m.content} for m in payload.messages]
        log.info("Генерация, сообщений: %s stream=%s", len(messages), payload.stream)

        def _kwargs():
            return dict(
                messages=messages,
                max_tokens=min(int(payload.max_tokens), 160),
                temperature=payload.temperature,
                top_p=payload.top_p,
                repeat_penalty=1.1,
                stop=["</s>", "<|im_end|>", "<|eot_id|>", "<|endoftext|>"],
            )

        if not payload.stream:
            def _run():
                return llm.create_chat_completion(**_kwargs())

            return await asyncio.to_thread(_run)

        async def event_stream():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def produce():
                try:
                    for chunk in llm.create_chat_completion(**_kwargs(), stream=True):
                        asyncio.run_coroutine_threadsafe(queue.put(("chunk", chunk)), loop).result()
                    asyncio.run_coroutine_threadsafe(queue.put(("done", None)), loop).result()
                except Exception as exc:
                    asyncio.run_coroutine_threadsafe(queue.put(("error", str(exc))), loop).result()

            task = loop.run_in_executor(None, produce)
            try:
                while True:
                    kind, item = await queue.get()
                    if kind == "chunk":
                        yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
                    elif kind == "done":
                        yield "data: [DONE]\n\n"
                        break
                    else:
                        yield f"data: {json.dumps({'error': item})}\n\n"
                        break
            finally:
                await task

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="API-сервер GGUF для TG-Assistant")
    parser.add_argument("--model", default="model.gguf", help="Путь к .gguf")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--n-ctx", type=int, default=1024)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--gpu-layers", type=int, default=-1)
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()
    layers = args.gpu_layers if args.gpu else 0
    load_model(args.model, args.n_ctx, layers)
    log.info("Слушаю http://%s:%s  (в панели укажите IP этого ПК)", args.host, args.port)
    uvicorn.run(create_app(args.api_key), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
