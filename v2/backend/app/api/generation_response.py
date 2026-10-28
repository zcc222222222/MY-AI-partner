"""Own the synchronous generator lifetime across real HTTP disconnects."""
import anyio
from fastapi.responses import StreamingResponse

from ..services import usage_service as usage


class GenerationResponse(StreamingResponse):
    def __init__(self, source, scope, **kwargs):
        self.source = source
        self.generation_scope = scope
        super().__init__(source, **kwargs)

    async def __call__(self, scope, receive, send):
        # Watch receive on every ASGI version, including while no token is sent.
        try:
            async with anyio.create_task_group() as group:
                async def disconnected():
                    await self.listen_for_disconnect(receive)
                    await anyio.to_thread.run_sync(self.generation_scope.cancel)
                    group.cancel_scope.cancel()

                async def stream():
                    try:
                        await self.stream_response(send)
                    except OSError:
                        await anyio.to_thread.run_sync(self.generation_scope.cancel)
                    finally:
                        # Also close an active upstream read on server/task
                        # cancellation before the task group waits for next().
                        with anyio.CancelScope(shield=True):
                            await anyio.to_thread.run_sync(self.generation_scope.cancel)
                        group.cancel_scope.cancel()

                group.start_soon(disconnected)
                group.start_soon(stream)
        finally:
            # next() runs in Starlette's shielded worker. Wait for it to leave
            # before close(); a generator cannot be closed while executing.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.generation_scope.cancel)
                await anyio.to_thread.run_sync(self._close)

    def _close(self):
        try:
            with usage.bound(self.generation_scope):
                self.source.close()
        finally:
            # Covers disconnect before the generator was entered at all.
            # finish is idempotent; its normal finalizer retains completed rows.
            usage.finish(self.generation_scope, 'cancelled')
