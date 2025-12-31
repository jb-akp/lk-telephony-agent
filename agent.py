from dotenv import load_dotenv
import json, logging, os, requests
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit import agents, rtc, api
from livekit.agents import AgentServer, AgentSession, Agent, room_io, RunContext, get_job_context, AutoSubscribe
from livekit.agents.llm import function_tool
from livekit.plugins import (
    openai, 
    noise_cancellation, 
    bey
)

load_dotenv(".env.local")

def send_transcript_to_n8n(transcript_data: str, timestamp: str):
    """Send transcript to n8n webhook."""

    payload = {
        "transcript": transcript_data,
        "timestamp": timestamp
    }

    response = requests.post(os.getenv("N8N_TRANSCRIPT_WEBHOOK_URL"), json=payload)
    logging.info(f"Transcript sent, status: {response.status_code}")
    
    return response.status_code == 200

@function_tool()
async def get_call_debrief(run_ctx: RunContext) -> str:
    """Retrieve call history and voicemail summaries from Google Sheets. Returns empty string if no calls exist."""
    run_ctx.disallow_interruptions()
    
    try:
        response = requests.get(os.getenv("N8N_MEMORY_WEBHOOK_URL"))
        return response.text if response.status_code == 200 else ""
    except Exception:
        return "No data available"

@function_tool()
async def hangup_call(run_ctx: RunContext, is_spam: bool = False):
    """Hang up the call. Use is_spam=True if the caller mentions: car warranty, extended warranty, insurance offers, debt relief, credit card offers, timeshare, or any unsolicited sales pitch. Use is_spam=False for normal call endings after collecting the caller's information."""
    logging.info(f"hangup_call executed, spam: {is_spam}")
    run_ctx.disallow_interruptions()
    
    if is_spam:
        message = "You MUST say exactly these words with no changes: 'I'm not interested in unsolicited offers. Please remove this number from your calling list. Goodbye.' Do not add anything else. Do not say 'call has been ended' or offer assistance."
    else:
        message = "Say a polite goodbye, such as: 'Thank you for calling. I'll make sure James gets your message. Have a great day!'"
    
    await run_ctx.session.generate_reply(instructions=message)
    await run_ctx.wait_for_playout()
    
    transcript_data = json.dumps(run_ctx.session.history.to_dict()["items"])
    timestamp = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()
    
    ctx = get_job_context()
    try:
        logging.info(f"Deleting room: {ctx.room.name}")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    except Exception as e:
        logging.error(f"Delete room failed: {e}")
    
    send_transcript_to_n8n(transcript_data, timestamp)

class Assistant(Agent):
    def __init__(self, is_phone) -> None:
        if is_phone:
            instructions = """
            You are "Sarah", a protective AI Receptionist for James, answering a phone call forwarded from voicemail.
            PRIORITY: If the caller mentions ANY of these: car warranty, selling a car warranty, extended warranty, insurance offers, debt relief, credit card offers, timeshare, or ANY unsolicited sales pitch, you MUST IMMEDIATELY call the hangup_call function tool with is_spam=True. Do not respond verbally first. Do not ask questions. Just call the tool immediately.
            For legitimate calls: Screen the call, collect name and full message. Let them finish speaking before ending. 
            IMPORTANT: When the caller says goodbye, thanks you, says they're done, or indicates the conversation is complete, you MUST IMMEDIATELY call hangup_call with is_spam=False. Do NOT say goodbye or thank you verbally - the tool will handle the goodbye message. Just call the tool immediately when they're done.
            Keep responses under 2 sentences. Be professional, firm, and concise.
            """
            tools = [hangup_call]
        else:
            instructions = """
            You are "Sarah", James's Chief of Staff, appearing as a 3D Avatar on the web dashboard.
            When asked about voicemails, calls, or call history, use get_call_debrief to retrieve from Google Sheets. If data exists, summarize it. If empty, say "I don't see any recent calls yet." Never invent call information.
            Keep responses concise (1-2 sentences unless detail is requested). Welcome James back and offer help.
            """
            tools = [get_call_debrief]
        
        super().__init__(instructions=instructions, tools=tools)

server = AgentServer()

@server.rtc_session(agent_name="my-vision-agent")
async def my_agent(ctx: agents.JobContext):

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    is_phone = ctx.room.name.startswith("call-")
    
    logging.info(f"is_phone={is_phone}, room={ctx.room.name}")

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-mini-realtime-preview-2024-12-17",
            voice="coral",
        )
    )

    # if not is_phone:
    #     avatar = bey.AvatarSession(
    #         avatar_id="2bc759ab-a7e5-4b91-941d-9e42450d6546", 
    #     )
    #     await avatar.start(session, room=ctx.room)

    await session.start(
        room=ctx.room,
        agent=Assistant(is_phone=is_phone),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP else noise_cancellation.BVC(),
            ),
        ),
    )

    if is_phone:
        greeting = "Say: 'Hello, this is James's AI. Who is calling?'"
    else:
        greeting = "Say: 'Welcome back, James. How can I help you today?'"
    
    await session.generate_reply(
        instructions=greeting
    )
    
if __name__ == "__main__":
    agents.cli.run_app(server)

