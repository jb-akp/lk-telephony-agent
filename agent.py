import json
import logging
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from livekit import agents, rtc, api
from livekit.agents import AgentServer, AgentSession, Agent, room_io, RunContext, get_job_context, AutoSubscribe
from livekit.agents.llm import function_tool
from livekit.plugins import openai, noise_cancellation, bey

load_dotenv(".env.local")

# Define tools outside class so they can be conditionally included
@function_tool()
async def get_call_debrief(run_ctx: RunContext) -> str:
    """Retrieve recent call history, voicemail summaries, and debrief information from Google Sheets.
    
    Use this when the user asks about:
    - Voicemails or voicemail summaries
    - Recent calls or call history
    - A debrief or summary of previous conversations
    - What happened in previous phone calls
    
    Returns the actual call history data from Google Sheets. If no calls exist yet, returns an empty string.
    """
    run_ctx.disallow_interruptions()
    
    response = requests.get(os.getenv("N8N_MEMORY_WEBHOOK_URL"))
    memory = response.text if response.status_code == 200 else ""
    
    return memory

@function_tool()
async def hangup_spam_call(run_ctx: RunContext) -> str:
    """Immediately hang up the call if the caller mentions: car warranty, extended warranty, insurance offers, debt relief, credit card offers, timeshare, or any unsolicited sales pitch. Call this tool as soon as you detect any of these spam indicators."""
    run_ctx.disallow_interruptions()
    handle = await run_ctx.session.generate_reply(
        instructions="Repeat this exact message word for word, do not change or add anything: 'I'm not interested in unsolicited offers. Please remove this number from your calling list. Have a good day.'"
    )
    await handle
    
    # Hang up by deleting the room
    ctx = get_job_context()
    if ctx is not None:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    
    return "Call ended"

class Assistant(Agent):
    def __init__(self, is_phone) -> None:
        # Customize persona based on connection source
        if is_phone:
            # Phone mode: Gatekeeper persona
            instructions = """
            You are "Sarah", a protective AI Receptionist for James, answering a phone call forwarded from voicemail.
            PRIORITY: If caller mentions car warranty, extended warranty, insurance offers, debt relief, credit card offers, timeshare, or any unsolicited sales pitch, IMMEDIATELY call hangup_spam_call. Do not engage or ask questions.
            For legitimate calls: Screen the call, collect name and full message. Let them finish speaking before ending.
            Keep responses under 2 sentences. Be professional, firm, and concise.
            """
            tools = [hangup_spam_call]  # Hangup tool for spam detection
        else:
            # Web mode: Chief of Staff persona
            instructions = """
            You are "Sarah", James's Chief of Staff, appearing as a 3D Avatar on the web dashboard.
            When asked about voicemails, calls, or call history, use get_call_debrief to retrieve from Google Sheets. If data exists, summarize it. If empty, say "I don't see any recent calls yet." Never invent call information.
            Keep responses concise (1-2 sentences unless detail is requested). Welcome James back and offer help.
            """
            tools = [get_call_debrief]  # Include tool for web mode
        
        super().__init__(instructions=instructions, tools=tools)

server = AgentServer()

@server.rtc_session(agent_name="my-vision-agent")
async def my_agent(ctx: agents.JobContext):
    # Connect to room first to see participants
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    # Detect if this is a phone call (SIP participant)
    is_phone = False
    for participant in ctx.room.remote_participants.values():
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            is_phone = True
            break
    
    logging.info(f"is_phone: {is_phone}, room: {ctx.room.name}")

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-mini-realtime-preview-2024-12-17",
            voice="coral",
        )
    )

    # Start avatar only for web (phones can't see video)
    if not is_phone:
        avatar = bey.AvatarSession(
            avatar_id="2bc759ab-a7e5-4b91-941d-9e42450d6546", 
        )
        await avatar.start(session, room=ctx.room)

    # Start agent with appropriate persona
    await session.start(
        room=ctx.room,
        agent=Assistant(is_phone=is_phone),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP else noise_cancellation.BVC(),
            ),
        ),
    )

    # Send transcript to n8n on SIP participant disconnect
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            logging.info("SIP participant disconnected, sending transcript to n8n...")
            payload = {
                "transcript": json.dumps(session.history.to_dict()["items"]),
                "timestamp": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()
            }
            response = requests.post(os.getenv("N8N_TRANSCRIPT_WEBHOOK_URL"), json=payload)
            logging.info(f"Transcript sent to n8n. Status: {response.status_code}")
    
    ctx.room.on("participant_disconnected", on_participant_disconnected)
    
    if is_phone:
        await session.generate_reply(
            instructions="Say exactly these words: 'Hello, this is James's AI. Who is calling?'"
        )
    else:
        await session.generate_reply(
            instructions="Welcome back, James. How can I help you today?"
        )
    
if __name__ == "__main__":
    agents.cli.run_app(server)