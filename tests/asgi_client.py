"""Socket-free HTTP test adapter.

A timer keeps asyncio responsive in sandboxes that suppress cross-thread
socketpair wakeups. Requests still traverse the real ASGI app and middleware.
No production networking code is replaced by this adapter.
"""
import asyncio
import httpx


class ASGIClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, url, **kwargs):
        async def run():
            async def heartbeat():
                while True:
                    await asyncio.sleep(0.005)
            timer = asyncio.create_task(heartbeat())
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver', follow_redirects=True) as client:
                    return await client.request(method, url, **kwargs)
            finally:
                timer.cancel()
        return asyncio.run(run())

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)
