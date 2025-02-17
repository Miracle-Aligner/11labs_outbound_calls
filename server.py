import os
import aiohttp
import logging
import asyncio
import json
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from dotenv import load_dotenv
from twilio.rest import Client

import websockets

load_dotenv()

app = FastAPI()
call_data_store = {}

# Environment Variables
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))

if not all(
    [
        ELEVENLABS_API_KEY,
        ELEVENLABS_AGENT_ID,
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
        TWILIO_PHONE_NUMBER,
    ]
):
    logging.error("Missing required environment variables")
    raise RuntimeError("Missing required environment variables")

# Twilio Client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def load_text(file_path):
    """Load text from a given file."""
    try:
        with open(file_path, "r") as file:
            return file.read().strip()
    except FileNotFoundError:
        logging.error(f"File not found: {file_path}")
        raise HTTPException(status_code=500, detail="Error reading file")


@app.get("/")
async def health_check():
    return {"message": "Server is running"}


async def get_signed_url():
    try:
        url = f"https://api.elevenlabs.io/v1/convai/conversation/get_signed_url?agent_id={ELEVENLABS_AGENT_ID}"
        headers = {"xi-api-key": ELEVENLABS_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to get signed URL: {response.status}",
                    )
                data = await response.json()
                return data.get("signed_url")
    except Exception as e:
        logging.error(f"Error getting signed URL: {e}")
        raise HTTPException(status_code=500, detail="Error getting signed URL")


@app.post("/outbound-call")
async def outbound_call(request: Request):
    body = await request.json()
    number = body.get("number")
    name = body.get("name")
    name = " " + name if name else ""

    prompt = load_text("data/prompt.txt")
    first_message = load_text("data/first_message.txt")

    prompt = prompt.format(name=name)
    first_message = first_message.format(name=name)

    if not number:
        raise HTTPException(status_code=400, detail="Phone number is required")

    try:
        call = twilio_client.calls.create(
            from_=TWILIO_PHONE_NUMBER,
            to=number,
            url=f"https://{HOST}/outbound-call-twiml",
        )
        call_data_store[call.sid] = {"prompt": prompt, "first_message": first_message}
        return {"success": True, "message": "Call initiated", "callSid": call.sid}
    except Exception as e:
        logging.error(f"Error initiating outbound call: {e}")
        raise HTTPException(status_code=500, detail="Failed to initiate call")


