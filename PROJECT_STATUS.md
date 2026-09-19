# StreamShare — PROJECT_STATUS

## Milestone atual
Fase 2 — UI Unificada (PySide6) e Descoberta Automática na LAN via Código de Sessão.

## Concluído
- **Fase 1**: Pipeline PoC LAN (captura MSS → H.264 FFmpeg → UDP fragmentado → decode FFmpeg → exibição).
- **Auto-Handshake**: Negociação automática de resolução, FPS epreset de qualidade (`source`, `720p`, `1080p`, `1440p`).
- **Letterbox Responsivo**: Redimensionamento adaptativo mantendo aspect-ratio sem distorcer o vídeo.
- **Fase 2 - Descoberta LAN (`discovery.py`)**: Geração de códigos de sessão amigáveis (`XXXX-XXXX`) e anúncio automático via UDP Broadcast (porta 5556).
- **Fase 3 - Hardware Encoding (`host.py`)**: Detecção e uso de encoders de hardware (NVENC, AMF, QSV) com probe real (1 frame de teste) para confirmar que o driver funciona antes de oferecer na UI. Parâmetros otimizados por encoder (`repeat_headers`, `ultralowlatency`, `ull` tune). Scaling GPU-nativo (`scale_cuda` para NVENC, `scale_vulkan` para AMF). Fallback automático para libx264 se o hw encoder falhar em runtime.

## Pendente (Próximas Fases)
- Hardware decoding no Viewer (FFmpeg hwaccel + dxva2/d3d11va) — Fase 3b
- Segurança/criptografia de mídia em trânsito — Fase 3+
- Internet / Signaling / STUN / TURN — Fase 4
- Bitrate adaptativo — Fase 5

## Arquivos do Projeto
- `main.py` — Entrypoint da interface PySide6.
- `discovery.py` — Protocolo de descoberta LAN e geração de código `XXXX-XXXX`.
- `host.py` — Engine de captura e transmissão (`HostSession`).
- `viewer.py` — Engine de recepção e decodificação (`ViewerSession`).
- `protocol.py` — Header e empacotamento de pacotes UDP de vídeo.
