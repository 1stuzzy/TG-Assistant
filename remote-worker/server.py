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
import socket
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

try:
    from host_metrics import collect as collect_load
except ImportError:
    collect_load = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

llm = None
loaded_name = None
device_label = "cpu"
load_error: Optional[str] = None
loading = False


def _lan_urls(port: int) -> list[str]:
    ips: list[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ips.append(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [f"http://{ip}:{port}" for ip in ips]


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


def _prepare_native_libs() -> None:
    """Windows: llama.dll не грузится, если не видит CUDA / VC++ runtime."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    dirs: list[str] = []
    cuda = os.environ.get("CUDA_PATH")
    if cuda:
        dirs += [os.path.join(cuda, "bin"), os.path.join(cuda, "bin", "x64")]
    toolkit = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
    if os.path.isdir(toolkit):
        for name in sorted(os.listdir(toolkit), reverse=True):
            dirs.append(os.path.join(toolkit, name, "bin"))
    try:
        import site
        for root in list(site.getsitepackages()) + [site.getusersitepackages()]:
            dirs.append(os.path.join(root, "llama_cpp", "lib"))
    except Exception:
        pass
    seen: set[str] = set()
    for path in dirs:
        if not path or path in seen or not os.path.isdir(path):
            continue
        seen.add(path)
        try:
            os.add_dll_directory(path)
        except OSError:
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")


def load_model(path: str, n_ctx: int, n_gpu_layers: int) -> None:
    global llm, loaded_name, device_label
    if n_gpu_layers <= 0:
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    _prepare_native_libs()
    try:
        from llama_cpp import Llama
    except Exception as exc:
        raise RuntimeError(
            "Не загрузился llama.dll. Запустите start.bat ещё раз. "
            "Если не поможет: https://aka.ms/vs/17/release/vc_redist.x64.exe "
            f"({exc})"
        ) from exc

    if not os.path.isfile(path):
        raise FileNotFoundError(f"Нет файла модели: {path}")

    n_threads = max(1, (os.cpu_count() or 4) - 1)
    kwargs = dict(
        model_path=path,
        n_ctx=n_ctx,
        n_threads=n_threads,
        n_gpu_layers=n_gpu_layers,
        n_batch=256,
        use_mmap=True,
        logits_all=False,
        embedding=False,
        chat_format="chatml" if "qwen" in path.lower() or "llama-3" not in path.lower() else "llama-3",
        verbose=False,
    )
    try:
        model = Llama(**kwargs)
    except OSError as exc:
        if getattr(exc, "winerror", None) == -1073741795 or "0xc000001d" in str(exc).lower():
            raise RuntimeError(
                "Процессор не принимает эту сборку llama.cpp (ошибка 0xc000001d). "
                "Закройте окно и снова запустите start.bat — он поставит другую CPU-версию."
            ) from exc
        raise
    llm = model
    loaded_name = os.path.basename(path)
    device_label = "gpu" if n_gpu_layers else "cpu"
    log.info("Готово: %s (%s, ctx=%s)", loaded_name, device_label, n_ctx)


def create_app(
    api_key: str = "",
    model_path: str = "",
    n_ctx: int = 1024,
    n_gpu_layers: int = 0,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        global loading, load_error
        loading = True
        load_error = None

        def _load() -> None:
            global loading, load_error
            try:
                load_model(model_path, n_ctx, n_gpu_layers)
            except Exception as exc:
                load_error = str(exc)
                log.exception("Модель не загрузилась")
            finally:
                loading = False

        task = asyncio.create_task(asyncio.to_thread(_load))
        yield
        task.cancel()

    app = FastAPI(title="TG worker", version="1.0", lifespan=lifespan)
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
        payload = {
            "ok": llm is not None and not loading,
            "loading": loading,
            "model": loaded_name,
            "device": device_label,
            "error": load_error,
        }
        if collect_load:
            try:
                payload["load"] = await asyncio.to_thread(collect_load)
            except Exception:
                pass
        return payload

    @app.get("/v1/models")
    async def models(authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        return {"data": [{"id": loaded_name or "local", "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat(payload: ChatRequest, authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        if llm is None:
            if loading:
                raise HTTPException(status_code=503, detail="Модель ещё загружается, подождите строку Готово в start.bat")
            raise HTTPException(
                status_code=503,
                detail=load_error or "Модель не загружена. Рядом со start.bat должен лежать model.gguf, окно не закрывайте.",
            )
        messages = [{"role": m.role, "content": m.content} for m in payload.messages]
        log.info("Генерация, сообщений: %s stream=%s", len(messages), payload.stream)
        for name in ("reset", "reset_chat"):
            fn = getattr(llm, name, None)
            if callable(fn):
                try:
                    fn()
                    break
                except Exception:
                    log.debug("Сброс контекста (%s) не удался", name, exc_info=True)

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
    urls = _lan_urls(args.port)
    log.info("Слушаю порт %s. В панели TG-Assistant укажите:", args.port)
    for url in urls or [f"http://<IP-этого-ПК>:{args.port}"]:
        log.info("  %s", url)
    log.info("Окно не закрывайте. Модель грузится после открытия порта.")
    uvicorn.run(
        create_app(args.api_key, args.model, args.n_ctx, layers),
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
