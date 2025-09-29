# Servidor P2P (Python) - Gossip + CRDT LWW-Map - Logs detalhados de convergência
import os
import asyncio
import random
import time
import logging
from typing import Dict, Tuple, List, Iterable, DefaultDict, Union
from collections import defaultdict

import aiohttp
from aiohttp import web

# ------------------ CRDT: LWW-Map ------------------
class LWWMap:
    def __init__(self):
        self._state: Dict[str, Tuple[int, int, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _maior(a, b) -> bool:
        (tsa, na), (tsb, nb) = a, b
        return (tsa > tsb) or (tsa == tsb and na > nb)

    async def put(self, key: str, ts: int, node_id: int, value: float):
        async with self._lock:
            cur = self._state.get(key)
            if not cur or self._maior((ts, node_id), (cur[0], cur[1])):
                self._state[key] = (ts, node_id, value)

    async def merge_many(self, items: Iterable[Tuple[str, int, int, float]]):
        async with self._lock:
            for k, ts, nid, val in items:
                cur = self._state.get(k)
                if not cur or (ts > cur[0] or (ts == cur[0] and nid > cur[1])):
                    self._state[k] = (ts, nid, float(val))

    async def snapshot(self) -> Dict[str, Tuple[int, int, float]]:
        async with self._lock:
            return dict(self._state)

# ------------------ Logging helpers ------------------
def configurar_logger(porta: int) -> logging.Logger:
    os.makedirs("/logs", exist_ok=True)
    logger = logging.getLogger(f"servidor_{porta}")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(f"/logs/servidor_{porta}.log")
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S,%f")
    logger.handlers.clear()
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

def _key_disp_ordem(nome: str):
    if nome.startswith("disp"):
        suf = nome[4:]
        if suf.isdigit(): return (0, int(suf))
    return (1, nome)

def _group_by_device(state: Dict[str, Tuple[int, int, float]]) -> DefaultDict[str, Dict[str, Tuple[float, int, int]]]:
    grouped: DefaultDict[str, Dict[str, Tuple[float, int, int]]] = defaultdict(dict)
    for k, (ts, nid, val) in state.items():
        if ":" in k: disp, met = k.split(":", 1)
        else: disp, met = k, "valor"
        grouped[disp][met] = (float(val), ts, nid)
    return grouped

def log_estado(logger: logging.Logger, state: Dict[str, Tuple[int, int, float]], titulo: str):
    grouped = _group_by_device(state)
    logger.info(f"[ESTADO] {titulo} — {len(grouped)} dispositivos")
    for disp in sorted(grouped.keys(), key=_key_disp_ordem):
        parts = []
        for met in sorted(grouped[disp].keys()):
            val, ts, nid = grouped[disp][met]
            parts.append(f"{met}={val:.2f}@{ts} nid={nid}")
        logger.info(f"[ESTADO] {disp}: " + ", ".join(parts))

# ------------------ Tarefas ------------------
async def gerar_metricas(app: web.Application):
    logger: logging.Logger = app["logger"]
    crdt: LWWMap = app["crdt"]
    node_id: int = app["porta"]
    try:
        while True:
            for disp in range(10):
                for met in ("temperatura", "vibracao"):
                    key = f"disp{disp}:{met}"
                    ts = int(time.time() * 1000)
                    val = float(random.randint(0, 100))
                    await crdt.put(key, ts, node_id, val)
                    logger.info(f"[LOCAL] {key} = {val:.2f} @ts={ts} nid={node_id}")
            estado = await crdt.snapshot()
            log_estado(logger, estado, "Após geração local")
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        logger.info("[STOP] gerar_metricas cancelada")
        raise

async def disseminar(app: web.Application):
    logger: logging.Logger = app["logger"]
    crdt: LWWMap = app["crdt"]
    companheiros: List[str] = app["companheiros"]
    session: aiohttp.ClientSession = app["session"]
    try:
        while True:
            estado = await crdt.snapshot()
            items = [(k, ts, nid, val) for k, (ts, nid, val) in estado.items()]
            payload = {"lww": items}
            for c in companheiros:
                url = f"http://{c}/gossip"
                try:
                    await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=3))
                except Exception as e:
                    logger.info(f"[ERRO] Envio gossip para {c}: {e}")
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        logger.info("[STOP] disseminar cancelada")
        raise

# ------------------ HTTP ------------------
def _coerce_items(items_raw: List[Union[list, tuple, dict]]):
    coerced = []
    for x in items_raw:
        if isinstance(x, dict):
            coerced.append((x["key"], int(x["ts"]), int(x["node_id"]), float(x["value"])))
        else:
            k, ts, nid, val = x
            coerced.append((str(k), int(ts), int(nid), float(val)))
    return coerced

async def handle_gossip(request: web.Request):
    data = await request.json()
    items = _coerce_items(data.get("lww", []))
    await request.app["crdt"].merge_many(items)
    request.app["logger"].info(f"[REMOTO] Recebidas {len(items)} entradas de {request.remote}")
    estado = await request.app["crdt"].snapshot()
    log_estado(request.app["logger"], estado, "Após merge remoto")
    return web.json_response({"ok": True})

async def handle_health(_request: web.Request):
    return web.Response(text="ok")

async def on_startup(app: web.Application):
    app["session"] = aiohttp.ClientSession()
    app["task_local"] = asyncio.create_task(gerar_metricas(app))
    app["task_gossip"] = asyncio.create_task(disseminar(app))
    app["logger"].info(f"[START] Python na porta {app['porta']}")

async def on_cleanup(app: web.Application):
    for key in ("task_local", "task_gossip"):
        t = app.get(key)
        if t:
            t.cancel()
            try: await t
            except asyncio.CancelledError: pass
    if app.get("session"): await app["session"].close()

if __name__ == "__main__":
    porta = int(os.getenv("PORTA", "5000"))
    companheiros = [p.strip() for p in os.getenv("COMPANHEIROS", "").split(",") if p.strip()]
    logger = configurar_logger(porta)

    app = web.Application()
    app["porta"] = porta
    app["companheiros"] = companheiros
    app["logger"] = logger
    app["crdt"] = LWWMap()

    app.router.add_post("/gossip", handle_gossip)
    app.router.add_get("/healthz", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Bind EXPLÍCITO: 0.0.0.0
    web.run_app(app, host="0.0.0.0", port=porta)
