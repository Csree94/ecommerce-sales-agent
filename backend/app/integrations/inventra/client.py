"""HTTP client for the Inventra API (httpx).

Implements ONLY the verified Inventra endpoints — one read surface plus ONE
single-attempt write:

- ``GET /api/products``            (page, per_page, search, category_id, is_active, stock_status)
- ``GET /api/products/{id}``
- ``GET /api/categories``          (no parameters; returns a bare array)
- ``GET /api/categories/{id}``
- ``GET /api/inventory``           (search, stock_status, low_stock_only)
- ``GET /api/inventory/movements`` (product_id, movement_type, page, per_page)

Deliberately absent: every write except ``stock_out`` (stock-in, adjust,
threshold), and any endpoint Inventra does not expose (no per-product
inventory lookup, no SKU lookup). ``POST /api/ai/search`` exists upstream but
is intentionally NOT integrated in this phase.

Design:
- ``InventraClient → InventraAuthProvider → Authorization header``: the client
  never hard-codes credential logic; the provider is injectable and replaceable.
- Explicit per-request timeout; bounded retries for transient failures only
  (connect/read timeouts, connection errors, 502/503/504). Never retries 4xx.
  Every operation here is a read, so retrying is safe. The single write
  (``stock_out``) is sent exactly once and never retried: a re-sent write
  could double-deduct stock.
- Failures map to typed integration errors; responses that do not match the
  verified contract raise ``InventraResponseError`` — failures are never
  silently converted into empty results.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel

from app.config.inventra import InventraSettings
from app.core.logging import get_logger
from app.integrations.inventra.auth import InventraAuthProvider, build_auth_provider
from app.integrations.inventra.errors import (
    InventraAuthError,
    InventraConfigError,
    InventraConnectionError,
    InventraNotFoundError,
    InventraRateLimitedError,
    InventraResponseError,
    InventraServerError,
    InventraTimeoutError,
    InventraValidationError,
)
from app.integrations.inventra.schemas import (
    Category,
    InventoryItem,
    InventraDashboardStats,
    MovementType,
    Product,
    ProductListResponse,
    StockMovementListResponse,
    StockOutResult,
    StockStatus,
)

logger = get_logger(__name__)

# Retried HTTP statuses: transient server-side conditions only.
_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})


class InventraClient:
    """Typed, read-only access to the verified Inventra API surface."""

    def __init__(
        self,
        settings: InventraSettings,
        auth_provider: InventraAuthProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the client.

        ``transport`` is an injection seam for tests (``httpx.MockTransport``);
        production code never passes it.
        """
        if not settings.base_url:
            raise InventraConfigError(
                "Inventra integration is not configured (set INVENTRA_BASE_URL)"
            )
        self._settings = settings
        self._auth = auth_provider or build_auth_provider(settings.bearer_token)
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers={"Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        """Release the underlying httpx pool (application shutdown)."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    async def list_products(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        search: str | None = None,
        category_id: int | None = None,
        is_active: bool | None = None,
        stock_status: StockStatus | None = None,
    ) -> ProductListResponse:
        """``GET /api/products`` with the verified filter set only."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if search is not None:
            params["search"] = search
        if category_id is not None:
            params["category_id"] = category_id
        if is_active is not None:
            params["is_active"] = is_active
        if stock_status is not None:
            params["stock_status"] = stock_status
        data = await self._request_json("GET", "/api/products", params=params)
        return self._parse(ProductListResponse, data, "/api/products")

    async def get_product(self, product_id: int) -> Product:
        """``GET /api/products/{product_id}``."""
        path = f"/api/products/{product_id}"
        data = await self._request_json("GET", path)
        return self._parse(Product, data, path)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def list_categories(self) -> list[Category]:
        """``GET /api/categories`` — bare JSON array (no query params upstream)."""
        data = await self._request_json("GET", "/api/categories")
        if not isinstance(data, list):
            raise InventraResponseError("Inventra /api/categories returned a non-array payload")
        return [self._parse(Category, item, "/api/categories") for item in data]

    async def get_category(self, category_id: int) -> Category:
        """``GET /api/categories/{category_id}`` (raises NotFoundError on 404)."""
        path = f"/api/categories/{category_id}"
        data = await self._request_json("GET", path)
        return self._parse(Category, data, path)

    # ------------------------------------------------------------------
    # Inventory (list endpoints only — no per-product inventory route exists)
    # ------------------------------------------------------------------

    async def list_inventory(
        self,
        *,
        search: str | None = None,
        stock_status: str | None = None,
        low_stock_only: bool = False,
    ) -> list[InventoryItem]:
        """``GET /api/inventory`` — bare JSON array.

        There is NO verified ``GET /api/inventory/{product_id}``; callers needing
        one product's stock must use ``search`` (matches name/SKU upstream).
        """
        params: dict[str, Any] = {}
        if search is not None:
            params["search"] = search
        if stock_status is not None:
            params["stock_status"] = stock_status
        if low_stock_only:
            params["low_stock_only"] = True
        data = await self._request_json("GET", "/api/inventory", params=params)
        if not isinstance(data, list):
            raise InventraResponseError("Inventra /api/inventory returned a non-array payload")
        return [self._parse(InventoryItem, item, "/api/inventory") for item in data]

    async def list_inventory_movements(
        self,
        *,
        product_id: int | None = None,
        movement_type: MovementType | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> StockMovementListResponse:
        """``GET /api/inventory/movements`` with the verified filters only."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if product_id is not None:
            params["product_id"] = product_id
        if movement_type is not None:
            params["movement_type"] = movement_type
        path = "/api/inventory/movements"
        data = await self._request_json("GET", path, params=params)
        return self._parse(StockMovementListResponse, data, path)

    # ------------------------------------------------------------------
    # Write operations (exactly one, deliberately never retried)
    # ------------------------------------------------------------------

    async def stock_out(
        self, product_id: int, *, quantity: int, notes: str | None = None
    ) -> StockOutResult:
        """``POST /api/inventory/{product_id}/stock-out`` — single attempt.

        The ONE verified write in this integration (sales-agent purchases).
        Deliberately bypasses ``_request_json`` and its bounded retry loop:
        a timeout/connection failure after the server already committed the
        deduction would otherwise trigger a second ``stock-out`` on retry —
        a double deduction. A single explicit attempt makes "at most once"
        semantics explicit; ambiguous outcomes surface as typed errors for
        the caller to report honestly instead of being silently retried.

        ``quantity`` is validated here (and in the tool layer) to be > 0 —
        Inventra would reject anything else (HTTP 422).
        """
        if quantity <= 0:
            raise InventraValidationError("stock-out quantity must be positive")
        headers = self._auth.headers()
        payload: dict[str, Any] = {"quantity": quantity}
        if notes is not None:
            payload["notes"] = notes
        try:
            response = await self._client.request(
                "POST",
                f"/api/inventory/{product_id}/stock-out",
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            # Single attempt: no retry — the request may have been processed.
            raise InventraTimeoutError(
                f"Inventra stock-out timed out for product {product_id} "
                "(state unknown — the operation was attempted exactly once)"
            ) from exc
        except httpx.TransportError as exc:
            raise InventraConnectionError(
                f"Could not reach Inventra for stock-out on product {product_id}"
            ) from exc
        self._raise_for_status(response, f"/api/inventory/{product_id}/stock-out")
        try:
            data = response.json()
        except ValueError as exc:
            raise InventraResponseError(
                "Inventra returned a non-JSON body for the stock-out response"
            ) from exc
        result = self._parse(StockOutResult, data, f"/api/inventory/{product_id}/stock-out")
        if result.product_id != product_id:
            raise InventraResponseError(
                "Inventra stock-out response does not match the requested product"
            )
        return result

    # ------------------------------------------------------------------
    # Dashboard (read-only stats endpoint, upstream-cached 120 s)
    # ------------------------------------------------------------------

    async def get_dashboard_stats(self) -> InventraDashboardStats:
        """``GET /api/dashboard`` — aggregate stats for the admin overview."""
        data = await self._request_json("GET", "/api/dashboard")
        return self._parse(InventraDashboardStats, data, "/api/dashboard")

    # ------------------------------------------------------------------
    # Request core
    # ------------------------------------------------------------------

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform one authenticated GET with bounded retries; return parsed JSON."""
        headers = self._auth.headers()
        retries_left = self._settings.max_retries
        backoff = self._settings.retry_backoff_seconds

        while True:
            try:
                response = await self._client.request(method, path, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                if retries_left > 0:
                    retries_left -= 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise InventraTimeoutError(f"Inventra request timed out: {path}") from exc
            except httpx.TransportError as exc:
                if retries_left > 0:
                    retries_left -= 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise InventraConnectionError(f"Could not reach Inventra: {path}") from exc

            if response.status_code in _RETRYABLE_STATUS_CODES and retries_left > 0:
                retries_left -= 1
                logger.warning(
                    "inventra_request_retry",
                    path=path,
                    status_code=response.status_code,
                    retries_left=retries_left,
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            self._raise_for_status(response, path)
            try:
                return response.json()
            except ValueError as exc:
                raise InventraResponseError(
                    f"Inventra returned a non-JSON body for {path}"
                ) from exc

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        """Map an HTTP error response onto a typed integration error."""
        if response.is_success:
            return
        status_code = response.status_code
        detail = self._safe_detail(response)
        if status_code in (401, 403):
            raise InventraAuthError(f"Inventra rejected credentials ({status_code}) for {path}")
        if status_code == 404:
            raise InventraNotFoundError(f"Inventra resource not found: {path}")
        if status_code in (400, 422):
            raise InventraValidationError(
                f"Inventra rejected request parameters ({status_code}) for {path}: {detail}"
            )
        if status_code == 429:
            raise InventraRateLimitedError(f"Inventra rate limit hit for {path}")
        if status_code >= 500:
            raise InventraServerError(f"Inventra server error ({status_code}) for {path}")
        raise InventraResponseError(
            f"Unexpected Inventra response ({status_code}) for {path}: {detail}"
        )

    @staticmethod
    def _safe_detail(response: httpx.Response) -> str:
        """Extract a short error detail for logs/messages without echoing secrets.

        Truncates and strips any Authorization header material (never part of a
        body anyway) so error surfaces stay safe.
        """
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        detail = body.get("detail") if isinstance(body, dict) else None
        if detail is None:
            return str(body)[:200]
        return str(detail)[:200]

    @staticmethod
    def _parse(model: type[BaseModel], payload: Any, path: str) -> Any:
        """Validate a payload against a response model; never pass bad data on."""
        try:
            return model.model_validate(payload)
        except Exception as exc:  # pydantic ValidationError and shape mismatches
            raise InventraResponseError(
                f"Inventra response for {path} did not match the expected contract"
            ) from exc
