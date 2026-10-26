
# astrbot-minimax-emb: managed
# 本文件由 install_minimax_emb.py 注入管理。
import asyncio
import os

import aiohttp

from astrbot import logger

from ..entities import ProviderType
from ..provider import EmbeddingProvider
from ..register import register_provider_adapter

_DEFAULT_API_BASE = "https://api.minimax.chat/v1/embeddings"
_DEFAULT_MODEL = "embo-01"


@register_provider_adapter(
    "minimax_embedding",
    "MiniMax Embedding 提供商适配器",
    provider_type=ProviderType.EMBEDDING,
)
class MiniMaxEmbeddingProvider(EmbeddingProvider):
    """MiniMax embedding provider (embo-01) via the native protocol.

    MiniMax does not expose an OpenAI-compatible /embeddings endpoint, it uses
    its own payload shape:

    POST {api_base}?GroupId={group_id}
    {"texts": ["..."], "model": "embo-01", "type": "db"}

    and answers with a top-level ``vectors`` array plus a ``base_resp`` block
    that carries the real business status code. Note that failures (e.g. the
    ~135 req/min rate limit) are reported with HTTP 200 and
    ``base_resp.status_code == 1002``, so the HTTP status alone is not enough.
    """

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.provider_config = provider_config

        self.api_key = provider_config.get("embedding_api_key") or os.getenv(
            "MINIMAX_API_KEY", ""
        )
        if not self.api_key:
            raise ValueError("MiniMax Embedding API Key 不能为空。")

        self.base_url = (
            provider_config.get("embedding_api_base", _DEFAULT_API_BASE)
            .rstrip("/")
            .removesuffix("/embeddings")
        )
        self.model = provider_config.get("embedding_model", _DEFAULT_MODEL)
        # "db" for indexing documents, "query" for retrieval queries.
        self.embed_type = provider_config.get("embedding_type", "db")
        self.group_id = provider_config.get("minimax-group-id", "")
        self.timeout = int(provider_config.get("timeout", 60))

        self.proxy = provider_config.get("proxy", "")
        if self.proxy:
            logger.info(f"[MiniMax Embedding] Using proxy: {self.proxy}")

        self.client = None
        self._client_loop = None
        self.set_model(self.model)
        logger.info(
            f"[MiniMax Embedding] {provider_config.get('id', 'unknown_id')} "
            f"Initialized, base_url={self.base_url}, model={self.model}"
        )

    async def _get_client(self):
        # aiohttp.ClientSession 绑定创建它的事件循环。若上层换了事件循环
        # 再复用同一个 session，会抛 "Event loop is closed"。
        # 这里记录创建时的循环，循环变了就重建，避免隐性地把嵌入链路搞断。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        stale = (
            self.client is not None
            and self._client_loop is not None
            and loop is not None
            and self._client_loop is not loop
        )
        if stale:
            logger.warning("[MiniMax Embedding] 事件循环已变更，重建 HTTP 会话")
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None

        if self.client is None or self.client.closed:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.client = aiohttp.ClientSession(
                headers={**self.request_headers, **headers},
                timeout=timeout,
            )
            self._client_loop = loop
        return self.client

    def _build_url(self) -> str:
        url = f"{self.base_url}/embeddings"
        if self.group_id:
            url = f"{url}?GroupId={self.group_id}"
        return url

    def _parse_response(self, response_data: dict) -> list[list[float]]:
        base_resp = response_data.get("base_resp") or {}
        status_code = base_resp.get("status_code", 0)
        status_msg = base_resp.get("status_msg", "")
        if status_code != 0:
            raise Exception(
                f"MiniMax Embedding API request failed: base_resp.status_code="
                f"{status_code}, status_msg={status_msg or '(no message)'}"
            )

        vectors = response_data.get("vectors", [])
        if not vectors:
            raise Exception(f"[MiniMax Embedding] No vectors returned: {response_data}")

        total_tokens = response_data.get("total_tokens", 0)
        if total_tokens:
            logger.debug(f"[MiniMax Embedding] total_tokens: {total_tokens}")

        return [list(vector) for vector in vectors]

    async def get_embedding(self, text: str) -> list[float]:
        embeddings = await self.get_embeddings([text])
        return embeddings[0] if embeddings else []

    async def get_embeddings(self, text: list[str]) -> list[list[float]]:
        if not text:
            return []

        client = await self._get_client()
        if not client or client.closed:
            raise Exception("[MiniMax Embedding] Client session not initialized")

        payload = {
            "texts": text,
            "model": self.model,
            "type": self.embed_type,
        }
        request_url = self._build_url()

        try:
            async with client.post(
                request_url, json=payload, proxy=self.proxy or None
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"[MiniMax Embedding] API Error: {response.status} - {error_text}"
                    )
                    raise Exception(
                        f"MiniMax Embedding API request failed: HTTP "
                        f"{response.status} - {error_text}"
                    )

                response_data = await response.json()
                return self._parse_response(response_data)

        except aiohttp.ClientError as e:
            logger.error(f"[MiniMax Embedding] Network error: {e}")
            raise
        except Exception as e:
            logger.error(f"[MiniMax Embedding] Error: {e}", exc_info=True)
            raise

    def get_dim(self) -> int:
        if "embedding_dimensions" in self.provider_config:
            try:
                return int(self.provider_config["embedding_dimensions"])
            except (ValueError, TypeError):
                logger.warning(
                    f"embedding_dimensions in embedding configs is not a valid integer: "
                    f"'{self.provider_config['embedding_dimensions']}', ignored."
                )
        return 0

    async def terminate(self):
        if self.client and not self.client.closed:
            await self.client.close()
        self.client = None
        self._client_loop = None
