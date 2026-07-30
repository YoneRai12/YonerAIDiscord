"""登録可能な完全local Media Pipeline primitive。"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime

import qrcode
from PIL import Image, ImageOps
from qrcode.exceptions import DataOverflowError

from .artifacts import CommitCheck, MediaArtifactStore
from .domain import (
    ArtifactKind,
    ArtifactScope,
    COMPOSE_GRID_REVISION,
    ComposeGridRequest,
    MediaValidationError,
    PLACE_ON_CANVAS_REVISION,
    PrimitiveId,
    PrimitiveResult,
    PlaceOnCanvasRequest,
    QR_ENCODE_REVISION,
    QrEncodeRequest,
    validate_image_dimensions,
)
from .quote import (
    QUOTE_CARD_REVISION,
    QuoteCardArtifactResult,
    QuoteCardRenderer,
    QuoteCardRequest,
)


class MediaPipelineService:
    """network・provider・Discordを持たない同期pure service。"""

    def __init__(
        self,
        store: MediaArtifactStore,
        *,
        quote_renderer: QuoteCardRenderer | None = None,
    ) -> None:
        if not isinstance(store, MediaArtifactStore):
            raise TypeError("store must be a MediaArtifactStore")
        if quote_renderer is not None and not isinstance(quote_renderer, QuoteCardRenderer):
            raise TypeError("quote_renderer must be a QuoteCardRenderer or None")
        self._store = store
        self._quote_renderer = quote_renderer

    @property
    def artifact_store(self) -> MediaArtifactStore:
        """実行中のservice/store identity確認に使う読み取り専用参照。"""

        return self._store

    @property
    def quote_renderer(self) -> QuoteCardRenderer | None:
        """引用カードのoptional runtime identity。"""

        return self._quote_renderer

    def quote_card(
        self,
        *,
        scope: ArtifactScope,
        display_name: str,
        body: str,
        timestamp: datetime,
        commit_check: CommitCheck,
    ) -> QuoteCardArtifactResult:
        renderer = self._quote_renderer
        if renderer is None:
            raise MediaValidationError("quote card renderer is unavailable")
        request = QuoteCardRequest(
            display_name=display_name,
            body=body,
            timestamp=timestamp,
            avatar_png=None,
        )
        rendered = renderer.render(request)
        recipe_digest = _recipe_digest(
            "quote.card",
            QUOTE_CARD_REVISION,
            {
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "display_name_sha256": hashlib.sha256(display_name.encode("utf-8")).hexdigest(),
                "timestamp": timestamp.isoformat(),
            },
        )
        with Image.open(io.BytesIO(rendered.png)) as source:
            source.load()
            image = source.convert("RGB")
        try:
            artifact = self._store.commit_image(
                image,
                scope=scope,
                recipe_digest=recipe_digest,
                kind=ArtifactKind.IMAGE,
                commit_check=commit_check,
            )
        finally:
            image.close()
        return QuoteCardArtifactResult(artifact)

    def qr_encode(self, request: QrEncodeRequest, *, commit_check: CommitCheck) -> PrimitiveResult:
        if not isinstance(request, QrEncodeRequest):
            raise TypeError("request must be a QrEncodeRequest")
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=request.border,
        )
        qr.add_data(request.payload, optimize=0)
        try:
            qr.make(fit=True)
        except DataOverflowError as exc:
            raise MediaValidationError("QR payload does not fit the bounded QR contract") from exc
        base = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        image = base
        try:
            if request.scale > 1:
                target_size = (base.width * request.scale, base.height * request.scale)
                validate_image_dimensions(*target_size)
                image = base.resize(target_size, Image.Resampling.NEAREST)
            else:
                validate_image_dimensions(*base.size)
            recipe_digest = _recipe_digest(
                PrimitiveId.QR_ENCODE,
                QR_ENCODE_REVISION,
                {
                    "border": request.border,
                    "payload_sha256": hashlib.sha256(request.payload.encode("utf-8")).hexdigest(),
                    "scale": request.scale,
                },
            )
            artifact = self._store.commit_image(
                image,
                scope=request.scope,
                recipe_digest=recipe_digest,
                kind=ArtifactKind.QR_CODE,
                commit_check=commit_check,
            )
        finally:
            if image is not base:
                image.close()
            base.close()
        return PrimitiveResult(
            primitive_id=PrimitiveId.QR_ENCODE,
            operation_revision=QR_ENCODE_REVISION,
            artifact=artifact,
        )

    def image_place_on_canvas(
        self,
        request: PlaceOnCanvasRequest,
        *,
        commit_check: CommitCheck,
    ) -> PrimitiveResult:
        if not isinstance(request, PlaceOnCanvasRequest):
            raise TypeError("request must be a PlaceOnCanvasRequest")
        source = self._load_owned_image(request.source, request.scope)
        canvas = Image.new("RGB", (request.canvas_width, request.canvas_height), request.background.tuple)
        try:
            x = (request.canvas_width - source.width) // 2 if request.x is None else request.x
            y = (request.canvas_height - source.height) // 2 if request.y is None else request.y
            canvas.paste(source, (x, y))
            recipe_digest = _recipe_digest(
                PrimitiveId.IMAGE_PLACE_ON_CANVAS,
                PLACE_ON_CANVAS_REVISION,
                {
                    "background": request.background.tuple,
                    "canvas": [request.canvas_width, request.canvas_height],
                    "source": request.source.artifact_id,
                    "x": x,
                    "y": y,
                },
            )
            artifact = self._store.commit_image(
                canvas,
                scope=request.scope,
                recipe_digest=recipe_digest,
                kind=ArtifactKind.IMAGE,
                commit_check=commit_check,
            )
        finally:
            source.close()
            canvas.close()
        return PrimitiveResult(
            primitive_id=PrimitiveId.IMAGE_PLACE_ON_CANVAS,
            operation_revision=PLACE_ON_CANVAS_REVISION,
            artifact=artifact,
            inputs=(request.source,),
        )

    def image_compose_grid(
        self,
        request: ComposeGridRequest,
        *,
        commit_check: CommitCheck,
    ) -> PrimitiveResult:
        if not isinstance(request, ComposeGridRequest):
            raise TypeError("request must be a ComposeGridRequest")
        rows = (len(request.sources) + request.columns - 1) // request.columns
        inner_width = request.canvas_width - (2 * request.padding) - (request.gap * (request.columns - 1))
        inner_height = request.canvas_height - (2 * request.padding) - (request.gap * (rows - 1))
        cell_width = inner_width // request.columns
        cell_height = inner_height // rows
        canvas = Image.new("RGB", (request.canvas_width, request.canvas_height), request.background.tuple)
        try:
            for index, ref in enumerate(request.sources):
                source = self._load_owned_image(ref, request.scope)
                fitted = source
                try:
                    if ref.kind is ArtifactKind.QR_CODE:
                        fitted = _fit_qr(source, cell_width, cell_height)
                    else:
                        fitted = ImageOps.contain(
                            source,
                            (cell_width, cell_height),
                            method=Image.Resampling.LANCZOS,
                        )
                    row, column = divmod(index, request.columns)
                    cell_x = request.padding + column * (cell_width + request.gap)
                    cell_y = request.padding + row * (cell_height + request.gap)
                    x = cell_x + (cell_width - fitted.width) // 2
                    y = cell_y + (cell_height - fitted.height) // 2
                    canvas.paste(fitted, (x, y))
                finally:
                    if fitted is not source:
                        fitted.close()
                    source.close()
            recipe_digest = _recipe_digest(
                PrimitiveId.IMAGE_COMPOSE_GRID,
                COMPOSE_GRID_REVISION,
                {
                    "background": request.background.tuple,
                    "canvas": [request.canvas_width, request.canvas_height],
                    "columns": request.columns,
                    "gap": request.gap,
                    "inputs": [ref.artifact_id for ref in request.sources],
                    "padding": request.padding,
                },
            )
            artifact = self._store.commit_image(
                canvas,
                scope=request.scope,
                recipe_digest=recipe_digest,
                kind=ArtifactKind.IMAGE,
                commit_check=commit_check,
            )
        finally:
            canvas.close()
        return PrimitiveResult(
            primitive_id=PrimitiveId.IMAGE_COMPOSE_GRID,
            operation_revision=COMPOSE_GRID_REVISION,
            artifact=artifact,
            inputs=request.sources,
        )

    def _load_owned_image(self, ref, scope) -> Image.Image:
        data = self._store.read_png(ref, scope=scope)
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.convert("RGB")


def _fit_qr(source: Image.Image, cell_width: int, cell_height: int) -> Image.Image:
    if source.width > cell_width or source.height > cell_height:
        raise MediaValidationError("QR input does not fit the grid cell without unsafe downscaling")
    factor = max(1, min(cell_width // source.width, cell_height // source.height))
    if factor == 1:
        return source
    return source.resize((source.width * factor, source.height * factor), Image.Resampling.NEAREST)


def _recipe_digest(primitive: PrimitiveId | str, revision: str, parameters: dict[str, object]) -> str:
    primitive_value = primitive.value if isinstance(primitive, PrimitiveId) else primitive
    payload = json.dumps(
        {
            "parameters": parameters,
            "primitive": primitive_value,
            "revision": revision,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(b"yonerai.media.recipe.v1\0" + payload).hexdigest()


__all__ = ["MediaPipelineService"]
