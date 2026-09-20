import asyncio
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer
from aiortc.contrib.signaling import BYE
import signaling

# Configuração PÚBLICA (substituir depois!)
TURN_CONFIG = RTCConfiguration(
    iceServers=[
        RTCIceServer(urls="stun:stun.l.google.com:19302"),
        # Adicionar TURN aqui futuramente:
        # RTCIceServer(urls="turn:...", username="...", credential="...")
    ]
)

class StreamShareConnection:
    def __init__(self, role: str, session_code: str):
        self.role = role # 'host' ou 'viewer'
        self.session_code = session_code
        self.pc = RTCPeerConnection(configuration=TURN_CONFIG)

    async def connect(self):
        if self.role == "host":
            # 1. Host cria oferta
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            # 2. Host posta oferta
            signaling.post_sdp(self.session_code, "offer", self.pc.localDescription.sdp)
            # 3. Host aguarda answer
            sdp_answer, _ = signaling.get_sdp(self.session_code, "answer")
            await self.pc.setRemoteDescription(sdp_answer)

        else: # viewer
            # 1. Viewer busca oferta
            sdp_offer, _ = signaling.get_sdp(self.session_code, "offer")
            await self.pc.setRemoteDescription(sdp_offer)
            # 2. Viewer cria resposta
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            # 3. Viewer posta resposta
            signaling.post_sdp(self.session_code, "answer", self.pc.localDescription.sdp)
