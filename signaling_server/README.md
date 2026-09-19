# StreamShare Signaling Server

Servidor de sinalização mínimo para conexões pela internet (Fase 4).

## Deploy gratuito no Render

1. Crie uma conta em [render.com](https://render.com)
2. New → Web Service → conecte seu repositório
3. **Root directory**: `signaling_server`
4. **Build command**: `pip install -r requirements.txt`
5. **Start command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`
6. Plano **Free** funciona bem (tráfego é mínimo)
7. Copie a URL gerada (ex: `https://streamshare-signal.onrender.com`)
8. Defina a variável de ambiente `STREAMSHARE_SIGNAL_URL` nos dois PCs:
   ```
   set STREAMSHARE_SIGNAL_URL=https://sua-url.onrender.com
   ```
   Ou edite `signaling.py` e troque o valor de `DEFAULT_SIGNAL_URL`.

## Deploy no Railway

1. [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Selecione a pasta `signaling_server` como root
3. Railway detecta o `Procfile` automaticamente
4. Copie a URL pública e configure `STREAMSHARE_SIGNAL_URL`

## Endpoints

| Método | Path | Descrição |
|--------|------|-----------|
| POST | `/session` | Host registra sessão |
| GET | `/session/{code}` | Viewer busca sessão |
| DELETE | `/session/{code}` | Host remove sessão |
| GET | `/health` | Healthcheck |

## Segurança

- Sessões expiram em 60s sem heartbeat (configurável via `SESSION_TTL`)
- Armazenamento em memória — reiniciar o servidor limpa tudo
- Sem autenticação (suficiente para uso privado entre amigos)
