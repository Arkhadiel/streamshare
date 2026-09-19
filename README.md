# StreamShare — Fase 2 (GUI PySide6 & Descoberta LAN)

Pipeline: captura de tela (MSS) → H.264 (FFmpeg) → UDP → recepção → decode (FFmpeg) → exibição em janela responsiva (PySide6).

Sem necessidade de digitar IPs: a conexão entre Host e Viewer é feita via **Código de Sessão (XXXX-XXXX)** anunciado automaticamente por **UDP Broadcast** na rede local.

---

## 1. Pré-requisitos (nos DOIS PCs)

- **Python 3.10+** instalado (`python --version` no terminal).
- **FFmpeg**: instalado e disponível no PATH (`ffmpeg -version`).

---

## 2. Instalação

Dentro da pasta do projeto:

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Como Executar (Aplicação Única)

Inicie a aplicação em ambos os PCs:

```powershell
python main.py
```

### Passo a Passo

1. **No PC Transmissor (Host):**
   - Clique em **"Transmitir Tela"**.
   - Selecione o monitor, qualidade (Source, 720p, 1080p, 1440p) e FPS.
   - Clique em **"Iniciar Transmissão"**.
   - O aplicativo gerará um código amigável (exemplo: `K89P-M37X`).

2. **No PC Receptor (Viewer):**
   - Clique em **"Assistir Transmissão"**.
   - Digite o código de sessão exibido no Host (`K89P-M37X`).
   - Clique em **"Conectar"**.
   - A descoberta na LAN localizará o Host automaticamente e iniciará a exibição do vídeo!

---

## 4. Firewall (Observação de Rede)

Se o Viewer não receber o vídeo ou o código não for localizado:
- Certifique-se de que ambos os PCs estão na **mesma rede Wi-Fi/LAN**.
- Permita o Python no Windows Firewall se for solicitado, ou libere as portas UDP **5555** (vídeo) e **5556** (descoberta).
