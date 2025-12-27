import json
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, room_io, AutoSubscribe, RunContext
from livekit.agents.llm import function_tool
from livekit.plugins import openai, noise_cancellation, bey

load_dotenv(".env.local")

class Assistant(Agent):
    def __init__(self, is_phone) -> None:
        # Customize persona based on connection source
        if is_phone:
            # Phone mode: Gatekeeper persona
            instructions = """
            You are "Sarah", a protective AI Receptionist for James.
            You are currently answering a real phone call (SIP) that was forwarded from James's voicemail.
            
            YOUR GOALS:
            1. Screen the call. James is busy and didn't answer, so the call was forwarded to you.
            2. If the caller is SPAM (car warranty, insurance, etc.), tell them to remove this number and hang up.
            3. If it is a legitimate call, collect their name and full message. Let them finish speaking completely before ending the call.
            
            Keep responses short (under 2 sentences). Be professional but firm. Make sure to capture the complete message from the caller.
            """
        else:
            # Web mode: Chief of Staff persona
            instructions = """
            You are "Sarah", James's Chief of Staff.
            You are currently appearing as a 3D Avatar on the web dashboard.
            
            YOUR GOALS:
            1. Welcome James back.
            2. When James asks about recent calls, a debrief, or what happened in previous conversations, use the get_call_debrief function tool to retrieve the information from Google Sheets.
            3. After retrieving the call history, summarize it clearly and offer to help with anything else.
            
            Always use the get_call_debrief tool when asked about call history, recent calls, or debriefs.
            """
        
        super().__init__(instructions=instructions)
        self.is_phone = is_phone
    
    @function_tool()
    async def get_call_debrief(self, run_ctx: RunContext) -> str:
        """Retrieve recent call history and debrief information from Google Sheets.
        
        Use this when the user asks for a debrief, summary of recent calls, or wants to know what happened in previous conversations.
        """
        run_ctx.disallow_interruptions()
        
        # Notify user we're retrieving the data
        await run_ctx.session.generate_reply(
            instructions="Let me check that for you."
        )
        
        response = requests.get("https://n8n.n8nsite.live/webhook/memory")
        memory = response.text if response.status_code == 200 else ""
        
        return memory
    
    async def on_enter(self):
        """Generate initial greeting based on connection source."""
        if not self.is_phone:
            await self.session.generate_reply(
                instructions="Welcome back, James. How can I help you today?"
            )
        else:
            await self.session.generate_reply(
                instructions="Say exactly: 'Hello, this is James's AI. Who is calling?'"
            )

server = AgentServer()

@server.rtc_session(agent_name="my-vision-agent")
async def my_agent(ctx: agents.JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    # Detect if this is a phone call (SIP participant)
    is_phone = any(p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP for p in ctx.room.remote_participants.values())

    model = openai.realtime.RealtimeModel(
        model="gpt-4o-mini-realtime-preview-2024-12-17",
        voice="coral",
    )

    session = AgentSession(llm=model)

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
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() 
                if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP 
                else noise_cancellation.BVC(),
            ),
        ),
    )

    # Send transcript to n8n on SIP participant disconnect
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            logging.info("SIP participant disconnected, sending transcript to n8n...")
            payload = {
                "transcript": json.dumps(session.history.to_dict()["messages"]),
                "timestamp": datetime.utcnow().isoformat()
            }
            response = requests.post("https://n8n.n8nsite.live/webhook/api/path", json=payload)
            logging.info(f"Transcript sent to n8n. Status: {response.status_code}")
    
    ctx.room.on("participant_disconnected", on_participant_disconnected)
    
if __name__ == "__main__":
    agents.cli.run_app(server)