@app.api_route("/outbound-call-twiml", methods=["GET", "POST"])
async def outbound_call_twiml(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid")

    if not call_sid or call_sid not in call_data_store:
        return Response(content="Invalid call SID", status_code=404)

    call_data = call_data_store[call_sid]
    prompt = call_data.get("prompt", "")
    first_message = call_data.get("first_message", "")

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="wss://{HOST}/outbound-media-stream">
                <Parameter name="prompt" value="{prompt}" />
                <Parameter name="first_message" value="{first_message}" />
            </Stream>
        </Connect>
    </Response>"""

    return Response(content=twiml_response, media_type="text/xml")


@app.websocket("/outbound-media-stream")
async def outbound_media_stream(websocket: WebSocket):
    await websocket.accept()
    print("[Twilio] Connected to outbound media stream")

    stream_sid = None
    call_sid = None
    custom_parameters = {}
    elevenlabs_ws = None

    async def connect_to_elevenlabs():
        try:
            signed_url = await get_signed_url()
            print(f"[ElevenLabs] Connecting to: {signed_url}")
            return await websockets.connect(signed_url)
        except Exception as e:
            logging.error(f"[ElevenLabs] Error connecting: {e}")
            return None

    # This task will continuously read messages from ElevenLabs and forward audio to Twilio
    async def handle_elevenlabs_messages(
        elevenlabs_ws: websockets.WebSocketClientProtocol,
    ):
        try:
            async for raw_msg in elevenlabs_ws:
                # Parse the JSON message from ElevenLabs
                try:
                    message = json.loads(raw_msg)
                except Exception as err:
                    logging.error("[ElevenLabs] Error parsing message: %s", err)
                    continue

                msg_type = message.get("type")

                if msg_type == "conversation_initiation_metadata":
                    print("[ElevenLabs] Received initiation metadata")

                elif msg_type == "audio":
                    # check either message.audio.chunk or message.audio_event.audio_base_64
                    audio_chunk = None
                    if "audio" in message and "chunk" in message["audio"]:
                        audio_chunk = message["audio"]["chunk"]
                    elif (
                        "audio_event" in message
                        and "audio_base_64" in message["audio_event"]
                    ):
                        audio_chunk = message["audio_event"]["audio_base_64"]

                    if audio_chunk and stream_sid:
                        # Forward audio chunk back to Twilio
                        audio_data = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_chunk},
                        }
                        await websocket.send_text(json.dumps(audio_data))
                    else:
                        print("[ElevenLabs] Received audio but no streamSid yet?")

                elif msg_type == "interruption":
                    if stream_sid:
                        clear_data = {"event": "clear", "streamSid": stream_sid}
                        await websocket.send_text(json.dumps(clear_data))

                elif msg_type == "ping":
                    ping_id = message.get("ping_event", {}).get("event_id")
                    if ping_id:
                        # Respond with a pong to keep the connection alive
                        await elevenlabs_ws.send(
                            json.dumps({"type": "pong", "event_id": ping_id})
                        )

                elif msg_type == "agent_response":
                    agent_resp = message.get("agent_response_event", {}).get(
                        "agent_response"
                    )
                    print(f"[ElevenLabs] agent_response: {agent_resp}")

                elif msg_type == "user_transcript":
                    user_transcript = message.get("user_transcription_event", {}).get(
                        "user_transcript"
                    )
                    print(f"[ElevenLabs] user_transcript: {user_transcript}")

                else:
                    print(f"[ElevenLabs] Unhandled message type: {msg_type}")

        except websockets.exceptions.ConnectionClosedOK:
            print("[ElevenLabs] Connection closed (OK)")
        except websockets.exceptions.ConnectionClosedError as e:
            logging.error(f"[ElevenLabs] Connection closed error: {e}")
        except Exception as e:
            logging.error(f"[ElevenLabs] Error while receiving messages: {e}")

    # Connect to ElevenLabs immediately
    elevenlabs_ws = await connect_to_elevenlabs()
    elevenlabs_task = None
    if elevenlabs_ws is not None:
        elevenlabs_task = asyncio.create_task(handle_elevenlabs_messages(elevenlabs_ws))

    # Helper to send the initial conversation config to ElevenLabs
    async def send_initial_config():
        # If no custom_parameters are found, default to the same that JS uses
        prompt_text = custom_parameters.get(
            "prompt", "you are gary from the phone store"
        )
        first_message = custom_parameters.get(
            "first_message", "hey there! how can I help you today?"
        )
        initial_config = {
            "type": "conversation_initiation_client_data",
            "conversation_config_override": {
                "agent": {
                    "prompt": {"prompt": prompt_text},
                    "first_message": first_message,
                }
            },
        }
        print(f"[ElevenLabs] Sending initiation config with prompt='{prompt_text}'")
        await elevenlabs_ws.send(json.dumps(initial_config))

    # Continuously read messages from Twilio and forward audio to ElevenLabs
    try:
        while True:
            raw_msg = await websocket.receive_text()
            msg = json.loads(raw_msg)
            event = msg.get("event")

            if event == "start":
                stream_sid = msg["start"].get("streamSid")
                call_sid = msg["start"].get("callSid")
                # Grab any custom parameters coming from Twilio
                custom_parameters = msg["start"].get("customParameters", {})
                print(
                    f"[Twilio] Stream started - StreamSid: {stream_sid}, CallSid: {call_sid}"
                )
                print(f"[Twilio] Start parameters: {custom_parameters}")

                # Now that we have the custom parameters, send initial config to ElevenLabs
                if elevenlabs_ws:
                    await send_initial_config()

            elif event == "media":
                # Forward audio chunk to ElevenLabs
                if elevenlabs_ws:
                    audio_chunk_base64 = msg["media"].get("payload")
                    if audio_chunk_base64:
                        await elevenlabs_ws.send(
                            json.dumps({"user_audio_chunk": audio_chunk_base64})
                        )

            elif event == "stop":
                print(f"[Twilio] Stream {stream_sid} ended")
                break

            else:
                print(f"[Twilio] Unhandled event: {event}")

    except WebSocketDisconnect:
        print("[Twilio] Client disconnected")
    except Exception as e:
        logging.error(f"[Twilio] Error in WebSocket: {e}")
    finally:
        # Clean up local Twilio socket
        await websocket.close()

        # Clean up ElevenLabs socket
        if elevenlabs_ws and not elevenlabs_ws.closed:
            await elevenlabs_ws.close()

        # Cancel the background task if it was started
        if elevenlabs_task:
            elevenlabs_task.cancel()
            try:
                await elevenlabs_task
            except asyncio.CancelledError:
                pass

        print("[Server] WebSocket connection closed.")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
