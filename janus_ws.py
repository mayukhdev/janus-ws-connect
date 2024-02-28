import argparse
import asyncio
import json
import logging
import random
import string
import aiohttp
import ssl
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRecorder

pcs = set()


ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# docker exec -it  janus_python_container /bin/bash
# python janus_ws.py --play-from video.mp4   ws://localhost:8187/

def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))

class JanusPlugin:
    def __init__(self, session, plugin_id):
        self._queue = asyncio.Queue()
        self._session = session
        self._plugin_id = plugin_id

    async def send(self, payload):
        message = {"janus": "message", "transaction": transaction_id(), "handle_id": self._plugin_id, "session_id": self._session.session_id}
        message.update(payload)
        print("send: {}".format(message))
        await self._session._websocket.send_json(message)

        response = await self._read_message()
        assert response["transaction"] == message["transaction"]
        return response
    
    async def _read_message(self):
        return await self._queue.get()

class JanusSession:
    def __init__(self, url):
        self._websocket = None
        self._plugins = {}
        self._url = url
        self._queue = asyncio.Queue()
        self.session_id = None

    async def connect(self):
        self._websocket = await aiohttp.ClientSession().ws_connect(self._url, protocols=['janus-protocol'], ssl=ssl_context)
        asyncio.ensure_future(self._receive_messages())

    async def create(self):
        # self._http = aiohttp.ClientSession()
        message = {"janus": "create", "transaction": transaction_id()}
        # await self._websocket.send_json(message)
        response = await self._send(message)
        # print(response)
        self.session_id = response["data"]["id"]

    async def attach(self, plugin_name: str) -> JanusPlugin:
        message = {"janus": "attach", "plugin": plugin_name, "transaction": transaction_id(), "session_id": self.session_id}
        # await self._websocket.send_json(message)

        response = await self._send(message)
        # print(response)
        plugin_id = response["data"]["id"]
        plugin = JanusPlugin(self, plugin_id)
        self._plugins[plugin_id] = plugin
        return plugin
    
    async def _read_message(self):
        return await self._queue.get()
        
    async def _receive_messages(self, session=None):
        while True:
            if self._websocket and not self._websocket.closed:
                msg = await self._websocket.receive()
                # print(msg)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    response = json.loads(msg.data)
                    print("_receive_messages {}".format(response))
                    if response["janus"] == "event":
                        plugin_id = response.get("sender")
                        if plugin_id and plugin_id in self._plugins:
                            await self._plugins[plugin_id]._queue.put(response)
                    if response["janus"] == "success":
                        await self._queue.put(response)
                    if response["janus"] == "timeout":
                        await self.timeout()
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise Exception("WebSocket error")
            else:
                break
        return
    
    async def timeout(self):
        message = {"janus": "destroy", "transaction": transaction_id()}
        response = await self._send(message)
        await self.destroy()

    async def destroy(self):
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

    async def _send(self, payload):
        print("_send: {}".format(payload))
        await self._websocket.send_json(payload)
        response = await self._read_message()
        assert response["transaction"] == payload["transaction"]
        return response
    
    async def _read_message(self):
        return await self._queue.get()

async def publish(plugin, player):
    pc = RTCPeerConnection()
    pcs.add(pc)

    # configure media
    media = {"audio": False, "video": True}
    if player and player.audio:
        pc.addTrack(player.audio)
        media["audio"] = True

    if player and player.video:
        pc.addTrack(player.video)
    else:
        pc.addTrack(VideoStreamTrack())

    # send offer
    await pc.setLocalDescription(await pc.createOffer())
    request = {"request": "configure"}
    request.update(media)
    response = await plugin.send(
        {
            "body": request,
            "jsep": {
                "sdp": pc.localDescription.sdp,
                "trickle": False,
                "type": pc.localDescription.type,
            },
        }
    )

    # apply answer
    await pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=response["jsep"]["sdp"], type=response["jsep"]["type"]
        )
    )

async def subscribe(session, room, feed, recorder):
    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("track")
    async def on_track(track):
        print("Track %s received" % track.kind)
        if track.kind == "video":
            recorder.addTrack(track)
        if track.kind == "audio":
            recorder.addTrack(track)

    # subscribe
    plugin = await session.attach("janus.plugin.videoroom")
    response = await plugin.send(
        {"body": {"request": "join", "ptype": "subscriber", "room": room, "feed": feed}}
    )

    # apply offer
    await pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=response["jsep"]["sdp"], type=response["jsep"]["type"]
        )
    )

    # send answer
    await pc.setLocalDescription(await pc.createAnswer())
    response = await plugin.send(
        {
            "body": {"request": "start"},
            "jsep": {
                "sdp": pc.localDescription.sdp,
                "trickle": False,
                "type": pc.localDescription.type,
            },
        }
    )
    await recorder.start()

async def run(player, recorder, room, session):
    await session.connect()
    await session.create()

    # join video room
    plugin = await session.attach("janus.plugin.videoroom")
    response = await plugin.send(
        {
            "body": {
                "display": "aiortc",
                "ptype": "publisher",
                "request": "join",
                "room": room,
            }
        }
    )
    publishers = response["plugindata"]["data"]["publishers"]
    for publisher in publishers:
        print("id: %(id)s, display: %(display)s" % publisher)

    # send video
    await publish(plugin=plugin, player=player)

    # receive video
    if recorder is not None and publishers:
        await subscribe(
            session=session, room=room, feed=publishers[0]["id"], recorder=recorder
        )

    # exchange media for 10 minutes
    print("Exchanging media")
    await asyncio.sleep(600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Janus")
    parser.add_argument("url", help="Janus root URL, e.g. http://localhost:8088/janus")
    parser.add_argument(
        "--room",
        type=int,
        default=1234,
        help="The video room ID to join (default: 1234).",
    )
    parser.add_argument("--play-from", help="Read the media from a file and sent it.")
    parser.add_argument("--record-to", help="Write received media to a file.")
    parser.add_argument(
        "--play-without-decoding",
        help=(
            "Read the media without decoding it (experimental). "
            "For now it only works with an MPEGTS container with only H.264 video."
        ),
        action="store_true",
    )
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # create signaling and peer connection
    session = JanusSession(args.url)

    # create media source
    if args.play_from:
        player = MediaPlayer(args.play_from, decode=not args.play_without_decoding)
    else:
        player = None

    # create media sink
    if args.record_to:
        recorder = MediaRecorder(args.record_to)
    else:
        recorder = None

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(
            run(player=player, recorder=recorder, room=args.room, session=session)
        )
    except KeyboardInterrupt:
        pass
    finally:
        if recorder is not None:
            loop.run_until_complete(recorder.stop())
        loop.run_until_complete(session.destroy())

        # close peer connections
        coros = [pc.close() for pc in pcs]
        loop.run_until_complete(asyncio.gather(*coros))