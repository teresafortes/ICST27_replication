"""HTTPS egress proxy for the ag_main_cc sandboxes. Accepts only CONNECT to the hosts in
$ALLOW (default api.anthropic.com:443); everything else gets 403. Every request is logged
(allowed or denied) to stdout, so attempts to reach the web are on record.
Runs in its own container attached to the internal network and the default bridge."""
import asyncio, os, sys, time

ALLOW = set(filter(None, os.environ.get("ALLOW", "api.anthropic.com:443").split(",")))
PORT = int(os.environ.get("PORT", "3128"))


def log(peer, decision, target):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {peer} {decision} {target}", flush=True)


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        w.close()


async def handle(cr, cw):
    peer = cw.get_extra_info("peername", ("?",))[0]
    try:
        request = (await cr.readline()).decode("latin-1").strip()
        while (await cr.readline()) not in (b"\r\n", b"\n", b""):
            pass
        method, target = (request.split() + ["", ""])[:2]
        if method != "CONNECT" or target not in ALLOW:
            log(peer, "DENY", f"{method} {target}")
            cw.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        host, port = target.rsplit(":", 1)
        ur, uw = await asyncio.open_connection(host, int(port))
        log(peer, "ALLOW", target)
        cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cw.drain()
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    except Exception as e:
        log(peer, "ERROR", repr(e))
        cw.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print(f"egress proxy on :{PORT}, allowed: {sorted(ALLOW)}", flush=True)
    async with server:
        await server.serve_forever()

asyncio.run(main())
